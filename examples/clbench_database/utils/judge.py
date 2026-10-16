"""CL-Bench database judge: the official task's own verdict (accuracy scoring: a correct answer is 1)."""


def score(task: dict, output) -> dict:
    extra = output.extra or {}
    return {"score": 1.0 if extra.get("success") else 0.0, "feedback": extra.get("verdict", ""),
            "queries_used": extra.get("queries_used")}
