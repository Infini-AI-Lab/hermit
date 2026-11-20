"""A task of ASPIRE's engine as the actor, the simulator service and the judge read it: {id, (benchmark), suite,
task_id, the seeds, (role, max_versions, search, findings)}, the row each task was in tasks.jsonl. A task directory
(utils/benchmark/prepare.py, and LIBERO's) has every field but its id under task.toml's [metadata], in the row's
order; this puts them back, so the prompts, the service's rules and the records are what they were. LIBERO's tasks
are read so too (their configs' data.view)."""
from __future__ import annotations

from pathlib import Path


def aspire_task(task: dict) -> dict:
    if "instruction" not in task:                       # already a row of that shape
        return dict(task)
    return {"id": task["id"], **(task.get("metadata") or {})}


def task_rows(tasks: Path) -> dict[str, dict]:
    """Every task's row by id: from the task directories prepare.py writes, or from a tasks.jsonl."""
    from hermit.task import Benchmark
    return {tid: t.given() for tid, t in Benchmark.load(Path(tasks), view=aspire_task).tasks.items()}
