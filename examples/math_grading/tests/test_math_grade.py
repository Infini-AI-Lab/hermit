"""The math benchmarks' grading (examples/math_grading/grade.py): the answer is the last `\\boxed{}` of the reply, its
braces balanced, and it is right when Math-Verify finds it equal to the reference, read by its LaTeX extractor alone
(never sympy's parse_expr, on the model's text on the host), also in a worker thread (where the run judges). No
stand-ins: Math-Verify itself, which the tests skip without."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("math_verify", reason="python -m pip install -r examples/math_grading/requirements.txt")

spec = importlib.util.spec_from_file_location("math_grade", Path(__file__).resolve().parents[1] / "grade.py")
grade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grade)


@pytest.mark.parametrize("reply, boxed", [
    ("so \\boxed{70}.", "70"),
    ("first \\boxed{1} then \\boxed{\\frac{1}{2}}", "\\frac{1}{2}"),
    ("\\fbox{ 3 }", "3"),
    ("no box here", None),
    ("\\boxed{\\frac{1}{2}", None),                  # never closed
])
def test_the_answer_is_the_last_boxed(reply, boxed):
    assert grade.last_boxed(reply) == boxed
    assert grade.answer(reply, {})["answer"] == boxed


@pytest.mark.parametrize("given, reference, right", [
    ("070", "70", True),
    ("71", "70", False),
    ("0.5", "\\frac{1}{2}", True),
    ("(3,\\pi/2)", "\\left( 3, \\frac{\\pi}{2} \\right)", True),
])
def test_math_verify_compares_values(given, reference, right):
    assert grade.equal(given, reference) is right


def test_only_math_verifys_latex_extractor_reads_the_models_answer(monkeypatch):
    """Math-Verify's default extractors include one that reads a plain expression with sympy's parse_expr, which runs
    Python; the judge runs on the host on the model's text, so the grader hands Math-Verify its LaTeX extractor
    alone."""
    import math_verify
    from math_verify import LatexExtractionConfig
    seen, parse = [], math_verify.parse

    def spy(text, **kwargs):
        seen.append(kwargs.get("extraction_config"))
        return parse(text, **kwargs)
    monkeypatch.setattr(math_verify, "parse", spy)
    assert grade.equal("070", "70") is True
    assert len(seen) == 2 and all(c is not None and [type(x) for x in c] == [LatexExtractionConfig] for c in seen)


def test_a_worker_thread_grades_too():
    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(grade.equal, "070", "70").result() is True


def test_the_feedback_names_the_reference():
    answers = {"t": "70"}
    out = lambda text: SimpleNamespace(output=grade.answer(text, {}))
    assert grade.score({"id": "t"}, out("\\boxed{70}"), answers) == {"score": 1.0, "answered": True,
                                                                      "feedback": "correct"}
    wrong = grade.score({"id": "t"}, out("\\boxed{71}"), answers)
    assert wrong["score"] == 0.0 and "70" in wrong["feedback"]
    none = grade.score({"id": "t"}, out("no idea"), answers)
    assert none["score"] == 0.0 and none["answered"] is False
