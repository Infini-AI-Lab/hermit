#!/usr/bin/env python3
"""Write the tasks (task directories in Harbor's format, tasks/<id>/) and one ordered split per order for the CL-Bench
poker venue.

    python prepare.py [--orders 0 1 2 3 4] [--step-size 5] [--clbench ~/code/clbench]

A task is one hand the HOST plays: {id, order, hand_num, block}, its fields but the id under task.toml's
[metadata] (utils/agent/poker_fields.py reads it back as that row; the stream's state is the host's, in the agent's
own loop, and the judge reads the hand's result from the agent's output, so a task has no tests/).

Natively a hand the opponent ends before the host acts is settled by the task inside the previous hand's last step;
it never reaches the host and does not count toward a learning block (the poker system learns every `learn_every`
hands it observed). Which hands those are is fixed by the deal (each hand restores its pre-generated RNG state
before the opponent's first action), so they are found here by replaying the stream through the task's own resume
path, without a host. `block` is the MWM block (index among the host's hands // step_size + 1); the run must use the
same data.step_size and data.shuffle: false.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

from hermit.task.directory import write_task as write_task_dir

HERE = Path(__file__).resolve().parent

LIST = """
import json, sys
from dataclasses import asdict
sys.path.insert(0, '.')
from src.tasks.exploitable_poker.task import Poker
out = {}
for order in json.loads(sys.argv[1]):
    outcomes, acted = [], []
    while True:
        t = Poker(schedule='default', run_index=order)
        t.prepare_run(order)
        t.reset()
        t.fast_forward(outcomes)
        outcomes = [asdict(o) for o in t.get_instance_outcomes()]
        if t.hands_played >= t.num_instances:
            break
        acted.append({'hand_num': t.hands_played + 1, 'hand_id': t._current_hand_id()})
        outcomes.append({'instance_id': t._current_hand_id(), 'reward': 0.0, 'metadata': {}})
    out[order] = {'hands': acted, 'total': t.num_instances}
print(json.dumps(out))
"""


def write_task(tasks: Path, row: dict) -> Path:
    """One hand as a task directory: its fields but the id under [metadata], in the row's order; no network."""
    instruction = (f"Play hand {row['hand_num']} of CL-Bench's exploitable-poker stream (order {row['order']}) as "
                   "the host.")
    return write_task_dir(tasks / row["id"], instruction, metadata={k: v for k, v in row.items() if k != "id"},
                          environment={"network_mode": "no-network"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", type=int, nargs="+", default=[0, 1, 2, 3, 4])
    ap.add_argument("--step-size", type=int, default=5)
    ap.add_argument("--clbench", type=Path, default=Path.home() / "code" / "clbench")
    a = ap.parse_args()
    run = subprocess.run([str(a.clbench / ".venv" / "bin" / "python"), "-c", LIST, json.dumps(a.orders)],
                         cwd=str(a.clbench), capture_output=True, text=True, check=True)
    streams = json.loads(run.stdout.strip().splitlines()[-1])
    rows = []
    (HERE / "splits").mkdir(exist_ok=True)
    for order in a.orders:
        ids = []
        hands = streams[str(order)]["hands"]
        for k, h in enumerate(hands):
            tid = f"o{order}_h{h['hand_num']:03d}"
            # The harness's hand id names the opponent's policy; it stays out of the task row, which the learning
            # material carries (the learner's record addresses a hand by its number only, as natively).
            rows.append({"id": tid, "order": order, "hand_num": h["hand_num"], "block": k // a.step_size + 1})
            ids.append(tid)
        (HERE / "splits" / f"order{order}.txt").write_text("\n".join(ids) + "\n")
        print(f"order {order}: the host plays {len(hands)} of {streams[str(order)]['total']} hands")
    if (HERE / "tasks").exists():                    # this script's own output: rewritten whole
        shutil.rmtree(HERE / "tasks")
    for row in rows:
        write_task(HERE / "tasks", row)


if __name__ == "__main__":
    main()
