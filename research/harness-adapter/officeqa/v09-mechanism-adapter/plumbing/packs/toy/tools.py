"""Toy pack CLI `lookup`: print the cells of matching table rows, each with a machine-readable tag.

    lookup widgets_2020_a.txt North
    ⟦toy widgets_2020_a.txt:5|Q1 2020|North|120.5⟧
    ...
Tag grammar: ⟦toy <file>:<line>|<period>|<row>|<raw>⟧ (raw as printed, 'r' = revised).
"""
from __future__ import annotations

import argparse
import sys

try:
    from .corpus import FILES, render
except ImportError:                      # run as a plain script by the kit wrapper: python -I -B .../packs/toy/tools.py
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from corpus import FILES, render  # type: ignore[no-redef]

TAG_OPEN = '⟦toy '


def tag(cell) -> str:
    return f'{TAG_OPEN}{cell.file}:{cell.line}|{cell.period}|{cell.row}|{cell.raw}⟧'


def lookup_text(file: str, rows: list[str] | None = None, table: str | None = None) -> str:
    if file not in FILES:
        return f'lookup: no such file {file}; files: {", ".join(FILES)}'
    text, cells = render(file)
    wanted = [r.lower() for r in rows or []]
    out, current = [], None
    for c in cells:
        if (wanted and c.row.lower() not in wanted) or (table and c.table != table):
            continue
        if c.table != current:
            current = c.table
            title = next(ln for ln in text.split('\n') if ln.startswith(f'Table {c.table}.'))
            out.append(title)
        out.append(tag(c))
    return '\n'.join(out) if out else 'lookup: no matching rows'


def lookup_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='lookup', description='Print tagged cells of a toy bulletin.')
    ap.add_argument('file')
    ap.add_argument('rows', nargs='*')
    ap.add_argument('--table')
    args = ap.parse_args(argv)
    print(lookup_text(args.file, args.rows, args.table))
    return 0


if __name__ == '__main__':
    sys.exit(lookup_main())
