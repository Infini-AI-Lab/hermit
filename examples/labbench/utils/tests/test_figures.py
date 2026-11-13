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


def test_an_evals_score_on_the_rows_is_taken_from_its_results_as_hermit_eval_takes_it(tmp_path):
    results = tmp_path / "results.jsonl"
    records = [("figqa-0002", "ok", 1.0), ("figqa-0003", "ok", 0.0), ("figqa-0004", "infra", None),
               ("figqa-0005", "ok", 1.0)]                       # figqa-0005: a test row not in the list
    results.write_text("".join(json.dumps({"id": i, "status": s, "judge": None if v is None else {"score": v}}) + "\n"
                               for i, s, v in records))
    assert load().score(results, ["figqa-0002", "figqa-0003", "figqa-0004", "figqa-0009"]) == {
        "rows": 4, "scored": 2, "unscored": 1, "missing": 1, "mean_score": 0.5}


@pytest.mark.parametrize("images", [0, 2])
def test_a_task_with_other_than_one_image_is_refused(tmp_path, images):
    workspace = tmp_path / "figqa-0001" / "environment" / "workspace"
    workspace.mkdir(parents=True)
    for i in range(images):
        (workspace / f"fig{i}.jpg").write_bytes(b"x")
    with pytest.raises(ValueError, match=f"{images} images"):
        load().figure(tmp_path / "figqa-0001")
