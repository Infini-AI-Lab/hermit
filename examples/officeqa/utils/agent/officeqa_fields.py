"""An OfficeQA task as the agents read it: {id, question, category, source_files, source_docs}, the row SkillOpt's
OfficeQA loader gives. A task directory (utils/benchmark/prepare.py) has the question as its instruction and the rest
under task.toml's [metadata]; this puts them back, so the agent, its prompt and an adapter's context tool see exactly
the row they saw when the task set was a tasks.jsonl."""
from __future__ import annotations


def officeqa_task(task: dict) -> dict:
    if "instruction" not in task:                       # already a row of that shape
        return dict(task)
    meta = task.get("metadata") or {}
    return {"id": task["id"], "question": task["instruction"], "category": meta["category"],
            "source_files": list(meta["source_files"]), "source_docs": list(meta["source_docs"])}
