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

from dataclasses import asdict
import json
import random
import shutil
import sys
import threading
import time
from pathlib import Path

import yaml

from harnessadapter.adapter.manifest import MANIFEST, files_of, read_manifest
from harnessadapter.config import Config
from harnessadapter.evolver.base import (Access, Context, Evolver, FinalResult, Progress, StepContext, StepResult, Usage,
                                         Workspace)
from harnessadapter.evolver.loader import find, instantiate
from harnessadapter.llm import ChatClient
from harnessadapter.sandbox import AllowlistProxy, Sandbox
from harnessadapter.spec import check_spec, repair
from harnessadapter.task import TaskData, load_callable
from harnessadapter.tools import FunctionTool, ToolServer, assemble
from harnessadapter.trainer import card
from harnessadapter.trainer.budget import Budget, BudgetExceeded
from harnessadapter.trainer.material import Material
from harnessadapter.trainer.rollout import run_tasks
from harnessadapter.tree import INVALID_MARK, ROOT_TAG, Tree



class StopRun(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def warnings_for(config: Config, val_given: bool) -> list[str]:
    """Risky settings: warned, never blocked."""
    d, out = config.data, []
    if not d["sandbox"]["enabled"]:
        out.append("sandbox.enabled is false: what the Evolver starts runs unconfined; it can read anything on this "
                   "machine, including the test split, and reach the network.")
    if d["sandbox"].get("env") is None:
        out.append("sandbox.env is not set: what the Evolver starts inherits every environment variable of the Trainer, "
                   "including keys kept in variables. List the variables it may see in sandbox.env.")
    if d["sandbox"]["network"] == "open":
        out.append("sandbox.network is open: learning material and other data can leave the machine.")
    if d["sandbox"]["network"] == "restricted" and d["sandbox"]["enabled"]:
        out.append("sandbox.network is restricted through a proxy (best effort): a program that ignores proxy "
                   "variables can still reach the network.")
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
    if dst.exists():
        shutil.rmtree(dst)
    if src.exists():
        shutil.copytree(src, dst, symlinks=True)
    else:
        dst.mkdir(parents=True)


def _set_origin(adapter: Path, template: dict) -> None:
    path = adapter / MANIFEST
    data = yaml.safe_load(path.read_text())
    origin = {"template": template["id"], "version": template["version"]}
    if data.get("origin") != origin:
        data["origin"] = origin
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


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
        self.pipeline = load_callable(d["pipeline"]["run"], config.base)
        self.judge = load_callable(f"{d['judge']['path']}:{d['judge']['name']}", config.base)
        self.spec = find(d["evolver"]["name"], config.base)
        self.evolver, self.params = instantiate(self.spec, d["evolver"]["params"])
        self.budget = Budget(**{k: d["trainer"]["budget"].get(k) for k in ("usd", "tokens", "hours")})
        llm = d.get("llm")
        self.llm = ChatClient(llm["base_url"], llm["model"], api_key_env=llm.get("api_key_env"),
                              price=llm.get("price"), extra_body=llm.get("extra_body")) if llm else None
        self.warnings = warnings_for(config, val_given=True)

    # ---------- setup ----------
    def _prepare(self) -> None:
        d = self.config.data
        errors = needs_errors(self.config, self.spec.needs, self.evolver.supports_resume)
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
            from harnessadapter.tree import git
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
        env = tuple(self.config.path(p) for p in d["evolver_access"]["environment"] or [])
        self.access = Access(validation=val, environment=env)
        self.proxy = AllowlistProxy(d["sandbox"]["allow"]) if (d["sandbox"]["network"] == "restricted") else None
        # The Evolver sees only what it is given: the workspace (read-write), its accesses, its own code, and what
        # the task lists in sandbox.read. The task, the answers and the run records are not among them.
        readable = [self.ws.learning_material, self.ws.task_info, self.ws.history, *([val] if val else []), *env,
                    self.run_dir / "root", self.spec.directory]
        self.sandbox = Sandbox(enabled=bool(d["sandbox"]["enabled"]), network=d["sandbox"]["network"],
                               allow=list(d["sandbox"]["allow"]),
                               read=[self.config.path(p) for p in d["sandbox"]["read"] or []], readable=readable,
                               writable=[self.ws.adapter, self.ws.state, self.ws.scratch, self.ws.output],
                               proxy_url=self.proxy.url if self.proxy else None,
                               env_allow=d["sandbox"].get("env"))
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
            result = check_spec(path, self.run_dir / "root", python=self.python) if (self.run_dir / "root").exists() \
                else None
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
        if not done and type(self.evolver).bootstrap is not Evolver.bootstrap:
            record = self._bootstrap()
            session = record.get("session") or session
        step_size = int(d["data"]["step_size"])
        train_ids = list(self.data.splits["train"])
        epochs = int(d["trainer"]["epochs"])
        stop_reason = None
        try:
            for epoch in range(1, epochs + 1):
                order = list(train_ids)
                if d["data"]["shuffle"]:
                    random.Random(int(d["data"]["shuffle_seed"]) + epoch).shuffle(order)
                chunks = [order[i:i + step_size] for i in range(0, len(order), step_size)]
                for index, chunk in enumerate(chunks):
                    number = (epoch - 1) * len(chunks) + index + 1
                    if number <= step:
                        continue
                    progress = Progress(epoch, number, is_epoch_end=index == len(chunks) - 1)
                    record = self._step(progress, chunk, last_feedback, session)
                    step = number
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
        tools = self._tools(None)
        return Context(workspace=self.ws, access=self.access, tools=tools, params=self.params,
                       message=self._message(None, tools), sandbox=self.sandbox)

    def _tools(self, cancel: threading.Event | None, metered: list | None = None) -> dict:
        tools = assemble(adapter=self.ws.adapter, root=self.run_dir / "root",
                         template_decls=read_manifest(self.run_dir / "root").get("evolver_tools") or [],
                         task_decls=_task_tool_decls(self.config), task_base=self.config.base,
                         budget=self.budget, llm_client=self.llm, python=self.python, metered=metered)
        tools["run_agent"] = self._run_agent_tool()
        for tool in tools.values():
            tool.log_path = self.run_dir / "tools.jsonl"
            tool.cancel = cancel
        return tools

    def _run_agent_tool(self) -> FunctionTool:
        """The acting-agent runner: an adapter through the user's pipeline and judge, on train or validation tasks."""
        allowed = {"train": set(self.data.splits["train"]), "val": set(self.data.splits["val"])}

        def run_agent(task_ids: list, adapter: str | None = None) -> dict:
            unknown = [t for t in task_ids if t not in allowed["train"] | allowed["val"]]
            if unknown:
                raise ValueError(f"only training or validation tasks may be run: {unknown[:5]}")
            target = Path(adapter) if adapter else self.ws.adapter
            if not target.resolve().is_relative_to(self.run_dir.resolve()):
                raise ValueError("the adapter must be inside the workspace")
            tasks = [self.data.tasks[t] for t in task_ids]
            results = run_tasks(tasks, target, self.pipeline, self.judge,
                                concurrency=int(self.config["pipeline"]["concurrency"]), budget=self.budget,
                                part="evolver")
            # A training task's run also names its log (the trajectory, when the pipeline keeps one).
            return {"results": [{k: r[k] for k in ("id", "status", "output", "judge", "cost_usd", "wall_s")}
                                | ({"log": r["log"]} if r["id"] in allowed["train"] else {}) for r in results]}
        return FunctionTool("run_agent", "Run an adapter (default: your working copy) on training or validation "
                            "tasks through the task's pipeline and judge; returns outputs and scores, and for "
                            "training tasks the path of each run's log. Its cost counts toward the run's budget.",
                            run_agent, source="run",
                            input_schema={"type": "object", "required": ["task_ids"], "additionalProperties": False,
                                          "properties": {"task_ids": {"type": "array", "items": {"type": "string"}},
                                                         "adapter": {"type": "string"}}})

    def _message(self, progress: Progress | None, tools: dict) -> str:
        lines = ["# This call", ""]
        if progress:
            lines.append(f"Epoch {progress.epoch}, step {progress.step}"
                         + (" (last step of the epoch)." if progress.is_epoch_end else "."))
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

    def _step(self, progress: Progress, chunk: list[str], feedback: list | None, session: str | None) -> dict:
        d = self.config.data
        n = progress.step
        self.budget.check()
        parent = Tree.head(self.checkout)
        t0 = time.time()
        self.log(f"step {n} (epoch {progress.epoch}): rollouts on {len(chunk)} tasks")
        # 1–2. rollouts with the current adapter (exported fresh from the commit).
        rollout_adapter = self.tree.export(parent, self.run_dir / "rollout_adapter")
        results = run_tasks([self.data.tasks[t] for t in chunk], rollout_adapter, self.pipeline, self.judge,
                            concurrency=int(d["pipeline"]["concurrency"]), budget=self.budget)
        rollout_s = time.time() - t0
        changed_since_last = self._last_rollout_commit is not None and parent != self._last_rollout_commit
        self._last_rollout_commit = parent
        # 3. material.
        self.material.write(n, progress.epoch, parent, changed_since_last, results, feedback)
        return self._evolve("step", progress, parent, chunk, results, rollout_s, session)

    def _bootstrap(self) -> dict:
        """Step 0 of a fresh run: the Evolver's bootstrap on the starting adapter, before any rollout."""
        self.budget.check()
        self.log("step 0: bootstrap (no rollouts)")
        record = self._evolve("bootstrap", Progress(0, 0, False), Tree.head(self.checkout), [], [], 0.0, None)
        if record["changed"]:
            self.tree.tag(f"{self.run_name}/bootstrap", record["commit"], force=True)
        return record

    def _evolve(self, what: str, progress: Progress, parent: str, chunk: list[str], results: list[dict],
                rollout_s: float, session: str | None) -> dict:
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
        tools = self._tools(cancel, metered)
        # 5. the Evolver.
        e0 = time.time()
        with ToolServer(tools) as server:
            ctx = StepContext(workspace=self.ws, access=self.access, tools=tools, params=self.params,
                              message=self._message(progress, tools) + f"Tool endpoint (HTTP): {server.url}\n",
                              sandbox=self.sandbox, cancel=cancel, tool_url=server.url, progress=progress,
                              session=session if d["evolver"]["resume"] else None)
            method = self.evolver.bootstrap if what == "bootstrap" else self.evolver.step
            box, stopped, alive = self._call(what, lambda: method(ctx), progress=progress,
                                             timeout_s=float(d["trainer"]["call_timeout_min"]) * 60, cancel=cancel)
        evolver_s = time.time() - e0
        result: StepResult
        if "error" in box:
            result = StepResult("failed", f"{what} raised {type(box['error']).__name__}: {box['error']}")
        elif stopped:
            result = box.get("value") or StepResult("failed", f"stopped by the Trainer ({stopped})")
            if stopped.startswith("budget"):
                raise BudgetExceeded(stopped)
            result = StepResult("failed", f"stopped by the Trainer ({stopped})", usage=result.usage)
        else:
            result = box.get("value")
            if result is None and what == "bootstrap":
                result = StepResult("done", "no bootstrap")
            elif not isinstance(result, StepResult):
                result = StepResult("failed", f"{what} returned {type(result).__name__}, not StepResult")
        for usage in result.usage:
            if not isinstance(usage, Usage):
                usage = Usage(**usage)
            self.budget.add("evolver", usage)
        # 6–7. check, repair, commit.
        commits, repaired, check = self._commit_step(progress, result, parent,
                                                     label="bootstrap (step 0)" if what == "bootstrap" else None)
        # 8. record.
        step_dir = self.run_dir / f"step_{n:04d}"
        _copy_dir(self.ws.output, step_dir / "output")
        _copy_dir(self.ws.state, step_dir / "state")
        (step_dir / "step_result.json").write_text(json.dumps(asdict(result), indent=1, default=str))
        record = {"step": n, "epoch": progress.epoch, "tasks": chunk, **({"bootstrap": True} if what == "bootstrap" else {}),
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

    def _commit_step(self, progress: Progress, result: StepResult, parent: str,
                     label: str | None = None) -> tuple[list[str], list, dict]:
        """Commit what the Evolver handed back; on a failed check commit it as is, then the repair."""
        label = label or f"step {progress.step} (epoch {progress.epoch})"
        if result.status in ("failed", "out_of_budget"):
            return [], [], {"skipped": f"status {result.status}: the previous adapter stays"}
        adapter = self.ws.adapter
        parent_copy = self.tree.export(parent, self.run_dir / "parent_adapter")
        Tree.sync(adapter, self.checkout)
        if self._dirty():               # the Evolver changed something: the committed adapter names its origin
            _set_origin(adapter, self.template)
            Tree.sync(adapter, self.checkout)
        message = (f"{label} [{self.spec.name}]: {result.summary}\n\n"
                   f"run: {self.run_name}\nevolver: {self.spec.name}\nrecords: {self.run_dir}/step_{progress.step:04d}\n")
        if (self.ws.output / "report.md").exists():
            message += f"report: {self.run_dir}/step_{progress.step:04d}/output/report.md\n"
        if result.rationale:
            message += f"\n{result.rationale.strip()[:2000]}\n"
        if not self._dirty():
            return [], [], {"passed": True, "note": "adapter unchanged"}
        check = check_spec(adapter, self.run_dir / "root", python=self.python)
        if check.passed:
            commit = Tree.commit(self.checkout, message)
            return [commit] if commit else [], [], check.to_dict()
        invalid = Tree.commit(self.checkout, message + f"\n{INVALID_MARK}\n")
        final, repaired = repair(adapter, self.run_dir / "root", parent_copy, python=self.python)
        _set_origin(adapter, self.template)
        Tree.sync(adapter, self.checkout)
        lines = "\n".join(f"- {r['path']}: restored from {r['restored_from']} ({r['reason']})" for r in repaired)
        fixed = Tree.commit(self.checkout, f"repair {label}: {len(repaired)} file(s)\n\n{lines}\n")
        if not final.passed:   # cannot happen while the parent passes; keep the branch valid regardless
            Tree.sync(parent_copy, self.checkout)
            fixed = Tree.commit(self.checkout, f"repair {label}: back to the parent\n") or fixed
        commits = [c for c in (invalid, fixed) if c]
        return commits, repaired, {**check.to_dict(), "after_repair": final.to_dict()}

    def _dirty(self) -> bool:
        from harnessadapter.tree import git
        git(self.checkout, "add", "-A")
        return bool(git(self.checkout, "status", "--porcelain"))

    def _checkpoint(self, record: dict) -> None:
        freq = self.config["trainer"]["save_freq"] or {}
        n, epoch = record["step"], record["epoch"]
        head = Tree.head(self.checkout)
        if freq.get("steps") and n % int(freq["steps"]) == 0:
            self.tree.tag(f"{self.run_name}/ckpt-step-{n}", head, force=True)
        steps_per_epoch = -(-len(self.data.splits["train"]) // int(self.config["data"]["step_size"]))
        if freq.get("epochs") and n % steps_per_epoch == 0 and epoch % int(freq["epochs"]) == 0:
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
                               "sandbox": d["sandbox"], "evolver_access": d["evolver_access"],
                               "validation_given": bool(d["data"].get("val"))},
                           warnings=self.warnings, summary=note, report=report)
        (self.ws.adapter / "CARD.md").write_text(text)
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
        if self.proxy:
            self.proxy.close()
        return summary

    def _tags(self, prefix: str) -> list[str]:
        from harnessadapter.tree import git
        return [t for t in git(self.tree.repo, "tag", "--list", f"{prefix}*").splitlines() if t]


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
