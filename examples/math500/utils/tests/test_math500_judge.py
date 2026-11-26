"""MATH-500's judge (utils/benchmark/judge.py) as the run calls it, `score(task row, output)`: it reads prepare.py's
references once, from its `PRIVATE` folder (a temporary one here, since no reference is committed), and scores with the
shared grader. Math-Verify itself is used, so the test skips without it."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("math_verify", reason="python -m pip install -r examples/math_grading/requirements.txt")

JUDGE = Path(__file__).resolve().parents[1] / "benchmark" / "judge.py"


@pytest.fixture
def judge(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))       # the judge puts the grader's folder on it
    monkeypatch.delitem(sys.modules, "grade", raising=False)
    spec = importlib.util.spec_from_file_location("math500_judge", JUDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "answers.jsonl").write_text(json.dumps({"id": "test-algebra-1", "answer": "\\frac{1}{2}"}) + "\n")
    monkeypatch.setattr(module, "PRIVATE", tmp_path)
    yield module
    sys.modules.pop("grade", None)


def test_the_judge_scores_the_boxed_answer_against_prepares_reference(judge):
    right = judge.score({"id": "test-algebra-1"}, SimpleNamespace(output={"answer": "0.5"}))
    wrong = judge.score({"id": "test-algebra-1"}, SimpleNamespace(output={"answer": "0.4"}))
    assert (right["score"], wrong["score"]) == (1.0, 0.0) and "\\frac{1}{2}" in wrong["feedback"]
