#!/usr/bin/env python3
"""Write rows/: tasks.jsonl (every row of ../utils/tasks.jsonl, then each training question k times as `<id>#<j>`,
interleaved by trial so that every question's first trial comes before any second one) and train_k<k>.txt (those
trial rows; smoke_k<k>.txt, the same for ../utils/splits/smoke_train.txt). RRSI evaluates each candidate on the whole evolve set with k trials per task; the validation and test
rows are the questions' own, unchanged.

    python examples/officeqa/rrsi_adapter/make_rows.py [--k 2]
"""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
UTILS = HERE.parent / "utils"
parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
parser.add_argument("--k", type=int, default=2)
args = parser.parse_args()
rows = [json.loads(l) for l in (UTILS / "tasks.jsonl").read_text().splitlines() if l.strip()]
by_id = {r["id"]: r for r in rows}
train = [l.strip() for l in (UTILS / "splits" / "train.txt").read_text().splitlines() if l.strip()]
out = HERE / "rows"
out.mkdir(exist_ok=True)
trials = [f"{q}#{j}" for j in range(1, args.k + 1) for q in train]
with open(out / "tasks.jsonl", "w") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
    for t in trials:
        f.write(json.dumps({**by_id[t.rsplit("#", 1)[0]], "id": t}, ensure_ascii=False) + "\n")
(out / f"train_k{args.k}.txt").write_text("".join(t + "\n" for t in trials))
smoke = [l.strip() for l in (UTILS / "splits" / "smoke_train.txt").read_text().splitlines() if l.strip()]
(out / f"smoke_k{args.k}.txt").write_text("".join(f"{q}#{j}\n" for j in range(1, args.k + 1) for q in smoke
                                                  if q in train))
print(f"wrote {out}: {len(rows)} rows + {len(trials)} trial rows ({len(train)} questions x k={args.k})")
