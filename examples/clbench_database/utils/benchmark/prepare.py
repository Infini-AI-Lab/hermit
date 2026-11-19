#!/usr/bin/env python3
"""Write the tasks (task directories in Harbor's format, tasks/<id>/) and one ordered split per order for the CL-Bench
database venue.

    python prepare.py [--orders 0 1 2 3 4] [--step-size 1] [--clbench ~/code/clbench]

A task is one question of one order's stream: {id, order, position, question_id, block}, its fields but the id under
task.toml's [metadata] (utils/agent/clbench_fields.py reads it back as that row); the stream's state is the host's
(the agent's own loop, one question a call, in its run's directory), and the judge (judge.py) reads the official
task's verdict from the agent's output, so a task has no tests/. `block` is the MWM block
the question belongs to (position // step_size + 1), which names its evidence directory; the run must use the same
`data.step_size` and `data.shuffle: false`, so a Trainer step is exactly one block. The question ids come from the
official task itself (clbench's environment), so a stream mismatch is caught again when a question is played.
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
sys.path.insert(0, '.')
from src.tasks.database_exploration.task import DatabaseExploration
out = {}
for order in json.loads(sys.argv[1]):
    t = DatabaseExploration(schedule='default', run_index=order)
    t.prepare_run(order)
    t.reset()
    out[order] = [str(q['question_id']) for q in t.instances]
    t.evaluate()
print(json.dumps(out))
"""


def write_task(tasks: Path, row: dict) -> Path:
    """One question as a task directory: its fields but the id under [metadata], in the row's order. The host needs
    no network but its model's (the run's sandbox reaches that, and the launcher's meter)."""
    instruction = (f"Answer question {row['question_id']} of CL-Bench's database exploration stream (order "
                   f"{row['order']}, position {row['position']}) as the host of its loop.")
    return write_task_dir(tasks / row["id"], instruction, metadata={k: v for k, v in row.items() if k != "id"},
                          environment={"network_mode": "no-network"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", type=int, nargs="+", default=[0, 1, 2, 3, 4])
    ap.add_argument("--step-size", type=int, default=1)
    ap.add_argument("--clbench", type=Path, default=Path.home() / "code" / "clbench")
    a = ap.parse_args()
    run = subprocess.run([str(a.clbench / ".venv" / "bin" / "python"), "-c", LIST, json.dumps(a.orders)],
                         cwd=str(a.clbench), capture_output=True, text=True, check=True)
    streams = json.loads(run.stdout.strip().splitlines()[-1])
    rows = []
    (HERE / "splits").mkdir(exist_ok=True)
    for order in a.orders:
        ids = []
        for position, qid in enumerate(streams[str(order)]):
            tid = f"o{order}_q{position + 1:03d}"
            rows.append({"id": tid, "order": order, "position": position, "question_id": qid,
                         "block": position // a.step_size + 1})
            ids.append(tid)
        (HERE / "splits" / f"order{order}.txt").write_text("\n".join(ids) + "\n")
    if (HERE / "tasks").exists():                    # this script's own output: rewritten whole
        shutil.rmtree(HERE / "tasks")
    for row in rows:
        write_task(HERE / "tasks", row)
    print(f"{len(rows)} tasks, {len(a.orders)} orders, step size {a.step_size}")


if __name__ == "__main__":
    main()
