"""A task set and its splits, as the run config names them (`data.tasks`, `data.train`, `data.val`, `data.test`).

    bench = Benchmark.from_config(config)
    bench.split("test")            # [Task, ...] in the split file's order
    bench.tasks["q1"]              # one Task by id

`data.tasks` is a `tasks.jsonl` (one row per task, every field public) or a directory of task directories in
Harbor's format (hermit.task.directory: each task's instruction and settings public, its tests private);
`data.rollouts: k` makes k rows of each directory task (`<task>#1..k`).
"""
from __future__ import annotations

from pathlib import Path

from hermit.task.data import TaskData, read_ids
from hermit.task.directory import tasks_of
from hermit.task.task import Task

SPLITS = ("train", "val", "test")


class Benchmark:
    def __init__(self, tasks: dict[str, Task], splits: dict[str, list[str]]):
        self.tasks = tasks
        self.splits = {name: list(splits.get(name) or []) for name in SPLITS}
        for name, ids in self.splits.items():
            missing = [i for i in ids if i not in self.tasks]
            if missing:
                raise ValueError(f"{name} split names unknown tasks: {missing[:5]}")

    @classmethod
    def load(cls, tasks: Path, train: Path | None = None, val: Path | None = None, test: Path | None = None, *,
             rollouts: int = 1) -> "Benchmark":
        splits = {"train": read_ids(train), "val": read_ids(val), "test": read_ids(test)}
        if Path(tasks).is_dir():
            return cls(tasks_of(Path(tasks), rollouts=int(rollouts)), splits)
        data = TaskData(tasks, None, None, None)
        return cls({tid: Task.from_row(row) for tid, row in data.tasks.items()}, splits)

    @classmethod
    def from_config(cls, config) -> "Benchmark":
        d = config.data["data"]
        return cls.load(config.path(d["tasks"]), config.path(d.get("train")), config.path(d.get("val")),
                        config.path(d.get("test")), rollouts=int(d.get("rollouts") or 1))

    def split(self, name: str) -> list[Task]:
        return [self.tasks[i] for i in self.splits[name]]

    def overlaps(self) -> list[str]:
        out = []
        for i, a in enumerate(SPLITS):
            for b in SPLITS[i + 1:]:
                common = set(self.splits[a]) & set(self.splits[b])
                if common:
                    out.append(f"{a} and {b} share {len(common)} tasks (e.g. {sorted(common)[:3]})")
        return out
