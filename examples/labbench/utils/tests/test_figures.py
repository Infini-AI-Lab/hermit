"""LAB-Bench's list of test questions with a figure no training question has, and an eval's score on them
(utils/figures.py): FigQA asks several questions about one figure, and SkillOpt's split is by question. The task
directories here are built under tmp_path in the published layout, a figure being the one image in
`<task>/environment/workspace/`; an eval's results.jsonl holds records as `hermit eval` writes them."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

FIGURES = Path(__file__).resolve().parents[1] / "figures.py"


def load():
    spec = importlib.util.spec_from_file_location("labbench_figures", FIGURES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def task(root: Path, name: str, image: bytes, suffix: str = ".jpg") -> None:
    workspace = root / name / "environment" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / f"fig{suffix}").write_bytes(image)
    (workspace / "setup.py").write_text("")


def test_a_test_question_on_a_training_questions_figure_is_left_out(tmp_path):
    task(tmp_path, "figqa-0001", b"figure A")
    task(tmp_path, "figqa-0002", b"figure A", suffix=".png")     # the same image, another file name
    task(tmp_path, "figqa-0003", b"figure B")
    task(tmp_path, "figqa-0004", b"figure C")
    assert load().new_figures(tmp_path, train=["figqa-0001", "figqa-0004"],
                              test=["figqa-0003", "figqa-0002"]) == ["figqa-0003"]


def row(tid: str, reward: float | None) -> dict:
    """A trial's row as `hermit eval` writes it: scored (the judge's `passed` in its extra), or, with no reward, one
    that is not the agent's result, which the judge refused (a failed run, scored 0)."""
    if reward is None:
        return {"id": tid, "status": "judge_error", "output": {"status": "infra"},
                "judge": {"score": 0.0, "feedback": "the judge failed: Unscored: ..."}}
    return {"id": tid, "status": "ok", "output": {"status": "ok", "reward": reward},
            "judge": {"score": reward, "feedback": "", "extra": {"passed": reward >= 1.0}}}


def test_an_evals_figures_on_the_rows_are_the_harbor_reports(tmp_path):
    results = tmp_path / "results.jsonl"
    rows = [row("figqa-0002", 1.0), row("figqa-0003", 0.0), row("figqa-0004", None),
            row("figqa-0005", 1.0)]                             # figqa-0005: a test row not in the list
    results.write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "summary.json").write_text(json.dumps({"tasks": 4, "repeats": 1}))      # hermit eval's, at its end
    got = load().score(results, ["figqa-0002", "figqa-0003", "figqa-0004", "figqa-0009"])
    assert got == {"rows": 4, "missing": 1, "trials": 3, "scored": 2, "infra": 1, "failed": 0, "pass_at_1": 0.5,
                   "mean_scored": 0.5, "mean_all": pytest.approx(1 / 3), "finished": True}
    (tmp_path / "summary.json").write_text(json.dumps({"tasks": 5, "repeats": 1}))      # one it did not finish
    got = load().score(results, ["figqa-0002", "figqa-0003", "figqa-0004", "figqa-0009"])
    assert got["finished"] is False and "counts 5 trials" in got["unfinished"]


@pytest.mark.parametrize("images", [0, 2])
def test_a_task_with_other_than_one_image_is_refused(tmp_path, images):
    workspace = tmp_path / "figqa-0001" / "environment" / "workspace"
    workspace.mkdir(parents=True)
    for i in range(images):
        (workspace / f"fig{i}.jpg").write_bytes(b"x")
    with pytest.raises(ValueError, match=f"{images} images"):
        load().figure(tmp_path / "figqa-0001")
