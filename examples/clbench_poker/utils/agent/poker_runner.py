#!/usr/bin/env python3
"""Play ONE CL-Bench poker hand with the ported metaworld host over a Harness Adapter.

Run by the poker pipeline in clbench's own environment:

    <clbench>/.venv/bin/python poker_runner.py --task '<json>' --adapter DIR --workdir DIR --model MODEL ...

The task is built as `clbench run exploitable_poker --schedule default --task.run-index <order>` builds it and
re-enters the stream through its own resume path (fast_forward) with the outcomes of every hand already graded in
this run ($workdir/outcomes.jsonl), so the stage, the opponent and the running profit in the prompts are the live
ones. A hand the opponent ends before the host acts is settled by the task without the host, exactly as natively:
it is folded into the previous hand's final observation and never reaches the host or counts toward a block. So a
task is one hand the host plays (prepare.py lists them); the hands settled on the way are recorded here too.

The host is `host.AdapterHost` over the adapter, its context for the hand (ADAPTER.md, plus what its context tool
renders for the hand's first prompt when it has one: ReHarness's retrieved reflections) as the system prompt.
Afterwards the hand's turns go into the evidence store in the native layout (blockNNN/hNNN.json and the per-hand
entry), and every outcome the task produced is appended to outcomes.jsonl. One JSON line on stdout, with the context
tool's whole result (`context_tool`, null without such a tool) for the learning material.
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--clbench", required=True)
    ap.add_argument("--framework", required=True)
    args = ap.parse_args()
    for p in (str(HERE), args.framework, args.clbench):
        if p not in sys.path:
            sys.path.insert(0, p)
    os.chdir(args.clbench)
    logging.basicConfig(level=logging.WARNING)

    from src.runtime.runner import run_task
    from src.tasks.exploitable_poker.task import Poker
    from host import AdapterHost
    from hermit import Session
    from hermit.algorithms.evolvers.mwm.evidence import poker as poker_evidence, readme as evidence_readme

    row = json.loads(args.task)
    order, hand_num = int(row["order"]), int(row["hand_num"])
    workdir = Path(args.workdir)
    evidence_dir = workdir / "evidence"
    host_ws = workdir / "host"
    host_ws.mkdir(parents=True, exist_ok=True)
    ledger = workdir / "outcomes.jsonl"
    graded = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()] if ledger.exists() else []
    if len(graded) > hand_num - 1:
        # Replaying a hand (the Trainer resumed a step that had not completed): the stream is strictly ordered, so
        # every outcome from this hand on is stale. The ledger holds one line per hand, in stream order.
        graded = graded[:hand_num - 1]
        ledger.write_text("".join(json.dumps(o) + "\n" for o in graded))
    from hermit.algorithms.evolvers.mwm import deployment as _dep
    venue_texts = TASKS / "task-info" / "mwm"
    deployment = _dep.host_settings(venue_texts)

    task = Poker(schedule="default", run_index=order)
    task.prepare_run(order)
    session = Session(Path(args.adapter), evidence=evidence_dir, deployment=deployment or None)
    system = AdapterHost(session, model=args.model, workspace=host_ws, venue="poker", name="mwm_framework_poker",
                         task_row=row)
    system.reset()
    task.reset()
    task.fast_forward(graded)
    if task.hands_played + 1 != hand_num:
        raise SystemExit(f"stream mismatch: the next hand the host plays is {task.hands_played + 1}, "
                         f"the task row says {hand_num} (outcomes.jsonl holds {len(graded)} hands)")
    original_step = task.step

    def one_hand(response):
        result = original_step(response)
        if result.instance_outcome is not None:
            result.done = True
            result.next_query = None
        return result

    task.step = one_hand
    t0 = time.time()
    run_task(task, system, show_progress=False, reset_system=False, initial_query=task.build_current_query())
    wall = time.time() - t0

    hands = poker_evidence.group_hands(system._pending)
    if len(hands) != 1 or hands[0]["hand_num"] != hand_num:
        raise SystemExit(f"expected the turns of hand {hand_num}, found {[h['hand_num'] for h in hands]}")
    evidence_readme.write(evidence_dir, (venue_texts / "contract" / "env_evidence.md").read_text())
    paths = poker_evidence.write_raw(hands, str(evidence_dir), int(row["block"]))
    entry = poker_evidence.one_hand(hands[0], paths[hand_num])

    from dataclasses import asdict
    new = [asdict(o) for o in task.get_instance_outcomes()[len(graded):]]
    with open(ledger, "a") as f:
        for o in new:
            f.write(json.dumps(o, default=str) + "\n")
    cost = [json.loads(l) for l in (host_ws / "cost.jsonl").read_text().splitlines() if l.strip()] \
        if (host_ws / "cost.jsonl").exists() else []
    last = cost[-1] if cost and cost[-1].get("role") == "host" else {}
    tokens = {k: int(last.get(k) or 0) for k in ("input_tokens", "cached_input_tokens", "output_tokens")}
    # The task's outcome list is in stream order: index hand_num - 1 is the host's hand; every other new one was
    # settled by the task without the host (before it, at re-entry, or chained after it).
    mine = hand_num - 1 - len(graded)
    settled = [o for k, o in enumerate(new) if k != mine]
    print(json.dumps({"hand_num": hand_num, "delta": hands[0]["delta"], "reward_bb": float(new[mine]["reward"]),
                      "settled_without_host": [o["instance_id"] for o in settled],
                      "settled_reward_bb": sum(float(o["reward"]) for o in settled),
                      "raw_path": paths[hand_num], "entry": entry, "context_tool": system.context_info,
                      "tokens": tokens, "wall_s": round(wall, 1)}))


if __name__ == "__main__":
    main()
