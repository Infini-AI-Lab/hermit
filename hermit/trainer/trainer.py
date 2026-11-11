"""The Trainer: the outer training loop around an Evolver.

Run directory (<output_dir>/<run_name>/):
  config.yaml          resolved config, versions, warnings
  steps.jsonl          one line per completed step
  step_NNNN/           the Evolver's output/, its StepResult, a snapshot of state/
  learning_material/   the material of every step
  summary.json         written at the end
  stream_summary.json  run.mode stream: the stream's prequential score (stream_summary)
  checkpoints.json     run.checkpoints: each checkpoint's commit and test results (checkpoints/<name>/<adapter>/)
  eval/<label>/        hermit eval's results (split mode: eval/final/, run when training ends)
  workspace/           the Evolver's directories for the current call
  access/validation/   validation tasks handed to the Evolver (when data.val is set)
  checkout/            the git worktree of this run's branch
  root/                the template's files, the reference for check_spec
  tools.jsonl          every tool call

How the run is done is the config's `run:` block (hermit/config.py): in a stream each row is scored by the adapter
the earlier rows taught (its rollout, steps.jsonl's `rollout.scores`) before the learner sees it, run_agent runs only
rows already reached (and validation rows), and the final adapter is the one the last step left; `orders` makes one
run per order (`train` below); the learner sees each verdict as run.feedback says (material.shown).
"""
from __future__ import annotations

import atexit

import dataclasses
from dataclasses import asdict
import json
import os
import random
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import yaml

from hermit.adapter.manifest import CARD, MANIFEST, read_manifest
from hermit.agent.base import login as subscription
from hermit.agent.base.interface import LoginFailed
from hermit.agent.model.backends import HARBOR_ACTING, bindings_of, problems_of, registry
from hermit.config import Config
from hermit.agent.evolver.base import (Access, Context, Evolver, FinalResult, Progress, StepContext, StepResult, Usage,
                                         Workspace)
from hermit.agent.evolver.loader import find, instantiate, load_class
from hermit.agent.model.llm import ChatClient
from hermit.isolate import CHATGPT_HOSTS, HOME_SECRETS, Recheck, ToolRunner, _cli_kind, _safe, agent_pipeline, \
    cli_sandbox, config_paths, host_pipeline, judge_for, run_secrets, sanitized_copy
from hermit.redact import redact_name, redact_path, redact_tree, redact_value
from hermit.sandbox import (AllowlistProxy, Bind, Sandbox, drop_unsafe, hidden_paths, inside_tools, model_access,
                            set_hidden)
from hermit.spec import check_spec, repair
from hermit.task import TaskData, read_ids
from hermit.tools import FunctionTool, ToolServer, assemble, evolver_agent
from hermit.trainer import card
from hermit.trainer.budget import Budget, BudgetExceeded
from hermit.trainer.material import Material, hide_label_files, shown
from hermit.trainer.rollout import mean_score, run_tasks, scored
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
    acting = d["agent"].get("params") or {}
    # With environment.image (the sandbox on) each session is in a container of its own (docker/claude/): it sees only
    # its workspace, home and mounts, reaches only the egress proxy, and its commands run in Claude Code's own sandbox.
    # In the task sandbox the session's commands share its network; on the host nothing holds them.
    contained = bool(d["environment"].get("image")) and d["sandbox"]["enabled"]
    reason = host_pipeline(config)                       # why the pipeline is not in the task sandbox; None: it is
    if acting.get("login") == "claude" and reason is None and d["environment"].get("network") == "public":
        out.append("the acting agent runs on login: claude with an open network (environment.network: public): a "
                   "command it runs (an adapter's hook, say) can read the token from its parent process and send it "
                   "out. Leave environment.network at no-network (api.anthropic.com is allowed by itself), or give "
                   "each session a container (environment.image).")
    harbor = str(d["agent"]["name"]) == "harbor"
    if acting.get("login") == "claude" and harbor:
        out.append("the acting agent runs on login: claude in each task's container (a Harbor task set): the token "
                   "reaches the CLI by name and through a descriptor, in no process's environment, but the session's "
                   "commands run as the CLI's user in that container, the task's sandbox, where a command that read "
                   "the CLI's memory could find it; the container reaches the network as the task allows.")
    elif acting.get("login") == "claude" and reason and not contained:
        out.append(f"the acting agent runs on login: claude on the host ({reason}): its sessions run unconfined, with "
                   "the token in their CLI's environment and the machine's files (the test answers among them) in "
                   "reach. With the sandbox on, run it in the task sandbox, or give each session a container "
                   "(environment.image).")
    # A CLI agent with no sign-in copies the user's own where the pipeline runs on the host or in a container; the task
    # sandbox refuses it (validate).
    on_claude = _cli_kind(d["agent"]["name"], acting) == "claude" and bool(reason)
    learner = d["evolver"].get("params") or {}             # an agent Evolver's base and model land here
    unsigned = [who for who, on, params in (("agent", on_claude, acting),
                                            ("evolver", learner.get("backend") in ("claude", "claude-code"), learner))
                if on and not params.get("login") and not params.get("binding")
                and "/" not in str(params.get("model") or "")]   # a <backend>/<model> reference pays with its key
    if unsigned:
        out.append(f"Claude Code ({', '.join(unsigned)}) runs with no login and no model binding: each session signs "
                   "in with a copy of your own Claude Code login (~/.claude), whose refresh can sign your own sessions "
                   "out, and an ANTHROPIC_API_KEY in the environment pays before any sign-in. For your subscription "
                   "set login: claude (docs/guides/claude-subscription.md).")
    if val_given and d["data"].get("val"):
        out.append("validation data is given to the Evolver with whatever it contains; if it contains answers, the "
                   "Evolver may overfit to the validation split.")
    out += [n for n in getattr(config, "notes", []) if n not in out]   # what validation set itself (a stream's shuffle)
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


def agent_errors(config: Config) -> list[str]:
    """What training asks of the acting agent: that it takes an adapter. One of Harbor's own agents takes none
    (Config.harbor_builtin): every rollout of the run would fail."""
    builtin = config.harbor_builtin()
    if builtin is None:
        return []
    return [f"the acting agent {builtin} (agent.params.acting_agent) is one of Harbor's own agents, which takes no "
            f"adapter: every rollout would fail; train with acting_agent.cli ({', '.join(HARBOR_ACTING.values())}), "
            "or evaluate it with hermit eval --adapter none"]


