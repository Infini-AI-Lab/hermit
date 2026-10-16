#!/usr/bin/env python3
"""Play ONE CL-Bench database question with the ported metaworld host over a Harness Adapter.

Run by the task's pipeline in clbench's own environment:

    <clbench>/.venv/bin/python database_runner.py --task '<json>' --adapter DIR --workdir DIR --model MODEL

The question is the official task's: the task is built as `clbench run database_exploration --schedule default
--task.run-index <order>` builds it, re-enters the stream at the question's position through the task's own resume
path (fast_forward, which also applies the schema drift when the position is past it), and is played by
clbench's runner until that one question is graded. The host is `host.AdapterHost`: a fresh bare API call per
question with native tools, the adapter's tools beside the task's, the per-episode wm call cap, the adapter's
ADAPTER.md as the system prompt.

Afterwards the question's turns go into the run's evidence store in the native layout (blockNNN/qNNN_id<id>.json
and the per-question entry with its /evidence address), and the database the question was played on is registered
for the learner's sql channel. One JSON line on stdout reports the result.

Differences from one long native process, recorded: the task's temporary database copy is fresh per question (a
host write would not carry to the next question; native hosts issue reads), and the task's question history before
the position holds placeholders (it feeds only the task's final metrics, which this pipeline does not use).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
TASKS = HERE.parent                                     # <task>/utils; the method folders sit beside utils/


def _paths(clbench: Path, framework: Path) -> None:
    for p in (str(HERE), str(framework), str(clbench)):
        if p not in sys.path:
            sys.path.insert(0, p)


def _register(workdir: Path, db: Path) -> None:
    """The database the learner's query channel reads (the task's variant may name it relative to clbench)."""
    pointer = workdir / "current_db"
    tmp = pointer.with_suffix(".tmp")
    tmp.write_text(str(Path(db).resolve()))
    os.replace(tmp, pointer)


def register_first(order: int, workdir: Path) -> None:
    """Before the bootstrap session: register the database the stream's first question is played on (natively the
    host registered it on the first query, before the bootstrap ran)."""
    from src.tasks.database_exploration import task as dbtask
    task = dbtask.DatabaseExploration(schedule="default", run_index=order)
    task.prepare_run(order)
    _register(workdir, Path(task.db_path))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--register-first", type=int, metavar="ORDER",
                    help="only register the first question's database for ORDER, then exit")
    ap.add_argument("--task", help="the task row (JSON)")
    ap.add_argument("--adapter")
    ap.add_argument("--workdir", required=True, help="the run's task directory (evidence/, host/, current_db)")
    ap.add_argument("--model")
    ap.add_argument("--clbench", required=True)
    ap.add_argument("--framework", required=True)
    args = ap.parse_args()
    _paths(Path(args.clbench), Path(args.framework))
    os.chdir(args.clbench)
    if args.register_first is not None:
        return register_first(args.register_first, Path(args.workdir))
    logging.basicConfig(level=logging.WARNING)

    from src.runtime.runner import run_task
    from src.tasks.database_exploration import task as dbtask
    from host import AdapterHost
    from harnessadapter import Session
    from harnessadapter.algorithms.evolvers.mwm.evidence import clbench as clbench_evidence, readme as evidence_readme

    row = json.loads(args.task)
    order, position = int(row["order"]), int(row["position"])
    workdir = Path(args.workdir)
    evidence_dir = workdir / "evidence"
    host_ws = workdir / "host"
    host_ws.mkdir(parents=True, exist_ok=True)
    from harnessadapter.algorithms.evolvers.mwm import deployment as _dep
    venue_texts = TASKS / "task-info" / "mwm"
    deployment = _dep.host_settings(venue_texts)

    task = dbtask.DatabaseExploration(schedule="default", run_index=order)
    task.prepare_run(order)
    session = Session(Path(args.adapter), evidence=evidence_dir, deployment=deployment or None)
    system = AdapterHost(session, model=args.model, workspace=host_ws, venue="database", name="mwm_framework")
    system.reset()
    task.reset()
    placeholders = [{"instance_id": q["question_id"], "success": False, "metadata": {}}
                    for q in task.instances[:position]]
    task.fast_forward(placeholders)
    question = task.instances[position]
    if str(question["question_id"]) != str(row["question_id"]):
        raise SystemExit(f"stream mismatch: position {position} is {question['question_id']}, "
                         f"the task row says {row['question_id']}")
    drifted = bool(getattr(task, "_drift_applied", False))
    original_step = task.step

    def one_question(response):
        result = original_step(response)
        if result.instance_outcome is not None:
            result.done = True
            result.next_query = None
        return result

    task.step = one_question
    t0 = time.time()
    run_task(task, system, show_progress=False, reset_system=False, initial_query=task.build_current_query())
    wall = time.time() - t0

    qs = clbench_evidence.group_questions(system._pending)
    if len(qs) != 1:
        raise SystemExit(f"expected one question's turns, found {len(qs)}")
    q = qs[0]
    evidence_readme.write(evidence_dir, (venue_texts / "contract" / "env_evidence.md").read_text())
    paths = clbench_evidence.write_raw(qs, str(evidence_dir), int(row["block"]))
    entry = clbench_evidence.layer1(q, paths[q["question_num"]])

    _register(workdir, dbtask.DEFAULT_DRIFTED_DB_PATH if drifted else Path(task.db_path))

    outcome = task.get_instance_outcomes()[-1]
    cost = [json.loads(l) for l in (host_ws / "cost.jsonl").read_text().splitlines() if l.strip()] \
        if (host_ws / "cost.jsonl").exists() else []
    last = cost[-1] if cost and cost[-1].get("role") == "host" else {}
    tokens = {k: int(last.get(k) or 0) for k in ("input_tokens", "cached_input_tokens", "output_tokens")}
    print(json.dumps({"answer": q["final_answer"], "verdict": q["verdict"], "success": bool(outcome.success),
                      "reward": outcome.reward, "question_num": q["question_num"], "question_id": q["question_id"],
                      "queries_used": q["queries_used"], "raw_path": paths[q["question_num"]], "entry": entry,
                      "drifted": drifted, "tokens": tokens, "wall_s": round(wall, 1)}))


if __name__ == "__main__":
    main()
