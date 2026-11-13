"""LAB-Bench's list of test questions with a figure no training question has (utils/figures.py): FigQA asks several
questions about one figure, and SkillOpt's split is by question. The task directories here are built under tmp_path
in the published layout, a figure being the one image in `<task>/environment/workspace/`."""
from __future__ import annotations

import importlib.util
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


@pytest.mark.parametrize("images", [0, 2])
def test_a_task_with_other_than_one_image_is_refused(tmp_path, images):
    workspace = tmp_path / "figqa-0001" / "environment" / "workspace"
    workspace.mkdir(parents=True)
    for i in range(images):
        (workspace / f"fig{i}.jpg").write_bytes(b"x")
    with pytest.raises(ValueError, match=f"{images} images"):
        load().figure(tmp_path / "figqa-0001")
