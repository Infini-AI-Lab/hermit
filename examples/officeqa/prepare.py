#!/usr/bin/env python3
"""Write tasks.jsonl and the splits from an OfficeQA prepared directory (questions only; answers stay private).

    python prepare.py /path/to/officeqa_official_133/prepared
Splits are deterministic (seed 0): 60 train, 13 validation, 60 test; smoke splits: 4 train, 2 validation, 2 test;
try splits: 12 train, 10 test.
"""
import json
from pathlib import Path
import random
import sys

prepared = Path(sys.argv[1]).resolve()
here = Path(__file__).resolve().parent
rows = [json.loads(l) for l in (prepared / "agent" / "tasks.jsonl").read_text().splitlines() if l.strip()]
with open(here / "tasks.jsonl", "w") as f:
    for r in rows:
        f.write(json.dumps({"id": r["task_id"], "question": r["question"]}, ensure_ascii=False) + "\n")
ids = sorted(r["task_id"] for r in rows)
random.Random(0).shuffle(ids)
splits = {"train": ids[:60], "val": ids[60:73], "test": ids[73:]}
(here / "splits").mkdir(exist_ok=True)
for name, part in splits.items():
    (here / "splits" / f"{name}.txt").write_text("\n".join(part) + "\n")
(here / "splits" / "smoke_train.txt").write_text("\n".join(ids[:4]) + "\n")
(here / "splits" / "smoke_val.txt").write_text("\n".join(ids[60:62]) + "\n")
(here / "splits" / "smoke_test.txt").write_text("\n".join(ids[73:75]) + "\n")
(here / "splits" / "try_train.txt").write_text("\n".join(ids[:12]) + "\n")
(here / "splits" / "try_test.txt").write_text("\n".join(ids[73:83]) + "\n")
(here / "data_root.txt").write_text(str(prepared) + "\n")
print({k: len(v) for k, v in splits.items()})
