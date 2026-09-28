"""OfficeQA Pro as an optimizer Environment (harnessadapter.optimize).

The actor is the per-arm runner's, unchanged: GLM-5.3-Flash in native Codex,
one fresh workspace per question, its own Fireworks broker, the OS sandbox,
the adapter rendered into AGENTS.md (arm.actor). The grader is the vendored
official reward.py at tolerance 0 (strict). Every optimizer in the framework
(GEPA, SkillOpt, the session Evolvers) is scored by this same function.

Splits follow the prepared 133-question order (prepare_full.py), which sorts
every question by a seeded hash independent of answers:

    test  ordinals 1-50    the published 50-question sample (held out)
    val   ordinals 51-70   selection / Pareto set (20)
    train ordinals 71-133  rollout evidence for reflection (63)

What an optimizer may learn per outcome (see optimize.environment):
train feedback names the official answer after the prediction is recorded,
as the gold-feedback learner protocol does; val feedback is the verdict only;
test is never evaluated during optimization.

The answer key is read by this controller process only. Actors never see it:
it is outside every sandbox, and prediction text is recorded before grading.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import arm  # noqa: E402  (puts the repository on sys.path, imports acting/campaign/broker)
import data  # noqa: E402
import records  # noqa: E402

from harnessadapter.optimize.environment import Environment, Rollout, Task  # noqa: E402

DEFAULT_SPLITS = {"test": "1-50", "val": "51-70", "train": "71-133"}
# Outcomes the infrastructure decided, not the agent: arm.actor has already
# retried transport failures three times before reporting transport_lost.
INFRA_STATUSES = ("transport_lost", "launch_error")
INFRA_PAUSE = 60


def is_infra(status: str) -> bool:
    return status in INFRA_STATUSES or status.startswith("error:")


def ordinals(spec: str) -> list[int]:
    """'1-50' or '51-60,71-80' -> ordinals."""
    out = []
    for part in str(spec).split(","):
        low, _, high = part.strip().partition("-")
        out.extend(range(int(low), int(high or low) + 1))
    return out


def render_trace(rows: list) -> tuple[str, list]:
    """The public trajectory (no model reasoning) as text and as records."""
    lines, steps = [], []
    for row in rows:
        kind = row.get("type")
        if kind == "agent_message":
            text = str(row.get("text", ""))
            lines.append(f"[assistant] {text}")
            steps.append({"role": "assistant", "content": text})
        elif kind == "command_execution":
            command, output = str(row.get("command", "")), str(row.get("aggregated_output", ""))
            lines.append(f"[command] {command}\n[output] {output}")
            steps.append({"type": "tool_call", "cmd": command, "obs": output})
        elif kind == "mcp_tool_call":
            call = json.dumps({k: row.get(k) for k in ("server", "tool", "arguments")}, ensure_ascii=False)
            result = json.dumps(row.get("result"), ensure_ascii=False)[:2000]
            lines.append(f"[tool] {call}\n[result] {result}")
            steps.append({"type": "tool_call", "cmd": call, "obs": result})
        elif kind == "file_change":
            changes = json.dumps(row.get("changes"), ensure_ascii=False)[:1000]
            lines.append(f"[file change] {changes}")
            steps.append({"type": "tool_call", "cmd": "file_change", "obs": changes})
        elif kind == "environment_feedback":
            pass
    return "\n".join(lines), steps


class OfficeQAEnvironment(Environment):
    name = "officeqa-pro-glm53flash-codex"

    def __init__(self, prepared: str, run_root: str, *, key_env: str = "FIREWORKS_API_KEY",
                 splits: dict | None = None, concurrency: int = 4, actor_timeout: int = 1200,
                 max_tool_ops: int = 100, tolerance: float = 0.0):
        self.prepared = Path(prepared).resolve()
        self.run_root = Path(run_root).resolve()
        self.run_root.mkdir(parents=True, exist_ok=True)
        self._key = os.environ.get(key_env, "")
        if not self._key:
            raise ValueError(f"Set {key_env} to the Fireworks key the actors use")
        prepared_dir = data._prepared(self.prepared)
        self.corpus = prepared_dir / "agent/corpus"
        self.index = prepared_dir / "agent/corpus.sqlite"
        rows = data.load_tasks(self.prepared)
        self._rows = {o: r for o, r in enumerate(rows, start=1)}
        self._answers = {r["task_id"]: r["answer"] for r in data._legacy.load_jsonl(prepared_dir / "private/answers.jsonl")}
        self.splits = {**DEFAULT_SPLITS, **(splits or {})}
        self._tasks = {}
        self._ordinal = {}
        self._split_of = {}
        for split, spec in self.splits.items():
            self._tasks[split] = []
            for o in ordinals(spec):
                row = self._rows[o]
                if row["task_id"] in self._split_of:
                    raise ValueError(f"{row['task_id']} is in two splits")
                self._ordinal[row["task_id"]] = o
                self._split_of[row["task_id"]] = split
                self._tasks[split].append(Task(id=row["task_id"], split=split, prompt=row["question"],
                                               kind="officeqa"))
        self.concurrency = max(1, int(concurrency))
        self.actor_timeout = int(actor_timeout)
        self.max_tool_ops = int(max_tool_ops) if max_tool_ops else None
        self.tolerance = float(tolerance)
        self._score = records.grader()
        self._evidence = self.run_root / "no-evidence"      # no README: nothing bound into the context
        self._evidence.mkdir(exist_ok=True)
        self._lock = threading.Lock()
        self._calls = len(list((self.run_root / "evaluations").glob("*"))) if (self.run_root / "evaluations").is_dir() else 0

    def tasks(self, split):
        return list(self._tasks[split])

    def describe(self):
        import acting
        return {"name": self.name, "actor_model": acting.MODEL, "actor_effort": acting.EFFORT,
                "harness": "native codex (" + str(arm.acting.CODEX) + ")", "deployment": arm.DEPLOYMENT.name,
                "grader": "vendored official reward.py", "tolerance": self.tolerance,
                "actor_timeout": self.actor_timeout, "max_tool_ops": self.max_tool_ops,
                "concurrency": self.concurrency, "splits": self.splits, "prepared": str(self.prepared)}

    def _feedback(self, task: Task, answer: str, correct: bool, status: str) -> str:
        # What may be revealed follows the task's registered split, never the
        # split label on the Task object a caller passed in.
        split = self._split_of[task.id]
        if split == "test":
            return ""
        if is_infra(status):
            return ("The episode was lost to an infrastructure failure before the agent finished "
                    f"({status}); it says nothing about the instructions.")
        note = "" if status == "completed" else f" The run ended with status {status}."
        if correct:
            return "CORRECT." + note
        if split == "val":
            return "INCORRECT." + note
        return (f"INCORRECT. Your answer: {answer or '(none)'}. "
                f"Correct answer: {self._answers[task.id]}." + note)

    def evaluate(self, adapter, tasks, *, label):
        tasks = list(tasks)
        with self._lock:
            self._calls += 1
            number = self._calls
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:80]
        # Unique across processes that share a run root.
        run = self.run_root / "evaluations" / f"{number:05d}-{os.getpid()}-{uuid.uuid4().hex[:6]}-{safe}"
        run.mkdir(parents=True)
        (run / "call.json").write_text(json.dumps({"label": label, "adapter": str(adapter) if adapter else None,
                                                   "tasks": [t.id for t in tasks]}, indent=1) + "\n")
        adapter = Path(adapter).resolve() if adapter else None
        # A task may appear more than once in one batch (GEPA pads its last
        # minibatch of an epoch): every occurrence is its own episode.
        seen: dict[str, int] = {}
        places = []
        for task in tasks:
            seen[task.id] = seen.get(task.id, 0) + 1
            places.append(run if seen[task.id] == 1 else run / f"repeat{seen[task.id] - 1}")

        def episode(task: Task, where: Path) -> Rollout:
            ordinal = self._ordinal[task.id]
            row = self._rows[ordinal]
            try:
                summary = arm.actor(self._key, where, ordinal, {"task_id": row["task_id"], "question": row["question"]},
                                    adapter, self._evidence, self.corpus, self.index, self.actor_timeout, self.max_tool_ops)
            except Exception as exc:   # a crashed episode is a scored failure, not a crashed optimizer
                status = f"error: {type(exc).__name__}"
                return Rollout(task_id=task.id, split=task.split, score=0.0, status=status, infra=True,
                               feedback=self._feedback(task, "", False, status), trace=str(exc)[:2000])
            status = summary.get("status", "unknown")
            answer = summary.get("answer", "")
            correct = bool(answer) and bool(int(self._score(self._answers[task.id], answer, tolerance=self.tolerance)))
            base = where / "questions" / f"{ordinal:03d}"
            events = base / "records/events.jsonl"
            trace, steps = render_trace(records.public_trajectory(events)) if events.is_file() else ("", [])
            return Rollout(task_id=task.id, split=task.split, score=1.0 if correct else 0.0, output=answer,
                           status=status, infra=is_infra(status),
                           feedback=self._feedback(task, answer, correct, status),
                           trace=trace, steps=steps, record=str(base),
                           usage={"usage": summary.get("usage"), "requests": summary.get("requests"),
                                  "elapsed_seconds": summary.get("elapsed_seconds")})

        def one(task: Task, where: Path) -> Rollout:
            rollout = episode(task, where)
            if rollout.infra:
                # One more fresh attempt after a pause before the loss stands.
                time.sleep(INFRA_PAUSE)
                rollout = episode(task, where / "infra-retry")
            return rollout

        with ThreadPoolExecutor(max_workers=min(self.concurrency, len(tasks)) or 1) as pool:
            rollouts = list(pool.map(one, tasks, places))
        (run / "scores.json").write_text(json.dumps(
            [{"task_id": r.task_id, "split": r.split, "score": r.score, "status": r.status, "infra": r.infra}
             for r in rollouts], indent=1) + "\n")
        return rollouts


def make_environment(**config) -> OfficeQAEnvironment:
    return OfficeQAEnvironment(**config)
