#!/usr/bin/env python3
"""Write rows/ beside this file: tasks.jsonl (every question's own row of ../utils/tasks.jsonl, then each training
question k times as `<UID>#<j>`, interleaved by trial: every question's first rollout, then every question's second)
and train_k<k>.txt / smoke_k<k>.txt (those trial rows for ../utils/splits/train.txt and, when it is there,
smoke_train.txt). AHE evolves on k rollouts of every question per iteration (the paper's k = 2): a trial row is the
question's own row under the id of its j-th rollout, with `uid` (the question's id, by which the Evolver groups the k
trials: evolver.params.task_field) and `rollout` (j). The validation and test rows are the questions' own, unchanged.

    python examples/officeqa/ahe_adapter/make_rows.py [--k 2]          # after ../utils/prepare.py
"""
import argparse
import json
from pathlib import Path

from hermit.task import read_ids

HERE = Path(__file__).resolve().parent
UTILS = HERE.parent / "utils"


def trials(ids: list[str], k: int) -> list[str]:
    """`<UID>#<j>` for j = 1..k, by trial."""
    return [f"{uid}#{j}" for j in range(1, k + 1) for uid in ids]


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--k", type=int, default=2, help="rollouts of each training question per iteration")
    parser.add_argument("--tasks", type=Path, default=UTILS / "tasks.jsonl")
    parser.add_argument("--splits", type=Path, default=UTILS / "splits")
    parser.add_argument("--out", type=Path, default=HERE / "rows")
    args = parser.parse_args(argv)
    lines = [line for line in args.tasks.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_id = {row["id"]: row for row in map(json.loads, lines)}
    splits = {"train": args.splits / "train.txt", "smoke": args.splits / "smoke_train.txt"}
    names = {}
    for name, path in splits.items():
        if name == "smoke" and not path.is_file():
            continue
        ids = read_ids(path)
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise SystemExit(f"{path} names questions that are not in {args.tasks}: {missing[:5]}")
        names[name] = trials(ids, args.k)
    rows = list(dict.fromkeys(t for ts in names.values() for t in ts))
    args.out.mkdir(parents=True, exist_ok=True)
    with open(args.out / "tasks.jsonl", "w", encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in lines))
        for trial in rows:
            uid, j = trial.rsplit("#", 1)
            f.write(json.dumps({**by_id[uid], "id": trial, "uid": uid, "rollout": int(j)}, ensure_ascii=False) + "\n")
    for name, ts in names.items():
        (args.out / f"{name}_k{args.k}.txt").write_text("".join(t + "\n" for t in ts))
    lists = ", ".join(f"{name}_k{args.k}.txt ({len(ts)})" for name, ts in names.items())
    print(f"wrote {args.out}: tasks.jsonl ({len(lines)} rows + {len(rows)} trial rows, k = {args.k}), {lists}"
          + ("" if "smoke" in names else "; no smoke split"))


if __name__ == "__main__":
    main()
