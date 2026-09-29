"""Collect the manifest-tool invocations of the v9 pilots (task R, step 1).

    python -B w0replay/collect.py [--out DIR]

Reads every shell line of the four pilots (b/b2 ledgers, a/c event logs), splits it conservatively into
direct find_tables / show_table / printings calls (calc and submit are skipped), keeps the calls that parse
with at least one tree's argparse, de-duplicates on argv and writes `<out>/invocations.json` plus a short
`<out>/collect-stats.json`.  Nothing is executed here.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402


def gather() -> tuple:
    """(invocations keyed by argv, stats) over every record of every cell."""
    inv, stats = collections.OrderedDict(), collections.Counter()
    for r in C.iter_records():
        stats["shell lines"] += 1
        stats[f"shell lines {r.cell}"] += 1
        head = r.raw_cmd.split(None, 1)[0] if r.raw_cmd.strip() else ""
        if head in C.SKIP_TOOLS:
            stats["skipped calc/submit"] += 1
            continue
        found, reason = C.tool_argvs(r.raw_cmd)
        if reason:
            stats[f"refused: {reason}"] += 1
        if not found:
            continue
        stats["shell lines with a tool call"] += 1
        for tool, args, expanded in found:
            stats["tool calls (with repeats)"] += 1
            stats[f"tool calls {tool}"] += 1
            key = (tool, tuple(args))
            if key not in inv:
                inv[key] = C.Invocation(tool, list(args), expanded=expanded)
            inv[key].sources.append((r.cell, r.ordinal, r.task_id, r.correct, r.call, r.raw_cmd[:300]))
    return inv, stats


def filter_parsable(inv: dict, stats) -> list:
    """Keep the invocations that parse with the v9 or the v10 argparse; record which."""
    v9, v10 = C.load_cli(C.V9_CLI), C.load_cli(C.V10_CLI)
    kept = []
    for x in inv.values():
        p9, p10 = C.parses(v9, x.tool, x.args), C.parses(v10, x.tool, x.args)
        if not (p9 or p10):
            stats["dropped: parses with neither argparse"] += 1
            continue
        stats["unique invocations"] += 1
        stats[f"unique {x.tool}"] += 1
        if p10 and not p9:
            stats["parses only with v10 argparse"] += 1
        if p9 and not p10:
            stats["parses only with v9 argparse"] += 1
        if x.right:
            stats["unique from a RIGHT cell"] += 1
        kept.append({"tool": x.tool, "args": x.args, "parses_v9": p9, "parses_v10": p10, "expanded": x.expanded,
                     "right": x.right, "sources": x.sources})
    return kept


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", default=str(C.SCRATCH))
    a = p.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    inv, stats = gather()
    kept = filter_parsable(inv, stats)
    (out / "invocations.json").write_text(json.dumps(kept, indent=1))
    (out / "collect-stats.json").write_text(json.dumps(dict(sorted(stats.items())), indent=1))
    for k, v in sorted(stats.items()):
        print(f"{v:6d}  {k}")
    print(f"wrote {out / 'invocations.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
