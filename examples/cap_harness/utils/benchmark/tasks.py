"""The tasks of a benchmark on the cap-harness engine, written as task directories (Harbor's format) with their seeds,
which the simulator service and the judge take from each task's row: the helpers a benchmark's own
utils/benchmark/prepare.py calls (../../../robosuite/, ../../../libero/).

A task directory's task.toml holds the row's fields but its id under [metadata], in the row's order, and asks for no
network (the session reaches only its model and the simulator service's forwarded port); it has no tests/: the judge
(judge.py) runs on the host, where the simulator is. ../agent/fields.py reads each back as its row.

Each task also has its rerun, `rerun-<id>` (`rerun_of: <id>`): the same task with the same seeds, under its own id
so that a test split of the tasks a run trained on does not share ids with its training split (a run's `launch.sh
eval`, or a continual method's evaluation of every task again with its final adapter).
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

from hermit.task.directory import write_task as write_task_dir

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from fields import task_rows  # noqa: E402
from sim import seeds_of  # noqa: E402


def seeds_from(presets: dict, argv=None) -> dict:
    """The run's seeds from the command line (a preset, each set replaceable): the three sets of a task row."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", choices=sorted(presets), default=next(iter(presets)))
    ap.add_argument("--dev", help="development seeds")
    ap.add_argument("--unseen", help="unseen-check seeds, run once by the session")
    ap.add_argument("--eval", help="held-out seeds, run only by the judge")
    a = ap.parse_args(argv)
    chosen = {k: getattr(a, k) or v for k, v in presets[a.preset].items()}
    # A held-out seed a session could run would be scored as unseen: the sets must not meet
    dev, unseen, held = (set(seeds_of(chosen[k])) for k in ("dev", "unseen", "eval"))
    for name, other in (("development", dev), ("unseen-check", unseen)):
        if held & other:
            ap.error(f"the held-out seeds overlap the {name} seeds: {sorted(held & other)[:10]}")
    if dev & unseen:
        ap.error(f"the unseen-check seeds overlap the development seeds: {sorted(dev & unseen)[:10]}")
    return {"debug_seeds": chosen["dev"], "unseen_seeds": chosen["unseen"], "eval_seeds": chosen["eval"]}


def instruction(row: dict) -> str:
    """What a task asks, in a line (a session's prompt is its config's, built from the row)."""
    what = f"{row.get('benchmark') or 'robosuite'} suite `{row['suite']}`, task id {row['task_id']}"
    if row.get("rerun_of"):
        return f"Solve {what} again (a rerun of `{row['rerun_of']}`)."
    return f"Write the program that solves {what} in ASPIRE's Code-as-Policy harness."


def write_task(tasks: Path, row: dict, text: str | None = None) -> Path:
    """One row as a task directory: its fields but the id under [metadata], in the row's order; no network."""
    return write_task_dir(tasks / row["id"], text or instruction(row),
                          metadata={k: v for k, v in row.items() if k != "id"},
                          environment={"network_mode": "no-network"})


def rows(tasks: Path) -> dict[str, dict]:
    """Every task directory under `tasks` as its row, by id (a method that adds rows of its own reads them here)."""
    return task_rows(tasks)


def write(here: Path, tasks: list[dict], seeds: dict, splits: dict[str, list[str]]) -> None:
    """tasks/ and splits/ under `here`: each task (`id`, `suite`, `task_id`, and `benchmark` when it is not
    Robosuite) with its seeds, and its rerun; splits/<name>.txt for each of `splits`, with <name>_rerun.txt beside
    it (rerun.txt for `all`)."""
    rows = [{**t, **seeds} for t in tasks]
    reruns = [{**t, "id": f"rerun-{t['id']}", "rerun_of": t["id"], **seeds} for t in tasks]
    if (here / "tasks").exists():                    # this script's own output: rewritten whole
        shutil.rmtree(here / "tasks")
    for row in rows + reruns:
        write_task(here / "tasks", row)
    out = here / "splits"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir()
    for name, ids in splits.items():
        (out / f"{name}.txt").write_text("".join(f"{i}\n" for i in ids))
        rerun = out / ("rerun.txt" if name == "all" else f"{name}_rerun.txt")
        rerun.write_text("".join(f"rerun-{i}\n" for i in ids))
    print(f"{len(rows)} tasks -> {here / 'tasks'}")
