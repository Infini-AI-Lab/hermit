"""ASPIRE's own task rows beside a benchmark's: for every task, its verifier. A benchmark of the cap-harness engine
writes only its tasks and their reruns (its utils/benchmark/prepare.py); an ASPIRE run reads them and these, which
this writes into a folder of its own (the ASPIRE configs' `tasks/` and `splits/`), from the benchmark's prepared
tasks and splits, with the engine's task writer (`--engine`: its utils/benchmark/tasks.py, which reads the tasks back
as rows and writes a row as a task directory; agents never import the task layer). The ASPIRE folder's tasks.sh runs
it after the benchmark's prepare.py (the engine's launch.sh):

    python -m hermit.agent.evolver.methods.aspire.rows --benchmark ../utils/benchmark --out . --verify-seeds 101-105 \
        --engine ../../cap_harness/utils/benchmark/tasks.py

- `verify-<id>`: the verifier of each task (ASPIRE verifier-prompt.md): a fresh session that re-solves the task from
  the library and the proposals alone, on its own development seeds (`--verify-seeds`, ASPIRE's first five) in at
  most two programs (the simulator service's attempt budget, `max_programs`), with no findings; the judge scores its
  program on those seeds. Only the coordinator runs them (the configs' `data.val`, through run_agent).

The benchmark's tasks and splits are copied as they are (its reruns are ASPIRE's Phase 3 rerun with the final
library, §2.4), and splits/verify.txt holds every verifier.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import importlib.util
from pathlib import Path
import re
import shutil
import sys

SEEDS = ("debug_seeds", "unseen_seeds", "eval_seeds")


def seeds_of(value) -> set[int]:
    """The seeds a set names: `101-125`, `1,3,5`, or none."""
    out: set[int] = set()
    for part in re.split(r"[,\s]+", str(value or "").strip()):
        if not part or part == "none":
            continue
        low, _, high = part.partition("-")
        out.update(range(int(low), int(high or low) + 1))
    return out


@lru_cache(maxsize=None)
def engine(path: str):
    """The engine's task writer (its utils/benchmark/tasks.py), loaded from its own folder once."""
    module_path = Path(path).resolve()
    if str(module_path.parent) not in sys.path:
        sys.path.insert(0, str(module_path.parent))
    spec = importlib.util.spec_from_file_location(f"_cap_tasks_{abs(hash(str(module_path)))}", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def what(row: dict) -> str:
    return f"{row.get('benchmark') or 'robosuite'} suite `{row['suite']}`, task id {row['task_id']}"


def verifier(row: dict, seeds: str) -> dict:
    base = {k: v for k, v in row.items() if k not in SEEDS}
    return {**base, "id": f"verify-{row['id']}", "role": "verifier", "debug_seeds": seeds, "eval_seeds": seeds,
            "score_label": "verifier", "max_programs": 2, "max_versions": 2, "search": "debug", "findings": False}


def write(benchmark: Path, out: Path, verify_seeds: str, engine_tasks: Path) -> None:
    eng = engine(str(engine_tasks))
    rows = eng.rows(benchmark / "tasks")
    order = (benchmark / "splits" / "all.txt").read_text().split()       # the benchmark's own order of its tasks
    tasks = {tid: rows[tid] for tid in order}
    for row in tasks.values():
        clash = seeds_of(verify_seeds) & seeds_of(row.get("eval_seeds"))
        if clash:
            raise ValueError(f"{row['id']}: the verifier's seeds overlap the held-out seeds: {sorted(clash)[:10]}")
    for name in ("tasks", "splits"):                # this script's own output: rewritten whole
        if (out / name).exists():
            shutil.rmtree(out / name)
    shutil.copytree(benchmark / "tasks", out / "tasks")
    for row in tasks.values():
        text = f"Verify the proposals for {what(row)}: solve it again from the library and the proposals alone."
        eng.write_task(out / "tasks", verifier(row, verify_seeds), text)
    shutil.copytree(benchmark / "splits", out / "splits")
    (out / "splits" / "verify.txt").write_text("".join(f"verify-{i}\n" for i in tasks))
    print(f"{len(tasks)} verifiers -> {out / 'tasks'}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--benchmark", type=Path, required=True, help="the benchmark's utils/benchmark (tasks/, splits/)")
    ap.add_argument("--out", type=Path, required=True, help="where tasks/ and splits/ go")
    ap.add_argument("--verify-seeds", required=True, help="the verifier's development seeds (ASPIRE's first five)")
    ap.add_argument("--engine", type=Path, required=True, help="the engine's task writer (utils/benchmark/tasks.py)")
    a = ap.parse_args(argv)
    write(a.benchmark, a.out, a.verify_seeds, a.engine)


if __name__ == "__main__":
    main()
