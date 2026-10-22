"""Private labels are kept in the judge, outside learner-visible task rows."""
import json

from hermit.task import JudgeResult, RunOutput

LABELS = {
    "train-001": "positive",
    "train-002": "negative",
    "train-003": "neutral",
    "val-001": "positive",
    "val-002": "negative",
    "val-003": "neutral",
    "test-001": "positive",
    "test-002": "negative",
    "test-003": "neutral",
}


def score(task: dict, output: RunOutput) -> JudgeResult:
    expected = LABELS[task["id"]]
    try:
        value = json.loads(output.output)
    except (ValueError, TypeError):
        value = None
    correct = isinstance(value, dict) and value == {"label": expected}
    feedback = "Correct." if correct else (
        'Return only a JSON object with exactly one key, "label", whose value is '
        '"positive", "negative", or "neutral". No Markdown or explanation. '
        f'The reference label for this attempt is "{expected}".'
    )
    return JudgeResult(score=float(correct), feedback=feedback)
