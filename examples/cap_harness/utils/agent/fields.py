"""A task of the cap-harness engine as the session's task functions, the simulator service and the judge read it:
{id, (benchmark), suite, task_id, the seeds, (max_programs), and whatever a method's own rows add}. A task directory
(a benchmark's utils/benchmark/prepare.py, through ../benchmark/tasks.py) has every field but its id under task.toml's
[metadata], in the row's order; this puts them back, so the prompts, the service's rules and the records read the row
as it was written. Every config of a benchmark on this engine names it as its data.view."""
from __future__ import annotations

from pathlib import Path


def task_row(task: dict) -> dict:
    if "instruction" not in task:                       # already a row of that shape
        return dict(task)
    return {"id": task["id"], **(task.get("metadata") or {})}


def task_rows(tasks: Path) -> dict[str, dict]:
    """Every task's row by id: from task directories, or from a tasks.jsonl."""
    from hermit.task import Benchmark
    return {tid: t.given() for tid, t in Benchmark.load(Path(tasks), view=task_row).tasks.items()}
