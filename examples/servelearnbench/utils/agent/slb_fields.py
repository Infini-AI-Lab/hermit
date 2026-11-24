"""A ServeLearnBench episode as the agent and its learners read it: {id, scenario, phase, timestamp, (block),
instruction}, the row each task was in tasks.jsonl. A task directory (utils/benchmark/prepare.py) has the stamped
instruction as instruction.md and the other fields under task.toml's [metadata], in the row's order; this puts them
back, so the instruction the benchmark's loop is given and the records are what they were."""


def slb_task(task: dict) -> dict:
    if "metadata" not in task:                          # already a row of that shape
        return dict(task)
    return {"id": task["id"], **(task.get("metadata") or {}), "instruction": task["instruction"]}
