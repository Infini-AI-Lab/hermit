"""The evaluation contract that optimizer Evolvers share.

An Environment runs the acting harness with a given adapter on a set of tasks
and scores the outcome. It is the piece the session Evolvers never needed (they
read recorded LearningMaterial) and that search-based optimizers such as GEPA
and SkillOpt cannot work without: they propose a candidate, roll it out, and
keep it only if the scores say so.

The environment owns the benchmark's secrets. It decides what an optimizer may
learn from each outcome, per split:

- train: the score, the agent's trace and the grader's feedback (which may
  name the reference answer, as both papers' feedback functions do);
- val:   the score and the trace, never the reference;
- test:  the score only, and optimizers never evaluate on it.

Environments that have no hidden reference simply return their feedback.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

SPLITS = ("train", "val", "test")


@dataclass(frozen=True)
class Task:
    """One benchmark item as an optimizer may see it: never its answer."""
    id: str
    split: str
    prompt: str
    kind: str = ""

    def __post_init__(self):
        if self.split not in SPLITS:
            raise ValueError(f"Unknown split {self.split!r}")


@dataclass
class Rollout:
    """The outcome of one task under one adapter.

    score is in [0, 1], higher is better. feedback is what the grader tells
    the optimizer (already filtered by split). trace is the agent's public
    trajectory as text; steps is the same trajectory as records
    ({"type": "tool_call", "cmd", "obs"} or {"role", "content"}) for
    optimizers that format it themselves. infra marks an episode lost to the
    infrastructure (provider outage, transport failure) rather than decided by
    the agent: it scores 0 for an optimizer, which cannot re-run it, and
    evaluate_adapters leaves it out of means and reports it separately.
    """
    task_id: str
    split: str
    score: float
    output: str = ""
    status: str = "completed"
    feedback: str = ""
    trace: str = ""
    steps: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    record: str | None = None
    infra: bool = False

    def to_json(self) -> dict:
        return asdict(self)

    def public(self) -> dict:
        """What may be stored in an optimizer's own state files."""
        return {"task_id": self.task_id, "split": self.split, "score": self.score,
                "output": self.output, "status": self.status, "infra": self.infra}


class Environment(ABC):
    """Tasks plus an evaluator for one acting harness and benchmark."""

    name = "environment"

    @abstractmethod
    def tasks(self, split: str) -> list[Task]:
        """The ordered tasks of one split."""

    @abstractmethod
    def evaluate(self, adapter: Path | None, tasks: Sequence[Task], *, label: str) -> list[Rollout]:
        """Run every task with the adapter deployed (None: no adapter) and
        return one Rollout per task, in order. One failed task is a Rollout
        with score 0 and its status, not an exception. label names the call
        (e.g. "gepa/iter-003/minibatch") so records can be found again."""

    def describe(self) -> dict:
        """Settings worth recording next to every result (model, harness,
        grader, budgets). Must not contain secrets."""
        return {"name": self.name}

    def task(self, task_id: str) -> Task:
        """The registered task for an id. Environments decide what a result
        may reveal from this record, never from a Task object a caller built."""
        for split in SPLITS:
            for task in self.tasks(split):
                if task.id == task_id:
                    return task
        raise KeyError(task_id)


def check_rollouts(tasks: Sequence[Task], rollouts: Sequence[Rollout]) -> list[Rollout]:
    """The contract every optimizer relies on: one in-range result per task."""
    rollouts = list(rollouts)
    if [r.task_id for r in rollouts] != [t.id for t in tasks]:
        raise ValueError("Environment returned rollouts for different tasks or in a different order")
    for r in rollouts:
        if not 0.0 <= float(r.score) <= 1.0:
            raise ValueError(f"Score out of range for {r.task_id}: {r.score}")
    return rollouts


def disjoint_splits(environment: Environment, splits: Sequence[str] = SPLITS) -> dict:
    """Split sizes, refusing any task that appears in two splits."""
    seen: dict[str, str] = {}
    sizes = {}
    for split in splits:
        tasks = environment.tasks(split)
        sizes[split] = len(tasks)
        for task in tasks:
            if task.id in seen:
                raise ValueError(f"Task {task.id} is in both {seen[task.id]} and {split}")
            seen[task.id] = split
    return sizes
