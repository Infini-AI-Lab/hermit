#!/usr/bin/env python3
"""The OfficeQA paper's scoring inside the task container (prepare.py copies it to every task's tests/judge.py).

The response is the agent's final message. For Codex the event stream must show the final turn completing with a reply;
Hermit can also write unfinished progress to last_message.txt. Other agents provide their reply in last_message.txt.
It is scored as the paper scores it, with the
OfficeQA repository's reward.py at the paper's commit (tests/reward.py): its `extract_final_answer` takes the first
<FINAL_ANSWER>...</FINAL_ANSWER> (the whole response without one), its `score_answer` matches that answer to the
expected one within the allowable relative error. The reward is the 0.0% threshold's, the paper's headline;
reward.json adds 0.1%, 1% and 5%. An answer reward.py cannot read (it raises ValueError: an empty tag, no number it
can parse) scores 0. The feedback a learning method reads is the verdict, never the expected answer. A scorer that
fails otherwise writes no reward (exit 1): the run records agent_error (verifier_failed) and scores it 0, as Harbor
grades a verifier that left no reward."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

# tests/reward.py by its path, not by name: the agent's phase may have left a `reward` package in /tests, which Harbor
# copies the tests over without clearing
_spec = importlib.util.spec_from_file_location("officeqa_reward", Path(__file__).resolve().with_name("reward.py"))
_reward = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_reward)
extract_final_answer, score_answer = _reward.extract_final_answer, _reward.score_answer

LOGS = Path("/logs/agent")
VERIFIER = Path("/logs/verifier")
CONFIG = Path("/tests/config.json")
THRESHOLDS = {"reward": 0.0, "reward_0.1pct": 0.001, "reward_1pct": 0.01, "reward_5pct": 0.05}


def last_message() -> tuple[str, str]:
    """(the final message, where it was read)."""
    stream = LOGS / "agent-codex.jsonl"
    try:
        lines = stream.read_text(encoding="utf-8", errors="replace").split("\n")
    except FileNotFoundError:
        file = LOGS / "last_message.txt"
        if file.is_file():
            return file.read_text(encoding="utf-8", errors="replace"), file.name
        return "", "no final message"
    except OSError:
        return "", "no final message"
    message = ""
    completed_message = ""
    for line in lines:                               # a line that is no JSON event (Codex's stderr) is skipped
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind in ("thread.started", "turn.started", "turn.failed"):
            message = completed_message = ""
        elif kind == "turn.completed":
            completed_message = message
        elif kind in ("item.started", "item.updated", "item.completed"):
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") not in ("todo_list", "error"):
                # A later tool call makes the earlier message progress. Todo and diagnostic events carry no answer.
                message = ""
                if kind == "item.completed" and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    message = item["text"]
    return completed_message, stream.name


def answer_of(response: str) -> str:
    """The answer reward.py scores: the first <FINAL_ANSWER>, else the whole response; "" where it raises (an empty
    response or an empty tag)."""
    try:
        return extract_final_answer(response)
    except ValueError:
        return ""


def score(gold: str, answer: str, tolerance: float) -> float:
    if not answer.strip():
        return 0.0
    try:
        return score_answer(gold, answer, tolerance)
    except ValueError:                                # the answer's side (main checks the expected answer first)
        return 0.0


def main() -> int:
    gold = str(json.loads(CONFIG.read_text(encoding="utf-8"))["expected_answer"]).strip()
    response, source = last_message()
    VERIFIER.mkdir(parents=True, exist_ok=True)
    try:
        if not gold:                                  # reward.py's ValueError on it would read as the agent's 0
            raise ValueError("the task has no expected answer")
        answer = answer_of(response)
        rewards = {key: score(gold, answer, tolerance) for key, tolerance in THRESHOLDS.items()}
    except Exception as exc:                          # noqa: BLE001 - no reward: the verifier failed, not the agent
        (VERIFIER / "feedback.txt").write_text(f"verifier error: {type(exc).__name__}\n", encoding="utf-8")
        print(f"verifier error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    (VERIFIER / "reward.txt").write_text(f"{rewards['reward']:g}\n")
    (VERIFIER / "reward.json").write_text(json.dumps(rewards) + "\n")
    if not response.strip():
        feedback = "FAIL: no final message"
    else:
        tagged = "<final_answer>" in response.lower()
        verdict = "PASS" if rewards["reward"] else "FAIL"
        feedback = f"{verdict}: final answer {answer.strip()[:200]!r}" + ("" if tagged else " (no <FINAL_ANSWER> tag)")
    (VERIFIER / "feedback.txt").write_text(feedback + "\n", encoding="utf-8")
    print(feedback, f"(from {source})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
