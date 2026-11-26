"""The grading the math benchmarks under examples/ share: the agent's answer is the content of the last complete
`\\boxed{}` in its reply, as MathArena parses it (matharena/parser.py, `find_last_boxed_content`, at commit d98156f),
and it is right when Math-Verify finds it equal to the reference (numbers, fractions, intervals, sets, matrices and
expressions compared as values; plain text compared as text).

    agent.params.answer: ../../math_grading/utils/grade.py:answer   # the acting agent's reply -> {answer, reply}
    environment.read: [../../math_grading/utils]                     # the hook runs in the task sandbox
    judge.path: ../utils/benchmark/judge.py                          # grade.score on the host

`answer` runs in the task sandbox and uses the standard library only. `equal` runs on the host, each comparison in a
process of its own (math_compare.py) with a time limit and, on Linux, a memory bound, so that an answer whose arithmetic never ends cannot hold the
run. The references stay on the host, in the benchmark's `.private/answers.jsonl`, which no sandbox is granted.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
REPLY_CHARS = 4000                      # the reply's tail kept in the output (the log has all of it)
COMPARE_TIMEOUT_S = 60                  # one comparison's wall clock (a process start and Math-Verify's own timeouts)


def last_boxed(text: str) -> str | None:
    """The content of the last complete `\\boxed{...}` (or `\\fbox{...}`) in `text`, its braces balanced: a box that is
    never closed is passed over for the one before it. None when there is none."""
    end = len(text)
    while True:
        start = max(text.rfind("\\boxed{", 0, end), text.rfind("\\fbox{", 0, end))
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
        end = start                     # never closed: the box before it


def answer(last_message: str, task: dict) -> dict:
    """The CLI agent's output (agent.params.answer): its last `\\boxed{}`, and the reply's tail."""
    return {"answer": last_boxed(last_message or ""), "reply": (last_message or "")[-REPLY_CHARS:]}


def equal(given: str, reference: str) -> bool | None:
    """Math-Verify's equivalence of the agent's answer and the reference (math_compare.py), in a process of its own;
    None when it did not finish within COMPARE_TIMEOUT_S (the process is killed) or ran past its memory bound."""
    try:
        done = subprocess.run([sys.executable, str(HERE / "math_compare.py")], capture_output=True, text=True,
                              input=json.dumps({"given": given, "reference": reference}), timeout=COMPARE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None
    if done.returncode != 0:
        raise RuntimeError(f"math_compare.py failed: {done.stderr.strip()[-2000:]}")
    lines = done.stdout.strip().splitlines()     # its answer is the last line: a library may print before it
    result = json.loads(lines[-1])["equal"] if lines else None
    return None if result is None else bool(result)


def require() -> None:
    """Math-Verify importable by this interpreter, which equal() runs math_compare.py with: checked when a judge loads,
    so that a run without it stops before any agent session rather than failing every judgement after them."""
    import importlib.util
    if importlib.util.find_spec("math_verify") is None:
        raise ImportError(f"Math-Verify is not installed for {sys.executable}: python -m pip install -r "
                          f"{HERE.parent / 'requirements.txt'}")


def load_answers(private: Path) -> dict[str, str]:
    path = Path(private) / "answers.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"{path}: run the benchmark's utils/benchmark/prepare.py first")
    return {r["id"]: r["answer"] for r in map(json.loads, path.read_text().splitlines()) if r}


def score(task: dict, output, answers: dict[str, str]) -> dict:
    """1 when the last `\\boxed{}` equals the reference, else 0; the feedback names the reference (what a learner reads
    of a training task), and why there was no answer when the agent's output says (a session that timed out)."""
    out = output.output if isinstance(output.output, dict) else {}
    reference = answers[task["id"]]
    given = out.get("answer")
    if not given:
        why = out.get("fail_reason") or "no \\boxed{} answer in the reply"
        return {"score": 0.0, "feedback": f"{why}; the answer is {reference}", "answered": False}
    right = equal(given, reference)
    if right is None:
        return {"score": 0.0, "answered": True, "compared": False,
                "feedback": f"answered {given}, which could not be compared with the answer, {reference}, within "
                            f"{COMPARE_TIMEOUT_S} s (and 2 GB of memory, on Linux)"}
    return {"score": float(right), "answered": True,
            "feedback": "correct" if right else f"answered {given}, but the answer is {reference}"}
