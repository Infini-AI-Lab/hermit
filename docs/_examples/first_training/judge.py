"""Compute the target privately rather than placing answers in task rows."""
from harnessadapter.task import JudgeResult, RunOutput


def score(task: dict, output: RunOutput) -> JudgeResult:
    correct = output.output == task["prompt"].upper()
    return JudgeResult(score=float(correct), feedback=(
        "Correct output format." if correct else "Output format: uppercase."
    ))