SHOWN_ORDER = ("none", "score", "full")          # run.feedback, from what shows the learner least


def feedback_errors(config: Config, name: str, cls, params: dict | None = None) -> list[str]:
    """run.feedback against what the Evolver's code works with under its params (evolver.params over its manifest's:
    its class's `min_feedback_for`, by default its `min_feedback`): below it, its code would fail on a verdict it is
    not shown, or read it as 0."""
    need = cls.min_feedback_for(params or {})
    if need not in SHOWN_ORDER:
        return [f"the Evolver {name} declares min_feedback {need!r}, not one of {', '.join(SHOWN_ORDER)}"]
    if SHOWN_ORDER.index(config.feedback) >= SHOWN_ORDER.index(need):
        return []
    which = "" if need == cls.min_feedback else " with these evolver.params"
    return [f"run.feedback is {config.feedback}, below what the Evolver {name} reads (its min_feedback {need}{which}): "
            f"set run.feedback to {' or '.join(SHOWN_ORDER[SHOWN_ORDER.index(need):])}"]


def _group(task_id: str, tasks: dict) -> str:
    """The task a row is an attempt (or a step) of: a Harbor row's `harbor_task` (its `<task>#k` rollouts and
    `<task>:<step>` steps share it), else the row itself."""
    return str((tasks.get(task_id) or {}).get("harbor_task") or task_id)


def _step_of_many(row: dict) -> bool:
    """Whether a row is a step of a multi-step task (hermit.harbor.dataset's step rows: a `harbor_task` with `steps`
    > 1; another task's own `steps` field is no such thing): it runs in its task's stream, after the steps before
    it."""
    return bool(row.get("harbor_task")) and isinstance(row.get("steps"), int) and row["steps"] > 1


def _ahead(wanted: list[str], to_run, tasks: dict) -> list[tuple[str, str]]:
    """The steps of a multi-step task that `wanted` names ahead of an earlier step of their stream (the task's
    rollout) still to run (in `to_run`, and not named before them in `wanted`), each with the first such step: a step
    taken ahead of an earlier one would wait for it in the task's stream."""
    def place(task_id: str) -> tuple[tuple, int]:
        row = tasks[task_id]
        return (row["harbor_task"], row.get("rollout", 1)), int(row.get("step_index") or 0)
    out = []
    for i, task_id in enumerate(wanted):
        if not _step_of_many(tasks[task_id]):
            continue
        stream, index = place(task_id)
        earlier = sorted((place(t)[1], t) for t in set(to_run) - set(wanted[:i])
                         if _step_of_many(tasks[t]) and place(t)[0] == stream and place(t)[1] < index)
        if earlier:
            out.append((task_id, earlier[0][1]))
    return out


def out_of_step(ids: list[str], tasks: dict, which: str) -> str | None:
    """Why rows cannot run in this order (`which` names it: a train order, a split an eval runs): it lists a step of a
    multi-step task ahead of an earlier step of its stream (the task's rollout) that no row before it names (_ahead),
    which it would wait for in the stream; None when each stream's steps come in step order, as the splits
    hermit.harbor.dataset writes list them."""
    # _ahead on each task's steps (it takes each rollout apart): on a whole order it builds a set of it for each step
    steps: dict[str, list[str]] = {}
    for task_id in ids:
        if _step_of_many(tasks[task_id]):
            steps.setdefault(tasks[task_id]["harbor_task"], []).append(task_id)
    ahead = [pair for rows in steps.values() for pair in _ahead(rows, rows, tasks)]
    if not ahead:
        return None
    return (f"{which} lists a step of a multi-step task ahead of an earlier step of its stream, which it would wait "
            "for (up to hours, then ending infra): "
            + ", ".join(f"{t} ahead of {e} (task {tasks[t]['harbor_task']})" for t, e in ahead[:5])
            + (f" and {len(ahead) - 5} more" if len(ahead) > 5 else "")
            + "; a task's steps run in order, each after the one before it: list them in step order")


def train_order(config: Config, data: TaskData, epoch: int) -> list[str]:
    """The epoch's training rows in the order the Trainer takes them: the split's (an order file's, in a run of
    run.orders), reshuffled per epoch when data.shuffle is set, a multi-step task's rows moved as one, in step order
    (a step taken ahead of an earlier one would wait for it in the task's stream); an order's seed shuffles the tasks
    instead, each task's rows kept together in the split's order (Harbor's `#k` attempts, a multi-step task's
    steps). An order taken as given that lists a step ahead of an earlier one is refused before training
    (run_problems)."""
    order = list(data.splits["train"])
    seed = (config.order or {}).get("seed")
    if seed is not None:
        groups: dict[str, list[str]] = {}
        for task_id in order:
            groups.setdefault(_group(task_id, data.tasks), []).append(task_id)
        keys = list(groups)
        random.Random(int(seed)).shuffle(keys)
        return [task_id for key in keys for task_id in groups[key]]
    if config.data["data"]["shuffle"]:
        # a multi-step task's rows under one key (its task's name), in step order; any other row under its place in
        # the split, each copy of a task the split names twice too (so a set with no multi-step task is shuffled as
        # it always was, row by row)
        rows: dict[str | int, list[str]] = {}
        for i, task_id in enumerate(order):
            many = _step_of_many(data.tasks[task_id])
            rows.setdefault(_group(task_id, data.tasks) if many else i, []).append(task_id)
        keys = list(rows)
        random.Random(int(config.data["data"]["shuffle_seed"]) + epoch).shuffle(keys)
        place = lambda t: int(data.tasks[t].get("step_index") or 0) if _step_of_many(data.tasks[t]) else 0
        return [task_id for key in keys for task_id in sorted(rows[key], key=place)]
    return order


def straddle(checkpoint: dict, records: list[dict]) -> str | None:
    """Why `checkpoint` has no commit to test in these step records: its `after` row is inside the first step that
    ran it, not that step's last row (a checkpoint follows a whole step). None when it does not."""
    for record in records:
        tasks = record.get("tasks") or []
        if record.get("bootstrap") or checkpoint["after"] not in tasks:
            continue
        if tasks[-1] == checkpoint["after"]:
            return None
        return (f"run.checkpoints {checkpoint['name']}: after {checkpoint['after']!r} is inside step "
                f"{record['step']}'s tasks ({tasks[0]} .. {tasks[-1]}), and a checkpoint follows a whole step: name "
                f"the step's last task, {tasks[-1]!r}, or remove the checkpoint")
    return None


