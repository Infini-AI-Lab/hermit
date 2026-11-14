"""Write the LIBERO-Pro tasks of ASPIRE's harness (`cap-harness --benchmark libero-pro`: the eighty (suite, task)
pairs ASPIRE's harness requires) as hermit tasks: one row per pair with its seeds, which the simulator service and the judge
take from the row (ASPIRE's LIBERO defaults: development 51-65, unseen-check 66-80, held-out 1-50; every run with
`--init-mode seeded`, which the engine sets, so that a seed is a scene of its own).

    python prepare.py                                  # ASPIRE's seeds
    python prepare.py --preset smoke                   # the smoke's: three development seeds, one unseen, three held-out
    # tasks.jsonl, splits/{all,long,<suite>,smoke}.txt, their *_rerun.txt (rerun.txt for all), verify.txt

LIBERO-Pro perturbs LIBERO's Goal, Object, Spatial and Long (libero_10) suites; ASPIRE runs two
perturbations of each, ten tasks a suite: `_swap` (the objects' positions exchanged) and `_task` (the instruction
and goal changed, so a task's name no longer states it: its language is the one `get_task_context()` gives).
splits/long.txt is LIBERO-Long-Pro: `libero_10_swap` and `libero_10_task`. A `_swap` and a `_task` suite share
their task ids, so a row's id carries the suite. The rows, the verifier's and the reruns are written by the Robosuite
example's prepare.py (`write`), whose engine this example runs on.
"""
from __future__ import annotations

from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "aspire_robosuite" / "utils"))
from prepare import seeds_from, write  # noqa: E402

BENCHMARK = "libero-pro"
FAMILIES = ("object", "spatial", "goal", "10")           # the curriculum's order; `10` is Long
SUITES = [f"libero_{family}_{kind}" for family in FAMILIES for kind in ("swap", "task")]
TASKS_PER_SUITE = 10
PRESETS = {"aspire": {"dev": "51-65", "unseen": "66-80", "eval": "1-50", "verify": "51-55"},
           "smoke": {"dev": "51-53", "unseen": "66", "eval": "1-3", "verify": "51-55"}}


def main() -> None:
    seeds, verify = seeds_from(PRESETS)
    # `t`: a task id is not read as the hex suffix of a rollout directory's name (the simulator service matches it)
    tasks = [{"id": f"{suite}-t{n:02d}", "benchmark": BENCHMARK, "suite": suite, "task_id": n}
             for suite in SUITES for n in range(TASKS_PER_SUITE)]
    of = lambda *suites: [t["id"] for t in tasks if t["suite"] in suites]
    ids = [t["id"] for t in tasks]
    write(HERE, tasks, seeds, verify, {"all": ids, "long": of("libero_10_swap", "libero_10_task"),
                                       **{suite: of(suite) for suite in SUITES}, "smoke": ids[:2]})


if __name__ == "__main__":
    main()
