"""Harm tests named by W0-PROPOSAL §1 (task R, step 3), run on the real corpus and both indexes.

    python -B w0replay/harm.py [--out DIR]

A. §1.5 - the W0-8 header repair must NOT fire on the right cells that saw the `spurious extra label`
   note: UID0112 b (FFO-2 2011_09), UID0130 a (TSO-1), UID0154 b (OFS-2 1977), UID0225 a/b2/c (MQ-1),
   UID0148 a/c (MQ-1).  Every show_table call those cells ran is replayed: v9 must print the old note
   somewhere in the cell (so the table really is a test case) and v10 must never print `LABELSHIFT-1` /
   `header realigned` for it.  A positive control (UID0165's OFS-2 2010_03:1869, where the repair is
   designed to fire) shows the detector is alive.
B. §1.2 - the right-cell find_tables queries keep their target's rank.  Each case is the query a right cell
   ran and the part it then opened; the rank is the 1-based position of the target's group line in the page
   (`[section]` lines excluded).  Allowed v10 ranks follow the proposal's own replay (UID0180 2->3,
   UID0220 1->2, UID0135 beyond --limit); UID0182 is the payload (absent -> rank 3).
C. §1.3 - no token-fraction ranking: static check of otables.py (no 'fraction' score) plus, for every
   case whose AND query has hits, the relative order of the groups common to v9 and v10 is unchanged.

Results: `<out>/harm.json` and a printed summary; no table values are read or printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

NO_FIRE_CELLS = [("UID0112", "b"), ("UID0130", "a"), ("UID0154", "b"), ("UID0225", "a"), ("UID0225", "b2"),
                 ("UID0225", "c"), ("UID0148", "a"), ("UID0148", "c")]
POSITIVE_CONTROL = ("treasury_bulletin_2010_03.txt", 1869)            # UID0165, OFS-2: designed to fire (tests line 623)
OLD_NOTE = "spurious extra label"
REPAIR_MARKS = ("LABELSHIFT-1", "header realigned")

# (uid, cell, query argv, targets: table ids or (file, hdr_line) parts the cell opened - the best rank counts,
#  v9 rank per the proposal, allowed v10 ranks)
RANK_CASES = [
    ("UID0130", "a", ["Total U.S. Federal Securities", "--all"], [("treasury_bulletin_1980_04.txt", 5359)], 7, {7}),
    ("UID0224", "a", ["high-grade corporate bonds average yields"], [("treasury_bulletin_1948_02.txt", 2948)], 10, {10}),
    ("UID0100", "b", ["Judicial Branch on-budget off-budget outlays", "--all"], ["FFO-3"], 1, {1}),
    ("UID0154", "b", ["Ownership of Public Debt Securities", "--all"], ["TSO-3", "OFS-2"], 1, {1}),
    ("UID0243", "b", ["Debt outstanding", "--years", "1962-1963", "--rows", "--all"], [("treasury_bulletin_1962_07.txt", 1509)], 1, {1}),
    ("UID0007", "a", ["budget expenditures", "--years", "1940-1949"], [("treasury_bulletin_1949_12.txt", 357)], 5, {5}),
    ("UID0112", "b", ["on-budget receipts off-budget receipts", "--all"], ["FFO-2"], 2, {2}),
    ("UID0109", "a", ["Department of Defense Outlays"], ["FFO-3"], 1, {1}),
    ("UID0122", "a", ["Exchange Stabilization Fund balances", "--all"], ["ESF-1"], 1, {1}),
    ("UID0228", "b", ["Government total receipts from the public", "--all"], [("treasury_bulletin_1957_04.txt", 802)], 1, {1}),
    ("UID0019", "b", ["net options positions", "--all"], ["FCP-II-1", "P-IV-1", "FCP-IV-1", "FCP-I-1"], 5, {5}),
    ("UID0180", "b2", ["Distribution of Federal Securities by Class of Investors and Type of Issues", "--years", "2009-2010", "--rows"],
     ["OFS-1"], 2, {2, 3}),
    ("UID0220", "a", ["total federal expenditures", "--years", "1938-1939", "--rows"], [("treasury_bulletin_1939_04.txt", 188)], 1, {1, 2}),
    ("UID0135", "a", ["Total Liabilities Foreign Countries U.S. banks", "--all"], ["CM-I-2"], 23, {"page"}),
    ("UID0182", "b", ["Canadian dollar liabilities", "--all"], ["CM-I-1"], None, {1, 2, 3}),
]


def run(cli: Path, index: Path, tool: str, args: list) -> str:
    """stdout of one tool call (stderr appended on a non-zero exit)."""
    p = subprocess.run([str(C.PYTHON), "-I", "-B", str(cli), tool, *args], env=C.run_env(index),
                       capture_output=True, text=True, timeout=60)
    return p.stdout + ("" if p.returncode == 0 else f"\n[exit {p.returncode}] {p.stderr[-300:]}")


def run_both(tool: str, args: list) -> tuple:
    return run(C.V9_CLI, C.V9_INDEX, tool, args), run(C.V10_CLI, C.V10_INDEX, tool, args)


# ---- A. header repair must not fire

def show_table_calls(uid: str, cell: str) -> list:
    """Distinct show_table argvs a cell ran (parsable ones), in call order."""
    seen, out = set(), []
    for r in C.iter_records((cell,)):
        if r.task_id != uid:
            continue
        for tool, args, _ in C.tool_argvs(r.raw_cmd)[0]:
            if tool == "show_table" and tuple(args) not in seen:
                seen.add(tuple(args))
                out.append(args)
    return out


def header_repair_test() -> dict:
    """Per cell: calls replayed, calls where v9 printed the old note, calls where v10 fired the repair (must
    be 0), plus a bare `--long` page of every table the cell opened (filters cannot hide a COLUMNS line)."""
    res = {}
    for uid, cell in NO_FIRE_CELLS:
        calls = show_table_calls(uid, cell)
        tables = sorted({(a[0], a[1]) for a in calls if len(a) >= 2 and a[1].isdigit()})
        argvs = calls + [[f, l, "--long"] for f, l in tables]
        old, fired, fired_on = 0, 0, []
        for args in argvs:
            v9, v10 = run_both("show_table", args)
            old += OLD_NOTE in v9
            if any(m in v10 for m in REPAIR_MARKS):
                fired += 1
                fired_on.append(" ".join(args))
        res[f"{uid} {cell}"] = {"calls": len(calls), "tables": [f"{f}:{l}" for f, l in tables], "v9_old_note_pages": old,
                                "v10_repair_pages": fired, "fired_on": fired_on, "ok": fired == 0 and old > 0}
    f, l = POSITIVE_CONTROL
    v9, v10 = run_both("show_table", [f, str(l), "--long"])
    res["positive control OFS-2 2010_03:1869"] = {"v9_old_note": OLD_NOTE in v9,
                                                  "v10_repair": all(m in v10 for m in REPAIR_MARKS),
                                                  "ok": OLD_NOTE in v9 and all(m in v10 for m in REPAIR_MARKS)}
    return res


# ---- B. ranks

parse_find_page = C.parse_find_page
TABLE_N_RX = C.TABLE_N_RX


def group_lines(page: str) -> list:
    """[(id, title)] of the table group lines in page order (`[section]` lines are not groups)."""
    return [(g["id"], g["title"]) for g in parse_find_page(page)]


def target_of(part) -> tuple:
    """(tid, title, part_ref) of a target: a bare table id is its own target; a (file, hdr_line) part is
    looked up in the index (untitled-id tables are then matched by title, or by the part line itself)."""
    if isinstance(part, str):
        return part, "", None
    f, line = part
    con = sqlite3.connect(f"file:{C.V10_INDEX}?mode=ro", uri=True)
    row = con.execute("SELECT tid, title, subtitle FROM t WHERE file = ? AND hdr_line = ?", (f, line)).fetchone()
    con.close()
    if row is None:
        raise SystemExit(f"{f}:{line} is not a table header in the index")
    return row[0] or "", (row[1] or row[2] or ""), f"{f}:{line}"


def _title_key(title: str, n: int = 40) -> str:
    return re.sub(r"\W+", "", TABLE_N_RX.sub("", title).lower())[:n]


def rank_of(groups: list, tid: str, title: str, part_ref=None):
    """1-based rank of the first group holding the target: by its part line when the page lists it, else by
    id, else by the title with a leading `Table N.` ignored (the W0-2 merge key)."""
    for i, g in enumerate(groups, 1):
        if part_ref and part_ref in g["parts"]:
            return i
    for i, g in enumerate(groups, 1):
        if tid and g["id"] == tid:
            return i
        if not tid and title and _title_key(title) and _title_key(title) in _title_key(g["title"]):
            return i
    return None


def full_rankings(cli_path: Path, index: Path) -> list:
    """The unbudgeted group ranking of every RANK_CASES query in one tree, computed in-process: the tree's
    own parser and `page()` with `--limit 200 --all`, the page budget and the per-group part cap lifted (the
    proposal's replay ranked groups the same way, without the 8000-byte cut, so these ranks are the ones
    comparable to its numbers)."""
    cli = C.load_cli(cli_path)
    cli.otables.PAGE_BUDGET = 10 ** 7
    if hasattr(cli.otables, "ALL_PARTS_CAP"):
        cli.otables.ALL_PARTS_CAP = 10 ** 6
    os.environ.update({"OFFICEQA_CORPUS": str(C.CORPUS), "TABLES_INDEX": str(index)})
    tables = cli.otables.Tables()
    out = []
    try:
        for _, _, args, *_ in RANK_CASES:
            extra = ["--limit", "200", "--budget", str(10 ** 7)] + ([] if "--all" in args else ["--all"])
            ns = cli.parser("find_tables").parse_args(args + extra)
            out.append(parse_find_page(cli.page("find_tables", ns, tables).text))
    finally:
        tables.close()
    return out


def _best(per: dict, i: int):
    return min((r[i] for r in per.values() if r[i] is not None), default=None)


def order_flips(f9: list, f10: list) -> list:
    """Pairs of groups present in both full rankings whose relative order changed, with their issue counts
    (a flip that follows a size change is the where-score/size ranking at work, not a new formula)."""
    key = lambda g: (g["id"], _title_key(g["title"], 200))  # noqa: E731
    dup = {k for lst in (f9, f10) for k in [key(g) for g in lst] if [key(g) for g in lst].count(k) > 1}
    pos10 = {key(g): (i, g["issues"]) for i, g in enumerate(f10) if key(g) not in dup}
    common = [(key(g), i, g["issues"]) for i, g in enumerate(f9) if key(g) in pos10]
    flips = []
    for a in range(len(common)):
        for b in range(a + 1, len(common)):
            ka, _, na = common[a]
            kb, _, nb = common[b]
            if pos10[ka][0] > pos10[kb][0]:
                name = lambda k: k[0] if k[0] not in ("", "-") else k[1][:30]  # noqa: E731
                flips.append({"above_in_v9": name(ka), "v9_issues": (na, nb), "v10_issues": (pos10[ka][1], pos10[kb][1]),
                              "below_in_v9": name(kb)})
    return flips


def rank_test() -> list:
    """One row per RANK_CASES entry: page-visible rank and full-ranking rank in v9 and v10, the proposal's
    expected v9 rank, the allowed v10 ranks, and whether the order of the groups common to both full
    rankings is unchanged (the §1.3 behavioural check)."""
    full9, full10 = full_rankings(C.V9_CLI, C.V9_INDEX), full_rankings(C.V10_CLI, C.V10_INDEX)
    rows = []
    for (uid, cell, args, targets, v9_expected, allowed), f9, f10 in zip(RANK_CASES, full9, full10):
        v9, v10 = run_both("find_tables", args)
        g9, g10 = parse_find_page(v9), parse_find_page(v10)
        page, full = {}, {}
        for t in targets:
            tid, title, ref = target_of(t)
            name = tid or title[:50]
            page[name] = (rank_of(g9, tid, title, ref), rank_of(g10, tid, title, ref))
            full[name] = (rank_of(f9, tid, title, ref), rank_of(f10, tid, title, ref))
        r9, r10 = _best(full, 0), _best(full, 1)
        p9, p10 = _best(page, 0), _best(page, 1)
        flips = order_flips(f9, f10)
        rows.append({"uid": uid, "cell": cell, "query": " ".join(args), "page_ranks": page, "full_ranks": full,
                     "v9_rank": r9, "v9_expected": v9_expected, "v10_rank": r10, "page_v9": p9, "page_v10": p10,
                     "allowed": sorted(str(x) for x in allowed),
                     "page_groups": (len(g9), len(g10)), "full_groups": (len(f9), len(f10)),
                     "v10_partial": "best partial matches" in v10,
                     "order_flips": len(flips), "flip_examples": flips[:3],
                     "full_rank_kept": r10 is not None and r9 is not None and r10 <= r9,
                     "ok": p10 is not None and (p9 is None or p10 <= p9 or "page" in allowed)})
    return rows


# ---- C. no token-fraction ranking (static)

def static_ranking_check() -> dict:
    src = (C.V10_TREE / "packs" / "officeqa" / "tools" / "otables.py").read_text()
    fp = src[src.index("def find_page"):src.index("def _assemble_find")]
    return {"'fraction' in otables.py": "fraction" in src.lower(),
            "find_page ranks partial hits by (tokens matched, title/header tokens, where-score, size)":
                "-max(x[\"ntok\"]" in fp and "-max(len(x[\"th_toks\"])" in fp and "-max(x[\"score\"]" in fp,
            "find_page ranks AND hits by (where-score, size) as v9 did":
                "key=lambda kv: (-max(x[\"score\"] for x in kv[1]), -len(kv[1]))" in fp}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", default=str(C.SCRATCH))
    a = p.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    res = {"A_header_repair_no_fire": header_repair_test(), "B_ranks": rank_test(), "C_static_ranking": static_ranking_check()}
    (out / "harm.json").write_text(json.dumps(res, indent=1))
    for k, v in res["A_header_repair_no_fire"].items():
        print("A", "ok " if v["ok"] else "FAIL", k, json.dumps({x: y for x, y in v.items() if x != "ok"}))
    for r in res["B_ranks"]:
        print("B", "ok " if r["ok"] else "FAIL", f"{r['uid']} {r['cell']:2s} full rank v9 {r['v9_rank']} (proposal {r['v9_expected']})"
              f" -> v10 {r['v10_rank']} allowed {r['allowed']} kept={r['full_rank_kept']}; page rank {r['page_v9']} -> {r['page_v10']};"
              f" groups full {r['full_groups']} page {r['page_groups']} partial={r['v10_partial']} flips={r['order_flips']}"
              f" | {r['query']} -> full {r['full_ranks']}")
        for f in r["flip_examples"]:
            print("      flip:", json.dumps(f))
    print("C", json.dumps(res["C_static_ranking"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
