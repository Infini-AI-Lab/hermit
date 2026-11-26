"""The math benchmarks' grading (examples/math_grading/utils/grade.py and math_compare.py): the answer is the reply's
last complete `\\boxed{}`; it is right when Math-Verify, run in a process of its own, finds it equal to the reference,
read by its LaTeX extractor alone (never sympy's parse_expr, on the model's text); a comparison past its time limit is
killed and scored 0, and none holds another judge; a reply without an answer is fed back with the agent's own
reason. No stand-ins: Math-Verify itself, which the tests skip without."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("math_verify", reason="python -m pip install -r examples/math_grading/requirements.txt")

UTILS = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"math_grading_{name}", UTILS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


grade, compare = load("grade"), load("math_compare")


@pytest.mark.parametrize("reply, boxed", [
    ("so \\boxed{70}.", "70"),
    ("first \\boxed{1} then \\boxed{\\frac{1}{2}}", "\\frac{1}{2}"),
    ("\\fbox{ 3 }", "3"),
    ("no box here", None),
    ("\\boxed{5} and at last \\boxed{\\frac{1}{2}", "5"),      # an unclosed last box: the complete one before it
])
def test_the_answer_is_the_last_complete_boxed(reply, boxed):
    assert grade.last_boxed(reply) == boxed
    assert grade.answer(reply, {})["answer"] == boxed


@pytest.mark.parametrize("given, reference, right", [
    ("070", "70", True),
    ("71", "70", False),
    ("0.5", "\\frac{1}{2}", True),
    ("(3,\\pi/2)", "\\left( 3, \\frac{\\pi}{2} \\right)", True),
])
def test_math_verify_compares_values_in_a_process_of_its_own(given, reference, right):
    assert grade.equal(given, reference) is right


def test_only_math_verifys_latex_extractor_reads_the_models_answer(monkeypatch):
    """Math-Verify's default extractors include one that reads a plain expression with sympy's parse_expr, which runs
    Python; the model's text is compared with the LaTeX extractor alone."""
    import math_verify
    from math_verify import LatexExtractionConfig
    seen, parse = [], math_verify.parse

    def spy(text, **kwargs):
        seen.append(kwargs.get("extraction_config"))
        return parse(text, **kwargs)
    monkeypatch.setattr(math_verify, "parse", spy)
    assert compare.compare("070", "70") is True
    assert len(seen) == 2 and all(c is not None and [type(x) for x in c] == [LatexExtractionConfig] for c in seen)


def test_an_answer_whose_arithmetic_never_ends_holds_no_other_judge():
    """A tower of powers keeps big-integer arithmetic running, which in a judging thread would hold the GIL for every
    other thread and, with Math-Verify's timeouts off there (signal.alarm needs the main thread), never return. In its
    own process Math-Verify's timeouts work: it scores 0 while another comparison goes on."""
    with ThreadPoolExecutor(2) as pool:
        stuck = pool.submit(grade.score, {"id": "t"}, SimpleNamespace(output={"answer": "10^{10^{10}}"}), {"t": "70"})
        assert pool.submit(grade.equal, "070", "70").result(timeout=30) is True
        assert stuck.result(timeout=grade.COMPARE_TIMEOUT_S + 10)["score"] == 0.0


def test_a_comparison_past_its_time_limit_is_killed_and_scores_0(monkeypatch):
    monkeypatch.setattr(grade, "COMPARE_TIMEOUT_S", 0.2)     # shorter than the process's start
    started = time.time()
    result = grade.score({"id": "t"}, SimpleNamespace(output={"answer": "70"}), {"t": "70"})
    assert result["score"] == 0.0 and result["compared"] is False and "70" in result["feedback"]
    assert time.time() - started < 5


def test_the_feedback_names_the_reference_and_why_there_was_no_answer():
    answers = {"t": "70"}
    out = lambda text, **extra: SimpleNamespace(output={**grade.answer(text, {}), **extra})
    assert grade.score({"id": "t"}, out("\\boxed{70}"), answers) == {"score": 1.0, "answered": True,
                                                                     "feedback": "correct"}
    wrong = grade.score({"id": "t"}, out("\\boxed{71}"), answers)
    assert wrong["score"] == 0.0 and "70" in wrong["feedback"]
    timed_out = grade.score({"id": "t"}, out("", fail_reason="session timed out after 3600 s without an answer"),
                            answers)
    assert timed_out["score"] == 0.0 and timed_out["feedback"].startswith("session timed out after 3600 s")
    assert grade.score({"id": "t"}, out("no idea"), answers)["answered"] is False


def test_the_answer_hook_uses_the_standard_library_only():
    """It runs in the task sandbox, which shows the framework's environment but not the grader's requirements."""
    source = (UTILS / "grade.py").read_text()
    assert "math_verify" not in source and "import sympy" not in source
