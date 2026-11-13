"""The test questions of LAB-Bench FigQA whose figure no training question has. FigQA asks up to three questions about
one figure (its 181 questions show 107 figures), and SkillOpt's split, like its paper's, is by question: 25 of the
127 test questions share a figure with a training question, whose verifier output (the right choice's text) the
learner reads. A figure is the sha256 of the task's image file.

    python examples/labbench/utils/figures.py   # utils/rows/test_new_figures.txt, from utils/tasks and utils/splits
"""
from __future__ import annotations

import hashlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMAGES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def figure(task: Path) -> str:
    """The sha256 of the task's one image, in environment/workspace/."""
    images = [p for p in sorted((Path(task) / "environment" / "workspace").iterdir()) if p.suffix.lower() in IMAGES]
    if len(images) != 1:
        raise ValueError(f"{task}: {len(images)} images in environment/workspace/, not one")
    return hashlib.sha256(images[0].read_bytes()).hexdigest()


def new_figures(tasks: Path, train: list[str], test: list[str]) -> list[str]:
    """The test rows whose figure no training row has, in the test's order (a row is its task's name)."""
    seen = {figure(Path(tasks) / name) for name in train}
    return [name for name in test if figure(Path(tasks) / name) not in seen]


def ids(path: Path) -> list[str]:
    return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]


if __name__ == "__main__":
    rows = new_figures(HERE / "tasks", ids(HERE / "splits" / "train.txt"), ids(HERE / "splits" / "test.txt"))
    out = HERE / "rows" / "test_new_figures.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text("".join(f"{row}\n" for row in rows))
    print(f"{len(rows)} test questions with a figure no training question has: {out}")
