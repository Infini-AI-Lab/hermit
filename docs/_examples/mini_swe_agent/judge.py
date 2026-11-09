"""Private answers are kept in the judge, outside learner-visible task rows."""
import json

from hermit.task import JudgeResult, RunOutput

ANSWERS = {
    "train-001": 195,
    "train-002": 170,
    "train-003": 4,
    "val-001": 345,
    "val-002": 194,
    "val-003": 6,
    "test-001": 80,
    "test-002": 448,
    "test-003": 5,
}


def score(task: dict, output: RunOutput) -> JudgeResult:
    expected = ANSWERS[task["id"]]
    try:
        value = json.loads(output.output)
    except (ValueError, TypeError):
        value = None
    correct = isinstance(value, dict) and value == {"answer": expected}
    feedback = "Correct." if correct else (
        'Return only a JSON object with exactly one key, "answer", whose value is an integer, for example '
        '{"answer": 12}. No Markdown, units or explanation. '
        f"The reference answer for this attempt is {expected}."
    )
    return JudgeResult(score=float(correct), feedback=feedback)
