"""Environments: how an adapter is scored.

An Environment runs the acting harness with a given adapter on a set of tasks
and scores the outcome. Optimizer Evolvers (GEPA, SkillOpt, your own plugin)
search with one, and `harnessadapter evaluate` compares finished adapters on
its held-out split with the same one, so every Evolver is judged alike.

The environment owns the benchmark's secrets. It decides what a result may
reveal from the task's registered split, never from a caller's label:

- train: the score, the agent's trace and the grader's feedback (which may
  name the reference answer, as the GEPA and SkillOpt feedback functions do);
- val:   the score and the trace, never the reference;
- test:  the score only. Optimizers never see it.

    harnessadapter evaluate --environment tasks/OfficeQA/host/environment.py:make_environment \\
      --environment-config env.yaml --split test \\
      --adapter w0=prompt-only --adapter gepa=.demo/evolution/evolve-gepa-XXXX/adapter --output runs/test
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from typing import Sequence

import yaml

from harnessadapter.paths import load_object, resolve_architecture

SPLITS = ("train", "val", "test")
INFRA_RERUNS = 2      # extra rounds for episodes the infrastructure lost
INFRA_PAUSE = 60      # seconds before each round


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

    score is in [0, 1], higher is better. feedback is what the grader tells an
    optimizer, already filtered by split. trace is the agent's public
    trajectory as text; steps is the same trajectory as records
    ({"type": "tool_call", "cmd", "obs"} or {"role", "content"}) for
    optimizers that format it themselves. record is where the environment
    keeps the episode (never shown to a plugin). infra marks an episode lost to
    the infrastructure (provider outage, transport failure) rather than decided
    by the agent; such episodes are re-run.
    """
    task_id: str
    split: str
    score: float
    output: str = ""
    status: str = "completed"
    feedback: str = ""
    trace: str = ""
    steps: list = field(default_factory=list)
    record: str | None = None
    infra: bool = False


class Environment(ABC):
    """Tasks plus an evaluator for one acting harness and benchmark."""

    name = "environment"

    @abstractmethod
    def tasks(self, split: str) -> list[Task]:
        """The ordered tasks of one split."""

    @abstractmethod
    def evaluate(self, adapter: Path | None, tasks: Sequence[Task], *, label: str) -> list[Rollout]:
        """Run every task with the adapter deployed (None: no adapter) and
        return one Rollout per task, in order. A failed task is a Rollout with
        score 0 and its status, not an exception. label names the call so its
        records can be found again."""

    def describe(self) -> dict:
        """Settings recorded next to every result (model, harness, grader,
        budgets). Must not contain secrets."""
        return {"name": self.name}


def check_rollouts(tasks: Sequence[Task], rollouts: Sequence[Rollout]) -> list[Rollout]:
    """The contract every caller relies on: one in-range result per task."""
    rollouts = list(rollouts)
    if [r.task_id for r in rollouts] != [t.id for t in tasks]:
        raise ValueError("Environment returned rollouts for different tasks or in a different order")
    for r in rollouts:
        if not 0.0 <= float(r.score) <= 1.0:
            raise ValueError(f"Score out of range for {r.task_id}: {r.score}")
    return rollouts


def split_sizes(environment: Environment) -> dict:
    """Split sizes, refusing any task that appears in two splits."""
    seen: dict[str, str] = {}
    sizes = {}
    for split in SPLITS:
        tasks = environment.tasks(split)
        sizes[split] = len(tasks)
        for task in tasks:
            if task.id in seen:
                raise ValueError(f"Task {task.id} is in both {seen[task.id]} and {split}")
            seen[task.id] = split
    return sizes


def run_tasks(environment: Environment, adapter, tasks: Sequence[Task], label: str, *,
              reruns: int = INFRA_RERUNS, pause: float = INFRA_PAUSE) -> tuple[list[Rollout], int]:
    """Evaluate, then re-run the episodes the infrastructure lost up to
    `reruns` more times, so an outage does not decide a score. Returns the
    rollouts and the number of extra episodes run."""
    tasks = list(tasks)
    rollouts = check_rollouts(tasks, environment.evaluate(adapter, tasks, label=label))
    extra = 0
    for round_number in range(1, reruns + 1):
        lost = [i for i, r in enumerate(rollouts) if r.infra]
        if not lost:
            break
        time.sleep(pause)
        again = check_rollouts([tasks[i] for i in lost], environment.evaluate(
            adapter, [tasks[i] for i in lost], label=f"{label}/infra-rerun-{round_number}"))
        extra += len(again)
        for i, rollout in zip(lost, again):
            rollouts[i] = rollout
    return rollouts, extra


