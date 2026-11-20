"""Write the Robosuite tasks of ASPIRE's harness (cap_harness/robosuite/registry.py) as hermit tasks: one task
directory (Harbor's format) per task with its seeds, which the simulator service and the judge take from the task's
row (ASPIRE's Robosuite defaults: development 101-125, unseen-check 126-130, held-out 1-100).

    python prepare.py                                  # ASPIRE's seeds
    python prepare.py --preset smoke                   # the smoke's: three development seeds, one unseen, three held-out
    python prepare.py --dev 101-103 --unseen 126 --eval 1-3      # any others
    # tasks/<id>/, splits/{all,smoke,single_arm,verify,rerun,smoke_rerun,single_arm_rerun}.txt

A task directory's task.toml holds the row's fields but its id under [metadata], in the row's order, and asks for no
network (the actor reaches only its model and the simulator service's forwarded port); it has no tests/: the judge
(judge.py) runs on the host, where the simulator is. utils/agent/aspire_fields.py reads each back as its row.

The verifier rows (splits/verify.txt) are the Evolver's: a run gives them as `data.val`, so only `run_agent` runs
them.

The order of splits/all.txt is the curriculum the coordinator walks (single-arm pick before stacking and
assembly, the fixed-tool wipe, then the bimanual tasks).
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

from hermit.task.directory import write_task as write_task_dir

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "agent"))
from sim import seeds_of  # noqa: E402
TASKS = [  # suite, what it is (task-info only; the actor reads the language from get_task_context())
    ("cube_lifting", "Lift"),
    ("cube_stack", "Stack"),
    ("cube_restack", "Stack, restacked"),
    ("nut_assembly", "NutAssemblySquare"),
    ("spill_wipe", "Wipe, fixed tool"),
    ("two_arm_lift", "TwoArmLift, bimanual"),
    ("two_arm_handover", "TwoArmHandover, bimanual"),
]


# The seeds of a run: ASPIRE's Robosuite defaults, and the smoke's
PRESETS = {"aspire": {"dev": "101-125", "unseen": "126-130", "eval": "1-100", "verify": "101-105"},
           "smoke": {"dev": "101-103", "unseen": "126", "eval": "1-3", "verify": "101-105"}}


def seeds_from(presets: dict, argv=None) -> tuple[dict, str]:
    """The run's seeds from the command line (a preset, each set replaceable): the three sets of a task row, and
    the verifier's."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", choices=sorted(presets), default="aspire")
    ap.add_argument("--dev", help="development seeds")
    ap.add_argument("--unseen", help="unseen-check seeds, run once by the actor")
    ap.add_argument("--eval", help="held-out seeds, run only by the judge")
    ap.add_argument("--verify", help="the verifier's seeds")
    a = ap.parse_args(argv)
    chosen = {k: getattr(a, k) or v for k, v in presets[a.preset].items()}
    # A held-out seed a session could run would be scored as unseen: the sets must not meet
    dev, unseen, held, verify = (set(seeds_of(chosen[k])) for k in ("dev", "unseen", "eval", "verify"))
    for name, other in (("development", dev), ("unseen-check", unseen), ("verifier", verify)):
        if held & other:
            ap.error(f"the held-out seeds overlap the {name} seeds: {sorted(held & other)[:10]}")
    if dev & unseen:
        ap.error(f"the unseen-check seeds overlap the development seeds: {sorted(dev & unseen)[:10]}")
    return {"debug_seeds": chosen["dev"], "unseen_seeds": chosen["unseen"], "eval_seeds": chosen["eval"]}, \
        chosen["verify"]


def instruction(row: dict) -> str:
    """What a task asks, in a line (the actor's prompt is utils/agent/task.py's, built from the row)."""
    what = f"{row.get('benchmark') or 'robosuite'} suite `{row['suite']}`, task id {row['task_id']}"
    if row.get("role") == "verifier":
        return f"Verify the proposals for {what}: solve it again from the library and the proposals alone."
    if row.get("role") == "rerun":
        return f"Solve {what} again with the final library."
    return f"Write the program that solves {what} in ASPIRE's Code-as-Policy harness."


def write_task(tasks: Path, row: dict) -> Path:
    """One row as a task directory: its fields but the id under [metadata], in the row's order; no network."""
    return write_task_dir(tasks / row["id"], instruction(row), metadata={k: v for k, v in row.items() if k != "id"},
                          environment={"network_mode": "no-network"})


def write(here: Path, tasks: list[dict], seeds: dict, verify: str, splits: dict[str, list[str]]) -> None:
    """tasks/ and splits/ under `here`: each task (`id`, `suite`, `task_id`, and `benchmark` when it is not
    Robosuite) with its seeds, its verifier task and its rerun task; splits/<name>.txt for each of `splits`, with
    <name>_rerun.txt beside it (rerun.txt for `all`), and verify.txt."""
    rows = [{**t, **seeds} for t in tasks]
    # The verifier of each task (ASPIRE verifier-prompt.md): a fresh actor that re-solves the task from the library
    # and the proposals alone, on 5 development seeds in at most 2 attempts, with no held-out run and no findings.
    verifiers = [{**t, "id": f"verify-{t['id']}", "role": "verifier", "debug_seeds": verify, "eval_seeds": "none",
                  "max_versions": 2, "search": "debug", "findings": False} for t in tasks]
    # The rerun of each task with the final library (ASPIRE §2.4, backward transfer): the same task, the same seeds
    # and a fresh actor; its own id so that the test split does not share ids with the training split.
    reruns = [{**t, "id": f"rerun-{t['id']}", "role": "rerun", "findings": False, **seeds} for t in tasks]
    if (here / "tasks").exists():                    # this script's own output: rewritten whole
        shutil.rmtree(here / "tasks")
    for row in rows + verifiers + reruns:
        write_task(here / "tasks", row)
    out = here / "splits"
    out.mkdir(exist_ok=True)
    for name, ids in splits.items():
        (out / f"{name}.txt").write_text("".join(f"{i}\n" for i in ids))
        (out / ("rerun.txt" if name == "all" else f"{name}_rerun.txt")).write_text("".join(f"rerun-{i}\n" for i in ids))
    (out / "verify.txt").write_text("".join(f"verify-{t['id']}\n" for t in tasks))
    print(f"{len(rows)} tasks -> {here / 'tasks'}")


def main() -> None:
    seeds, verify = seeds_from(PRESETS)
    tasks = [{"id": f"robosuite-{suite}", "suite": suite, "task_id": 0} for suite, _ in TASKS]
    ids = [t["id"] for t in tasks]
    write(HERE, tasks, seeds, verify, {"all": ids, "single_arm": ids[:5], "smoke": ids[:2]})


if __name__ == "__main__":
    main()
