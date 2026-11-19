"""A task set and its splits, as the run config names them (`data.tasks`, `data.train`, `data.val`, `data.test`).

    bench = Benchmark.from_config(config)
    bench.split("test")            # [Task, ...] in the split file's order
    bench.tasks["q1"]              # one Task by id
"""
from __future__ import annotations

from pathlib import Path

from hermit.task.data import TaskData
from hermit.task.task import Task


class Benchmark:
    def __init__(self, data: TaskData):
        self.data = data
        self.tasks: dict[str, Task] = {tid: Task.from_row(row) for tid, row in data.tasks.items()}
        self.splits = data.splits

    @classmethod
    def load(cls, tasks: Path, train: Path | None = None, val: Path | None = None,
             test: Path | None = None) -> "Benchmark":
        return cls(TaskData(tasks, train, val, test))

    @classmethod
    def from_config(cls, config) -> "Benchmark":
        d = config.data["data"]
        return cls.load(config.path(d["tasks"]), config.path(d["train"]), config.path(d.get("val")),
                        config.path(d.get("test")))

    def split(self, name: str) -> list[Task]:
        return [self.tasks[i] for i in self.splits[name]]

    def overlaps(self) -> list[str]:
        return self.data.overlaps()
