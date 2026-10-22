#!/usr/bin/env python3
"""stateful-batch-planner demo: propagation requests vs per-seed-bank allowances.

Evaluates a batch of propagation requests IN SUBMISSION ORDER against
monthly propagation-bench allowances that each accepted request
consumes. Accepted requests carry the exact write arguments to
copy. One unparseable item refuses the whole batch — order-dependent
counts past the gap would be wrong.

Usage: python3 run.py sample_data/batch.jsonl sample_data/allowances.json
"""

import json
import sys


def load_json(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return None


def parse_batch(path):
    """All-or-nothing parse: returns (items, None) or (None, bad_line_no)."""
    items = []
    for n, line in enumerate(open(path), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            items.append((n, item["seedbank"], item["ref"], item["bench"]))
        except (ValueError, KeyError, TypeError):
            return None, n
    return items, None


def plan(batch_path, allowance_path):
    cfg = load_json(allowance_path)
    if cfg is None:
        print("CATCH: allowance input unparseable; refusing to plan")
        return None
    items, bad = parse_batch(batch_path)
    if items is None:
        print(f"CATCH: item at line {bad} unparseable; batch refused "
              "(running counts past the gap would be wrong)")
        return None
    allowances = cfg["propagation_allowance"]
    used = dict(cfg.get("already_used", {}))
    out = []
    for i, (_, seedbank, ref, bench) in enumerate(items, 1):
        limit = allowances.get(seedbank)
        if limit is None:
            out.append(f"CATCH: item {i} {seedbank} refused - "
                       "no propagation allowance on file for this seed bank")
            continue
        n = used.get(seedbank, 0)
        if n + 1 > limit:
            out.append(f"CATCH: item {i} {seedbank} refused - allowance "
                       f"{n}/{limit} already consumed (earlier items count)")
            continue
        used[seedbank] = n + 1
        out.append(
            f"PLAN|{i}|{seedbank}|ACCEPT|used={n + 1}/{limit}|"
            f"write=book_bench(seedbank='{seedbank}', ref='{ref}', "
            f"bench='{bench}', month='{cfg['month']}')")
    for line in out:
        print(line)
    return out


if __name__ == "__main__":
    args = sys.argv[1:] or ["sample_data/batch.jsonl",
                            "sample_data/allowances.json"]
    sys.exit(0 if plan(*args) is not None else 1)
