"""Scores the offline echo task."""
from hermit.task import JudgeResult, RunOutput


def score(task: dict, output: RunOutput) -> JudgeResult:
    correct = output.output == task["expected"]
    return JudgeResult(score=float(correct), feedback="match" if correct else "mismatch")
