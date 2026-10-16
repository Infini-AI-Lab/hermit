"""SkillOpt's OfficeQA scorer (skillopt/envs/officeqa/evaluator.py): exact match after its normalisation, no numeric
tolerance; token F1 is recorded as `f1`. Feedback is SkillOpt's fail_reason, which names the reference answer."""
from __future__ import annotations

import json
from pathlib import Path

from skillopt.envs.officeqa.evaluator import evaluate

HERE = Path(__file__).resolve().parent
_answers_file = Path(json.loads((HERE / "data.json").read_text())["answers"])
ANSWERS = {r["id"]: r["answer"] for r in map(json.loads, _answers_file.read_text().splitlines()) if r}


def score(task: dict, output) -> dict:
    out = output.output or {}
    gold = ANSWERS[task["id"]]
    if not out.get("answer"):
        return {"score": 0.0, "feedback": out.get("fail_reason") or "no answer", "f1": 0.0, "answered": False}
    result = evaluate(out["answer"], gold)
    feedback = "correct" if result["em"] else f"predicted '{result['predicted_answer']}' but expected '{gold}'"
    return {"score": result["em"], "feedback": feedback, "f1": result["f1"], "answered": True}
