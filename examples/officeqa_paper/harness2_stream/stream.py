#!/usr/bin/env python3
"""Write the task stream for data.train: a split in upstream Harness²'s order for a seed (harness2.core.schedule
.stream_order: the sorted ids, shuffled by Random(f"{seed}:{domain}")), one id per line.

    python stream.py --seed 0 --split ../utils/splits/test.txt --out stream_s0.txt [--max 2]
"""
import argparse
from pathlib import Path

from harness2.core.schedule import stream_order

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--seed", type=int, required=True)
parser.add_argument("--split", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--domain", default="default", help="the library domain (the Evolver's, for a task without one)")
parser.add_argument("--max", type=int, default=None, help="only the first N of the stream (a smoke run)")
args = parser.parse_args()
ids = [line.strip() for line in args.split.read_text().splitlines() if line.strip()]
order = [tid for _, tid in stream_order({args.domain: ids}, args.seed, [args.domain])][:args.max]
args.out.write_text("\n".join(order) + "\n")
print(f"{args.out}: {len(order)} of {len(ids)} tasks, seed {args.seed}, first {order[:3]}")
