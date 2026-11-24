"""An OfficeQA task as the agents read it: {id, question, category, source_files, source_docs}, the row SkillOpt's
OfficeQA loader gives. A task directory (utils/benchmark/prepare.py) has the question as its instruction and the rest
under task.toml's [metadata]; this puts them back, so the agent, its prompt and an adapter's context tool see exactly
the row they saw when the task set was a tasks.jsonl."""
from __future__ import annotations

from pathlib import Path


def officeqa_task(task: dict) -> dict:
    if "instruction" not in task:                       # already a row of that shape
        return dict(task)
    meta = task.get("metadata") or {}
    return {"id": task["id"], "question": task["instruction"], "category": meta["category"],
            "source_files": list(meta["source_files"]), "source_docs": list(meta["source_docs"])}


def question_rows(tasks: Path, splits: Path) -> list[dict]:
    """Every question's row as the agents read it, in the splits' order (train, validation, test; a split file that
    is not there is skipped): from the task directories of utils/benchmark/prepare.py, or from a tasks.jsonl."""
    import json
    from hermit.task import read_ids
    from hermit.task.directory import tasks_of
    if not Path(tasks).is_dir():
        return [json.loads(line) for line in Path(tasks).read_text(encoding="utf-8").splitlines() if line.strip()]
    found = {tid: officeqa_task(task.given()) for tid, task in tasks_of(Path(tasks)).items()}
    order = [i for name in ("train", "val", "test") if (Path(splits) / f"{name}.txt").is_file()
             for i in read_ids(Path(splits) / f"{name}.txt")]
    return [found[i] for i in dict.fromkeys([*order, *sorted(found)]) if i in found]
