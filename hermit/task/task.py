"""One task: what the agent is given and what only the judge reads.

    task = Task.from_row({"id": "q1", "question": "...", "_private": {...}})
    task.public      # {"id": "q1", "question": "..."}: what the acting agent gets, and what a run records as its input
    task.private     # what the judge alone may read (answers, tests, held-out data); never given to the agent

A row of tasks.jsonl is all public: a task set keeps its answers apart (the judge reads them from where the agent's
sandbox cannot see, `.private/` beside the data), so `private` is empty for those rows. The split is a type, not a
convention: what an agent is handed is `task.public`, which has no field for the judge's own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

PRIVATE = "_private"            # a row's key for what only the judge reads (a task directory's tests are private too)


@dataclass(frozen=True)
class Task:
    id: str
    public: Mapping[str, Any]
    private: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if PRIVATE in self.public:
            raise ValueError(f"task {self.id}: its public half holds {PRIVATE!r}, the judge's own")
        object.__setattr__(self, "public", MappingProxyType(dict(self.public)))
        object.__setattr__(self, "private", MappingProxyType(dict(self.private)))

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Task":
        """A row with an `id` (or `task_id`); its `_private` mapping, when it has one, is the judge's own."""
        row = dict(row)
        private = row.pop(PRIVATE, None) or {}
        tid = str(row.get("id", row.get("task_id")))
        if tid in ("None", ""):
            raise ValueError("every task needs an id")
        row.setdefault("id", tid)
        return cls(id=tid, public=row, private=private)

    def given(self) -> dict:
        """What the acting agent is handed: a copy of the public half."""
        return dict(self.public)

    def row(self) -> dict:
        """The task as a row again, what a judge is called with: the public half and, if any, the private one under
        `_private`."""
        return {**self.public, PRIVATE: dict(self.private)} if self.private else dict(self.public)


def as_task(value) -> Task:
    """A Task as it is, or one made from a row (what callers held before tasks were typed)."""
    return value if isinstance(value, Task) else Task.from_row(value)
