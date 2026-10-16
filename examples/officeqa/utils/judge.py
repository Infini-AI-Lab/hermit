"""OfficeQA judge: the official reward.score_answer, strict (tolerance 0). Feedback includes the reference answer
unless OFFICEQA_FEEDBACK=score."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
PREPARED = Path((HERE / "data_root.txt").read_text().strip())
_spec = importlib.util.spec_from_file_location("officeqa_reward", PREPARED / "private" / "reward.py")
reward = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reward)
ANSWERS = {r["task_id"]: r["answer"] for r in
           (json.loads(l) for l in (PREPARED / "private" / "answers.jsonl").read_text().splitlines() if l.strip())}


def score(task: dict, output) -> dict:
    gold = ANSWERS[task["id"]]
    text = output.output or ""
    got = reward.extract_final_answer(text)
    value = reward.score_answer(gold, text, 0.0)
    if os.environ.get("OFFICEQA_FEEDBACK", "gold") == "score":
        feedback = "correct" if value else "incorrect"
    else:
        feedback = ("correct" if value else "incorrect") + f"; the agent answered {got!r}; the reference answer is {gold!r}"
    return {"score": value, "feedback": feedback, "answered": bool(got)}
