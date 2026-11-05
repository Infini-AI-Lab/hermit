"""Write the Robosuite tasks of ASPIRE's harness (cap_harness/robosuite/registry.py) as hermit tasks: one row per
task, its seeds given by the run's config (agent.params.debug_seeds / eval_seeds; ASPIRE's Robosuite defaults are
development 101-125 and held-out 1-100).

    python prepare.py            # tasks.jsonl, splits/{all,smoke,single_arm,verify,rerun,smoke_rerun}.txt

The verifier rows (splits/verify.txt) are the Evolver's: a run gives them as `data.val`, so only `run_agent` runs
them.

The order of splits/all.txt is the curriculum the coordinator walks (single-arm pick before stacking and
assembly, the fixed-tool wipe, then the bimanual tasks).
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS = [  # suite, what it is (task-info only; the actor reads the language from get_task_context())
    ("cube_lifting", "Lift"),
    ("cube_stack", "Stack"),
    ("cube_restack", "Stack, restacked"),
    ("nut_assembly", "NutAssemblySquare"),
    ("spill_wipe", "Wipe, fixed tool"),
    ("two_arm_lift", "TwoArmLift, bimanual"),
    ("two_arm_handover", "TwoArmHandover, bimanual"),
]


def main() -> None:
    rows = [{"id": f"robosuite-{suite}", "suite": suite, "task_id": 0} for suite, _ in TASKS]
    # The verifier of each task (ASPIRE verifier-prompt.md): a fresh actor that re-solves the task from the library
    # and the proposals alone, on 5 development seeds in at most 2 attempts, with no held-out run and no findings.
    verifiers = [{"id": f"verify-{r['id']}", "suite": r["suite"], "task_id": 0, "role": "verifier",
                  "debug_seeds": "101-105", "eval_seeds": "none", "max_versions": 2, "search": "debug",
                  "findings": False} for r in rows]
    # The rerun of each task with the final library (ASPIRE §2.4, backward transfer): the same task, the same seeds
    # (agent.params), a fresh actor; its own id so that the test split does not share ids with the training split.
    reruns = [{"id": f"rerun-{r['id']}", "suite": r["suite"], "task_id": 0, "role": "rerun",
               "findings": False} for r in rows]
    (HERE / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows + verifiers + reruns))
    splits = HERE / "splits"
    splits.mkdir(exist_ok=True)
    ids = [r["id"] for r in rows]
    (splits / "all.txt").write_text("\n".join(ids) + "\n")
    (splits / "single_arm.txt").write_text("\n".join(ids[:5]) + "\n")
    (splits / "smoke.txt").write_text("\n".join(ids[:2]) + "\n")
    (splits / "verify.txt").write_text("".join(f"verify-{i}\n" for i in ids))
    (splits / "rerun.txt").write_text("".join(f"rerun-{i}\n" for i in ids))
    (splits / "smoke_rerun.txt").write_text("".join(f"rerun-{i}\n" for i in ids[:2]))
    print(f"{len(rows)} tasks -> {HERE / 'tasks.jsonl'}")


if __name__ == "__main__":
    main()
