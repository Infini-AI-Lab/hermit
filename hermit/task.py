"""The evolving task: data and splits, the user's pipeline (how one task is run) and judge (how it is scored).

    def run(task: dict, adapter: Path | None) -> RunOutput        # pipeline; adapter=None = no adapter at all
    def score(task: dict, output: RunOutput) -> JudgeResult | dict  # judge
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Callable


@dataclass
class RunOutput:
    output: Any                          # what the judge scores
    log: Path | None = None              # the host's original log (the trajectory), a file or a directory
    cost_usd: float | None = None        # None = unknown
    wall_s: float = 0.0
    api_speed: dict | None = None        # for example output tokens per second
    messages: list | None = None         # optional normalised message list of the trajectory
    tokens: int | None = None            # input + output tokens, if known
    extra: dict = field(default_factory=dict)


@dataclass
class JudgeResult:
    score: float
    feedback: str = ""
    extra: dict = field(default_factory=dict)


def load_callable(spec: str, base: Path) -> Callable:
    """'file.py:function' relative to `base`, or 'package.module:function'."""
    module_part, _, name = spec.partition(":")
    if not name:
        raise ValueError(f"Expected 'file.py:function' or 'module:function', got {spec!r}")
    if module_part.endswith(".py"):
        path = (Path(base) / module_part).resolve()
        mod_name = f"_task_{path.stem}_{abs(hash(path))}"
        mod_spec = importlib.util.spec_from_file_location(mod_name, path)
        module = importlib.util.module_from_spec(mod_spec)
        sys.modules[mod_name] = module
        if str(path.parent) not in sys.path:
            sys.path.insert(0, str(path.parent))
        mod_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(module_part)
    fn = getattr(module, name, None)
    if not callable(fn):
        raise ValueError(f"{spec} is not callable")
    return fn


def load_pipeline(spec: str, base: Path, params: dict | None = None) -> Callable:
    """A `run(task, adapter)` callable with `agent.params` and the config directory bound when its signature asks
    for them (keyword parameters named `params` and `base`); `hermit.agents.FunctionAgent` is the Agent form."""
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
