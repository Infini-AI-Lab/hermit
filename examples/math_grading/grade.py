"""The grading the math benchmarks under examples/ share (a benchmark judged by a model, LiveMathBench, takes only the
answer from here): the agent's answer is the content of the last `\\boxed{}` in its reply, as MathArena parses it
(matharena/parser.py, `find_last_boxed_content`), and it is right when Math-Verify finds it equal to the reference
(`math_verify.verify`: numbers, fractions, intervals, sets, matrices and expressions compared as values; plain text
compared as text).

    agent.params.answer: ../../math_grading/grade.py:answer      # the acting agent's reply -> {answer, reply}
    judge.path: ../utils/benchmark/judge.py                      # grade(answer, reference) on the host

The references stay on the host, in the benchmark's `.private/answers.jsonl`, which no sandbox is granted.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logging.getLogger("math_verify").setLevel(logging.ERROR)    # its notice that the timeouts are off, on every call

REPLY_CHARS = 4000                      # the reply's tail kept in the output (the log has all of it)


def last_boxed(text: str) -> str | None:
    """The content of the last `\\boxed{...}` (or `\\fbox{...}`) in `text`, its braces balanced; None when there is none
    or it is never closed."""
    start = max(text.rfind("\\boxed{"), text.rfind("\\fbox{"))
    if start < 0:
        return None
    i = text.index("{", start) + 1
    depth, begin = 1, i
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[begin:i].strip()
        i += 1
    return None


def answer(last_message: str, task: dict) -> dict:
    """The CLI agent's output (agent.params.answer): its last `\\boxed{}`, and the reply's tail."""
    return {"answer": last_boxed(last_message or ""), "reply": (last_message or "")[-REPLY_CHARS:]}


def equal(given: str, reference: str) -> bool:
    """Math-Verify's equivalence of the agent's answer and the reference, both read as LaTeX, and only as LaTeX: its
    other extractor reads a plain expression with sympy's parse_expr, which evaluates Python, and the answer is the
    model's text, judged on the host. Its timeouts are off: they use signal.alarm, which works only in the main thread,
    and the run judges in worker threads."""
    from math_verify import LatexExtractionConfig, parse, verify
    latex = [LatexExtractionConfig()]
    gold = parse(f"${reference}$", extraction_config=latex, parsing_timeout=None)
    return bool(gold) and verify(gold, parse(f"${given}$", extraction_config=latex, parsing_timeout=None),
                                 timeout_seconds=None)


def load_answers(private: Path) -> dict[str, str]:
    path = Path(private) / "answers.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"{path}: run the benchmark's utils/benchmark/prepare.py first")
    return {r["id"]: r["answer"] for r in map(json.loads, path.read_text().splitlines()) if r}


def score(task: dict, output, answers: dict[str, str]) -> dict:
    """1 when the last `\\boxed{}` equals the reference, else 0; the feedback names the reference (what a learner reads
    of a training task)."""
    out = output.output if isinstance(output.output, dict) else {}
    reference = answers[task["id"]]
    given = out.get("answer")
    if not given:
        return {"score": 0.0, "feedback": f"no \\boxed{{}} answer in the reply; the answer is {reference}",
                "answered": False}
    right = equal(given, reference)
    return {"score": float(right), "answered": True,
            "feedback": "correct" if right else f"answered {given}, but the answer is {reference}"}
