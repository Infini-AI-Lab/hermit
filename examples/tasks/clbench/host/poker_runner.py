#!/usr/bin/env python3
"""Play ONE CL-Bench poker hand with the native metaworld host seat over a Harness Adapter.

Run by the poker pipeline in clbench's own environment:

    <clbench>/.venv/bin/python poker_runner.py --task '<json>' --adapter DIR --workdir DIR --model MODEL ...

The task is built as `clbench run exploitable_poker --schedule default --task.run-index <order>` builds it and
re-enters the stream through its own resume path (fast_forward) with the outcomes of every hand already graded in
this run ($workdir/outcomes.jsonl), so the stage, the opponent and the running profit in the prompts are the live
ones. A hand the opponent ends before the host acts is settled by the task without the host, exactly as natively:
it is folded into the previous hand's final observation and never reaches the host or counts toward a block. So a
task is one hand the host plays (prepare.py lists them); the hands settled on the way are recorded here too.

The host is the old port's HarnessAdapterPokerSystem without its learner: metaworld's poker seat over the adapter's
package. Afterwards the hand's turns go into the evidence store in the native layout (blockNNN/hNNN.json and the
per-hand entry), and every outcome the task produced is appended to outcomes.jsonl. One JSON line on stdout.
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
TASKS = HERE.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--clbench", required=True)
    ap.add_argument("--metaworld", required=True)
    ap.add_argument("--framework", required=True)
    args = ap.parse_args()
    for p in (str(TASKS), str(HERE), args.framework, args.metaworld, args.clbench):
        if p not in sys.path:
            sys.path.insert(0, p)
    os.chdir(args.clbench)
    logging.basicConfig(level=logging.WARNING)

    from src.interface import Query, Response
    from src.runtime.runner import run_task
    from src.systems.metaworld_poker.system import MetaWorldPokerSystem
    from src.systems.utils import native_tools
    from src.tasks.exploitable_poker.task import Poker
    from adapter_iface import AdapterIface
    from clb import evidence_readme, poker_evidence

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
    deployment = json.loads((TASKS / "poker" / "task-info" / "deployment" / "host.json").read_text())

    class MwmPokerHost(MetaWorldPokerSystem):
        """The old port's poker host (tasks/CLBench/host/poker_system.py), without the learner and the bootstrap."""

        def __init__(self):
            placeholder = host_ws / f"wrepo_{host_ws.name}"
            placeholder.mkdir(exist_ok=True)
            (placeholder / "README.md").write_text(
                "placeholder: this run's world model is the Harness Adapter served by the framework\n")
            super().__init__(model=args.model, learn_every=10 ** 9, name="mwm_framework_poker",
                             workspace=str(host_ws), meta_backend="none", scenario="poker")
            self.wrepo = Path(args.adapter)
            self.evidence_dir = evidence_dir

        def respond(self, query: Query) -> Response:
            self._interaction_count += 1
            if query.instance_id != self._current_instance:
                self._end_sub_on_episode_end()
                self._current_instance = query.instance_id
                self._messages = []
                self._delegates_used = 0
                self._wm_calls = 0
                self._pending_call_id = None
            self._seat_messages().append({"role": "user", "content": query.prompt or "(no content)"})
            try:
                turn = self._seat_turn(query, native_tools)
            except Exception as exc:                       # noqa: BLE001
                logging.warning("host call exhausted retries (%s); cooling down 120s", exc)
                time.sleep(120)
                turn = self._seat_turn(query, native_tools)
            self._pending_call_id = turn.pending_call_id
            return Response(action=turn.action, metadata={
                "system_type": "harness_adapter_poker", "architecture": "mwm", "model": self.model,
                "interaction_count": self._interaction_count, "channel_actions": self._wm_calls})

        @property
        def _iface(self):
            if self._iface_obj is None:
                self._iface_obj = AdapterIface(self.wrepo, self.evidence_dir, deployment)
            return self._iface_obj

        @staticmethod
        def _transport_note() -> str:
            return ""

        def _learn(self) -> None:
            return

    task = Poker(schedule="default", run_index=order)
    task.prepare_run(order)
    system = MwmPokerHost()
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
    evidence_readme.write(str(evidence_dir), "poker")
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
                      "raw_path": paths[hand_num], "entry": entry, "tokens": tokens, "wall_s": round(wall, 1)}))


if __name__ == "__main__":
    main()
