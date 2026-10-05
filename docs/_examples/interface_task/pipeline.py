"""Offline pipeline stub for exercising the interface; no model is called."""
from pathlib import Path

from harnessadapter.adapter.loader import Adapter
from harnessadapter.task import RunOutput


def run(task: dict, adapter: Path | None) -> RunOutput:
    context = Adapter(adapter).context() if adapter is not None else ""
    # Replace this echo with the acting system that consumes context and the task.
    answer = task["prompt"]
    return RunOutput(
        output=answer,
        messages=[
            {"role": "system", "content": context},
            {"role": "user", "content": task["prompt"]},
            {"role": "assistant", "content": answer},
        ],
        cost_usd=0.0,
        tokens=0,
    )
