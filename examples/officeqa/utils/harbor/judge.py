#!/usr/bin/env python3
"""The OfficeQA judge inside the task container (copied to every task's tests/judge.py by build_tasks.py).

The answer is the text inside the last `<answer>...</answer>` of the agent's final message, read from Codex's
`--json` event stream in /logs/agent: HermitCodex's agent-codex.jsonl (Hermit's own launch of the session), else the
codex.txt Harbor's own Codex agent writes; or the content of /app/answer.txt when the agent wrote one (that file
wins). Scoring is SkillOpt's: exact match after its normalisation, no tolerance; token F1 is recorded beside
it. The normalisation is SkillOpt's envs/officeqa/evaluator.py (microsoft/SkillOpt, MIT), copied so that the
container needs nothing but python3. Writes /logs/verifier/reward.txt (the exact match) and reward.json
({reward, f1, answered}); prints the feedback utils/judge.py gives on the host."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import string

ANSWER = re.compile(r"<answer>(.*?)</answer>", re.IGNORECASE | re.DOTALL)
_NUMERIC_CHARS = set("0123456789.-")
VERIFIER = Path("/logs/verifier")
ANSWER_FILE = Path("/app/answer.txt")
STREAMS = (Path("/logs/agent/agent-codex.jsonl"),         # HermitCodex's session (hermit/harbor/launch.py's stream)
           Path("/logs/agent/codex.txt"))                 # Harbor's own Codex agent's (its `codex exec --json | tee`)


def normalize_answer(text: str) -> str:
    text = text.lower().strip()
    text = text.replace(",", "")
    text = "".join(ch for ch in text if ch not in string.punctuation or ch in _NUMERIC_CHARS or ch == "%")
    text = re.sub(r"\b(million|millions|billion|billions|dollars|dollar|nominal)\b", " ", text)
    return " ".join(text.split())


def token_f1(prediction: str, gold: str) -> float:
    pred_tokens, gold_tokens = normalize_answer(prediction).split(), normalize_answer(gold).split()
    if not pred_tokens or not gold_tokens:
        return 1.0 if pred_tokens == gold_tokens else 0.0
    common = sum((Counter(pred_tokens) & Counter(gold_tokens)).values())
    if common == 0:
        return 0.0
    precision, recall = common / len(pred_tokens), common / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


def last_message(events_file: Path) -> str:
    """The agent's final message from Codex's `--json` event stream (a line that is no JSON object, such as a line of
    Codex's stderr, which shares the file, or one nested past the JSON parser's depth, is skipped). U+0085, U+2028 and
    U+2029 end no line: Codex leaves them unescaped in an event's strings, where str.splitlines() would end one."""
    text = ""
    try:
        lines = events_file.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return ""
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):              # no JSON, or nested past the parser's depth
            continue
        if not isinstance(event, dict):                   # a bare number, string or null: no event
            continue
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            text = item["text"]
    return text


def find_answer() -> tuple[str, str]:
    """(answer, how): /app/answer.txt, else the last <answer> of the final message (from the first of STREAMS that
    exists)."""
    file = ANSWER_FILE
    if file.is_file() and file.read_text(encoding="utf-8", errors="replace").strip():
        text = file.read_text(encoding="utf-8", errors="replace").strip()
        found = ANSWER.findall(text)
        return (found[-1].strip() if found else text), "answer.txt"
    message = next((last_message(events) for events in STREAMS if events.is_file()), "")
    found = ANSWER.findall(message)
    if found:
        return found[-1].strip(), "final message"
    return "", "no <answer>...</answer> in the final message" if message else "no final message"


def main() -> int:
    gold = json.loads(Path("/tests/gold.json").read_text(encoding="utf-8"))["answer"]
    answer, how = find_answer()
    VERIFIER.mkdir(parents=True, exist_ok=True)
    if not answer:
        em, f1 = 0.0, 0.0
        print(f"no answer: {how}")
    else:
        em = 1.0 if normalize_answer(answer) == normalize_answer(gold) else 0.0
        f1 = token_f1(answer, gold)
        print("correct" if em else f"predicted '{answer}' but expected '{gold}'", f"(from the {how})")
    (VERIFIER / "reward.txt").write_text(f"{em:g}\n")
    (VERIFIER / "reward.json").write_text(json.dumps({"reward": em, "f1": round(f1, 4), "answered": bool(answer)}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
