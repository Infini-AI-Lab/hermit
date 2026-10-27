"""A deterministic teaching agent that interprets one adapter rule."""
from pathlib import Path
from hermit import Session
from hermit.task import RunOutput


def run(task: dict, adapter: Path | None) -> RunOutput:
    context = Session(adapter).context() if adapter is not None else ""
    answer = task["prompt"]
    if "Output format: uppercase." in context:
        answer = answer.upper()
    return RunOutput(output=answer, cost_usd=0.0, tokens=0, messages=[
        {"role": "system", "content": context},
        {"role": "user", "content": task["prompt"]},
        {"role": "assistant", "content": answer},
    ])
