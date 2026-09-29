"""Structural tests of the OfficeQA table tools (packs/officeqa/tools/otables.py, tools/cli.py).

Two tiers:
  - fast: page format (header line, <= 8,000-byte pages, --from-row continuation, determinism), read-only
    index, CLI error handling; they use a small index over a few issues (built per session, < 1 s).
  - slow (`-m slow`): the 21 structural cases of officeqa-v9-design/understand/tables_proto/run_cases.py
    (tables named in the failure analysis) over the FULL tables index, built once per session into a tmp dir
    (about 20 s).  Checks are about STRUCTURE (periods, labels, flags, identities, which issues print a row),
    never about answer values; nothing is printed.
Run: adapter-comparison-work/.venv/bin/python -m pytest tests/test_officeqa_tables.py [-m "not slow" | -m slow]
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packs.officeqa.tools import cli, otables as O  # noqa: E402

CORPUS = Path(os.environ.get("OFFICEQA_CORPUS") or ROOT.parent / "opus-officeqa-133" / "corpus")
SMALL_FILES = ["treasury_bulletin_1987_06.txt", "treasury_bulletin_1987_09.txt", "treasury_bulletin_1987_12.txt",
               "treasury_bulletin_1988_03.txt", "treasury_bulletin_2003_06.txt", "treasury_bulletin_2003_09.txt",
               "treasury_bulletin_2003_12.txt", "treasury_bulletin_2004_03.txt", "treasury_bulletin_1970_06.txt",
               "treasury_bulletin_1950_02.txt", "treasury_bulletin_2001_03.txt", "treasury_bulletin_1972_03.txt"]

pytestmark = pytest.mark.skipif(not (CORPUS / SMALL_FILES[0]).is_file(), reason="OfficeQA corpus not available")


@pytest.fixture(scope="session")
def small_index(tmp_path_factory):
    out = tmp_path_factory.mktemp("otables-small") / "tables.sqlite"
    O.build_index(CORPUS, out, files=SMALL_FILES)
    return out


@pytest.fixture(scope="session")
def small(small_index):
    t = O.Tables(CORPUS, small_index)
    yield t
    t.close()


@pytest.fixture(scope="session")
def full_index(tmp_path_factory):
    out = tmp_path_factory.mktemp("otables-full") / "tables.sqlite"
    stats = O.build_index(CORPUS, out)
    assert stats["files"] == 697 and stats["tables"] > 90_000
    return out


@pytest.fixture(scope="session")
def T(full_index):
    t = O.Tables(CORPUS, full_index)
    yield t
    t.close()


HEADER = re.compile(r"^TABLE (.+?) \| (\S+\.txt):(\d+) \| part (\d+) \| page rows (\d+)-(\d+) of (\d+)$")


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


# ---------------------------------------------------------------------------------------------
# fast: page format, budget, determinism, read-only index, CLI
# ---------------------------------------------------------------------------------------------

def test_build_index_small_is_readonly_and_described(small_index):
    con = O.connect_ro(small_index)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    assert json.dumps(O.INDEX_VERSION) == meta["version"] and int(meta["files"]) == len(SMALL_FILES)
    assert int(meta["sections"]) > 0                                # W0-12: narrative sections are indexed
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO meta VALUES ('x', '1')")
    con.close()
    assert not os.access(small_index, os.W_OK)


def test_show_page_header_budget_and_continuation(small):
    fname, line = "treasury_bulletin_2004_03.txt", 728
    first = small.show_page(fname, line, stitch=True)
    m = HEADER.match(first.lines[0])
    assert m and m.group(1) == "FFO-3" and m.group(2) == fname and m.group(3) == "728" and m.group(4) == "1"
    a, b, n = int(m.group(5)), int(m.group(6)), int(m.group(7))
    assert a == 1 and b < n and _bytes(first.text) <= O.PAGE_BUDGET
    assert f"continue with --from-row {b}" in first.text
    second = small.show_page(fname, line, stitch=True, from_row=b)
    m2 = HEADER.match(second.lines[0])
    assert int(m2.group(5)) == b + 1 and int(m2.group(6)) == n and _bytes(second.text) <= O.PAGE_BUDGET
    assert "continue with --from-row" not in second.text
    rows1 = {c.locator for c in first.cells}
    rows2 = {c.locator for c in second.cells}
    assert rows1 and rows2 and not rows1 & rows2          # pages partition the rows


def test_budget_never_exceeded_in_every_mode(small):
    for kw in ({}, {"stitch": True}, {"long": True}, {"long": True, "stitch": True}, {"raw": True},
               {"budget": 2000}, {"budget": 99999}):
        pg = small.show_page("treasury_bulletin_2004_03.txt", 728, **kw)
        limit = min(O.PAGE_BUDGET, max(O.MIN_BUDGET, kw.get("budget", O.PAGE_BUDGET)))
        assert _bytes(pg.text) <= limit, kw
        assert HEADER.match(pg.lines[0]), kw
    for pg in (small.printings_page("1987-01", "FFO-3"), small.find_page("outlays", all_hits=True)):
        assert _bytes(pg.text) <= O.PAGE_BUDGET


def test_pages_are_deterministic_and_cells_sit_on_their_lines(small):
    a = small.show_page("treasury_bulletin_1970_06.txt", 4946)
    O.analysis_at.cache_clear()
    O.file_tables.cache_clear()
    b = small.show_page("treasury_bulletin_1970_06.txt", 4946)
    assert a.text == b.text and [(c.line, c.locator, c.text) for c in a.cells] == [(c.line, c.locator, c.text) for c in b.cells]
    for c in a.cells:
        assert c.text and c.text in a.lines[c.line]
        assert c.attrs["oq.table_ref"].startswith("treasury_bulletin_1970_06.txt:")
        assert isinstance(c.attrs["oq.flags"], list)


def test_numeric_cells_are_never_cut_in_the_grid(small):
    pg = small.show_page("treasury_bulletin_2004_03.txt", 728, stitch=True)
    for c in pg.cells:
        if c.value is not None:
            assert "…" not in c.text


def test_cli_entry_points_and_errors(small_index, monkeypatch, capsys):
    monkeypatch.setenv("OFFICEQA_CORPUS", str(CORPUS))
    monkeypatch.setenv("TABLES_INDEX", str(small_index))
    assert cli.show_table(["treasury_bulletin_1987_06.txt", "463", "--match", "judiciary"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("TABLE FFO-3 | treasury_bulletin_1987_06.txt:463 | part 1 | page rows 1-")
    assert cli.printings(["--table", "FFO-3", "--row", "1987-01", "--col", "judiciary"]) == 0
    assert "latest printing 1988_03" in capsys.readouterr().out
    assert cli.find_tables(["FFO-3", "--years", "1987"]) == 0
    assert "[FFO-3]" in capsys.readouterr().out
    assert cli.show_table(["../../etc/passwd", "1"]) == 2
    assert cli.show_table(["treasury_bulletin_1987_06.txt"]) == 2
    assert cli.printings(["--row", "1987-01"]) == 2
    err = capsys.readouterr().err
    assert "error" in err and "Traceback" not in err
    monkeypatch.setenv("TABLES_INDEX", str(small_index) + ".missing")
    assert cli.find_tables(["FFO-3"]) == 2
    assert "tables index not found" in capsys.readouterr().err
    # a file that is not an index (or a damaged one): one error line, never a traceback
    monkeypatch.setenv("TABLES_INDEX", str(CORPUS / "treasury_bulletin_1987_06.txt"))
    assert cli.find_tables(["FFO-3"]) == 3
    err = capsys.readouterr().err
    assert err.startswith("find_tables: internal error:") and "Traceback" not in err and err.count("\n") == 1


def test_cli_startup_is_fast(small_index):
    import subprocess
    import time
    env = {**os.environ, "OFFICEQA_CORPUS": str(CORPUS), "TABLES_INDEX": str(small_index)}
    t0 = time.time()
    p = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "packs/officeqa/tools/cli.py"), "show_table",
                        "treasury_bulletin_1987_06.txt", "463"], env=env, capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.startswith("TABLE FFO-3")
    assert time.time() - t0 < 1.5


def test_parse_cell_and_numbers_keep_flags():
    assert O.parse_cell("r 16,311") == (16311.0, ("r",))
    assert O.parse_cell("2,659r") == (2659.0, ("r",))
    assert O.parse_cell("1-156")[1] == ("text",)
    assert O.numbers("| 2,659r | r 16,311 | r-144,112 | (1,234) | -.5 | 1,23 |") == [2659.0, 16311.0, -144112.0, 1234.0, -0.5, 1.0, 23.0]


# ---------------------------------------------------------------------------------------------
# slow: the 21 structural cases (run_cases.py) over the full index
# ---------------------------------------------------------------------------------------------

def analysis(T, fname, line):
    t = T.table_at(fname, line)
    return t, T.analysis(t)


slow = pytest.mark.slow


@slow
def test_case_ffo3_2004_03_stitch(T):                          # Q81 Q16 Q86 - PASS
    parts = T.parts_of("treasury_bulletin_2004_03.txt", T.table_at("treasury_bulletin_2004_03.txt", 728), True)
    nums = set()
    for p in parts:
        a = O.analyze(p)
        for c in range(1, a["ncols"]):
            m = re.search(r"\((\d+)\)\s*$", a["col_label"][c])
            if m:
                nums.add(int(m.group(1)))
    t0, a0 = analysis(T, "treasury_bulletin_2004_03.txt", 728)
    exp = ["FY1999", "FY2000", "FY2001", "FY2002", "FY2003", "FY2004 est", "2002-12"] + \
        [f"2003-{m:02d}" for m in range(1, 13)] + ["FY2004 to date"]
    assert len(parts) == 3 and nums == set(range(1, 38)) and [o["period"] for o in a0["rows"]] == exp
    assert O.parse_cell("r 16,311")[1] == ("r",)


@slow
def test_case_ffo3_printings_2003_01_gsa(T):                   # Q81 - PASS
    out = T.printings("2003-01", "FFO-3", column="general services")
    issues = sorted(set(re.findall(r"^  (\d{4}_\d\d)  ", out, re.M)))
    assert issues == ["2003_06", "2003_09", "2003_12", "2004_03"]


@slow
def test_case_ffo3_printings_1987_01_judiciary(T):             # Q86 - PASS
    out = T.printings("1987-01", "FFO-3", column="judiciary")
    issues = re.findall(r"^  (\d{4}_\d\d)  ", out, re.M)
    distinct = re.search(r"(\d+) distinct values", out)
    assert issues == ["1987_06", "1987_09", "1987_12", "1988_03"]
    assert distinct and distinct.group(1) == "2" and "REVISED" in out


@slow
def test_case_ay1_1970_06_years_lost(T):                       # Q11 Q18 - PASS
    out = T.show_table("treasury_bulletin_1970_06.txt", 4946, max_rows=2000)
    t, a = analysis(T, "treasury_bulletin_1970_06.txt", 4946)
    monthly = [o for o in a["rows"] if o["segment"] == 0]
    groups = sorted({o["panel"] for o in monthly})
    per_group = {g: len([o for o in monthly if o["panel"] == g]) for g in groups}
    assert groups == [1, 2, 3, 4] and all(per_group[g] >= 29 for g in groups)
    assert all("YEAR_UNKNOWN" in o["flags"] for o in monthly)
    assert len([o for o in monthly if o["panel"] == 4 and (o["period"] or "").startswith("Y?2-")]) == 5
    assert "SIBLINGS" in out and ("1970_05" in out or "1970_07" in out)


@slow
def test_case_ay1_1970_05_years_anchored(T):                   # Q18 - PASS
    t, a = analysis(T, "treasury_bulletin_1970_05.txt", 4848)
    rng = {}
    for o in a["rows"]:
        if re.match(r"^\d{4}-\d\d$", o["period"] or ""):
            rng.setdefault(o["panel"], []).append(o["period"])
    spans = {g: (v[0], v[-1]) for g, v in rng.items()}
    assert spans == {1: ("1959-01", "1961-12"), 2: ("1962-01", "1964-12"), 3: ("1965-01", "1967-12"),
                     4: ("1968-01", "1970-04")}
    assert "Treasury bonds" in a["labels"][1]


@slow
def test_case_yields_1944_03_label_slide(T):                   # Q30 - PASS
    t, a = analysis(T, "treasury_bulletin_1944_03.txt", 4015)
    flagged = {(o["line"], o["panel"]) for o in a["rows"] if any(f.startswith("SEQ") or f == "YEAR_BACKWARDS" for f in o["flags"])}
    weekly = [o for o in a["rows"] if "Weekly" in (o["section"] or "")]
    assert (4031, 2) in flagged and (4031, 3) in flagged
    assert sum("NOT reliable" in n for n in a["notes"]) >= 2
    assert weekly and all(re.match(r"^\d{4}-\d\d-\d\d$", o["period"] or "") for o in weekly)
    assert any("label_unreliable" in v for v in a["colflags"].values())


@slow
def test_case_yields_1956_07_header_shift(T):                  # Q62 - PASS
    t, a = analysis(T, "treasury_bulletin_1956_07.txt", 2603)
    lay = a["layouts"][0]["labels"]
    assert "10 to 20" in lay.get(1, "") and "after" in lay.get(2, "") and "Moody" in lay.get(3, "")
    assert any("VERIFY" in n for n in a["notes"])
    assert "label_realigned" in a["colflags"].get(1, [])
    t2, a2 = analysis(T, "treasury_bulletin_1956_08.txt", 3176)
    assert [x for x in a2["layouts"] if "20 years and after" in x["labels"].get(2, "")]


@slow
def test_case_ffo2_windows(T):                                 # Q14 Q130 - PASS
    res = []
    for fname, want in (("treasury_bulletin_2011_03.txt", [f"2010-{m:02d}" for m in range(1, 13)]),
                        ("treasury_bulletin_2018_12.txt", [f"2017-{m:02d}" for m in (10, 11, 12)] + [f"2018-{m:02d}" for m in range(1, 10)]),
                        ("treasury_bulletin_2011_12.txt", [f"2011-{m:02d}" for m in range(10, 13)])):
        hdr = T.con().execute("SELECT hdr_line FROM t WHERE tid='FFO-2' AND file=? ORDER BY hdr_line", (fname,)).fetchone()[0]
        t, a = analysis(T, fname, hdr)
        pers = [o["period"] for o in a["rows"]]
        res.append(all(w in pers for w in want))
    assert res == [True, True, False]                   # 2011_12 must NOT contain Oct-Dec 2011


@slow
def test_case_pdo1_1972_03_row_shift(T):                       # Q9 - PARTIAL (garbled year labels not repaired)
    t, a = analysis(T, "treasury_bulletin_1972_03.txt", 2076)
    assert sum("SHIFT+1" in o["flags"] for o in a["rows"]) >= 20
    ident = [n for n in a["notes"] if n.startswith("identity")]
    assert any("2085" in n and "2090" in n for n in ident)
    assert any(2085 in idn["bad"] and 2090 in idn["bad"] for idn in a["identities"])


@slow
def test_case_mq1_1982_03_header_shift(T):                     # Q15 - PARTIAL (warns, does not realign)
    t, a = analysis(T, "treasury_bulletin_1982_03.txt", 4363)
    assert [n for n in a["notes"] if "header probably shifted" in n]
    assert any("header_shift" in v for v in a["colflags"].values())


@slow
def test_case_cm_column_periods(T):                            # Q89 Q13 - PASS
    t, a = analysis(T, "treasury_bulletin_2000_12.txt", 3043)
    assert a["colper"].get(5, ("",))[0] == "2000-06"
    t, a = analysis(T, "treasury_bulletin_2001_12.txt", 3267)
    assert len([c for c, v in a["colper"].items() if v[0] == "2001-06"]) == 1
    t, a = analysis(T, "treasury_bulletin_2013_03.txt", 2247)
    cp3 = {c: v[0] for c, v in a["colper"].items()}
    assert cp3.get(2) == "2012-06" and cp3.get(8) == "2012-12"


@slow
def test_case_fd1_2000_09_identities(T):                       # Q101 Q8 - PASS
    t, a = analysis(T, "treasury_bulletin_2000_09.txt", 957)
    ids = [n for n in a["notes"] if n.startswith("identity")]
    assert t.get("tid") == "FD-1"
    assert all(any(w in n for n in ids) for w in ["c1 = sum c2+c3", "c4 = sum c5+c6", "c7 = sum c8+c9"])
    assert "[FD-1]" in T.find_tables("summary of federal debt", years="2000")


@slow
def test_case_months_by_year_1950_02(T):                       # Q17 - PASS
    t, a = analysis(T, "treasury_bulletin_1950_02.txt", 369)
    ids = [n for n in a["notes"] if n.startswith("identity")]
    fails = re.findall(r"FAILS at lines ([\d,]+)", " ".join(ids))
    lines = [int(x) for x in fails[0].split(",")] if fails else []
    lab = {o["line"]: (o["section"], o["period"]) for o in a["rows"]}
    hit = [lab[x] for x in lines if x in lab]
    assert any(s and "expenditures" in s.lower() and p.endswith("1942") for s, p in hit)
    assert any(s and "expenditures" in s.lower() and p.endswith("1947") for s, p in hit)


@slow
def test_case_tso3_1963_03_spurious_label(T):                  # Q43 - PARTIAL (cannot tell which label)
    t, a = analysis(T, "treasury_bulletin_1963_03.txt", 3762)
    assert [n for n in a["notes"] if n.startswith("identity")]
    assert any("spurious extra label" in n for n in a["notes"])
    assert any("label_unreliable" in v for v in a["colflags"].values())


@slow
def test_case_sbn3_2001_03_blocks(T):                          # Q122 - PASS
    hdr = T.con().execute("SELECT hdr_line FROM t WHERE tid='SBN-3' AND file='treasury_bulletin_2001_03.txt' "
                          "ORDER BY hdr_line").fetchone()[0]
    t, a = analysis(T, "treasury_bulletin_2001_03.txt", hdr)
    pers = [o["period"] for o in a["rows"] if o["period"]]
    assert any(p.startswith("FY") for p in pers) and any(p.startswith("CY") for p in pers)
    mar = [o for o in a["rows"] if o["label"].startswith("Mar")]
    assert mar and mar[0]["period"] == "2000-03" and "year from section banner" in mar[0]["flags"]
    assert re.search(r"ISOLATED: 2001_03", T.printings("2000-03", "SBN-3", column="(8)", years="2000-2001"))


@slow
def test_case_expenditures_1956_08_two_bands(T):               # Q60 - PASS
    t = T.table_at("treasury_bulletin_1956_08.txt", 250)
    parts = T.parts_of("treasury_bulletin_1956_08.txt", t, True)
    labs = [x for p in parts for x in O.analyze(p)["labels"] if "efense" in x]
    assert any(">" in x for x in labs)


@slow
def test_case_ffo5_1981_12_garbled_cell(T):                    # Q88 - PARTIAL (fused cell not decoded)
    t, a = analysis(T, "treasury_bulletin_1981_12.txt", 905)
    assert any("fell out of the pipe table" in n and "916" in n for n in a["notes"])
    assert O.cell_type("1-156") == "text"


@slow
def test_case_ofs2_2013_03_newest_first(T):                    # Q105 Q44 - PASS
    t, a = analysis(T, "treasury_bulletin_2013_03.txt", 1853)
    pers = [o["period"] for o in a["rows"] if o["period"]][:9]
    assert pers == ["2012-12", "2012-09", "2012-06", "2012-03", "2011-12", "2011-09", "2011-06", "2011-03", "2010-12"]
    assert not [o for o in a["rows"] if any(f.startswith("SEQ") or f == "YEAR_BACKWARDS" for f in o["flags"])]


@slow
def test_case_ofs1_2007_09_stitch(T):                          # Q44 Q77 Q101 - PASS
    pages = []
    frm = 0
    while True:                                         # the stitched table spans several 8 KB pages
        pg = T.show_page("treasury_bulletin_2007_09.txt", 1797, stitch=True, from_row=frm)
        pages.append(pg)
        m = HEADER.match(pg.lines[0])
        if int(m.group(6)) >= int(m.group(7)):
            break
        frm = int(m.group(6))
    text = "\n".join(p.text for p in pages)
    assert "2006-06" in text and "+1 continuation parts stitched" in pages[0].text
    assert any("c3 = sum c4+c5" in x for x in pages[0].lines if "identity" in x)


@slow
def test_case_esf1_2012_06_balance_sheet(T):                   # Q39 - PASS
    t, a = analysis(T, "treasury_bulletin_2012_06.txt", 4722)
    cp = {c: v[0] for c, v in a["colper"].items()}
    assert cp.get(1, "").startswith("Sept. 30") and "through" in cp.get(2, "") and cp.get(3, "").startswith("Dec. 31")
    assert any(" > " in x for x in {o["section"] for o in a["rows"] if o["section"]})
    rec = next(c for c in T.show_page("treasury_bulletin_2012_06.txt", 4722).cells if c.attrs["oq.col"] == "c1")
    assert rec.attrs["oq.period"] == "2011-09-30"      # balance-sheet date column normalised


@slow
def test_case_find_tables_recall(T):                           # Q109 Q101 Q16 - PASS
    r1 = T.find_tables("sales and redemptions", years="1982")
    r2 = T.find_tables("judicial branch", years="2013")
    r3 = T.find_tables("summary of federal debt", years="2000")
    r4 = T.find_tables("calendar year", years="2000", limit=40)
    assert "[SN-1]" in r1 and "[SB-2]" in r1 and "[FFO-3]" in r2 and "[FD-1]" in r3 and "CM-I-3" in r4


@slow
def test_sweep_no_crash_first_issue_per_decade(T):
    """Robustness: analyze + show every table of one issue per decade (the prototype's sweep, reduced)."""
    for fname in ("treasury_bulletin_1939_06.txt", "treasury_bulletin_1944_03.txt", "treasury_bulletin_1952_04.txt",
                  "treasury_bulletin_1968_06.txt", "treasury_bulletin_1975_03.txt", "treasury_bulletin_1984_03.txt",
                  "treasury_bulletin_1995_03.txt", "treasury_bulletin_2005_03.txt", "treasury_bulletin_2016_03.txt",
                  "treasury_bulletin_2024_03.txt"):
        for t in T.file_tables(fname):
            pg = T.show_page(fname, t["hdr_line"])
            assert _bytes(pg.text) <= O.PAGE_BUDGET and HEADER.match(pg.lines[0])


# ---------------------------------------------------------------------------------------------
# v10 W0 tool changes (officeqa-v10-analysis/synthesis/W0-PROPOSAL.md §(d), W0-1 .. W0-12).  Structural
# pins only (table ids, issues, line numbers, which lines a page prints); never a cell value.
# ---------------------------------------------------------------------------------------------

def _line_index(lines, prefix):
    return next(i for i, x in enumerate(lines) if x.startswith(prefix))


def test_w0_cli_parsers_accept_every_new_option_through_observe():
    """observe.py re-runs recorded commands with cli.parser: every W0 option must parse there (W0-1/6/11)."""
    from packs.officeqa import observe
    cases = {
        "find_tables 'canadian dollar' --years 2009-2011 --title-only --rows --all --limit 5":
            ("find_tables", {"title_only": True, "rows": True, "all": True, "limit": 5}),
        "show_table treasury_bulletin_2004_03.txt 728 --grep 1996-Mar --grep 'Dec 1997' --period FY2003 --from-row 3":
            ("show_table", {"grep": ["1996-Mar", "Dec 1997"], "period": "FY2003", "from_row": 3}),
        "printings --table FFO-3 --row 2003-01 --col '(2)' --section 'budget receipts' --all-periods":
            ("printings", {"section": "budget receipts", "all_periods": True, "col": "(2)"}),
    }
    for cmd, (tool, want) in cases.items():
        (got_tool, args), = observe.invocations(cmd)
        ns = cli.parser(got_tool).parse_args(args)
        assert got_tool == tool and {k: getattr(ns, k) for k in want} == want, cmd
    assert cli.parser("show_table").parse_args(["f.txt", "1"]).grep is None      # old form: no --grep at all
    assert cli.parser("show_table").parse_args(["f.txt", "1", "--grep", "Jan"]).grep == ["Jan"]


def test_w0_grep_terms_period_forms_and_windows():
    assert O.grep_terms(None) == [] and O.grep_terms("a|b") == ["a", "b"] and O.grep_terms(["a", "b|c"]) == ["a", "b", "c"]
    row = {"label": "Nov.", "period": "1969-11", "section": "Monthly"}
    assert O.row_matches(row, ["1969-Nov"]) and O.row_matches(row, ["Nov 1969"]) and O.row_matches(row, ["1969"])
    assert O.row_matches(row, ["zzz", "monthly"]) and not O.row_matches(row, ["1970-11"]) and not O.row_matches(row, ["Dec"])
    assert O.period_window("FY2021") == ("FY", 2021, (2020, 10), (2021, 9))
    assert O.period_window("cy1996") == ("CY", 1996, (1996, 1), (1996, 12))
    assert O.in_period_window("2020-10", O.period_window("FY2021")) and not O.in_period_window("2020-09", O.period_window("FY2021"))
    assert O.in_period_window("FY2021", O.period_window("FY2021")) and not O.in_period_window("2022-01", O.period_window("FY2021"))
    with pytest.raises(O.ToolError):
        O.period_window("March 2021")


def test_w0_exact_value_keys_never_round():
    from decimal import Decimal
    assert O.exact_value("1,234,567.89") == Decimal("1234567.89")
    assert O.exact_value("r 16,311") == O.exact_value("16311.0") == Decimal("16311")   # '.0' spelling = one value
    assert O.exact_value("(1,234)") == Decimal("-1234") and O.exact_value("3.91 4/") == Decimal("3.91")
    assert str(O.exact_value("123456789")) == "123456789" and O.exact_value("n.a.") == "n.a."


def test_w0_find_tables_scope_prefix_partial_and_group_first(small):
    default = small.find_page("outlays agency")
    same = small.find_page("outlays agency", rows=True)                    # --rows is the default (no-op)
    assert default.text == same.text and "[FFO-3]" in default.text
    lines = default.lines
    first_part = next(i for i, x in enumerate(lines) if x.startswith("  [") and "] parts (" in x)
    assert all(x.startswith("[") for x in lines[1:first_part])            # every group line before any part block
    assert "| match:" in lines[1] and lines[first_part + 1].startswith("    treasury_bulletin_")
    assert default.cells and all(c.attrs["oq.kind"] == "table_hit" and lines[c.line].strip() == c.text for c in default.cells)
    assert "[FFO-3]" in small.find_page("outlay", title_only=True).text                # prefix on 4+ letter tokens
    partial = small.find_page("zzqxv outlays agency")
    assert partial.lines[1].startswith("no table has all 3 words; best partial matches (matched 2 of 3):")
    assert "match:2 of 3 tokens(" in partial.text and "[FFO-3]" in partial.text
    assert _bytes(partial.text) <= O.PAGE_BUDGET
    every = small.find_page("outlays", all_hits=True)
    assert "more parts (add --years)" in every.text or "parts (all " in every.text
    with pytest.raises(O.ToolError):
        small.find_page("   ")


def test_w0_show_table_grep_or_period_and_locator_lines(small):
    f = "treasury_bulletin_2004_03.txt"
    one = small.show_page(f, 728, grep="2003-Jan")
    both = small.show_page(f, 728, grep=["2003-Jan", "Feb 2003"])
    piped = small.show_page(f, 728, grep="2003-Jan|Feb 2003")
    assert one.meta["rows"] == 1 and both.meta["rows"] == 2 and piped.text == both.text
    fy = small.show_page(f, 728, period="FY2003")
    assert any(x.startswith("  FY2003 = 2002-10..2003-09:") and "rows kept" in x for x in fy.lines)
    assert {c.attrs["oq.period"][:4] for c in fy.cells} <= {"2002", "2003", "FY20"}
    none = small.show_page(f, 728, grep="Judicial")
    assert none.meta["rows"] == 0 and any(x.startswith("  0 rows: --grep is a case-insensitive substring") for x in none.lines)
    assert any("'Judicial' matches column c" in x and "--match" in x for x in none.lines)
    past = small.show_page(f, 728, grep="2003-Jan", from_row=5)
    assert any(x.startswith("  --from-row 5 is past the last filtered row (1)") for x in past.lines)
    inside = small.show_page(f, 740)
    assert any(x == "  line 740 is row 11 of TABLE FFO-3 starting at 728" for x in inside.lines)
    assert any(x.startswith("  this part has 20 rows; the table continues in part 2 at :751 (use --stitch)") for x in inside.lines)
    assert not any("the table continues" in x for x in small.show_page(f, 728, stitch=True).lines)
    before = small.show_page(f, 720)
    assert any(x.startswith("  no table starts at treasury_bulletin_2004_03.txt:720; showing the next table at :728 (") for x in before.lines)
    assert HEADER.match(before.lines[0]) and before.lines[0].startswith("TABLE FFO-3 |")
    with pytest.raises(O.ToolError):
        small.show_page(f, 728, period="Jan 2003")


def test_w0_raw_view_prints_notes_and_long_hint(small):
    raw = small.show_page("treasury_bulletin_1970_06.txt", 4946, raw=True)
    assert "NOTES:" in raw.lines and _line_index(raw.lines, "NOTES:") < _line_index(raw.lines, "RAW LINES:")
    assert any("carry no year in this printing" in x for x in raw.lines) and all(c.attrs.get("oq.raw") for c in raw.cells)
    assert _bytes(raw.text) <= O.PAGE_BUDGET
    long = small.show_page("treasury_bulletin_2004_03.txt", 728, long=True)     # 20 rows x 12 cells: cut
    cut = next((x for x in long.lines if x.startswith("... PAGE ENDS")), "")
    assert cut and "fit one page without --long" in cut


def test_w0_printings_summary_first_exact_values_and_diagnostics(small):
    pg = small.printings_page("1987-01", "FFO-3", column="judiciary")
    i_sum = _line_index(pg.lines, "SUMMARY (")
    i_list = _line_index(pg.lines, "  issue    file:line")
    assert i_sum < i_list and "e+" not in pg.text and "%g" not in pg.text
    assert [c.attrs["oq.issue"] for c in pg.cells if not c.attrs.get("oq.summary")] == ["1987_06", "1987_09", "1987_12", "1988_03"]
    assert all(pg.lines[c.line].strip() and c.text in pg.lines[c.line] for c in pg.cells)
    miss = small.printings_page("1987-01", "FFO-3", column="nosuchcolumn")
    assert miss.lines[1].startswith("  no matching row/column found") and not miss.cells
    assert any(x.startswith("  no column matches 'nosuchcolumn'; columns of treasury_bulletin_1987_06.txt:") and "| c2 " in x
               for x in miss.lines)
    gone = small.printings_page("1987-01", "FFO-3", section="no such panel")
    assert any(x.startswith("  --section 'no such panel' kept no rows; sections of ") for x in gone.lines)
    assert " | section='no such panel'" in gone.lines[0] and observe_header_ok(gone.lines[0])
    nowhere = small.printings_page("1987-01", "ZZZ-9")
    assert any("no table matches --table 'ZZZ-9'" in x for x in nowhere.lines)
    late = small.printings_page("2003-01", "FFO-3", years="2010-2011")
    assert any(x.startswith("  FFO-3 is printed in issues 1970_06..2004_03 (") for x in late.lines)
    assert any("later issues do not print FFO-3" in x for x in late.lines)
    assert any(x.startswith("  rows of FFO-3 were last printed in 2004_03; --years 2010-2011 starts after that") for x in late.lines)


def observe_header_ok(first_line: str) -> bool:
    from packs.officeqa import observe
    return observe.PRINTINGS_RX.match(first_line) is not None


def test_w0_printings_month_rows_exact_unless_all_periods(small):
    exact = small.printings_page("2003-01", "FFO-3", column="(2)", years="2003-2004")
    assert {f.attrs["oq.row_period"] for f in exact.cells} == {"2003-01"}
    every = small.printings_page("2003-01", "FFO-3", column="(2)", years="2003-2004", all_periods=True)
    assert every.lines[0].endswith(" | all-periods") and observe_header_ok(every.lines[0])
    assert {f.attrs["oq.row_period"] for f in every.cells} >= {"2003-01"}


def test_w0_printings_panel_keys_on_stacked_panels(small):
    """UID0007-type table (1950_02:369 'Summary by Months and Calendar Years', stacked panels sharing the
    year rows): the SUMMARY is keyed by panel and says where else the same column/period is printed."""
    pg = small.printings_page("1942", "Summary by Months and Calendar Years", column="Mar", years="1950")
    heads = [x for x in pg.lines if x.startswith("  [CY1942] part1 panel '")]
    assert len(heads) >= 2 and len({x.split("panel ")[1].split("'")[1] for x in heads}) == len(heads)
    assert any(x.startswith("      also printed under panel '") and x.endswith("(not merged)") for x in pg.lines)
    one = small.printings_page("1942", "Summary by Months and Calendar Years", column="Mar", years="1950",
                               section="budget expenditures")
    assert len([x for x in one.lines if x.startswith("  [CY1942] part1 panel '")]) == 1
    assert all("expenditures" in c.attrs["oq.section"].lower() for c in one.cells)


def test_w0_narrative_sections_indexed_and_listed(small):
    """W0-12: the 1988_03 narrative section (UID0215-type) is one section with its sub-headings."""
    pg = small.find_page("treasury financing operations auction notes", years="1988")
    sec = [x for x in pg.lines if x.startswith("[section] TREASURY FINANCING OPERATIONS, OCTOBER-DECEMBER 1987 | treasury_bulletin_1988_03.txt:1688-")]
    assert sec and "sub-headings: Auction of 2-Year Notes (:1692)" in sec[0]
    groups = [i for i, x in enumerate(pg.lines) if x.startswith("[") and not x.startswith("[section]")]
    assert groups and _line_index(pg.lines, "[section]") < groups[0]             # above the table groups
    assert "[section]" not in small.find_page("outlays", years="1988").text        # one content token: no block
    nothing = small.find_page("zzqxv financing operations", years="1988")
    assert any(x.startswith("no table matched;") or x.startswith("no table has all") for x in nothing.lines)


def test_w0_tools_accept_an_index_without_the_section_tables(tmp_path, monkeypatch):
    """A v9.1-shaped index (no s/fs tables) still serves find_tables; the [section] block is just absent."""
    monkeypatch.setattr(O, "_SCHEMA", tuple(x for x in O._SCHEMA if " s(" not in x and " fs " not in x and "s_file" not in x))
    monkeypatch.setattr(O, "narrative_sections", lambda lines: [])
    out = tmp_path / "old.sqlite"
    O.build_index(CORPUS, out, files=SMALL_FILES[:4])
    t = O.Tables(CORPUS, out)
    try:
        pg = t.find_page("treasury financing operations auction notes", years="1988")
        assert "[section]" not in pg.text and pg.lines[0].startswith("find_tables(")
    finally:
        t.close()


# ---- slow: the development items named in W0-PROPOSAL §(d) "Tests pinned to development items"

@slow
def test_w0_1_uid0182_cm_i_1_visible_by_row_labels(T):
    pg = T.find_page("Canadian dollar liabilities", years="2009-2011")
    cm = [x for x in pg.lines if x.startswith("[CM-I-1] ")]
    assert cm and "rowlabels: canadian" in cm[0] and "[CM-III-1] " in pg.text
    assert "[CM-I-1]" not in T.find_page("Canadian dollar liabilities", years="2009-2011", title_only=True).text
    assert _bytes(pg.text) <= O.PAGE_BUDGET


@slow
def test_w0_2_uid0243_1962_table_1_groups_merged(T):
    pg = T.find_page("summary of federal securities", years="1960-1968")
    assert not any(x.startswith("[-] Table 1. Summary of Federal Securities") for x in pg.lines)
    top = [x for x in pg.lines if x.startswith("[Table 1 in DEBT OUTSTANDING] Summary of Federal Securities  |")]
    assert top and int(top[0].split("| ")[1].split(" issues")[0]) >= 94 and "| rows " in top[0]
    every = T.find_page("summary of federal securities", years="1962", all_hits=True)
    assert "treasury_bulletin_1962_07.txt:1509" in {c.locator for c in every.cells}
    assert any("[snapshot]" in x for x in pg.lines)                                # as-of tables, no period column


@slow
def test_w0_8_uid0165_ofs2_header_shift_strict(T):
    for fname, hl in (("treasury_bulletin_2007_03.txt", 2301), ("treasury_bulletin_2010_03.txt", 1869)):
        a = T.analysis(T.table_at(fname, hl))
        assert sorted(a["labelshift"]) == [8, 9, 10, 11, 12] and all(O.column_key(a["col_label"][c])[0] is None for c in [8])
        assert any(n.startswith("header realigned: 13 labels for 12 numeric columns; identity c3 = sum c4..c12") for n in a["notes"])
        pg = T.show_page(fname, hl)
        assert any(x.startswith("  v9   = c9 ") and "LABELSHIFT-1 -> " in x for x in pg.lines)
        assert any("header realigned" in x and "; sibling treasury_bulletin_" in x and " agrees" in x for x in pg.lines)
    assert not T.analysis(T.table_at("treasury_bulletin_2008_03.txt", 1839))["labelshift"]
    # harm tests of W0-PROPOSAL §1.5: FFO-2 2011_09 (ordinals (22)..(34), identities hold unshifted), MQ-1,
    # TSO-1 and OFS-2 1977 (no ordinals): the old note stays, no shift
    for fname, hl in (("treasury_bulletin_2011_09.txt", 628), ("treasury_bulletin_1982_03.txt", 4363),
                      ("treasury_bulletin_1963_03.txt", 3762), ("treasury_bulletin_1977_03.txt", 4635)):
        a = T.analysis(T.table_at(fname, hl))
        assert not a["labelshift"], (fname, hl)


@slow
def test_w0_8_uid0148_mq1_1972_05_date_label_repaired(T):
    a = T.analysis(T.table_at("treasury_bulletin_1972_05.txt", 4647))
    assert a["date_label_fix"] == [2, 3] and a["coltype"][2] == "date"
    fixed = T.date_labels(T.table_at("treasury_bulletin_1972_05.txt", 4647), a)
    assert set(fixed) == {2, 3} and all(lab.startswith("Issue date > ") for _, lab in fixed.values())
    b = T.analysis(T.table_at("treasury_bulletin_1974_05.txt", 4599))
    assert b["col_label"][2] == "Issue date > 13-week" and b["coltype"][2] == "date" and not b["date_label_fix"]
    pg = T.show_page("treasury_bulletin_1972_05.txt", 4647, cols="1-3")
    assert any(x.startswith("  v2   = c2 ") and "(label repaired from sibling treasury_bulletin_" in x
               and ": Issue date > 13-week)" in x for x in pg.lines)


@slow
def test_w0_10_uid0028_ay1_1970_06_years_anchored(T):
    t = T.table_at("treasury_bulletin_1970_06.txt", 4946)
    a = T.anchored(t)
    note = next(n for n in a["notes"] if n.startswith("YEARS ANCHORED from treasury_bulletin_1970_05.txt:4848 ("))
    assert "panel1 = 1959,1960,1961" in note and "panel4 = 1968,1969,1970 (partial, ends 1970-05)" in note
    assert not any("YEAR_UNKNOWN" in o["flags"] for o in a["rows"])
    assert all("YEAR_UNKNOWN" in o["flags"] for o in T.analysis(t)["rows"] if o["segment"] == 0)   # pure analysis kept
    pg = T.show_page("treasury_bulletin_1970_06.txt", 4946, grep="1964-02")
    assert pg.meta["rows"] == 1 and all(c.attrs["oq.period"] == "1964-02" and "anchored" in c.attrs["oq.flags"] for c in pg.cells)
    pr = T.printings_page("1964-02", "AY-1", years="1970")
    assert "1970_06" in {c.attrs["oq.issue"] for c in pr.cells}


@slow
def test_w0_5_uid0114_my2_end_of_series(T):
    pg = T.printings_page("2004-01", "MY-2")
    assert pg.lines[1].startswith("  no matching row/column found")
    assert any(x.startswith("  MY-2 is printed in issues 1988_06..2003_03 (") for x in pg.lines)
    assert any("the last issue printing it is 2003_03 (treasury_bulletin_2003_03.txt:" in x and
               x.endswith("later issues do not print MY-2") for x in pg.lines)
    assert any(x.startswith("  note in treasury_bulletin_2003_06.txt:2169: ") and "discontinu" in x.lower() for x in pg.lines)


@slow
def test_w0_pages_stay_within_budget_on_wide_queries(T):
    for phrase, kw in (("total", {}), ("total", {"all_hits": True}), ("summary of federal securities", {"limit": 200}),
                       ("zzqxv treasury debt securities outstanding", {}), ("federal", {"years": "1939-2025"})):
        pg = T.find_page(phrase, **kw)
        assert _bytes(pg.text) <= O.PAGE_BUDGET and pg.lines[0].startswith("find_tables("), (phrase, kw)
    pr = T.printings_page("1987-01", "FFO-3")                                    # every column: listing cut, SUMMARY kept
    assert _bytes(pr.text) <= O.PAGE_BUDGET and "SUMMARY (" in pr.text


def test_w0_observe_recovers_printings_pages_carrying_the_new_options(small):
    """A printings page printed by a loop (unparsable invocation) is recovered from its frozen first line,
    including --section / --all-periods and the effective issue window it prints (observe.PRINTINGS_RX)."""
    from packs.officeqa import observe
    pg = small.printings_page("1942", "Summary by Months and Calendar Years", column="Mar", years="1950",
                              section="budget expenditures", all_periods=True)
    evs = observe.observations("for s in a b; do printings --table X --row 1942 --section $s; done", pg.text,
                               source="t", call=1, t=0.0, next_ref=lambda: "r", tables=small)
    assert evs and {e.attrs["parse"] for e in evs} == {"header"}
    assert {e.attrs["oq.issue"][:4] for e in evs} == {"1950"} and all("expenditures" in e.attrs["oq.section"].lower() for e in evs)
    old = small.printings_page("1987-01", "FFO-3", column="judiciary")             # the v9 first-line form
    evs = observe.observations("for x in y; do printings $x; done", old.text, source="t", call=1, t=0.0,
                               next_ref=lambda: "r", tables=small)
    assert [e.attrs["oq.issue"] for e in evs if not e.attrs.get("oq.summary")] == ["1987_06", "1987_09", "1987_12", "1988_03"]


# ---- fix pass after the W0 review / replay (tasks V and R): F1, F2, F4, F5, F6, F7 and replay F1-F4

def test_fix_panel_stub_strips_whole_words_only():
    """F4: noise words are removed as whole words, so 'Contract authority' and 'Marketable' keep their letters."""
    assert O.panel_stub("Contract authority 2/") == "contractauthority" and O.panel_stub("Marketable") == "marketable"
    assert O.panel_stub("Summary") == "summary" and O.panel_stub("Separate accounts") == "separateaccounts"
    assert O.panel_stub("Calendar years") == O.panel_stub("Calendaryears") == O.panel_stub("Dec. 1999 - Dec. 2000") == ""
    assert O.panel_stub("Budget expenditures 2/") == "budgetexpenditures"
    assert O.same_panel("monthlyseriesaverages", "monthlyaeriesaverages") and O.same_panel("", "")
    assert not O.same_panel("budgetreceipts", "budgetexpenditures") and not O.same_panel("basis", "")


def test_fix_printings_rejects_option_abbreviations():
    """replay F4: `printings --all` is an error, not a silent `--all-periods`; the long form still parses."""
    with pytest.raises(O.ToolError):
        cli.parser("printings").parse_args(["--table", "FFO-3", "--row", "2003-01", "--all"])
    assert cli.parser("printings").parse_args(["--table", "FFO-3", "--row", "2003-01", "--all-periods"]).all_periods
    with pytest.raises(O.ToolError):
        cli.parser("find_tables").parse_args(["x", "--lim", "3"])


def test_fix_day_level_query_periods_and_period_range():
    """replay F2: a day-level --row keeps its month (parse_query_period) and its day (query_day); the no-match
    period range ignores years OCR put far outside the issue's reach."""
    assert O.parse_query_period("1939-Jan. 19") == (1939, 1) and O.query_day("1939-Jan. 19") == 19
    assert O.parse_query_period("1939-01-19") == (1939, 1) and O.query_day("1939-01-19") == 19
    assert O.query_day("1939-01") is None and O.query_day("Jan. 19") is None and O.query_day("FY1939") is None
    pairs = [("1803", "1939_04"), ("1939-01-04", "1939_04"), ("1940-10", "1940_12"), ("2095", "1939_04")]
    assert O._period_range(pairs) == ("1939-01-04", "1940-10") and O._period_range([("1803", "1939_04")]) == ()


def test_fix_period_range_is_deterministic_on_tied_labels():
    """Integration fix (replay 2026-09-29: one `printings` no-match page differed between two runs of the same
    tools): labels that tie on the (year, month, day) key - 'FY1935-1953', 'CY1935-1951', 'FY1935-1951' all
    parse to 1935 - are ordered by their text, never by set iteration order (which follows the hash seed)."""
    ties = ["FY1935-1953", "CY1935-1951", "FY1935-1951"]
    assert len({O._period_sort(p) for p in ties}) == 1
    for order in (ties, ties[::-1], [ties[1], ties[0], ties[2]]):
        pairs = [(p, "1960_10") for p in order] + [("1960-10", "1960_10")]
        assert O._period_range(pairs) == ("CY1935-1951", "1960-10")
    assert O._period_range([(p, "1960_10") for p in ties]) == ("CY1935-1951", "FY1935-1953")


def test_fix_summary_merges_banner_variants_and_keeps_real_panels(small):
    """replay F3: within one series a banner that varies between printings (OCR 'series'/'aeries', a banner
    missing in one issue) is one panel; two banners inside one issue stay two panels.  Single-printing keys
    carry their locator on the value line and no first/latest line."""
    def cell(issue, section, raw, period="1981-08", col="Treasury bonds 1/", line=10):
        return {"issue": issue, "file": f"treasury_bulletin_{issue}.txt", "line": line, "part": 1, "period": period,
                "label": "Aug", "col": 3, "col_label": col, "raw": raw, "section": section, "asof": "", "snapshot": False,
                "rec": {"locator": "x", "text": "y", "value": None, "attrs": {}}}
    varying = [cell("1981_11", "Monthly series", "1"), cell("1982_02", "Monthly aeries", "1"),
               cell("1982_05", "", "2"), cell("1982_10", "Monthly series", "2")]
    keys, merged = O.Tables._panel_keys(varying)
    assert len(set(keys.values())) == 1 and set(merged) == {"monthlyseries"}
    lines = small._summary_lines(varying, lambda *a: None)
    heads = [x for x in lines if x.startswith("  [1981-08] part1")]
    assert len(heads) == 1 and "4 printings, 2 distinct values - REVISED" in heads[0]
    assert any(x.startswith("      panel banner varies across printings (") and x.endswith("one panel per issue, merged") for x in lines)
    stacked = [cell("1950_02", "Budget receipts", "1", line=10), cell("1950_02", "Budget expenditures", "2", line=30),
               cell("1950_04", "Budget receipts", "1", line=12), cell("1950_04", "Budget expenditures", "2", line=32)]
    keys, merged = O.Tables._panel_keys(stacked)
    assert set(keys.values()) == {"budgetreceipts", "budgetexpenditures"} and not merged
    lines = small._summary_lines(stacked, lambda *a: None)
    assert len([x for x in lines if x.startswith("  [1981-08] part1 panel '")]) == 2
    assert any(x.startswith("      also printed under panel '") for x in lines)
    once = small._summary_lines([cell("1950_02", "", "1")], lambda *a: None)
    assert any(x.startswith("      ") and x.endswith(" (1) treasury_bulletin_1950_02.txt:10") for x in once)
    assert not any("first printing" in x for x in once)


def test_fix_printings_day_rows_hidden_note_and_day_level_row(small):
    """replay F2 on PDO-2 1987_09:1674 (dated auction rows next to month rows): a month --row says how many
    daily rows it hid; a day-level --row matches that day only; an unknown day lists the dated rows."""
    t = small.table_at("treasury_bulletin_1987_09.txt", 1674)
    a = small.analysis(t)
    days = sorted({o["period"] for o in a["rows"] if re.match(r"^\d{4}-\d\d-\d\d$", o["period"] or "")})
    months = sorted({o["period"] for o in a["rows"] if re.match(r"^\d{4}-\d\d$", (o["period"] or "").split(" ")[0])})
    month = next(m for m in months if any(d.startswith(m + "-") for d in days))
    day = next(d for d in days if d.startswith(month + "-"))
    pg = small.printings_page(month, "PDO-2", years="1987")
    note = [x for x in pg.lines if f"daily/weekly rows of {month} not listed" in x]
    assert note and pg.meta["found"] and all(c.attrs["oq.row_period"] == month for c in pg.cells)
    one = small.printings_page(day, "PDO-2", years="1987")
    assert one.meta["found"] and {c.attrs["oq.row_period"] for c in one.cells} == {day}
    exact = small.printings_data(month, "PDO-2", years="1987")
    every = small.printings_data(month, "PDO-2", years="1987", all_periods=True)    # the page is cut: data level
    assert exact["hidden"] > 0 and {f["period"] for f in exact["found"]} == {month} and every["hidden"] == 0
    assert {f["period"] for f in every["found"]} > {month, day} and len(every["found"]) > len(exact["found"])
    absent = next(d for d in (f"{month}-{i:02d}" for i in range(1, 29)) if d not in days)
    missing = small.printings_page(absent, "PDO-2", years="1987")
    assert missing.meta["found"] == 0 and any(x.startswith(f"  no row is dated {absent}; the daily/weekly rows of {month} are ")
                                              and day in x for x in missing.lines)


def test_fix_no_match_span_for_a_title_hint(small):
    """F6: a title-word --table outside the window gets the same issue-span diagnosis as an id (no 'check the id')."""
    pg = small.printings_page("1942", "Summary by Months and Calendar Years", years="2010-2011")
    assert any(x.startswith("  'Summary by Months and Calendar Years' is printed in issues 1950_02..1950_02 (") for x in pg.lines)
    assert any("later issues do not print 'Summary by Months and Calendar Years'" in x for x in pg.lines)
    assert not any("check the id" in x for x in pg.lines)
    none = small.printings_page("1942", "zzqxv wholly unknown title")
    assert any(x.startswith("  no table matches --table 'zzqxv wholly unknown title': try find_tables with fewer") for x in none.lines)


def test_fix_grep_hint_uses_the_tables_own_period(small):
    """F7: the 0-row --grep hint quotes the table's first filled period, never a fixed year."""
    pg = small.show_page("treasury_bulletin_2004_03.txt", 728, grep="zzqxv")
    hint = next(x for x in pg.lines if x.startswith("  0 rows: --grep is a case-insensitive substring of the filled period ("))
    assert re.search(r"filled period \((?:FY|CY)?\d{4}(?:-\d\d)?\)", hint) and "1942-12" not in O.GREP_HINT


def test_fix_sections_found_without_years_and_collapsed_per_heading(small):
    """F1: the [section] block is scored over every FTS match (no LIMIT), so the 1988_03 section is found
    without --years; issues repeating one heading collapse into one line with the span and the latest place."""
    pg = small.find_page("treasury financing operations auction notes")
    sec = [x for x in pg.lines if x.startswith("[section] TREASURY FINANCING OPERATIONS, OCTOBER-DECEMBER 1987 | ")]
    assert sec and "4 issues 1987_06..1988_03, latest treasury_bulletin_1988_03.txt:1688-" in sec[0]
    assert "sub-headings: Auction of 2-Year Notes (:1692)" in sec[0]
    assert sum(x.startswith("[section]") for x in pg.lines) <= O.SECTION_HITS
    pinned = small.find_page("treasury financing operations auction notes", years="1988")
    assert any(x.startswith("[section] TREASURY FINANCING OPERATIONS, OCTOBER-DECEMBER 1987 | treasury_bulletin_1988_03.txt:1688-")
               for x in pinned.lines)


def test_fix_partial_fallback_groups_span_every_matching_part(small):
    """F2: the OR fallback groups all matching parts before ranking, so a group line shows the same issue span
    and part count as the AND query of the same table."""
    and_line = next(x for x in small.find_page("outlays agency").lines if x.startswith("[FFO-3] On-budget"))
    or_page = small.find_page("zzqxv outlays agency")
    or_line = next(x for x in or_page.lines if x.startswith("[FFO-3] On-budget"))
    span = lambda s: s.split("  | ")[1].split(" | match:")[0]  # noqa: E731 - '9 issues A..B | rows ...'
    assert span(and_line) == span(or_line) and "match:2 of 3 tokens(" in or_line
    assert or_page.lines[0].startswith("find_tables('zzqxv outlays agency', years=None): ") and " groups" in or_page.lines[0]
    top = or_page.lines.index(next(x for x in or_page.lines if x.startswith("  [") and "] parts (" in x))
    assert re.match(r"^\s+treasury_bulletin_\d{4}_\d{2}\.txt:\d+ part\d", or_page.lines[top + 1])


def test_fix_top_group_parts_survive_a_cut_page(small):
    """replay F1: however small the page, the best group prints at least one file:line part line (whole block
    reserved; truncated with '... N more parts' when even that does not fit)."""
    pg = small.find_page("outlays", budget=O.MIN_BUDGET, all_hits=True)
    assert _bytes(pg.text) <= O.MIN_BUDGET and any(x.startswith("... OUTPUT CUT at 1500 bytes:") for x in pg.lines)
    first_block = next(i for i, x in enumerate(pg.lines) if x.startswith("  [") and "] parts (" in x)
    assert pg.lines[first_block].startswith("  [FFO-3] parts (") and re.match(r"^\s+treasury_bulletin_", pg.lines[first_block + 1])
    assert pg.cells and pg.cells[0].attrs["oq.table_ref"] == pg.lines[first_block + 1].split()[0]
    assert any(x.startswith("    ... ") and "more parts" in x for x in pg.lines)
    assert "parts shown for the first 1;" in pg.lines[-1]
    full = small.find_page("outlays", all_hits=True)
    assert pg.lines[1] == full.lines[1]                            # the group order is the same


def test_fix_shift_sibling_reads_the_index_header(small):
    """F5: the W0-8 sibling confirmation reads headers from the index, no sibling parse (FFO-3 2004_03 has no
    shift, so the memo stays empty and no sibling is analysed)."""
    t = small.table_at("treasury_bulletin_2004_03.txt", 728)
    a = small.analysis(t)
    assert small.shift_sibling(t, a) is None and not a.get("labelshift")
    rows = small._nearby_headers(t)
    assert rows and all(len(r) == 4 and " | " in (r[3] or "") for r in rows) and rows[0][2] != t["issue"]
    assert not hasattr(O, "SHIFT_SIBLINGS") and not hasattr(O, "PARTIAL_RIDS")
