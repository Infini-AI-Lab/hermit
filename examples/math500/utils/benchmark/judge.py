"""MATH-500's judge, on the host: the last `\boxed{}` of the agent's reply against the reference (examples/math_grading:
Math-Verify's equivalence, MathArena's parsing). 1 or 0; the feedback names the reference."""
from __future__ import annotations

from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "math_grading"))
import grade  # noqa: E402

ANSWERS = grade.load_answers(HERE / ".private")


def score(task: dict, output) -> dict:
    return grade.score(task, output, ANSWERS)
