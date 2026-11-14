"""Write LIBERO's tasks (`cap-harness --benchmark libero-pro`, the suites of the LIBERO it pins) as hermit tasks: one
row per (suite, task id) with its seeds, which the simulator service and the judge take from the row (ASPIRE's
LIBERO defaults: development 51-65, unseen-check 66-80, held-out 1-50; every run with `--init-mode seeded`, which
the engine sets, so that a seed is a scene of its own).

    python prepare.py                                  # ASPIRE's seeds
    python prepare.py --preset smoke                   # the smoke's: three development seeds, one unseen, three held-out
    # tasks.jsonl, splits/{all,libero,libero_90,libero_pro,libero_pro_long,<suite>,smoke,smoke_pro}.txt,
    # their *_rerun.txt (rerun.txt for all), verify.txt

LIBERO's own five suites (Liu et al., 2023): `libero_object`, `libero_spatial`, `libero_goal` and `libero_10`
(LIBERO-Long), ten tasks each, which splits/libero.txt holds in that order, and `libero_90` (splits/libero_90.txt).

LIBERO-Pro's perturbations of the four ten-task suites, the eighty (suite, task) pairs ASPIRE's
harness requires: `_swap` (the objects' positions exchanged) and `_task` (the instruction and goal changed, so a
task's name no longer states it: its language is the one `get_task_context()` gives); splits/libero_pro.txt, and
splits/libero_pro_long.txt for LIBERO-Long-Pro (`libero_10_swap`, `libero_10_task`). A suite and its perturbations
share task ids, so a row's id carries the suite.

The rows, the verifier's and the reruns are written by the Robosuite example's prepare.py (`write`), whose engine
this benchmark runs on.
"""
from __future__ import annotations

from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "aspire_robosuite" / "utils"))
from prepare import seeds_from, write  # noqa: E402

BENCHMARK = "libero-pro"                 # cap-harness's name for its LIBERO adapter
SUITES = {"libero_object": 10, "libero_spatial": 10, "libero_goal": 10, "libero_10": 10, "libero_90": 90}
PRO_FAMILIES = ("object", "spatial", "goal", "10")       # the curriculum's order; `10` is Long
PRO_SUITES = {f"libero_{family}_{kind}": 10 for family in PRO_FAMILIES for kind in ("swap", "task")}
PRESETS = {"aspire": {"dev": "51-65", "unseen": "66-80", "eval": "1-50", "verify": "51-55"},
           "smoke": {"dev": "51-53", "unseen": "66", "eval": "1-3", "verify": "51-55"}}


def main() -> None:
    seeds, verify = seeds_from(PRESETS)
    # `t`: a task id is not read as the hex suffix of a rollout directory's name (the simulator service matches it)
    tasks = [{"id": f"{suite}-t{n:02d}", "benchmark": BENCHMARK, "suite": suite, "task_id": n}
             for suite, count in {**SUITES, **PRO_SUITES}.items() for n in range(count)]
    of = lambda *suites: [t["id"] for t in tasks if t["suite"] in suites]
    ids = [t["id"] for t in tasks]
    original = of(*[s for s in SUITES if s != "libero_90"])
    pro = of(*PRO_SUITES)
    write(HERE, tasks, seeds, verify,
          {"all": ids, "libero": original, "libero_90": of("libero_90"), "libero_pro": pro,
           "libero_pro_long": of("libero_10_swap", "libero_10_task"),
           **{suite: of(suite) for suite in (*SUITES, *PRO_SUITES) if suite != "libero_90"},
           "smoke": original[:2], "smoke_pro": pro[:2]})


if __name__ == "__main__":
    main()
