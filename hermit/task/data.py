"""A task set's data: its rows (`tasks.jsonl`) and splits, the judge's verdict and the records a run keeps.

    def run(task: dict, adapter: Path | None) -> RunOutput        # pipeline; adapter=None = no adapter at all
    def score(task: dict, output: RunOutput) -> JudgeResult | dict  # judge
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Callable

from hermit.common import RunOutput, load_callable


@dataclass
class JudgeResult:
    score: float
    feedback: str = ""
    extra: dict = field(default_factory=dict)


def load_pipeline(spec: str, base: Path, params: dict | None = None) -> Callable:
    """A `run(task, adapter)` callable with `agent.params` and the config directory bound when its signature asks
    for them (keyword parameters named `params` and `base`); `hermit.agent.acting.FunctionAgent` is the Agent form."""
    import functools
    import inspect
    fn = load_callable(spec, base)
    wanted = inspect.signature(fn).parameters
    extra = {}
    if "params" in wanted:
        extra["params"] = dict(params or {})
    if "base" in wanted:
        extra["base"] = Path(base)
    return functools.partial(fn, **extra) if extra else fn


def read_ids(path: Path | None) -> list[str]:
    if path is None:
        return []
    return [line.strip() for line in Path(path).read_text().splitlines() if line.strip() and not line.startswith("#")]


class TaskData:
    def __init__(self, tasks: Path, train: Path | None, val: Path | None, test: Path | None):
        self.tasks: dict[str, dict] = {}
        for line in Path(tasks).read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                tid = str(row.get("id", row.get("task_id")))
                if tid in ("None", "") or tid in self.tasks:
                    raise ValueError(f"Every task needs a unique id (problem at {tid!r})")
                row.setdefault("id", tid)
                self.tasks[tid] = row
        self.splits = {"train": read_ids(train), "val": read_ids(val), "test": read_ids(test)}
        for name, ids in self.splits.items():
            missing = [i for i in ids if i not in self.tasks]
            if missing:
                raise ValueError(f"{name} split names unknown tasks: {missing[:5]}")

    def overlaps(self) -> list[str]:
        out = []
        names = list(self.splits)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                common = set(self.splits[a]) & set(self.splits[b])
                if common:
                    out.append(f"{a} and {b} share {len(common)} tasks (e.g. {sorted(common)[:3]})")
        return out

    def rows(self, split: str) -> list[dict]:
        return [self.tasks[i] for i in self.splits[split]]


def normalise_judge(value) -> JudgeResult:
    if isinstance(value, JudgeResult):
        return value
    if isinstance(value, dict):
        rest = {k: v for k, v in value.items() if k not in ("score", "feedback")}
        return JudgeResult(float(value["score"]), str(value.get("feedback", "")), rest)
    return JudgeResult(float(value))


def output_record(output: RunOutput) -> dict:
    record = asdict(output)
    record["log"] = str(output.log) if output.log else None
    return record