def run_problems(config: Config, data: TaskData, deterministic: bool | None) -> list[str]:
    """What the run asks of the data, checked before training: each order the run takes rows in (the train order, but
    an eval config's, which hermit eval never runs; split mode's test split, which its final test runs, and an eval
    config's; each checkpoint's split) lists a multi-step task's steps in step order (out_of_step); a stream's
    validation rows are none of its train rows (run_agent runs a validation row at any step, before the stream
    reaches it); each checkpoint's `after` row is in the train split and, when the steps' chunks are known ahead
    (`deterministic`: an Evolver that does not choose its tasks), the last row of a step; each checkpoint's split
    names known tasks."""
    out = []
    order = train_order(config, data, 1)       # every epoch's but data.shuffle's, which keeps the steps in step order
    step_size = int(config.data["data"]["step_size"])
    which = f"order {config.order['index']}'s train split" if config.order else "the train split"
    trains = order if config.mode != "eval" else []
    tests = data.splits["test"] if config.mode in ("split", "eval") else []
    for ids, name in ((trains, which), (tests, "the test split")):
        problem = out_of_step(ids, data.tasks, name)
        if problem:
            out.append(problem)
    both = sorted(set(order) & set(data.splits["val"]))
    if config.mode == "stream" and both:
        out.append(f"run.mode: stream: {which} and the validation split share {len(both)} row(s) (e.g. {both[:3]}): "
                   "run_agent runs a validation row at any step, before the stream scores it as a train row; name "
                   "each row in one of the two")
    for c in config.checkpoints():
        if c["after"] != "end":
            if c["after"] not in order:
                out.append(f"run.checkpoints {c['name']}: after {c['after']!r} is not a row of {which}")
            elif deterministic:
                i = order.index(c["after"])
                chunks = [{"step": n // step_size + 1, "tasks": order[n:n + step_size]}
                          for n in range(0, len(order), step_size)]
                problem = straddle(c, chunks[: i // step_size + 1])
                if problem:
                    out.append(problem + f" (data.step_size {step_size})")
        try:
            ids = read_ids(c["split"]) if c["split"] is not None else []
        except OSError as exc:
            out.append(f"run.checkpoints {c['name']}: split {c['split']}: {exc.strerror or exc}")
            continue
        missing = [i for i in ids if i not in data.tasks]
        if not ids:
            out.append(f"run.checkpoints {c['name']}: split {c['split']} names no task")
        elif missing:
            out.append(f"run.checkpoints {c['name']}: split {c['split']} names unknown tasks: {missing[:5]}")
        elif problem := out_of_step(ids, data.tasks, f"run.checkpoints {c['name']}: split {c['split']}"):
            out.append(problem)
    return out


def stream_summary(steps: list[dict], order: list[str], stop_reason: str) -> dict:
    """A stream's score from its step records: each row's score in the order the rows ran, the infra rows left out
    and counted; the prequential mean, the means of the stream's first and second half (by the planned rows, ⌈n/2⌉
    first), each step's, and the coverage: rows scored of the rows in the stream (a run stopped early says why). A
    row's score is its rollout's (scored before the learner saw the row), or the Evolver's own where it gives one
    (the record's `own_scores`, Evolver.step_scores: Harness²'s deliverable; None, a row its protocol leaves
    unscored, is left out and counted); then `own_scores` counts the rows it scored and `rollout_mean` is the
    rollouts' mean beside it."""
    rows, rollouts, per_step, own_rows = [], [], [], 0
    for record in steps:
        if record.get("bootstrap"):
            continue
        scores = (record.get("rollout") or {}).get("scores") or {}
        own = record.get("own_scores") or {}
        tasks = list(record.get("tasks") or [])
        mine = [own[t] if t in own else scores.get(t) for t in tasks]
        own_rows += sum(1 for t in tasks if own.get(t) is not None)
        got = [v for v in mine if v is not None]
        per_step.append({"step": record["step"], "tasks": tasks, "mean": _mean(got), "scored": len(got),
                         "unscored": len(mine) - len(got), **({"own": sorted(t for t in tasks if t in own)}
                                                               if own else {})})
        rows += mine
        rollouts += [scores.get(t) for t in tasks]
    half = (len(order) + 1) // 2
    got = [v for v in rows if v is not None]
    out = {"rows": len(order), "reached": len(rows), "scored": len(got), "unscored": len(rows) - len(got),
           "mean": _mean(got), "first_half": _mean([v for v in rows[:half] if v is not None]),
           "second_half": _mean([v for v in rows[half:] if v is not None]),
           "coverage": round(len(got) / len(order), 4) if order else None}
    if any("own" in s for s in per_step):              # the Evolver's own verdict on some row, a score or none
        out |= {"own_scores": own_rows, "rollout_mean": _mean([v for v in rollouts if v is not None])}
    if stop_reason != "completed all epochs":
        out["stopped"] = stop_reason
    return out | {"steps": per_step}


STREAM_HEADLINE = ("rows", "reached", "scored", "unscored", "mean", "first_half", "second_half", "coverage",
                   "own_scores", "rollout_mean", "stopped")


def train(config: Config, *, log=print) -> dict:
    """`hermit train`: the run (Trainer.train), or with run.orders one run per order, `<run_name>-o<i>`, one after
    the other (an order whose run finished is kept, its checkpoint tests completed; with trainer.resume_mode: disable
    it is started over, as a run without orders is), and their combined stream score in
    `<output_dir>/<run_name>/streams.json`: each order's and the mean over the orders. Every order to train is
    checked before the first one trains (run.feedback against the Evolver, the acting agent, its run block against
    its train split)."""
    runs = config.order_runs()
    if runs == [config]:
        return Trainer(config, log=log).train()
    fresh = str(config.data["trainer"]["resume_mode"]) == "disable"
    spec = find(config.data["evolver"]["name"], config.base)
    cls = load_class(spec)
    problems = feedback_errors(config, spec.name, cls, {**spec.params, **(config.data["evolver"]["params"] or {})})
    problems += agent_errors(config)
    for cfg in runs:
        if fresh or not (cfg.run_dir() / "summary.json").is_file():
            d = cfg.data["data"]
            data = TaskData(cfg.path(d["tasks"]), cfg.path(d["train"]), cfg.path(d["val"]), cfg.path(d["test"]))
            problems += [f"order {cfg.order['index']}: {p}"
                         for p in run_problems(cfg, data, deterministic=not cls.chooses_tasks)]
    if problems:
        raise SystemExit("train refuses to start: " + "; ".join(problems))
    summaries = []
    for cfg in runs:
        done = cfg.run_dir() / "summary.json"
        if done.is_file() and not fresh:
            log(f"order {cfg.order['index']}: {cfg.run_name()} finished before; kept")
            if cfg.checkpoints():
                from hermit.evaluate import checkpoint_tests
                checkpoint_tests(cfg, log=log)
            summaries.append(json.loads(done.read_text()))
        else:
            summaries.append(Trainer(cfg, log=log).train())
    orders = []
    for cfg, summary in zip(runs, summaries):
        stream = summary.get("stream") or {}
        orders.append({"order": cfg.order["index"], **{k: cfg.order[k] for k in ("seed", "file") if k in cfg.order},
                       "run": cfg.run_name(), "stop_reason": summary.get("stop_reason"),
                       **{k: stream.get(k) for k in STREAM_HEADLINE if k in stream}})
    combined = {"run": config.run_name(), "orders": orders}
    for key in ("mean", "first_half", "second_half"):
        combined[key] = _mean([o.get(key) for o in orders])
    place = config.run_dir() / "streams.json"
    place.parent.mkdir(parents=True, exist_ok=True)
    place.write_text(json.dumps(combined, indent=1))
    log(f"{len(orders)} orders: mean {combined['mean']} ({place})")
    return combined


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
        sanitized_copy(src, dst)                         # the learner's unreadable files read (owner_readable)
    else:
        dst.mkdir(parents=True)


def _sanitize_in_place(path: Path) -> None:
    """Replace a directory the learner wrote by its sanitized copy (links that leave it, pipes and devices dropped),
    so that the host's later reads and writes there (the spec check, the repair, the tree) stay inside it. Its own
    directory is kept (a sandbox may bind it)."""
    if not path.is_dir() or path.is_symlink():
        return
    clean = path.with_name(path.name + f".clean-{uuid.uuid4().hex[:6]}")
    os.chmod(path, os.stat(path).st_mode | 0o700)            # left read-only: its entries are replaced below
    sanitized_copy(path, clean)
    def writable(function, name, _):          # a directory the learner left read-only: made the owner's again
        os.chmod(os.path.dirname(name), 0o700)
        function(name)
    for child in list(path.iterdir()):
        if child.is_dir() and not child.is_symlink():
            os.chmod(child, 0o700)
            shutil.rmtree(child, onexc=writable)
        else:
            child.unlink()
    for child in list(clean.iterdir()):
        child.rename(path / child.name)
    clean.rmdir()


class Trainer:
    def __init__(self, config: Config, *, python: str | None = None, log=print):
        if config.mode == "eval":
            raise ValueError("run.mode is eval: this config is for hermit eval; to train, set run.mode to split or "
                             "stream")
        if config.orders() and config.order is None:
            raise ValueError("run.orders makes one run per order: hermit train (hermit.trainer.trainer.train) runs "
                             "them, or Trainer(config.for_order(i)) one of them")
        self.config = config
        self.python = python or sys.executable
        self.log = log
        d = config.data
        # What only the judge may read, covered in every other sandbox this run (and the processes it starts) makes.
        set_hidden(hidden_paths(d["sandbox"].get("hide") or []))
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
        self.judge = None                # set when the run starts (judge_for): a sandboxed judge is never imported here
        self._login_failed: LoginFailed | None = None   # a sign-in that failed inside a learner's tool call
        self.spec = find(d["evolver"]["name"], config.base)
        self.evolver, self.params = instantiate(self.spec, d["evolver"]["params"],
                                                backends=registry(d.get("backends")), default_model=d.get("model"))
        # What the learner is shown of each verdict (material.shown): run.feedback, unless the Evolver hides the
        # labels from its sessions itself (`hides_labels`) and reads the full records in its own process.
        self.policy = "full" if getattr(self.evolver, "hides_labels", False) else config.feedback
        self._ran: list[str] = []       # the stream's rows reached so far (a step's, once its rollouts start)
        self.budget = Budget(**{k: d["trainer"]["budget"].get(k) for k in ("usd", "tokens", "hours")})
        if isinstance(d.get("llm"), dict) and d["llm"].get("cli"):     # a model only a CLI reaches
            from hermit.agent.base.endpoint import cli_model
            try:
                self.llm = cli_model(d["llm"], log_dir=self.run_dir / "llm_sessions", sandbox=self._llm_sandbox(d))
            except BaseException:
                self._close_sandboxes()                  # its proxy, started for it
                raise
        else:
            self.llm = ChatClient.from_spec(d["llm"]) if d.get("llm") else None
        self.warnings = warnings_for(config, val_given=True)

    def _scrub_access(self) -> None:
        """What the learner is shown of the task (evolver_access.environment: an evidence store the agents wrote) holds
        no model key's value; no agent runs while the learner works."""
        for path in self.access.environment:
            redact_path(Path(path).resolve())            # a configured link: its target, which the sandbox shows

    def _llm_sandbox(self, d: dict) -> Sandbox | None:
        """The sandbox of a model only a CLI reaches (`llm.cli`, isolation: sandbox, the default; cli_sandbox): each
        session sees its empty workspace and home, reaches its model's hosts (a ChatGPT login's; a Claude login's
        api.anthropic.com, its token passed by name with the session) through a proxy of its own and holds its model's
        key; nothing of the run. None with isolation: container."""
        self._llm_box, self._llm_proxy = cli_sandbox(
            d["llm"], records=self.run_dir / "sandbox", enabled=d["sandbox"]["enabled"],
            enforce=d["sandbox"].get("enforce", "required"), probe_absent=[*run_secrets(self.config), *HOME_SECRETS])
        return self._llm_box

    # ---------- setup ----------
    def _prepare(self) -> None:
        d = self.config.data
        errors = needs_errors(self.config, self.spec.needs, self.evolver.supports_resume)
        errors += feedback_errors(self.config, self.spec.name, type(self.evolver), self.params)
        errors += agent_errors(self.config)
        for who, binding, api in bindings_of(d, self.params):
            errors += [f"{who}: {p}" for p in problems_of(binding, api)]
        for who, login, program, contained, root in subscription.logins_of(d, self.params):
            errors += [f"{who}: {p}" for p in subscription.problems_of(login, program, contained, root)]
        errors += [f"evolver: {p}" for p in subscription.evolver_problems(self.params)]
        errors += run_problems(self.config, self.data, deterministic=not self.evolver.chooses_tasks)
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
        self.material = Material(self.run_dir / "learning_material", policy=self.policy)
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
        self._recheck_grants = Recheck([*env, self.ws.task_info], own=self.run_dir)
        # The learner's sandbox (docs/developer/sandbox-design.md): the workspace under fixed names below /w, its
        # models, the Trainer's tool endpoint, and the hosts the run declares. A Codex learner on a ChatGPT login
        # reaches its plan's hosts, a Claude Code learner on a Claude login api.anthropic.com.
        ev = d["sandbox"]["evolver"]
        records = self.run_dir / "sandbox"
        allow = list(ev.get("allowed_hosts") or [])
        roles = self.params.get("roles") if isinstance(self.params.get("roles"), dict) else {}   # RRSI's sessions
        if "chatgpt" in (self.params.get("login"), roles.get("login"),
                         d["llm"].get("login") if isinstance(d.get("llm"), dict) else None):
            allow += [h for h in CHATGPT_HOSTS if h not in allow]
        # The Evolver's and its roles' sessions on a login reach the login's hosts; a Claude login's token goes with
        # each session by name (the Base Agent keeps it), never to the learner's other commands. A Claude llm's
        # sessions run in a sandbox of their own (_llm_sandbox): nothing of it here.
        allow += [h for h in subscription.hosts(self.params.get("login"), roles.get("login")) if h not in allow]
        # The learner reaches its models as Harbor's agents do: the key variables pass, the providers' hosts are
        # allowed, a server on the loopback (a meter) is forwarded. Its models: the Evolver's, and its roles'.
        model_keys, model_hosts, model_ports = model_access([self.params.get("binding"), roles.get("binding")])
        allow += [h for h in model_hosts if h not in allow]
        atexit.register(self._close_sandboxes)      # whatever is started from here is closed, a failure included
        self.proxy = AllowlistProxy(allow, log=records / "network.jsonl") if allow else None
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
                               env_allow=[*(ev.get("env") or []), *model_keys], forward=model_ports,
                               enforce=d["sandbox"].get("enforce", "required"),
                               framework=bool(getattr(self.evolver, "sandbox_framework", False)), record_dir=records,
                               # The isolation check inside proves these absent: the data, the judge, the runs,
                               # the home's secrets.
                               probe_absent=[*run_secrets(self.config), *HOME_SECRETS],
                               limits=dict(ev.get("resources") or {}), role="learner")
        resolved = {"config": d, "template": self.template, "tree": str(self.tree.repo), "branch": self.branch,
                    "start": start, "evolver": {"name": self.spec.name, "directory": str(self.spec.directory),
                                                "class": self.spec.class_path, "needs": self.spec.needs,
                                                "params": self.params},
                    "run": {"mode": self.config.mode, "feedback": self.config.feedback, "shown": self.policy,
                            **({"order": self.config.order} if self.config.order else {})},
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
        try:
            self._prepare()
        except BaseException:
            self._close_sandboxes()                 # what the constructor started (the llm's proxy)
            raise
        d = self.config.data
        done = self._completed_steps()
        # A checkpoint whose `after` row a recorded step ran inside its tasks has no commit: refused before more steps
        # (the steps are kept; a checkpoint that names a step's last task, or none, lets the run go on).
        problems = [p for c in self.config.checkpoints() for p in [straddle(c, done)] if p]
        if problems:
            self._close_sandboxes()
            raise SystemExit("train refuses to go on: " + "; ".join(problems))
        self._ran = [t for r in done if not r.get("bootstrap") for t in r["tasks"]]
        if done:
            self._restore(done[-1])
        else:
            self.material.truncate(0)     # leftovers of a step that never completed
        last_feedback = done[-1].get("check_feedback_next") if done else None
        self._last_rollout_commit = done[-1]["parent"] if done and not done[-1].get("bootstrap") else None
        ctx = self._context()
        if not done:
            self._recheck_grants()               # before every learner call: others' records in its grants
            self._scrub_access()
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
                    self._ran += chunk            # reached: run_agent may run them from this step on (a stream)
                    record = self._step(progress, chunk, last_feedback, session,
                                        {"chosen_by": "evolver"} if chosen else None)
                    wanted = record.get("next_tasks")
                    last_feedback = record.get("check_feedback_next")
                    session = record.get("session") or session
                    self._checkpoint(record)
                    self._checkpoint_tests_during(record)
                    if record["status"] == "stop" and d["trainer"]["allow_early_stop"]:
                        raise StopRun("the Evolver stopped the run (allow_early_stop)")
        except StopRun as exc:
            stop_reason = exc.reason
        except BudgetExceeded as exc:
            stop_reason = f"hard budget: {exc}"
        return self._after_training(self._finish(stop_reason or "completed all epochs"))

    def _checkpoint_tests_during(self, record: dict) -> None:
        """After a step: a checkpoint whose `after` row this step ran must be its last row (checked here when the
        Evolver chose the tasks; else before the run started), else the run stops, its steps kept; with
        run.checkpoints_during, the test of each checkpoint this step reached runs now (its final adapter's, and an
        `end` checkpoint's, after training), outside the training budget: its time, like its cost, is not spent from
        trainer.budget."""
        problems = [p for c in self.config.checkpoints() for p in [straddle(c, [record])] if p]
        if problems:
            self._close_sandboxes()
            raise SystemExit("train stops: " + "; ".join(problems) + "; the steps so far are kept: fix run.checkpoints "
                             "and run hermit train again to go on")
        reached = [c["name"] for c in self.config.checkpoints()
                   if c["after"] != "end" and (record.get("tasks") or [None])[-1] == c["after"]]
        if reached and self.config.data["run"].get("checkpoints_during"):
            from hermit.evaluate import checkpoint_tests
            with self.budget.paused():                      # its spending goes to the eval's own budget
                checkpoint_tests(self.config, names=reached, log=self.log)

    def _after_training(self, summary: dict) -> dict:
        """What the run mode adds once training ended: split's test of the final adapter on data.test (as `hermit eval
        --adapter final` runs it), the checkpoint tests not run yet; both kept in summary.json."""
        from hermit.evaluate import checkpoint_tests, evaluate
        try:
            if self.config.mode == "split" and self.config.data["data"].get("test"):
                summary["test"] = evaluate(self.config, "final", log=self.log)
            if self.config.checkpoints():
                summary["checkpoint_tests"] = checkpoint_tests(self.config, log=self.log)["overview"]
        except BaseException:
            self.log(f"the run {self.run_name} finished, its tests did not: hermit eval --adapter final (split) or "
                     "hermit eval --checkpoints (a stream's) runs what is left")
            raise
        return summary

    def _order(self, epoch: int) -> list[str]:
        """The epoch's training tasks in the order the Trainer takes them (train_order)."""
        return train_order(self.config, self.data, epoch)

    def _completed_steps(self) -> list[dict]:
        path = self.run_dir / "steps.jsonl"
        if not self.resuming or not path.exists():
            return []
        # "\n" alone ends a record: U+0085, U+2028 and U+2029 in its strings (a learner's summary) are kept unescaped
        # (_evolve writes with ensure_ascii=False), where str.splitlines() would cut the record in pieces
        steps = [json.loads(l) for l in path.read_text().split("\n") if l.strip()]
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
            tool.fn = self._latched(tool.fn, cancel)
        return tools

    def _latched(self, fn, cancel: threading.Event | None):
        """A tool's function whose LoginFailed the Trainer keeps: raised in the tool server's thread (a learner's `llm`
        or `run_agent` call) it would reach the learner as a failed call, and the step would go on after the sign-in
        failed. The learner is stopped, and the call it was in ends the run (_raise_login_failed)."""
        def call(**arguments):
            try:
                return fn(**arguments)
            except LoginFailed as exc:
                self._login_failed = self._login_failed or exc
                if cancel is not None:
                    cancel.set()
                raise
        return call

    def _raise_login_failed(self, box: dict) -> None:
        """The run ends, unfinished, when the Evolver's call or a tool call in it met a failed sign-in."""
        failed = box["error"] if isinstance(box.get("error"), LoginFailed) else self._login_failed
        if failed is not None:
            raise failed

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
                try:
                    run = subprocess.run(args, env=env, capture_output=True, text=True, timeout=300)
                finally:
                    self.sandbox.abandon(args)               # a report that never came is not waited for
                if run.returncode != 0:
                    raise ValueError(f"the adapter could not be copied from the workspace: {run.stderr.strip()[-300:]}")
            else:
                shutil.copytree(source, raw, symlinks=True, dirs_exist_ok=True)
            sanitized_copy(raw, dest)
        finally:
            shutil.rmtree(raw, ignore_errors=True)
        return dest

    def _run_agent_scope(self) -> tuple[set[str], set[str]]:
        """(the training rows run_agent may run now, the validation rows): the train split (split mode, and no run
        mode); in a stream the rows reached so far, this step's included, never one still ahead; in neither set a
        training row that is a step of a multi-step task (`_steps_trained`), even one the validation split names too."""
        train = set(self._ran) if self.config.mode == "stream" else set(self.data.splits["train"])
        steps = self._steps_trained()
        return train - steps, set(self.data.splits["val"]) - steps

    def _steps_trained(self) -> set[str]:
        """The training rows that are steps of a multi-step task: each runs in the Trainer's own stream of its task
        (the same pipeline, the same run), after the steps before it, so run_agent's run of one would find its turn
        taken, or take the Trainer's (whose step would then be lost)."""
        return {t for t in self.data.splits["train"] if _step_of_many(self.data.tasks[t])}

    def _run_agent_tool(self) -> FunctionTool:
        """The acting-agent runner: an adapter through the user's pipeline and judge, on train or validation tasks
        (_run_agent_scope); a task it may not run comes back as an error result. The results are shown as the run's
        feedback policy says (material.shown), their logs without the verdict's files."""
        stream = self.config.mode == "stream"

        def run_agent(task_ids: list, adapter: str | None = None, _snapshot=None) -> dict:
            train, val = self._run_agent_scope()
            why = ("not a task of this step or an earlier step of the stream, nor a validation task" if stream
                   else "not a training or validation task")
            steps = self._steps_trained()
            refused = {t: f"{t!r} is " + ("a step of a multi-step training task, which runs only in the Trainer's "
                                          "own stream of that task" if t in steps else why)
                       + ": run_agent does not run it" for t in task_ids if t not in train | val}
            target = self.sandbox.outside(adapter) if adapter else self.ws.adapter      # a name the learner sees
            if not target.resolve().is_relative_to(self.run_dir.resolve()):
                raise ValueError("the adapter must be inside the workspace")
            tasks = [self.data.tasks[t] for t in task_ids if t not in refused]
            results: list[dict] = []
            if tasks:
                snapshot = (_snapshot or self._snapshot_adapter)(target)     # a caller's narrower view may copy it
                try:
                    results = run_tasks(tasks, snapshot, self.pipeline, self.judge,
                                        concurrency=int(self.config["agent"]["concurrency"]), budget=self.budget,
                                        part="evolver")
                finally:
                    shutil.rmtree(snapshot, ignore_errors=True)
            # Each run under the id it was asked by: TaskData names a row by str(id), and the run's record keeps the
            # row's own id (a number where tasks.jsonl gives one). run_tasks returns a run of each task, or raises (a
            # budget reached: BudgetExceeded, and so does this whole call, with no results).
            ran = {str(r["id"]): r for r in results}
            # A training task's run also names its log (the trajectory, when the pipeline keeps one), copied into the
            # run's agent_logs/ (shown read-only at /w/agent-logs); a run that failed says why; and a run whose
            # pipeline reports how its session ended (a CLI agent's exit code and timeout) says so.
            # Scrubbed of model keys: the agent held its own, and the learner's may be another provider's.
            out = []
            for t in task_ids:
                if t in refused:
                    out.append(shown({"id": t, "status": "error", "error": refused[t], "output": None,
                                      "judge": {"score": 0.0, "feedback": f"not run: {refused[t]}"},
                                      "cost_usd": 0.0, "wall_s": 0.0}, self.policy))
                    continue
                r = shown(ran[t], self.policy)
                out.append({k: r[k] for k in ("id", "status", "output", "judge", "cost_usd", "wall_s") if k in r}
                           | ({"log": self._visible_log(r)} if t in train else {})
                           | ({"error": r.get("error")} if r.get("status") in ("error", "infra") else {})
                           | {k: (r.get("pipeline_extra") or {})[k] for k in ("exit_code", "timed_out")
                              if k in (r.get("pipeline_extra") or {})})
            return {"results": redact_value(out)}
        which = ("this step's tasks, an earlier step's tasks or validation tasks" if stream
                 else "training or validation tasks")
        if self._steps_trained():
            which += " (not a step of a multi-step training task)"
        returns = {"full": "outputs and scores", "score": "outputs and each run's score (no feedback)",
                   "none": "outputs (no score)"}[self.policy]
        logs = "tasks of the stream" if stream else "training tasks"
        return FunctionTool("run_agent", f"Run an adapter (default: your working copy) on {which} through the task's "
                            f"pipeline and judge; returns {returns}, and for {logs} the path of each run's log. Its "
                            "cost counts toward the run's budget.",
                            run_agent, source="run",
                            input_schema={"type": "object", "required": ["task_ids"], "additionalProperties": False,
                                          "properties": {"task_ids": {"type": "array", "items": {"type": "string"}},
                                                         "adapter": {"type": "string"}}})

    def _visible_log(self, result: dict) -> str | None:
        """A run's log as the learner can open it: a copy (a file read once, never through a link; a directory through
        the sanitized archive, less the verdict's files unless the feedback policy is full; scrubbed of model keys) in
        the run's agent_logs/, which the learner's sandbox shows read-only at /w/agent-logs. A host path: a server
        that serves the tool to a sandboxed session names it as the sandbox shows it."""
        log = Path(result["log"]) if result.get("log") else None
        if log is None or not os.path.lexists(log):
            return None
        place = self.run_dir / "agent_logs" / f"{_safe(result['id'])}-{uuid.uuid4().hex[:6]}"   # this process's own
        try:
            if log.is_dir() and not log.is_symlink():
                sanitized_copy(log.parent, place, [log.name])
                hide_label_files(place / log.name, getattr(self, "policy", "full"))
            else:
                # One file, read once (never through a link, only a regular file): a run of the same task going on
                # beside this one may be rewriting it, which an archive's size check refuses.
                fd = os.open(log, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as f:
                    if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                        return None
                    place.mkdir(parents=True)
                    with open(place / log.name, "wb") as out:
                        shutil.copyfileobj(f, out)           # streamed: a large trajectory is not held in memory
        except OSError:
            return None                      # the run's result stands without its log
        redact_tree(place)                   # the agent held its model's key; the learner's may be another's
        return str(place / redact_name(log.name))     # as the scrub may have renamed it

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
            self._recheck_grants()               # the learner's declared paths, searched again for others' records
            self._scrub_access()
            method = self.evolver.bootstrap if what == "bootstrap" else self.evolver.step
            box, stopped, alive = self._call(what, lambda: method(ctx), progress=progress,
                                             timeout_s=float(d["trainer"]["call_timeout_min"]) * 60, cancel=cancel)
        evolver_s = time.time() - e0
        # What the learner could write is read by this process from here on: no link out of it, no pipe in it.
        for place in (self.ws.adapter, self.ws.state, self.ws.scratch, self.ws.output):
            drop_unsafe(place)
        result: StepResult
        self._raise_login_failed(box)                   # the run ends, unfinished: this step is not recorded
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
        # What the learner reports is kept (step records, steps.jsonl, the commit message): no model key it held.
        result = dataclasses.replace(result, status=redact_value(result.status), summary=redact_value(result.summary),
                                     rationale=redact_value(result.rationale), session=redact_value(result.session),
                                     extra=redact_value(result.extra), next_tasks=redact_value(result.next_tasks),
                                     usage=[dataclasses.replace(u, model=redact_value(u.model))
                                            if isinstance(u, Usage) else redact_value(u) for u in result.usage])
        # 6–7. check, repair, commit.
        commits, repaired, check = self._commit_step(progress, result, parent,
                                                     label="bootstrap (step 0)" if what == "bootstrap" else None)
        # 8. record.
        step_dir = self.run_dir / f"step_{n:04d}"
        _copy_dir(self.ws.output, step_dir / "output")
        _copy_dir(self.ws.state, step_dir / "state")
        redact_tree(step_dir)                # the learner held its model's key
        (step_dir / "step_result.json").write_text(json.dumps(asdict(result), indent=1, default=str))
        own = self._own_scores(result, chunk) if what != "bootstrap" else None
        record = {"step": n, "epoch": progress.epoch, "tasks": chunk, **({"bootstrap": True} if what == "bootstrap" else {}),
                  **(selection or {}), "epoch_end": progress.is_epoch_end, **self._request(progress, result),
                  **({"own_scores": own} if own else {}),
                  # infra rows (not the agent's result) in no mean: their score is null, and counted
                  "rollout": {"mean_score": mean_score(results),
                              "scores": {r["id"]: None if r["status"] == "infra" else r["judge"]["score"]
                                         for r in results},
                              "unscored": len(results) - len(scored(results)),
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
        record = redact_value(record)        # the rollouts' own fields (api_speed, ...) came from agents holding keys
        with open(self.run_dir / "steps.jsonl", "a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self.log(f"step {n}: mean score {record['rollout']['mean_score']}, evolver {result.status}: {result.summary}"
                 f"; adapter {'changed' if record['changed'] else 'unchanged'}"
                 + (f"; repaired {len(repaired)} file(s)" if repaired else ""))
        return record

    def _own_scores(self, result: StepResult, chunk: list[str]) -> dict | None:
        """The Evolver's own scores of the step's tasks (Evolver.step_scores), for the step's tasks only: numbers, and
        None for a row its protocol leaves unscored; None when it gives none (an error in it is a warning: the step is
        recorded all the same)."""
        try:
            scores = self.evolver.step_scores(result)
        except Exception as exc:                          # noqa: BLE001 - its own scores are an addition
            self.warnings.append(f"the Evolver's step_scores raised {type(exc).__name__}: {exc}")
            return None
        if not isinstance(scores, dict):
            return None
        return {t: None if scores[t] is None else float(scores[t]) for t in chunk
                if t in scores and (scores[t] is None or isinstance(scores[t], (int, float))
                                    and not isinstance(scores[t], bool))} or None

    def _request(self, progress: Progress, result: StepResult) -> dict:
        """The Evolver's choice of the next step's tasks, checked as it is made: {"next_tasks": [...]} when accepted,
        {"next_tasks_refused": reason} when not, {} without one. Accepted are distinct task ids, at most
        data.step_size, of progress.remaining, or at an epoch's end (nothing remaining) of the training split, which
        the next epoch runs again; of those, a step of a multi-step task only when each earlier step of its stream
        among them comes before it in the request (_ahead); and only from an Evolver that chooses_tasks."""
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
        elif ahead := _ahead(wanted, allowed, self.data.tasks):
            problem = ("a step of a multi-step task ahead of an earlier one still to run (a task's steps run in order, "
                       "in its stream: name the earlier one first): "
                       + ", ".join(f"{t} ahead of {e}" for t, e in ahead[:5]))
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
        redact_tree(adapter)                 # and no model key the learner held is kept in the tree
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
        self._recheck_grants()
        self._scrub_access()
        box, stopped, _ = self._call("finalize", lambda: self.evolver.finalize(ctx), progress=None,
                                     timeout_s=float(d["trainer"]["call_timeout_min"]) * 60)
        if reason == "completed all epochs":
            # Unfinished, as at a step: `hermit train` comes straight back here once the sign-in is restored. A run that
            # stopped early (the Evolver, a budget) would go back into training instead, so it ends as finalize failed.
            self._raise_login_failed(box)
        final: FinalResult | None = box.get("value") if isinstance(box.get("value"), FinalResult) else None
        note = final.summary if final else f"finalize failed ({box.get('error') or stopped}); the last adapter is final"
        note = redact_value(note)            # the final commit, the card and summary.json keep it
        stream = self.config.mode == "stream"
        if stream:
            # A stream picks no final: the adapter its last step left is final; what finalize handed back instead, or
            # changed in the working copy, is not kept (its summary is).
            swapped = final is not None and Path(final.adapter).resolve() != self.ws.adapter.resolve()
            _sanitize_in_place(self.ws.adapter)
            Tree.sync(self.ws.adapter, self.checkout)
            edited = self._dirty()
            Tree.reset(self.checkout, head)
            self.tree.export(head, self.ws.adapter, history=True)
            if swapped or edited:
                self.warnings.append("run.mode is stream: the final adapter is the one the stream's last step left; "
                                     "the adapter finalize " + ("handed back" if swapped else "wrote") +
                                     " was not kept")
        elif final and Path(final.adapter).resolve() != self.ws.adapter.resolve():
            _copy_dir(Path(final.adapter), self.ws.adapter)
        steps = [json.loads(l) for l in (self.run_dir / "steps.jsonl").read_text().split("\n") if l.strip()] \
            if (self.run_dir / "steps.jsonl").exists() else []          # "\n" alone ends a record (_completed_steps)
        self.material.truncate(steps[-1]["step"] if steps else 0)   # a step stopped midway is discarded
        llm = d.get("llm") if isinstance(d.get("llm"), dict) else {}
        for place in (self.ws.output, self.ws.state, self.ws.scratch,      # the learner's own files the run keeps,
                      self.ws.state.parent / "agent_home",                # its CLI's home (transcripts) too,
                      *config_paths(self.config, "environment.write", d["environment"].get("write")),  # the agents'
                      *([Path(os.path.expanduser(str(llm["home"])))] if llm.get("home") else [])):   # llm.cli's home
            redact_path(Path(place).resolve())
        if final and not stream:
            result = StepResult("done", f"finalize: {note}")
            self._commit_step(Progress(steps[-1]["epoch"] if steps else 0, steps[-1]["step"] if steps else 0, True),
                              result, head, label="finalize")
        report = None
        if steps and (self.run_dir / f"step_{steps[-1]['step']:04d}" / "output" / "report.md").exists():
            report = f"step_{steps[-1]['step']:04d}/output/report.md"
        prequential = None
        if stream:
            prequential = stream_summary(steps, self._order(1), reason)
            (self.run_dir / "stream_summary.json").write_text(json.dumps(prequential, indent=1))
            prequential = {k: prequential[k] for k in STREAM_HEADLINE if k in prequential}
        self.tree.export(Tree.head(self.checkout), self.ws.adapter)
        text = card.render(template=self.template, run_name=self.run_name, parent=self.start_commit,
                           evolver=self.spec.name, task=str(d["task_info"] or d["data"]["tasks"]),
                           epochs_done=steps[-1]["epoch"] if steps else 0,
                           steps_done=sum(1 for s in steps if not s.get("bootstrap")),
                           budget=self.budget.snapshot(), settings={
                               "sandbox": d["sandbox"], "environment": d["environment"],
                               "judge_mode": d["judge"].get("mode") or "host", "evolver_access": d["evolver_access"],
                               "validation_given": bool(d["data"].get("val")),
                               "run": {"mode": self.config.mode, "feedback": self.config.feedback,
                                       "shown": self.policy}},
                           warnings=self.warnings, summary=note, report=report, stream=prequential)
        (self.ws.adapter / CARD).parent.mkdir(parents=True, exist_ok=True)
        (self.ws.adapter / CARD).write_text(text)
        redact_tree(self.ws.adapter)
        Tree.sync(self.ws.adapter, self.checkout)
        Tree.commit(self.checkout, f"final: {self.run_name}\n\n{note}\n")
        final_commit = Tree.head(self.checkout)
        self.tree.tag(f"{self.run_name}/final", final_commit, force=True)
        summary = {"run": self.run_name, "stop_reason": reason, "final_commit": final_commit,
                   "final_tag": f"{self.run_name}/final", "tree": str(self.tree.repo), "branch": self.branch,
                   "steps": sum(1 for s in steps if not s.get("bootstrap")),
                   "bootstrap": any(s.get("bootstrap") for s in steps), "spending": self.budget.snapshot(),
                   "checkpoints": self._tags(prefix=f"{self.run_name}/ckpt-"), "finalize": note,
                   "warnings": self.warnings, **({"stream": prequential} if prequential is not None else {})}
        (self.run_dir / "summary.json").write_text(json.dumps(summary, indent=1))
        # Finished, the run never resumes: a Harbor agent (the pipeline is its bound run: hermit.isolate.agent_pipeline)
        # kept its streams for a resume, and now stops those left unfinished as it is closed (HarborAgent.close).
        owner = getattr(self.pipeline, "__self__", None)
        if getattr(owner, "keep_streams", False):
            owner.params["keep_streams"] = False
        self._close_sandboxes()
        return summary

    def _close_sandboxes(self) -> None:
        for close in (getattr(self, "_close_pipeline", None), getattr(getattr(self, "tool_runner", None), "close", None),
                      getattr(getattr(self, "llm", None), "close", None),       # a CLI model's endpoint (cli_model)
                      getattr(getattr(self, "proxy", None), "close", None),
                      getattr(getattr(self, "sandbox", None), "close", None),
                      getattr(getattr(self, "_llm_proxy", None), "close", None),
                      getattr(getattr(self, "_llm_box", None), "close", None)):
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
