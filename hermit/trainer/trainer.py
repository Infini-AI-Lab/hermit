"""The Trainer: the outer training loop around an Evolver.

Run directory (<output_dir>/<run_name>/):
  config.yaml          resolved config, versions, warnings
  steps.jsonl          one line per completed step
  step_NNNN/           the Evolver's output/, its StepResult, a snapshot of state/
  learning_material/   the material of every step
  summary.json         written at the end
  workspace/           the Evolver's directories for the current call
  access/validation/   validation tasks handed to the Evolver (when data.val is set)
  checkout/            the git worktree of this run's branch
  root/                the template's files, the reference for check_spec
  tools.jsonl          every tool call
"""
from __future__ import annotations

import atexit

from dataclasses import asdict
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import yaml

from hermit.adapter.manifest import CARD, MANIFEST, files_of, read_manifest
from hermit.agent.model.backends import bindings_of, problems_of, registry
from hermit.config import Config
from hermit.agent.evolver.base import (Access, Context, Evolver, FinalResult, Progress, StepContext, StepResult, Usage,
                                         Workspace)
from hermit.agent.evolver.loader import find, instantiate
from hermit.agent.model.llm import ChatClient
from hermit.isolate import CHATGPT_HOSTS, HOME_SECRETS, ToolRunner, _safe, agent_pipeline, config_paths, \
    judge_for, run_secrets, sanitized_copy
from hermit.model_proxy import ModelProxy
from hermit.sandbox import AllowlistProxy, Bind, Sandbox, drop_unsafe, inside_tools
from hermit.spec import check_spec, repair
from hermit.task import TaskData, load_callable
from hermit.tools import FunctionTool, ToolServer, assemble, evolver_agent
from hermit.trainer import card
from hermit.trainer.budget import Budget, BudgetExceeded
from hermit.trainer.material import Material
from hermit.trainer.rollout import run_tasks
from hermit.tree import INVALID_MARK, ROOT_TAG, Tree



