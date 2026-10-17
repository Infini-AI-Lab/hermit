"""A deterministic teaching agent that interprets one adapter rule."""
from pathlib import Path
from harnessadapter.adapter.loader import Adapter
from harnessadapter.task import RunOutput


def run(task: dict, adapter: Path | None) -> RunOutput:
    context = Adapter(adapter).context() if adapter is not None else ""
    answer = task["prompt"]
    if "Output format: uppercase." in context:
        answer = answer.upper()
    return RunOutput(output=answer, cost_usd=0.0, tokens=0, messages=[
        {"role": "system", "content": context},
        {"role": "user", "content": task["prompt"]},
        {"role": "assistant", "content": answer},
    ])
