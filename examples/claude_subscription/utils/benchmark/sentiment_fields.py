"""A sentiment task as the acting agent reads it: {id, prompt}, the row each task was before it became a task directory
(its instruction is the prompt), so the prompt and the records stay what they were."""


def sentiment_task(task: dict) -> dict:
    if "instruction" not in task:                       # already a row of that shape
        return dict(task)
    return {"id": task["id"], "prompt": task["instruction"]}