class StopRun(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def warnings_for(config: Config, val_given: bool) -> list[str]:
    """Risky settings: warned, never blocked."""
    d, out = config.data, []
    if not d["sandbox"]["enabled"]:
        out.append("sandbox.enabled is false: what the run starts runs unconfined; the learner can read anything on "
                   "this machine, including the test split, and reach the network. Not for reported results.")
    elif d["sandbox"].get("enforce", "required") == "best_effort":
        out.append("sandbox.enforce is best_effort: without bubblewrap the run starts its processes unconfined (each "
                   "one is recorded). Not for reported results.")
    if d["sandbox"]["enabled"] and d["environment"].get("runner") == "host":
        out.append("environment.runner is host: the acting pipeline runs in the Trainer's process and must isolate the "
                   "adapter's code it runs itself.")
    if d["environment"].get("network") == "public":
        out.append("environment.network is public: the acting agent and the adapter's code reach any host.")
    if d["evolver_access"]["send_material_out"]:
        out.append("evolver_access.send_material_out is true: learning material is sent to an outside service.")
    if val_given and d["data"].get("val"):
        out.append("validation data is given to the Evolver with whatever it contains; if it contains answers, the "
                   "Evolver may overfit to the validation split.")
    return out


def needs_errors(config: Config, needs: list[str], supports_resume: bool) -> list[str]:
    d, out = config.data, []
    if "validation_data" in needs and not d["data"].get("val"):
        out.append("the Evolver needs validation_data but data.val is not set")
    if "environment" in needs and not d["evolver_access"]["environment"]:
        out.append("the Evolver needs environment but evolver_access.environment is empty")
    if "send_material_out" in needs and not d["evolver_access"]["send_material_out"]:
        out.append("the Evolver sends learning material to an outside service; set "
                   "evolver_access.send_material_out: true to allow it")
    if d["evolver"]["resume"] and not supports_resume:
        out.append("evolver.resume is true but this Evolver does not support resume")
    return out


def _task_tool_decls(config: Config) -> list[dict]:
    out = []
    for item in config["evolver_access"]["tools"] or []:
        if isinstance(item, str):
            data = yaml.safe_load(config.path(item).read_text())
            out += data if isinstance(data, list) else [data]
        else:
            out.append(item)
    return out


def _copy_dir(src: Path, dst: Path) -> None:
    """A copy of a directory the learner wrote: through the sanitized archive (links that leave it, pipes and devices
    left out, nothing followed)."""
    if dst.exists():
        shutil.rmtree(dst)
    if src.exists():
        sanitized_copy(src, dst)
    else:
        dst.mkdir(parents=True)


def _sanitize_in_place(path: Path) -> None:
    """Replace a directory the learner wrote by its sanitized copy (links that leave it, pipes and devices dropped),
    so that the host's later reads and writes there (the spec check, the repair, the tree) stay inside it. Its own
    directory is kept (a sandbox may bind it)."""
    if not path.is_dir() or path.is_symlink():
        return
    clean = path.with_name(path.name + f".clean-{uuid.uuid4().hex[:6]}")
    sanitized_copy(path, clean)
    for child in list(path.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in list(clean.iterdir()):
        child.rename(path / child.name)
    clean.rmdir()


class Trainer:
    def __init__(self, config: Config, *, python: str | None = None, log=print):
        self.config = config
        self.python = python or sys.executable
        self.log = log
        d = config.data
        self.template_dir = config.template_dir()
        self.template = read_manifest(self.template_dir)["template"]
        self.run_name = config.run_name()
        self.run_dir = config.run_dir()
        self.branch = self.run_name
        self.data = TaskData(config.path(d["data"]["tasks"]), config.path(d["data"]["train"]),
                             config.path(d["data"]["val"]), config.path(d["data"]["test"]))
        # The spec checks run the adapter's modules in a strict sandbox, as strictly as the run asks; None: the run's
        # sandbox is off and they run unconfined like everything else.
        self.enforce = d["sandbox"].get("enforce", "required") if d["sandbox"]["enabled"] else None
        self.pipeline = None             # set when the run starts: the agent runs in its sandbox, never imported here
        self.judge = load_callable(f"{d['judge']['path']}:{d['judge']['name']}", config.base)   # replaced in _prepare
                                                                                                   # by judge_for
        self.spec = find(d["evolver"]["name"], config.base)
        self.evolver, self.params = instantiate(self.spec, d["evolver"]["params"],
                                                backends=registry(d.get("backends")), default_model=d.get("model"))
        self.budget = Budget(**{k: d["trainer"]["budget"].get(k) for k in ("usd", "tokens", "hours")})
        if isinstance(d.get("llm"), dict) and d["llm"].get("cli"):     # a model only a CLI reaches
            from hermit.agent.base.endpoint import cli_model
            self.llm = cli_model(d["llm"], log_dir=self.run_dir / "llm_sessions")
        else:
            self.llm = ChatClient.from_spec(d["llm"]) if d.get("llm") else None
        self.warnings = warnings_for(config, val_given=True)
        if d["sandbox"]["enabled"] and not self.params.get("binding") and self.params.get("login") != "chatgpt" \
                and self.params.get("home"):
            self.warnings.append(f"the Evolver names no model reference: its CLI's own login ({self.params['home']} "
                                 "from your home) is copied into the learner's sandbox, where the session can read "
                                 "it (a recorded exception, like a ChatGPT login); name a model to route it through "
                                 "the model proxy instead.")

    # ---------- setup ----------
    def _prepare(self) -> None:
        d = self.config.data
        errors = needs_errors(self.config, self.spec.needs, self.evolver.supports_resume)
        for who, binding, api in bindings_of(d, self.params):
            errors += [f"{who}: {p}" for p in problems_of(binding, api)]
        if errors:
            raise SystemExit("train refuses to start: " + "; ".join(errors))
        for w in self.warnings:
            self.log(f"WARNING: {w}")
        self.tree = Tree.open_or_create(self.config.tree_dir(self.template["id"]), self.template_dir)
        mode = str(d["trainer"]["resume_mode"])
        unfinished = (self.run_dir / "steps.jsonl").exists() and not (self.run_dir / "summary.json").exists()
        if self.run_dir.exists() and mode == "disable":
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self.run_dir.rename(self.run_dir.with_name(f"{self.run_name}.replaced-{stamp}"))
            from hermit.tree import git
            git(self.tree.repo, "worktree", "prune")
            if git(self.tree.repo, "rev-parse", "--verify", f"refs/heads/{self.branch}", check=False):
                git(self.tree.repo, "branch", "-m", self.branch, f"{self.branch}.replaced-{stamp}")
            unfinished = False
        elif self.run_dir.exists() and (self.run_dir / "summary.json").exists():
            raise SystemExit(f"run {self.run_name} already finished; choose another trainer.run_name "
                             "(or trainer.resume_mode=disable to start it over)")
        self.resuming = unfinished and mode != "disable"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.root_copy = self.tree.export(ROOT_TAG, self.run_dir / "root")
        start = self._start_ref()
        self.checkout = self.tree.checkout_run(self.branch, start, self.run_dir / "checkout")
        self.start_commit = start
        self.material = Material(self.run_dir / "learning_material")
        ws = self.run_dir / "workspace"
        self.ws = Workspace(adapter=ws / "adapter", learning_material=self.material.root,
                            task_info=self.config.path(d["task_info"]) or (ws / "task-info"),
                            history=ws / "history", state=ws / "state", scratch=ws / "scratch", output=ws / "output")
        self.ws.state.mkdir(parents=True, exist_ok=True)
        if not self.ws.task_info.exists():
            self.ws.task_info.mkdir(parents=True)
        val = None
        if d["data"].get("val"):
            val = self.run_dir / "access" / "validation"
            val.mkdir(parents=True, exist_ok=True)
            with open(val / "tasks.jsonl", "w") as f:
                for row in self.data.rows("val"):
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        env = tuple(config_paths(self.config, "evolver_access.environment", d["evolver_access"]["environment"]))
        self.access = Access(validation=val, environment=env)
        # The learner's sandbox (docs/developer/sandbox-design.md): the workspace under fixed names below /w, the
        # model proxy (its sessions never hold a model key), the Trainer's tool endpoint, and the hosts the run
        # declares. A Codex learner on a ChatGPT login reaches its plan's hosts (the recorded exception).
        ev = d["sandbox"]["evolver"]
        records = self.run_dir / "sandbox"
        allow = list(ev.get("allowed_hosts") or [])
        if self.params.get("login") == "chatgpt" or (isinstance(d.get("llm"), dict) and d["llm"].get("login") ==
                                                       "chatgpt"):
            allow += [h for h in CHATGPT_HOSTS if h not in allow]
        atexit.register(self._close_sandboxes)      # whatever is started from here is closed, a failure included
        self.proxy = AllowlistProxy(allow, log=records / "network.jsonl") if allow else None
        self.model_proxy = ModelProxy(log=records / "models.jsonl")
        # What else the run starts has a sandbox of its own: the agent that does the tasks (rollouts, and run_agent
        # for the Evolver), its judge when sandboxed, and the tools a template or task declares.
        self.pipeline, self._close_pipeline = agent_pipeline(self.config, "rollout", records=records)
        self.judge = judge_for(self.config, self.pipeline)
        self.tool_runner = ToolRunner(self.config, records=records, work_root=self.run_dir / "tool_calls")
        (self.run_dir / "agent_logs").mkdir(exist_ok=True)       # run_agent's log copies, shown read-only
        binds = [Bind(self.ws.adapter, "/w/adapter", True), Bind(self.ws.learning_material, "/w/material"),
                 Bind(self.ws.task_info, "/w/task-info"), Bind(self.ws.history, "/w/history"),
                 Bind(self.ws.state, "/w/state", True), Bind(self.ws.scratch, "/w/scratch", True),
                 Bind(self.ws.output, "/w/output", True), Bind(self.run_dir / "root", "/w/root"),
                 Bind(self.spec.directory, "/w/method"), Bind(self.run_dir / "agent_logs", "/w/agent-logs")]
        if val:
            binds.append(Bind(val, "/w/validation"))
        names = set()
        for path in env:
            name = path.name or "environment"
            while name in names:
                name += "_"
            names.add(name)
            binds.append(Bind(path, f"/w/environment/{name}"))
        self.sandbox = Sandbox(enabled=d["sandbox"]["enabled"], network="allowlist" if allow else "no-network",
                               allow=allow, binds=binds, layout=(self.ws.adapter.parent, "/w"),
                               proxy_url=self.proxy.url if self.proxy else None,
                               env_allow=list(ev.get("env") or []), enforce=d["sandbox"].get("enforce", "required"),
                               framework=bool(getattr(self.evolver, "sandbox_framework", False)), record_dir=records,
                               # The isolation check inside proves these absent: the data, the judge, the runs,
                               # the home's secrets.
                               probe_absent=[*run_secrets(self.config), *HOME_SECRETS],
                               limits=dict(ev.get("resources") or {}), role="learner")
        self.sandbox.model_proxy = self.model_proxy
        self.sandbox.model_bindings = [b for b in (self.params.get("binding"),) if isinstance(b, dict)]
        resolved = {"config": d, "template": self.template, "tree": str(self.tree.repo), "branch": self.branch,
                    "start": start, "evolver": {"name": self.spec.name, "directory": str(self.spec.directory),
                                                "class": self.spec.class_path, "needs": self.spec.needs,
                                                "params": self.params},
                    "warnings": self.warnings, "python": self.python}
        if not self.resuming:
            (self.run_dir / "config.yaml").write_text(yaml.safe_dump(resolved, sort_keys=False, allow_unicode=True))

    def _start_ref(self) -> str:
        value = self.config.data.get("start_from")
        if not value:
            return self.tree.resolve(ROOT_TAG)
        path = self.config.path(value)
        if path is not None and (path / MANIFEST).is_file():
            mine = read_manifest(path)["template"]
            if mine != self.template:
                raise SystemExit(f"start_from {value} belongs to template {mine}, not {self.template}")
            result = check_spec(path, self.run_dir / "root", python=self.python, enforce=self.enforce) \
                if (self.run_dir / "root").exists() else None
            if result is not None and not result.passed:
                raise SystemExit(f"start_from {value} fails check_spec: {result.to_dict()['issues'][:3]}")
            # Import the folder as a node: a commit on an import branch from the root.
            work = self.run_dir / "import"
            branch = f"import/{self.run_name}"
            checkout = self.tree.checkout_run(branch, ROOT_TAG, work)
            Tree.sync(path, checkout)
            commit = Tree.commit(checkout, f"import: adapter from {path}") or Tree.head(checkout)
            return commit
        commit = self.tree.resolve(str(value))
        if not self.tree.is_valid(commit):
            raise SystemExit(f"start_from {value} is an invalid (unrepaired) commit")
        return commit

    # ---------- the loop ----------
    def train(self) -> dict:
        self._prepare()
        d = self.config.data
        done = self._completed_steps()
        if done:
            self._restore(done[-1])
        else:
            self.material.truncate(0)     # leftovers of a step that never completed
        last_feedback = done[-1].get("check_feedback_next") if done else None
        self._last_rollout_commit = done[-1]["parent"] if done and not done[-1].get("bootstrap") else None
        ctx = self._context()
        if not done:
            box, stopped, _ = self._call("setup", lambda: self.evolver.setup(ctx), progress=None,
                                         timeout_s=float(d["trainer"]["call_timeout_min"]) * 60)
            if "error" in box or stopped:
                raise SystemExit(f"the Evolver's setup failed: {box.get('error') or stopped}")
        step = done[-1]["step"] if done else 0
        session = done[-1].get("session") if done else None
        wanted = done[-1].get("next_tasks") if done else None
        if not done and type(self.evolver).bootstrap is not Evolver.bootstrap:
            record = self._bootstrap()
            session = record.get("session") or session
            wanted = record.get("next_tasks")
        step_size = int(d["data"]["step_size"])
        epochs = int(d["trainer"]["epochs"])
        stop_reason = None
        try:
            for epoch in range(1, epochs + 1):
                # An epoch runs every training task once: the tasks of its steps already recorded are done.
                ran = [t for r in done if r["epoch"] == epoch and not r.get("bootstrap") for t in r["tasks"]]
                remaining = _without(self._order(epoch), ran)
                while remaining:
                    # The Evolver's accepted request (checked when it was made), else the order's next tasks.
                    chosen = bool(wanted) and not _without(wanted, remaining)
                    chunk = list(wanted) if chosen else remaining[:step_size]
                    remaining = _without(remaining, chunk)
                    step += 1
                    progress = Progress(epoch, step, is_epoch_end=not remaining,
                                        remaining=tuple(remaining) if self.evolver.chooses_tasks else ())
                    record = self._step(progress, chunk, last_feedback, session,
                                        {"chosen_by": "evolver"} if chosen else None)
                    wanted = record.get("next_tasks")
                    last_feedback = record.get("check_feedback_next")
                    session = record.get("session") or session
                    self._checkpoint(record)
                    if record["status"] == "stop" and d["trainer"]["allow_early_stop"]:
                        raise StopRun("the Evolver stopped the run (allow_early_stop)")
        except StopRun as exc:
            stop_reason = exc.reason
        except BudgetExceeded as exc:
            stop_reason = f"hard budget: {exc}"
        return self._finish(stop_reason or "completed all epochs")

    def _order(self, epoch: int) -> list[str]:
        """The epoch's training tasks in the order the Trainer takes them: the split's, reshuffled per epoch unless
        data.shuffle is false."""
        order = list(self.data.splits["train"])
        if self.config.data["data"]["shuffle"]:
            random.Random(int(self.config.data["data"]["shuffle_seed"]) + epoch).shuffle(order)
        return order

    def _completed_steps(self) -> list[dict]:
        path = self.run_dir / "steps.jsonl"
        if not self.resuming or not path.exists():
            return []
        steps = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        if steps:
            self.log(f"resuming {self.run_name} after step {steps[-1]['step']}")
        return steps

    def _restore(self, last: dict) -> None:
        """Back to the end of the last completed step: adapter, state, material, spending."""
        Tree.reset(self.checkout, last["commit"])
        snapshot = self.run_dir / f"step_{last['step']:04d}" / "state"
        _copy_dir(snapshot, self.ws.state)
        self.material.truncate(last["step"])
        for record_dir in self.run_dir.glob("step_*"):        # records of a step that never completed
            if int(record_dir.name.split("_")[1]) > last["step"]:
                shutil.rmtree(record_dir)
        spent = last.get("budget_after") or {}
        self.budget = Budget(**{k: self.config["trainer"]["budget"].get(k) for k in ("usd", "tokens", "hours")},
                             spent=spent)

    def _context(self) -> Context:
        agent = self._agent(None)
        tools = self._tools(None, agent)
        return Context(workspace=self.ws, access=self.access, tools=tools, params=self.params, base=self.config.base,
                       message=self._message(None, tools), sandbox=self.sandbox, agent=agent)

    def _agent(self, cancel: threading.Event | None, metered: list | None = None):
        """The run's model as the Evolver's `direct` Base Agent, metered; None without an `llm` section."""
        if self.llm is None:
            return None
        return evolver_agent(self.llm, self.budget, metered, cancel, self.run_dir / "tools.jsonl")

    def _tools(self, cancel: threading.Event | None, agent=None) -> dict:
        # The `llm` tool's own agent: metered alike, its calls logged by the tool (so a refused call is logged too)
        tool_agent = None if agent is None else evolver_agent(self.llm, self.budget, agent.model.metered, cancel)
        tools = assemble(adapter=self.ws.adapter, root=self.run_dir / "root",
                         template_decls=read_manifest(self.run_dir / "root").get("evolver_tools") or [],
                         task_decls=_task_tool_decls(self.config), task_base=self.config.base,
                         budget=self.budget, agent=tool_agent, python=self.python, enforce=self.enforce,
                         resolve=self.sandbox.outside, snapshot=self._snapshot_adapter,
                         runner=self.tool_runner if self.tool_runner.enabled else None,
                         within=[self.ws.scratch, self.ws.state])
        tools["run_agent"] = self._run_agent_tool()
        for tool in tools.values():
            tool.log_path = self.run_dir / "tools.jsonl"
            tool.cancel = cancel
        return tools

    def _snapshot_adapter(self, source: Path) -> Path:
        """A private copy of the adapter the learner names, made inside the learner's own sandbox (a link it swaps in
        the meantime can only reach what the learner sees), then sanitized on its way out: links that leave the
        adapter, special files and setuid bits are dropped. The rollout gets the copy, which the learner cannot
        change while it runs."""
        dest = self.run_dir / "agent_runs" / uuid.uuid4().hex[:12]
        raw = dest.with_name(dest.name + ".raw")
        raw.mkdir(parents=True)
        try:
            if self.sandbox.enabled and shutil.which("bwrap"):
                args, env = self.sandbox.wrap(["cp", "-a", "--no-preserve=ownership", f"{source}/.", str(raw)],
                                              writable=[raw])
                run = subprocess.run(args, env=env, capture_output=True, text=True, timeout=300)
                if run.returncode != 0:
                    raise ValueError(f"the adapter could not be copied from the workspace: {run.stderr.strip()[-300:]}")
            else:
                shutil.copytree(source, raw, symlinks=True, dirs_exist_ok=True)
            sanitized_copy(raw, dest)
        finally:
            shutil.rmtree(raw, ignore_errors=True)
        return dest

    def _run_agent_tool(self) -> FunctionTool:
        """The acting-agent runner: an adapter through the user's pipeline and judge, on train or validation tasks."""
        allowed = {"train": set(self.data.splits["train"]), "val": set(self.data.splits["val"])}

        def run_agent(task_ids: list, adapter: str | None = None, _snapshot=None) -> dict:
            unknown = [t for t in task_ids if t not in allowed["train"] | allowed["val"]]
            if unknown:
                raise ValueError(f"only training or validation tasks may be run: {unknown[:5]}")
            target = self.sandbox.outside(adapter) if adapter else self.ws.adapter      # a name the learner sees
            if not target.resolve().is_relative_to(self.run_dir.resolve()):
                raise ValueError("the adapter must be inside the workspace")
            tasks = [self.data.tasks[t] for t in task_ids]
            snapshot = (_snapshot or self._snapshot_adapter)(target)     # a caller's narrower view may copy it
            try:
                results = run_tasks(tasks, snapshot, self.pipeline, self.judge,
                                    concurrency=int(self.config["agent"]["concurrency"]), budget=self.budget,
                                    part="evolver")
            finally:
                shutil.rmtree(snapshot, ignore_errors=True)
            # A training task's run also names its log (the trajectory, when the pipeline keeps one), copied into the
            # run's agent_logs/ (shown read-only at /w/agent-logs); and a run whose pipeline reports how its session
            # ended (a CLI agent's exit code and timeout) says so.
            return {"results": [{k: r[k] for k in ("id", "status", "output", "judge", "cost_usd", "wall_s")}
                                | ({"log": self._visible_log(r)} if r["id"] in allowed["train"] else {})
                                | {k: (r.get("pipeline_extra") or {})[k] for k in ("exit_code", "timed_out")
                                   if k in (r.get("pipeline_extra") or {})} for r in results]}
        return FunctionTool("run_agent", "Run an adapter (default: your working copy) on training or validation "
                            "tasks through the task's pipeline and judge; returns outputs and scores, and for "
                            "training tasks the path of each run's log. Its cost counts toward the run's budget.",
                            run_agent, source="run",
                            input_schema={"type": "object", "required": ["task_ids"], "additionalProperties": False,
                                          "properties": {"task_ids": {"type": "array", "items": {"type": "string"}},
                                                         "adapter": {"type": "string"}}})

    def _visible_log(self, result: dict) -> str | None:
        """A run's log as the learner can open it: a copy (through the sanitized archive) in the run's agent_logs/,
        which the learner's sandbox shows read-only at /w/agent-logs. A host path: a server that serves the tool to a
        sandboxed session names it as the sandbox shows it."""
        log = Path(result["log"]) if result.get("log") else None
        if log is None or not os.path.lexists(log):
            return None
        place = self.run_dir / "agent_logs" / f"{_safe(result['id'])}-{uuid.uuid4().hex[:6]}"   # this process's own
        sanitized_copy(log.parent, place, [log.name])
        return str(place / log.name)

    def _message(self, progress: Progress | None, tools: dict) -> str:
        lines = ["# This call", ""]
        if progress:
            lines.append(f"Epoch {progress.epoch}, step {progress.step}"
                         + (" (last step of the epoch)." if progress.is_epoch_end else "."))
            if progress.remaining:            # only an Evolver that chooses_tasks is given them
                shown = ", ".join(progress.remaining[:50]) + (" ..." if len(progress.remaining) > 50 else "")
                which = "the first epoch's" if progress.step == 0 else "this epoch's"   # step 0: the bootstrap
                lines.append(f"Training tasks {which} steps have still to run ({len(progress.remaining)}): {shown}")
        lines += [f"Adapter (your working copy, read/write): {self.ws.adapter}",
                  f"Learning material (read-only): {self.ws.learning_material} — start with README.md and current/",
                  f"Task description (read-only): {self.ws.task_info} — start with README.md",
                  f"History of the adapter (read-only): {self.ws.history}",
                  f"Your state across calls (read/write): {self.ws.state}",
                  f"Scratch (read/write, not kept): {self.ws.scratch}",
                  f"Output (write; report.md is shown with the commit): {self.ws.output}"]
        if self.access.validation:
            lines.append(f"Validation tasks (read-only): {self.access.validation}")
        for e in self.access.environment:
            lines.append(f"Task environment (read-only): {e}")
        lines += ["", "Tools: " + ", ".join(f"{t.name} ({t.source})" for t in tools.values()),
                  f"Remaining budget: {json.dumps(self.budget.remaining())}",
                  "Hand back exactly one adapter: the contents of the adapter directory when you return."]
        return "\n".join(lines) + "\n"

    def _call(self, what: str, fn, *, progress, timeout_s: float | None = None, cancel: threading.Event | None = None):
        """Run an Evolver method in a thread; stop it on a hard budget or the per-call timeout."""
        box: dict = {}
        cancel = cancel or threading.Event()

        def target():
            try:
                box["value"] = fn()
            except BaseException as exc:          # noqa: BLE001 — reported as the call's failure
                box["error"] = exc
        thread = threading.Thread(target=target, daemon=True, name=f"evolver-{what}")
        thread.start()
        started = time.time()
        reason = None
        while thread.is_alive():
            thread.join(2)
            if reason is None:
                if self.budget.exceeded():
                    reason = f"budget: {self.budget.exceeded()}"
                elif timeout_s and time.time() - started > timeout_s:
                    reason = f"timeout: the call exceeded {timeout_s / 60:.0f} min"
                if reason:
                    # Stop at the next point the Trainer controls: tools refuse further calls, started programs are
                    # killed, and the call is awaited (no abandoned threads). Spending may slightly exceed the budget.
                    cancel.set()
        return box, reason, thread.is_alive()

    def _spend(self, result: StepResult) -> None:
        """The Evolver call's own usage (its StepResult's), on the budget."""
        for usage in result.usage:
            if not isinstance(usage, Usage):
                usage = Usage(**usage)
            self.budget.add("evolver", usage)

    def _step(self, progress: Progress, chunk: list[str], feedback: list | None, session: str | None,
              selection: dict | None = None) -> dict:
        d = self.config.data
        n = progress.step
        self.budget.check()
        parent = Tree.head(self.checkout)
        t0 = time.time()
        self.log(f"step {n} (epoch {progress.epoch}): rollouts on {len(chunk)} tasks")
        # 1–2. rollouts with the current adapter (exported fresh from the commit).
        rollout_adapter = self.tree.export(parent, self.run_dir / "rollout_adapter")
        results = run_tasks([self.data.tasks[t] for t in chunk], rollout_adapter, self.pipeline, self.judge,
                            concurrency=int(d["agent"]["concurrency"]), budget=self.budget)
        rollout_s = time.time() - t0
        changed_since_last = self._last_rollout_commit is not None and parent != self._last_rollout_commit
        self._last_rollout_commit = parent
        # 3. material.
        self.material.write(n, progress.epoch, parent, changed_since_last, results, feedback)
        return self._evolve("step", progress, parent, chunk, results, rollout_s, session, selection)

    def _bootstrap(self) -> dict:
        """Step 0 of a fresh run: the Evolver's bootstrap on the starting adapter, before any rollout."""
        self.budget.check()
        self.log("step 0: bootstrap (no rollouts)")
        remaining = tuple(self._order(1)) if self.evolver.chooses_tasks else ()
        record = self._evolve("bootstrap", Progress(0, 0, False, remaining=remaining), Tree.head(self.checkout), [],
                              [], 0.0, None)
        if record["changed"]:
            self.tree.tag(f"{self.run_name}/bootstrap", record["commit"], force=True)
        return record

    def _evolve(self, what: str, progress: Progress, parent: str, chunk: list[str], results: list[dict],
                rollout_s: float, session: str | None, selection: dict | None = None) -> dict:
        """One Evolver call (`step` or `bootstrap`) on a fresh workspace, then check, repair, commit and record."""
        d = self.config.data
        n = progress.step
        # 4. workspace: the Evolver's copy carries the adapter's git history (the rollouts' copy does not).
        self.tree.export(parent, self.ws.adapter, history=True)
        self.tree.history_view(parent, self.ws.history)
        _fresh(self.ws.scratch)
        _fresh(self.ws.output)
        cancel = threading.Event()
        metered: list[Usage] = []
        agent = self._agent(cancel, metered)
        tools = self._tools(cancel, agent)
        # 5. the Evolver.
        e0 = time.time()
        # The endpoint serves sandboxed programs: results name paths as the sandbox shows them (ctx.tools, called in
        # this process, keep host paths).
        with ToolServer(inside_tools(tools, self.sandbox)) as server:
            ctx = StepContext(workspace=self.ws, access=self.access, tools=tools, params=self.params, base=self.config.base,
                              message=self._message(progress, tools) + f"Tool endpoint (HTTP): {server.url}\n",
                              sandbox=self.sandbox, cancel=cancel, tool_url=server.url, progress=progress,
                              session=session if d["evolver"]["resume"] else None, agent=agent)
            method = self.evolver.bootstrap if what == "bootstrap" else self.evolver.step
            box, stopped, alive = self._call(what, lambda: method(ctx), progress=progress,
                                             timeout_s=float(d["trainer"]["call_timeout_min"]) * 60, cancel=cancel)
        evolver_s = time.time() - e0
        # What the learner could write is read by this process from here on: no link out of it, no pipe in it.
        for place in (self.ws.adapter, self.ws.state, self.ws.scratch, self.ws.output):
            drop_unsafe(place)
        result: StepResult
        if "error" in box:
            result = StepResult("failed", f"{what} raised {type(box['error']).__name__}: {box['error']}")
        elif stopped:
            result = box.get("value") or StepResult("failed", f"stopped by the Trainer ({stopped})")
            if stopped.startswith("budget"):
                if isinstance(result, StepResult):
                    self._spend(result)                 # what the stopped call reports it spent counts all the same
                raise BudgetExceeded(stopped)
            result = StepResult("failed", f"stopped by the Trainer ({stopped})",
                                usage=result.usage if isinstance(result, StepResult) else [])
        else:
            result = box.get("value")
            if result is None and what == "bootstrap":
                result = StepResult("done", "no bootstrap")
            elif not isinstance(result, StepResult):
                result = StepResult("failed", f"{what} returned {type(result).__name__}, not StepResult")
        self._spend(result)
        # 6–7. check, repair, commit.
        commits, repaired, check = self._commit_step(progress, result, parent,
                                                     label="bootstrap (step 0)" if what == "bootstrap" else None)
        # 8. record.
        step_dir = self.run_dir / f"step_{n:04d}"
        _copy_dir(self.ws.output, step_dir / "output")
        _copy_dir(self.ws.state, step_dir / "state")
        (step_dir / "step_result.json").write_text(json.dumps(asdict(result), indent=1, default=str))
        record = {"step": n, "epoch": progress.epoch, "tasks": chunk, **({"bootstrap": True} if what == "bootstrap" else {}),
                  **(selection or {}), "epoch_end": progress.is_epoch_end, **self._request(progress, result),
                  "rollout": {"mean_score": _mean([r["judge"]["score"] for r in results]),
                              "scores": {r["id"]: r["judge"]["score"] for r in results},
                              "cost_usd": _sum([r["cost_usd"] for r in results]),
                              "unknown_cost_tasks": sum(1 for r in results if r["cost_usd"] is None),
                              "wall_s": round(rollout_s, 1),
                              "api_speed": [r["api_speed"] for r in results if r.get("api_speed")]},
                  "evolver": {"status": result.status, "summary": result.summary, "wall_s": round(evolver_s, 1),
                              "usage": [asdict(u) if isinstance(u, Usage) else u for u in result.usage],
                              "metered_tool_usage": [asdict(u) for u in metered]},
                  "status": result.status, "session": result.session, "parent": parent, "commits": commits,
                  "commit": Tree.head(self.checkout), "changed": Tree.head(self.checkout) != parent,
                  "check": check, "repaired": repaired, "check_feedback_next": repaired or None,
                  "budget_after": self.budget.snapshot(), "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with open(self.run_dir / "steps.jsonl", "a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self.log(f"step {n}: mean score {record['rollout']['mean_score']}, evolver {result.status}: {result.summary}"
                 f"; adapter {'changed' if record['changed'] else 'unchanged'}"
                 + (f"; repaired {len(repaired)} file(s)" if repaired else ""))
        return record

    def _request(self, progress: Progress, result: StepResult) -> dict:
        """The Evolver's choice of the next step's tasks, checked as it is made: {"next_tasks": [...]} when accepted,
        {"next_tasks_refused": reason} when not, {} without one. Accepted are distinct task ids, at most
        data.step_size, of progress.remaining, or at an epoch's end (nothing remaining) of the training split, which
        the next epoch runs again; and only from an Evolver that chooses_tasks."""
        wanted = result.next_tasks
        if wanted is None or (isinstance(wanted, (list, tuple)) and not wanted):
            return {}                         # no request: an empty list names nothing
        step_size = int(self.config.data["data"]["step_size"])
        allowed = progress.remaining or self.data.splits["train"]
        if not self.evolver.chooses_tasks:
            problem = "the Evolver does not choose tasks (set chooses_tasks = True on its class)"
        elif not isinstance(wanted, (list, tuple)) or not all(isinstance(t, str) for t in wanted):
            problem = "not a list of task ids"
        elif len(set(wanted)) != len(wanted):
            problem = "it names a task twice"
        elif len(wanted) > step_size:
            problem = f"{len(wanted)} tasks, more than data.step_size ({step_size})"
        elif any(t not in allowed for t in wanted):
            problem = f"not training tasks still to run: {[t for t in wanted if t not in allowed][:5]}"
        else:
            return {"next_tasks": list(wanted)}
        self.log(f"step {progress.step}: next_tasks refused, {problem}")
        return {"next_tasks_refused": f"{wanted!r:.200}: {problem}"}

    def _commit_step(self, progress: Progress, result: StepResult, parent: str,
                     label: str | None = None) -> tuple[list[str], list, dict]:
        """Commit what the Evolver handed back; on a failed check commit it as is, then the repair."""
        label = label or f"step {progress.step} (epoch {progress.epoch})"
        if result.status in ("failed", "out_of_budget"):
            return [], [], {"skipped": f"status {result.status}: the previous adapter stays"}
        adapter = self.ws.adapter
        _sanitize_in_place(adapter)          # the checks, the repair and the tree read and write only what is in it
        parent_copy = self.tree.export(parent, self.run_dir / "parent_adapter")
        Tree.sync(adapter, self.checkout)
        message = (f"{label} [{self.spec.name}]: {result.summary}\n\n"
                   f"run: {self.run_name}\nevolver: {self.spec.name}\nrecords: {self.run_dir}/step_{progress.step:04d}\n")
        if (self.ws.output / "report.md").exists():
            message += f"report: {self.run_dir}/step_{progress.step:04d}/output/report.md\n"
        if result.rationale:
            message += f"\n{result.rationale.strip()[:2000]}\n"
        if not self._dirty():
            return [], [], {"passed": True, "note": "adapter unchanged"}
        check = check_spec(adapter, self.run_dir / "root", python=self.python, parent=parent_copy,
                           enforce=self.enforce)
        if check.passed:
            commit = Tree.commit(self.checkout, message)
            return [commit] if commit else [], [], check.to_dict()
        invalid = Tree.commit(self.checkout, message + f"\n{INVALID_MARK}\n")
        final, repaired = repair(adapter, self.run_dir / "root", parent_copy, python=self.python, enforce=self.enforce)
        Tree.sync(adapter, self.checkout)
        lines = "\n".join(f"- {r['path']}: restored from {r['restored_from']} ({r['reason']})" for r in repaired)
        fixed = Tree.commit(self.checkout, f"repair {label}: {len(repaired)} file(s)\n\n{lines}\n")
        if not final.passed:   # cannot happen while the parent passes; keep the branch valid regardless
            Tree.sync(parent_copy, self.checkout)
            fixed = Tree.commit(self.checkout, f"repair {label}: back to the parent\n") or fixed
        commits = [c for c in (invalid, fixed) if c]
        return commits, repaired, {**check.to_dict(), "after_repair": final.to_dict()}

    def _dirty(self) -> bool:
        from hermit.tree import git
        git(self.checkout, "add", "-A")
        return bool(git(self.checkout, "status", "--porcelain"))

    def _checkpoint(self, record: dict) -> None:
        freq = self.config["trainer"]["save_freq"] or {}
        n, epoch = record["step"], record["epoch"]
        head = Tree.head(self.checkout)
        if freq.get("steps") and n % int(freq["steps"]) == 0:
            self.tree.tag(f"{self.run_name}/ckpt-step-{n}", head, force=True)
        if freq.get("epochs") and record.get("epoch_end") and epoch % int(freq["epochs"]) == 0:
            self.tree.tag(f"{self.run_name}/ckpt-epoch-{epoch}", head, force=True)

    # ---------- the end ----------
    def _finish(self, reason: str) -> dict:
        d = self.config.data
        self.log(f"finishing: {reason}")
        head = Tree.head(self.checkout)
        self.tree.export(head, self.ws.adapter, history=True)
        ctx = self._context()
        box, stopped, _ = self._call("finalize", lambda: self.evolver.finalize(ctx), progress=None,
                                     timeout_s=float(d["trainer"]["call_timeout_min"]) * 60)
        final: FinalResult | None = box.get("value") if isinstance(box.get("value"), FinalResult) else None
        note = final.summary if final else f"finalize failed ({box.get('error') or stopped}); the last adapter is final"
        if final and Path(final.adapter).resolve() != self.ws.adapter.resolve():
            _copy_dir(Path(final.adapter), self.ws.adapter)
        steps = [json.loads(l) for l in (self.run_dir / "steps.jsonl").read_text().splitlines() if l.strip()] \
            if (self.run_dir / "steps.jsonl").exists() else []
        self.material.truncate(steps[-1]["step"] if steps else 0)   # a step stopped midway is discarded
        if final:
            result = StepResult("done", f"finalize: {note}")
            self._commit_step(Progress(steps[-1]["epoch"] if steps else 0, steps[-1]["step"] if steps else 0, True),
                              result, head, label="finalize")
        report = None
        if steps and (self.run_dir / f"step_{steps[-1]['step']:04d}" / "output" / "report.md").exists():
            report = f"step_{steps[-1]['step']:04d}/output/report.md"
        self.tree.export(Tree.head(self.checkout), self.ws.adapter)
        text = card.render(template=self.template, run_name=self.run_name, parent=self.start_commit,
                           evolver=self.spec.name, task=str(d["task_info"] or d["data"]["tasks"]),
                           epochs_done=steps[-1]["epoch"] if steps else 0,
                           steps_done=sum(1 for s in steps if not s.get("bootstrap")),
                           budget=self.budget.snapshot(), settings={
                               "sandbox": d["sandbox"], "environment": d["environment"],
                               "judge_mode": d["judge"].get("mode") or "host", "evolver_access": d["evolver_access"],
                               "validation_given": bool(d["data"].get("val"))},
                           warnings=self.warnings, summary=note, report=report)
        (self.ws.adapter / CARD).parent.mkdir(parents=True, exist_ok=True)
        (self.ws.adapter / CARD).write_text(text)
        Tree.sync(self.ws.adapter, self.checkout)
        Tree.commit(self.checkout, f"final: {self.run_name}\n\n{note}\n")
        final_commit = Tree.head(self.checkout)
        self.tree.tag(f"{self.run_name}/final", final_commit, force=True)
        summary = {"run": self.run_name, "stop_reason": reason, "final_commit": final_commit,
                   "final_tag": f"{self.run_name}/final", "tree": str(self.tree.repo), "branch": self.branch,
                   "steps": sum(1 for s in steps if not s.get("bootstrap")),
                   "bootstrap": any(s.get("bootstrap") for s in steps), "spending": self.budget.snapshot(),
                   "checkpoints": self._tags(prefix=f"{self.run_name}/ckpt-"), "finalize": note,
                   "warnings": self.warnings}
        (self.run_dir / "summary.json").write_text(json.dumps(summary, indent=1))
        self._close_sandboxes()
        return summary

    def _close_sandboxes(self) -> None:
        for close in (getattr(self, "_close_pipeline", None), getattr(getattr(self, "tool_runner", None), "close", None),
                      getattr(getattr(self, "proxy", None), "close", None),
                      getattr(getattr(self, "model_proxy", None), "close", None),
                      getattr(getattr(self, "sandbox", None), "close", None)):
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
        self._close_pipeline = None

    def _tags(self, prefix: str) -> list[str]:
        from hermit.tree import git
        return [t for t in git(self.tree.repo, "tag", "--list", f"{prefix}*").splitlines() if t]


def _without(tasks: list[str], taken: list[str]) -> list[str]:
    """`tasks` less one occurrence of each of `taken` (a split may name a task twice)."""
    left = list(tasks)
    for t in taken:
        if t in left:
            left.remove(t)
    return left


def _fresh(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _sum(values):
    known = [v for v in values if v is not None]
    return sum(known) if known else None
