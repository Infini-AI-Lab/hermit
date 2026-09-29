"""Command-line entry points of the OfficeQA table tools (L1): find_tables, show_table, printings.

Paths come from the environment: OFFICEQA_CORPUS (directory of treasury_bulletin_*.txt) and TABLES_INDEX
(the read-only tables.sqlite built by `build`).  Each entry point takes argv (default sys.argv[1:]), prints
exactly the page text of otables.Tables and returns an exit code (0 ok, 2 usage/lookup error), so the kit can
wrap it as `bin/<tool>`:

  find_tables "judicial branch" --years 2003-2004 [--title-only] [--all] [--limit 25]   (--rows is the default)
  show_table treasury_bulletin_2004_03.txt 728 [--stitch] [--cols 1-3] [--match gsa] [--grep Jan [--grep Feb]]
             [--period FY2021] [--long] [--raw] [--from-row N] [--max-rows N] [--budget 8000]
  printings --table FFO-3 --row 2003-01 [--col "general services"] [--period ...] [--years 2003-2006]
            [--section "budget receipts"] [--all-periods]
  python -m packs.officeqa.tools.cli build --corpus DIR --out tables.sqlite  (prepare time, not for actors)

Stdlib only; startup is an import of otables (no index access until a command needs it).  The same
argument parsers are used by observe.py to re-derive what the model saw, so they must stay here
(v10 W0: every option added here is accepted by observe.py's re-runs through `parser`/`page`).
This directory (packs/officeqa/tools/) holds only the actor-side code: the kit grants cli.py + otables.py
(its sibling-import closure) and a listing of this directory, nothing else of the pack.
"""
from __future__ import annotations

import argparse
import sys

try:
    from . import otables
except ImportError:                      # run as a plain script: python .../packs/officeqa/tools/cli.py show_table ...
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import otables  # type: ignore[no-redef]

TOOLS = ("find_tables", "show_table", "printings")


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of exiting (observe.py parses recorded commands with it) and accepts no
    option abbreviations (`printings --all` must stay an error, not a silent `--all-periods`)."""

    def __init__(self, *args, **kw):
        super().__init__(*args, allow_abbrev=False, **kw)

    def error(self, message):
        raise otables.ToolError(f"{self.prog}: {message}")


def parser(tool: str) -> argparse.ArgumentParser:
    if tool == "find_tables":
        p = _Parser(prog="find_tables", description="Find tables by id (FFO-3) or words in title, header and row "
                                                      "labels (prefix match on words of 4+ letters; one query per call).")
        p.add_argument("phrase")
        p.add_argument("--years", help="YYYY or YYYY-YYYY (issue years)")
        p.add_argument("--limit", type=int, default=25, help="max table groups listed")
        p.add_argument("--rows", action="store_true", help="also search row labels (default)")
        p.add_argument("--title-only", action="store_true", help="search title and header only, not row labels")
        p.add_argument("--all", action="store_true", help="list every part (up to 12 per group), not the latest 3")
        p.add_argument("--budget", type=int, default=otables.PAGE_BUDGET, help=argparse.SUPPRESS)
        return p
    if tool == "show_table":
        p = _Parser(prog="show_table", description="Show one table (by file and a line inside or at its header), "
                                                     "with filled periods, repairs and checks; pages of <= 8000 bytes.")
        p.add_argument("file")
        p.add_argument("line", type=int)
        p.add_argument("--cols", help="source columns to keep, e.g. 1-3,7")
        p.add_argument("--match", help="comma list of column-label substrings to keep")
        p.add_argument("--grep", action="append", help="keep rows whose filled period, raw label or section contains "
                                                       "this substring (not a regex; repeat or use 'a|b' for OR)")
        p.add_argument("--period", help="keep rows whose filled period falls in FY2021 or CY1996")
        p.add_argument("--stitch", action="store_true", help="join the continuation parts of a multi-part table")
        p.add_argument("--raw", action="store_true", help="print the original lines")
        p.add_argument("--long", action="store_true", help="one line per cell with row and column labels")
        p.add_argument("--max-rows", type=int, default=400)
        p.add_argument("--from-row", type=int, default=0,
                       help="continue a cut page from this row (0-based: pass the number the PAGE ENDS line prints)")
        p.add_argument("--budget", type=int, default=otables.PAGE_BUDGET, help="max page bytes (<= 8000)")
        return p
    if tool == "printings":
        p = _Parser(prog="printings", description="The same row (and column) in every issue that prints the table: "
                                                    "revisions, first and latest printing.")
        p.add_argument("--table", required=True, help="table id (FFO-3) or title words")
        p.add_argument("--row", required=True, help="period (2003-01, Jan 2003, FY2003, 1995) or row label")
        p.add_argument("--period")
        p.add_argument("--col", help="column label substring or printed number such as (8)")
        p.add_argument("--years", help="issue years YYYY or YYYY-YYYY (default: period year .. +3 / +12)")
        p.add_argument("--section", help="keep rows whose panel/section banner contains this substring")
        p.add_argument("--all-periods", action="store_true",
                       help="a month --row also matches daily/weekly rows of that month (default: the month row only)")
        p.add_argument("--budget", type=int, default=otables.PAGE_BUDGET, help=argparse.SUPPRESS)
        return p
    raise ValueError(f"unknown tool {tool!r}")


def page(tool: str, args: argparse.Namespace, tables: "otables.Tables"):
    """Run one parsed invocation and return its otables.Page (shared by the CLI and observe.py)."""
    if tool == "find_tables":
        return tables.find_page(args.phrase, args.years, args.limit, args.rows, args.all, budget=args.budget,
                                title_only=args.title_only)
    if tool == "show_table":
        return tables.show_page(args.file, args.line, cols=args.cols, grep=args.grep, stitch=args.stitch, raw=args.raw,
                                max_rows=args.max_rows, long=args.long, match=args.match, from_row=args.from_row,
                                budget=args.budget, period=args.period)
    if tool == "printings":
        return tables.printings_page(args.row, args.table, args.period, args.col, args.years, budget=args.budget,
                                     section=args.section, all_periods=args.all_periods)
    raise ValueError(tool)


def _run(tool: str, argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] in (["-h"], ["--help"]):
        parser(tool).print_help()
        return 0
    try:
        args = parser(tool).parse_args(argv)
        tables = otables.Tables()
        try:
            print(page(tool, args, tables).text)
        finally:
            tables.close()
        return 0
    except otables.ToolError as e:
        print(f"{tool}: error: {e}", file=sys.stderr)
        return 2
    except Exception as e:                                # noqa: BLE001 - e.g. a damaged index: one line, no traceback
        print(f"{tool}: internal error: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
        return 3


def find_tables(argv=None) -> int:
    return _run("find_tables", argv)


def show_table(argv=None) -> int:
    return _run("show_table", argv)


def printings(argv=None) -> int:
    return _run("printings", argv)


def build(argv=None) -> int:
    p = argparse.ArgumentParser(prog="build", description="Build the tables index once (prepare time).")
    p.add_argument("--corpus", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args(argv)
    stats = otables.build_index(a.corpus, a.out, verbose=not a.quiet)
    print(" ".join(f"{k}={v}" for k, v in stats.items()))
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in TOOLS + ("build",):
        print(f"usage: cli.py {{{','.join(TOOLS + ('build',))}}} ...", file=sys.stderr)
        return 2
    return build(argv[1:]) if argv[0] == "build" else _run(argv[0], argv[1:])


if __name__ == "__main__":
    sys.exit(main())
