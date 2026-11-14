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
    # Left as it is when it holds these pages and nothing else (a running launch reads it); otherwise made anew:
    # nothing of an earlier run's or of another commit, and no entry that is not one of the pages
    entries = list(a.out.iterdir()) if a.out.is_dir() and not a.out.is_symlink() else None
    same = entries is not None and all(p.is_file() and not p.is_symlink() for p in entries) and \
        {p.name: p.read_bytes() for p in entries} == {name: text.encode() for name, text in pages.items()}
    if not same:
        if a.out.is_symlink() or a.out.is_file():
            a.out.unlink()
        shutil.rmtree(a.out, ignore_errors=True)
        a.out.mkdir(parents=True, exist_ok=True)
        for name, text in pages.items():
            (a.out / name).write_text(text)
    print(f"{len(pages)} pages -> {a.out}")


if __name__ == "__main__":
    main()
