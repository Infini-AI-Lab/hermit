"""Write the Robosuite tasks of ASPIRE's harness (cap_harness/robosuite/registry.py) as hermit tasks: one task
directory (Harbor's format) per task with its seeds, which the simulator service and the judge take from the task's
row (ASPIRE's Robosuite defaults: development 101-125, unseen-check 126-130, held-out 1-100), through the cap-harness
engine's helpers (../../../cap_harness/utils/benchmark/tasks.py).

    python prepare.py                                  # ASPIRE's seeds
    python prepare.py --preset smoke                   # the smoke's: three development seeds, one unseen, three held-out
    python prepare.py --dev 101-103 --unseen 126 --eval 1-3      # any others
    # tasks/<id>/ (and rerun-<id>/), splits/{all,smoke,single_arm}.txt and their *_rerun.txt (rerun.txt for all)

The order of splits/all.txt is a curriculum from simple to hard (single-arm pick before stacking and assembly, the
fixed-tool wipe, then the bimanual tasks).
"""
from __future__ import annotations

from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "cap_harness" / "utils" / "benchmark"))
from tasks import seeds_from, write  # noqa: E402

TASKS = [  # suite, what it is (task-info only; the session reads the language from get_task_context())
    ("cube_lifting", "Lift"),
    ("cube_stack", "Stack"),
    ("cube_restack", "Stack, restacked"),
    ("nut_assembly", "NutAssemblySquare"),
    ("spill_wipe", "Wipe, fixed tool"),
    ("two_arm_lift", "TwoArmLift, bimanual"),
    ("two_arm_handover", "TwoArmHandover, bimanual"),
]


# The seeds of a run: ASPIRE's Robosuite defaults, and the smoke's
PRESETS = {"aspire": {"dev": "101-125", "unseen": "126-130", "eval": "1-100"},
           "smoke": {"dev": "101-103", "unseen": "126", "eval": "1-3"}}


def main() -> None:
    seeds = seeds_from(PRESETS)
    tasks = [{"id": f"robosuite-{suite}", "suite": suite, "task_id": 0} for suite, _ in TASKS]
    ids = [t["id"] for t in tasks]
    write(HERE, tasks, seeds, {"all": ids, "single_arm": ids[:5], "smoke": ids[:2]})


if __name__ == "__main__":
    main()
