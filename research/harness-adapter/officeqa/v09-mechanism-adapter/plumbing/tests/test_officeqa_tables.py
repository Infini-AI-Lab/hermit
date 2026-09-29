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
    assert '"otables-index-v9.1"' == meta["version"] and int(meta["files"]) == len(SMALL_FILES)
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
