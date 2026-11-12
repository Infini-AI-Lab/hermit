"""ASPIRE's docs as the actors may read them, written outside its checkout: the task sandbox is shown these
(environment.read, agent.params.aspire_docs), never the checkout's own pages, which name ASPIRE's worked programs
per task. launch.sh runs it on the host before a run:

    python stage_docs.py --aspire-root $ASPIRE_ROOT --tasks utils/tasks.jsonl --out utils/aspire-docs
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sim  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aspire-root", type=Path, required=True)
    ap.add_argument("--tasks", type=Path, required=True, help="the task rows: their benchmarks' pages are staged")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    rows = [json.loads(line) for line in a.tasks.read_text().splitlines() if line.strip()]
    pages = {}
    for benchmark in sorted({str(r.get("benchmark") or "robosuite") for r in rows}):
        pages.update(sim.workspace_docs(a.aspire_root, benchmark))
    staged = {p.name: p.read_text() for p in a.out.iterdir() if p.is_file()} if a.out.is_dir() else None
    if staged != pages:                 # left as it is when it is already these pages: a running launch reads it
        shutil.rmtree(a.out, ignore_errors=True)             # nothing of an earlier run's, of another commit
        a.out.mkdir(parents=True)
        for name, text in pages.items():
            (a.out / name).write_text(text)
    print(f"{len(pages)} pages -> {a.out}")


if __name__ == "__main__":
    main()
