"""A CL-Bench question as the agent and its learners read it: {id, order, position, question_id, block}, the row each
task was in tasks.jsonl. A task directory (utils/benchmark/prepare.py) has every field but its id under task.toml's
[metadata], in the row's order; this puts them back, so the host, its records and the learning material are what they
were."""


def clbench_task(task: dict) -> dict:
    if "instruction" not in task:                       # already a row of that shape
        return dict(task)
    return {"id": task["id"], **(task.get("metadata") or {})}
