"""MATH-500's judge, on the host: the last `\\boxed{}` of the agent's reply against the reference
(examples/math_grading/utils/grade.py: MathArena's parsing, Math-Verify's equivalence). 1 or 0; the feedback names the
reference."""
from __future__ import annotations

from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "math_grading" / "utils"))
import grade  # noqa: E402

PRIVATE = HERE / ".private"            # prepare.py's references
_answers: dict[str, str] | None = None


def score(task: dict, output) -> dict:
    global _answers
    if _answers is None:
        _answers = grade.load_answers(PRIVATE)
    return grade.score(task, output, _answers)