def load_environment(spec: str, config: Path | None = None) -> Environment:
    """module:factory or path/to/file.py:factory, called with the YAML/JSON
    mapping in config as keyword arguments."""
    settings = yaml.safe_load(Path(config).read_text()) if config else {}
    if not isinstance(settings or {}, dict):
        raise ValueError(f"{config} must contain a mapping")
    environment = load_object(spec)(**(settings or {}))
    if not isinstance(environment, Environment):
        raise ValueError(f"{spec} did not return a harnessadapter.environment.Environment")
    return environment


def evaluate_adapters(environment: Environment, adapters: dict[str, Path | None], split: str, *,
                      output: Path, repeats: int = 1, reruns: int = INFRA_RERUNS,
                      pause: float = INFRA_PAUSE) -> dict:
    """Score several adapters on the same split (normally test) with the same
    environment: the comparison every Evolver is judged by. Episodes still
    lost to the infrastructure after re-runs are left out of the adapter's
    mean and listed; paired_mean is over the tasks every adapter and repeat
    scored, the like-for-like comparison. Saved as <output>/<split>-scores.json
    after every adapter."""
    tasks = environment.tasks(split)
    results = {}
    for name, adapter in adapters.items():
        runs = []
        for repeat in range(repeats):
            rollouts, _ = run_tasks(environment, adapter, tasks, f"{split}/{name}/r{repeat + 1}",
                                    reruns=reruns, pause=pause)
            valid = [r for r in rollouts if not r.infra]
            runs.append({"mean": sum(r.score for r in valid) / len(valid) if valid else None,
                         "scored": len(valid), "infra_lost": [r.task_id for r in rollouts if r.infra],
                         "scores": {r.task_id: r.score for r in rollouts},
                         "statuses": {r.task_id: r.status for r in rollouts}})
        means = [r["mean"] for r in runs if r["mean"] is not None]
        results[name] = {"adapter": str(adapter) if adapter else None, "runs": runs,
                         "mean": sum(means) / len(means) if means else None}
        common = [t.id for t in tasks
                  if all(t.id not in run["infra_lost"] for r in results.values() for run in r["runs"])]
        for r in results.values():
            per_run = [sum(run["scores"][t] for t in common) / len(common) for run in r["runs"]] if common else []
            r["paired_mean"] = sum(per_run) / len(per_run) if per_run else None
        path = Path(output) / f"{split}-scores.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"split": split, "tasks": [t.id for t in tasks], "paired_tasks": len(common),
                                    "environment": environment.describe(), "results": results},
                                   ensure_ascii=False, indent=2) + "\n")
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(prog="harnessadapter evaluate", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--environment", required=True, help="module:factory or file.py:factory returning an Environment")
    parser.add_argument("--environment-config", type=Path, help="YAML/JSON mapping passed to the factory")
    parser.add_argument("--split", default="test", choices=SPLITS)
    parser.add_argument("--adapter", action="append", required=True, metavar="NAME=PATH",
                        help="An adapter to score (repeatable); an empty PATH (NAME=) scores the harness without one")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    adapters = {}
    for item in args.adapter:
        name, sep, path = item.partition("=")
        if not sep or not name.strip():
            parser.error("--adapter takes NAME=PATH")
        adapters[name.strip()] = resolve_architecture(path).resolve() if path.strip() else None
    environment = load_environment(args.environment, args.environment_config)
    results = evaluate_adapters(environment, adapters, args.split, output=args.output, repeats=args.repeats)
    print(json.dumps({name: {"mean": r["mean"], "paired_mean": r["paired_mean"],
                             "runs": [{"mean": run["mean"], "scored": run["scored"], "infra_lost": len(run["infra_lost"])}
                                      for run in r["runs"]]} for name, r in results.items()}, indent=2))
    return 0
