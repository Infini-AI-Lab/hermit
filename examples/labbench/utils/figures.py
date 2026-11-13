"""The test questions of LAB-Bench FigQA whose figure no training question has, and an eval's accuracy on them. FigQA
asks up to three questions about one figure (its 181 questions show 107 figures), and SkillOpt's split, like its
paper's, is by question: 25 of the 127 test questions share a figure with a training question, whose verifier output
(the right choice's text) the learner reads. A figure is the sha256 of the task's image file.

    python examples/labbench/utils/figures.py               # utils/rows/test_new_figures.txt, from utils/tasks
    python examples/labbench/utils/figures.py <eval dir>... # each eval's accuracy on those rows, from its results
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROWS = HERE / "rows" / "test_new_figures.txt"
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


def score(results: Path, rows: list[str]) -> dict:
    """An eval's mean score on the given rows, from its results.jsonl, as `hermit eval` takes it on all of them: infra
    trials are left out and counted (`unscored`); rows the eval did not run are counted as `missing`."""
    keep, seen, scores, unscored = set(rows), set(), [], 0
    for line in Path(results).read_text().splitlines():
        record = json.loads(line) if line.strip() else None
        if not record or str(record.get("id")) not in keep:
            continue
        seen.add(str(record["id"]))
        if record.get("status") == "infra":
            unscored += 1
        else:
            scores.append(float((record.get("judge") or {}).get("score") or 0.0))
    return {"rows": len(keep), "scored": len(scores), "unscored": unscored, "missing": len(keep - seen),
            "mean_score": sum(scores) / len(scores) if scores else None}


if __name__ == "__main__":
    if sys.argv[1:]:
        for arg in sys.argv[1:]:
            path = Path(arg) / "results.jsonl" if Path(arg).is_dir() else Path(arg)
            print(f"{path}: {json.dumps(score(path, ids(ROWS)))}")
    else:
        rows = new_figures(HERE / "tasks", ids(HERE / "splits" / "train.txt"), ids(HERE / "splits" / "test.txt"))
        ROWS.parent.mkdir(exist_ok=True)
        ROWS.write_text("".join(f"{row}\n" for row in rows))
        print(f"{len(rows)} test questions with a figure no training question has: {ROWS}")
