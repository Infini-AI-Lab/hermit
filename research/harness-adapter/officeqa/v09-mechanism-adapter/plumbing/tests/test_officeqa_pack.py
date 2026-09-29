"""Tests of the OfficeQA pack: observation re-derivation, question spec, pack predicates/offers, seeds, leak
screen, and one end-to-end gate check through the harness_core ledger/gate.

Observation and end-to-end tests read the corpus and a small tables index (built per session, < 1 s);
predicate/offer unit tests use synthetic events, a stub tables object and a fake ComputeRunner.
No answer values are used or printed anywhere.
Run: adapter-comparison-work/.venv/bin/python -m pytest tests/test_officeqa_pack.py -q
"""
from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness_core import contracts as C  # noqa: E402
from harness_core.engine import RuleEngine  # noqa: E402
from harness_core.numeric import code_literals  # noqa: E402
from harness_core import rules as core_rules  # noqa: E402
from harness_core import predicates as core_predicates, offers as core_offers  # noqa: E402
from packs.officeqa import observe, rules as R, spec as S  # noqa: E402
from packs.officeqa.tools import cli, otables as O  # noqa: E402
from packs.officeqa.pack import OfficeQAPack  # noqa: E402

CORPUS = Path(os.environ.get("OFFICEQA_CORPUS") or ROOT.parent / "opus-officeqa-133" / "corpus")
QUESTIONS = ROOT.parent / "opus-officeqa-133" / "questions.json"
SMALL_FILES = ["treasury_bulletin_1987_06.txt", "treasury_bulletin_1987_09.txt", "treasury_bulletin_1987_12.txt",
               "treasury_bulletin_1988_03.txt", "treasury_bulletin_2003_06.txt", "treasury_bulletin_2004_03.txt",
               "treasury_bulletin_1944_03.txt", "treasury_bulletin_1970_06.txt", "treasury_bulletin_2001_03.txt"]
needs_corpus = pytest.mark.skipif(not (CORPUS / SMALL_FILES[0]).is_file(), reason="OfficeQA corpus not available")


@pytest.fixture(scope="session")
def small_index(tmp_path_factory):
    if not (CORPUS / SMALL_FILES[0]).is_file():
        pytest.skip("OfficeQA corpus not available")
    out = tmp_path_factory.mktemp("otables-pack") / "tables.sqlite"
    O.build_index(CORPUS, out, files=SMALL_FILES)
    return out


@pytest.fixture(scope="session")
def tables(small_index):
    t = O.Tables(CORPUS, small_index)
    yield t
    t.close()


def refs():
    c = itertools.count(1)
    return lambda: f"c{next(c)}"


def run_cli(tables, argv):
    """What the model would see: the page text the CLI prints for argv (tool first)."""
    return cli.page(argv[0], cli.parser(argv[0]).parse_args(argv[1:]), tables).text + "\n"


def obs(tables, command, output):
    return observe.observations(command, output, source="x1", call=3, t=1.5, next_ref=refs(), tables=tables)


CODEX_HEAD = "Chunk ID: 9f00aa\nWall time: 0.2 seconds\nProcess exited with code 0\nOriginal token count: 900\nOutput:\n"

# ---------------------------------------------------------------------------------------------
# observations: pack tool pages
# ---------------------------------------------------------------------------------------------


@needs_corpus
def test_show_table_page_cells_become_observations(tables):
    cmd = "show_table treasury_bulletin_1987_06.txt 463 --match judiciary"
    out = run_cli(tables, cmd.split())
    evs = obs(tables, cmd, CODEX_HEAD + out)
    page = tables.show_page("treasury_bulletin_1987_06.txt", 463, match="judiciary")
    assert len(evs) == len(page.cells) == 21
    e = next(x for x in evs if x.attrs["oq.period"] == "1987-01")
    assert e.kind == "observe" and e.source == "x1" and e.call == 3 and e.ref.startswith("c")
    assert e.locator == "treasury_bulletin_1987_06.txt:482" and e.value is not None and e.text
    assert e.attrs["oq.table"] == "FFO-3" and e.attrs["oq.file"] == "treasury_bulletin_1987_06.txt"
    assert e.attrs["oq.column"] == "The Judiciary" and e.attrs["oq.col"] == "c2"
    assert e.attrs["oq.table_ref"] == "treasury_bulletin_1987_06.txt:463"
    for k in ("oq.flags", "oq.repair", "oq.identity_fail", "oq.page", "oq.units", "oq.issue"):
        assert k in e.attrs
    assert e.attrs["oq.page"] == "1-21/21" and isinstance(e.attrs["oq.flags"], list)
    assert len({x.ref for x in evs}) == len(evs)


@needs_corpus
def test_pipes_truncation_and_from_row_limit_cells_to_what_was_seen(tables):
    cmd = "show_table treasury_bulletin_2004_03.txt 728 --stitch"
    page = tables.show_page("treasury_bulletin_2004_03.txt", 728, stitch=True)
    out = page.text + "\n"
    full = obs(tables, cmd, out)
    key = lambda c: (c.locator, c.attrs["oq.table_ref"], c.attrs["oq.col"])  # noqa: E731
    assert {key(e) for e in full} == {key(c) for c in page.cells}
    lines = page.lines
    first_row = min(c.line for c in page.cells)
    n = first_row + 4
    head = obs(tables, cmd + f" | head -n {n}", "\n".join(lines[:n]))
    assert head and {key(e) for e in head} == {key(c) for c in page.cells if c.line < n} != {key(c) for c in page.cells}
    # Codex head+tail truncation: the rows touching the marker are cut and yield nothing
    cut_at = first_row + 3
    text = ("\n".join(lines[:cut_at]) + "\n" + lines[cut_at][:25] + "…5120 chars truncated…"
            + lines[-5][30:] + "\n" + "\n".join(lines[-4:]))
    tr = obs(tables, cmd, CODEX_HEAD + "Warning: truncated output (original token count: 3000)\nTotal output lines: 40\n\n" + text)
    want = {key(c) for c in page.cells if c.line < cut_at or c.line > len(lines) - 5}
    assert tr and {key(e) for e in tr} == want
    # a continuation page only yields its own rows
    m = observe.HEADER_RX.match(lines[0])
    nxt = int(m.group(6))
    cmd2 = f"show_table treasury_bulletin_2004_03.txt 728 --stitch --from-row {nxt}"
    p2 = obs(tables, cmd2, run_cli(tables, cmd2.split()))
    assert p2 and not {e.locator for e in p2} & {e.locator for e in full if e.attrs["oq.page"].startswith("1-")}
    assert all(e.attrs["oq.page"].startswith(f"{nxt + 1}-") for e in p2)


@needs_corpus
def test_invocation_forms_modes_and_loop_fallback(tables):
    base = ["show_table", "treasury_bulletin_1987_06.txt", "463", "--match", "judiciary", "--long"]
    out = run_cli(tables, base)
    n = len(obs(tables, " ".join(base), out))
    assert n == 21
    for cmd in ("python3 packs/officeqa/tools/cli.py " + " ".join(base),
                "python -m packs.officeqa.tools.cli " + " ".join(base),
                "python -m packs.officeqa.cli " + " ".join(base),          # pre-move module path still parsed
                "cd /ws && OFFICEQA_CORPUS=/c /ws/.harness/bin/" + " ".join(base) + " 2>&1",
                "timeout 30 " + " ".join(base) + " | cat"):
        assert len(obs(tables, cmd, out)) == n, cmd
    # a loop the parser cannot expand: pages are recovered from their 'TABLE ... | file:line | ...' first line
    a = run_cli(tables, ["show_table", "treasury_bulletin_1987_06.txt", "463"])
    b = run_cli(tables, ["show_table", "treasury_bulletin_1970_06.txt", "4946"])
    loop = 'for x in "treasury_bulletin_1987_06.txt 463" "treasury_bulletin_1970_06.txt 4946"; do show_table $x; done'
    evs = obs(tables, loop, a + b)
    files = {e.attrs["oq.file"] for e in evs}
    assert files == {"treasury_bulletin_1987_06.txt", "treasury_bulletin_1970_06.txt"}
    y_unknown = [e for e in evs if e.attrs["oq.file"] == "treasury_bulletin_1970_06.txt"]
    assert y_unknown and all("YEAR_UNKNOWN" in e.attrs["oq.flags"] for e in y_unknown if e.attrs["oq.row_period"].startswith("Y?"))


@needs_corpus
def test_printings_and_find_tables_observations(tables):
    cmd = 'printings --table FFO-3 --row 1987-01 --col judiciary'
    out = run_cli(tables, ["printings", "--table", "FFO-3", "--row", "1987-01", "--col", "judiciary"])
    evs = obs(tables, cmd, out)
    listing = [e for e in evs if not e.attrs.get("oq.summary")]
    assert [e.attrs["oq.issue"] for e in listing] == ["1987_06", "1987_09", "1987_12", "1988_03"]
    assert all(e.attrs["oq.printing_of"] == "FFO-3|1987-01|judiciary" and e.value is not None for e in evs)
    assert {e.locator for e in evs if e.attrs.get("oq.summary")} <= {e.locator for e in listing}
    fcmd = "find_tables FFO-3 --years 1987 --all"
    hits = obs(tables, fcmd, run_cli(tables, fcmd.split()))
    assert hits and all(e.attrs["oq.kind"] == "table_hit" and e.value is None and C.VALUES_ATTR not in e.attrs for e in hits)
    assert "treasury_bulletin_1987_06.txt:463" in {e.locator for e in hits}


@needs_corpus
def test_unparsable_or_foreign_outputs_never_raise(tables):
    for cmd, out in (("show_table", "garbage"), ("show_table nosuch.txt 1", "TABLE X | nosuch.txt:1 | part 1 | page rows 1-1 of 1"),
                     ("printings --table", ""), ("echo 'unterminated", "x"), ("", ""), ("ls", "a\nb")):
        assert isinstance(obs(tables, cmd, out), list)

# ---------------------------------------------------------------------------------------------
# observations: raw corpus lines
# ---------------------------------------------------------------------------------------------


@needs_corpus
def test_raw_line_commands_are_verified_against_the_corpus(tables):
    f = "treasury_bulletin_1987_06.txt"
    lines = tables.lines(f)
    rng = range(478, 486)
    # rg / grep -rn: 'path/file:line:text'
    rg = "\n".join(f"corpus/{f}:{n}:{lines[n - 1]}" for n in rng)
    evs = obs(tables, "rg -n Jan corpus", rg)
    assert [e.locator for e in evs] == [f"{f}:{n}" for n in rng]
    e = evs[4]
    n4 = int(e.locator.rsplit(":", 1)[1])
    assert e.attrs[C.VALUES_ATTR] == O.numbers(lines[n4 - 1])
    assert e.attrs["oq.table_ref"] == f"{f}:463" and e.value is None and len(e.text) <= 200
    # grep -n on one file: 'line:text'
    g = "\n".join(f"{n}:{lines[n - 1]}" for n in rng)
    assert [x.locator for x in obs(tables, f"grep -n 1987 corpus/{f}", g)] == [f"{f}:{n}" for n in rng]
    # sed -n range (unnumbered), with Codex header
    sed = CODEX_HEAD + "\n".join(lines[n - 1] for n in rng)
    assert [x.locator for x in obs(tables, f"sed -n '478,485p' corpus/{f}", sed)] == [f"{f}:{n}" for n in rng]
    # cat -n
    catn = "\n".join(f"{n:>6}\t{lines[n - 1]}" for n in rng)
    assert len(obs(tables, f"cat -n corpus/{f} | sed -n '478,485p'", catn)) == len(rng)
    # a tampered line is not an observation of the corpus
    bad = f"corpus/{f}:482:" + lines[481].replace("|", "| 999999 |", 1)
    got = obs(tables, "rg -n x corpus", bad)
    assert len(got) == 1 and got[0].locator == "x1:L1" and got[0].attrs["oq.via"] == "raw"
    assert 999999.0 in got[0].attrs[C.VALUES_ATTR] and "oq.table_ref" not in got[0].attrs
    # numbers printed in any other format still reach the ledger as raw lines (grounding), words do not
    raw = obs(tables, "python3 script.py", "Jan 1987 judiciary: 78\nno numbers here\ntotal = 1,234.5")
    assert [(e.locator, e.attrs[C.VALUES_ATTR]) for e in raw] == [("x1:L1", [1987.0, 78.0]), ("x1:L3", [1234.5])]


@needs_corpus
def test_officeqa_read_outputs_dict_and_json(tables):
    import officeqa_tools
    tool = officeqa_tools.OfficeQACorpus(CORPUS, CORPUS / "unused.sqlite")
    r = tool.read("treasury_bulletin_1944_03.txt", 4015, 12)
    for text in (repr(r), json.dumps(r, indent=2), str(r)):
        evs = obs(tables, "python -c 'from officeqa import OfficeQACorpus; print(c.read(...))'", text)
        nums = [e.locator for e in evs if e.attrs["oq.via"] != "raw"]
        assert nums and all(x.startswith("treasury_bulletin_1944_03.txt:") for x in nums)
        assert {int(x.rsplit(":", 1)[1]) for x in nums} <= set(range(4015, 4027))
    # a truncation marker inside the content drops the two cut lines only
    t = repr(r)
    mid = t.index("4020: ")
    cut = t[:mid + 10] + "…300 chars truncated…" + t[mid + 400:]
    got = {int(e.locator.rsplit(":", 1)[1]) for e in obs(tables, "python read", cut) if e.attrs["oq.via"] == "read"}
    assert 4020 not in got and 4015 in got
    # officeqa.py search results: excerpt lines are located inside the chunk's start_line..end_line
    f = "treasury_bulletin_1944_03.txt"
    lines = tables.lines(f)
    excerpt = " … tail of a line\n" + "\n".join(lines[4016:4020]) + "\nhead of a line … "
    res = {"query": "q", "results": [{"filename": f, "start_line": 3990, "end_line": 4089, "excerpt": excerpt, "rank": -3.2}]}
    s_evs = [e for e in obs(tables, "python -c 'print(c.search(...))'", repr(res)) if e.attrs["oq.via"] == "search"]
    assert [e.locator for e in s_evs] == [f"{f}:{n}" for n in range(4017, 4021) if len(lines[n - 1].strip()) >= 8]


@needs_corpus
def test_line_cap(tables):
    f = "treasury_bulletin_1944_03.txt"
    lines = tables.lines(f)
    big = "\n".join(f"{f}:{n}:{lines[n - 1]}" for n in range(1, 1200) if lines[n - 1].strip())
    assert len(obs(tables, "rg -n . corpus", big)) == observe.LINE_CAP

# ---------------------------------------------------------------------------------------------
# question spec
# ---------------------------------------------------------------------------------------------


def _q(n):
    if not QUESTIONS.is_file():
        pytest.skip("questions.json not available")
    return next(x["question"] for x in json.loads(QUESTIONS.read_text()) if x["ordinal"] == n)


HAND = {   # hand-checked against the question texts (count, list, decimals, form, type, pinned, periods)
    1: (3, True, (3, 3, 3), "decimal", "number", False, ("FY2011", "FY2019")),
    7: (1, False, (3,), "decimal", "number", False, ("1938-12", "1940-12")),
    9: (1, False, (3,), "unspecified", "number", False, ("CY1972", "CY1976")),
    11: (1, False, (0,), "unspecified", "number", True, ("CY1960", "CY1969")),
    12: (1, False, (4,), "unspecified", "number", True, ("FY1991", "FY2010")),
    13: (1, False, (2,), "percent", "number", False, ("CY2002", "CY2012")),
    14: (1, False, (2,), "percent", "number", False, ("CY2010", "CY2011")),
    16: (3, True, (2, 3, 3), "percent", "number", False, ("FY2007", "FY2013")),
    18: (1, False, (None,), "unspecified", "number", True, ("CY1960", "CY1969")),
    22: (1, False, (2,), "percent", "number", False, ("1961-10",)),
    25: (12, True, (), "unspecified", "number", True, ("1969-01", "1980-01")),
    26: (1, False, (0,), "unspecified", "integer", False, ("1977-01", "1978-01")),
    37: (1, False, (3,), "decimal", "number", False, ("2009", "2011")),
    41: (2, True, (3, 3), "unspecified", "number", True, ()),
    49: (1, False, (3,), "unspecified", "number", True, ("1982-08", "1981-08")),
    53: (1, False, (2,), "unspecified", "number", False, ("FY1948", "FY1964")),
    59: (1, False, (), "unspecified", "year", False, ("1950", "1990")),
    63: (2, True, (None, None), "unspecified", "number", True, ("CY2013", "CY2019")),
    67: (3, True, (2, 0, 0), "unspecified", "number", False, ("1990", "1998")),
    78: (2, True, (1, 0), "percent", "number", False, ("2000-03", "2005-03")),
    86: (1, False, (3,), "unspecified", "number", False, ("1984-01", "1987-03")),
    88: (1, False, (2,), "unspecified", "number", False, ("FY1981",)),
    91: (2, True, (3, 0), "unspecified", "number", False, ("1949-11", "1949-12")),
    95: (1, False, (), "unspecified", "number", False, ("2000-12",)),
    116: (1, False, (3,), "percent", "number", False, ("CY2018", "CY2019")),
    122: (1, False, (2,), "unspecified", "number", False, ("2001-03", "2006-03", "2011-03")),
    124: (1, False, (0,), "unspecified", "number", False, ("CY1989", "CY2013", "CY2025")),
    125: (1, False, (1,), "unspecified", "number", True, ("1995-07", "1998-03")),
    130: (1, False, (3,), "unspecified", "number", False, ("FY2018",)),
}


@pytest.mark.parametrize("n", sorted(HAND))
def test_question_spec_hand_checked(n):
    sp = S.question_spec(_q(n))
    count, lst, dec, form, vt, pin, pers = HAND[n]
    assert (sp.n_values, sp.list_format, sp.decimals, sp.percent_form, sp.value_type, sp.pins_date) == \
        (count, lst, dec, form, vt, pin)
    assert all(p in sp.periods for p in pers), (pers, sp.periods)
    assert sp.keywords and all(w == w.lower() for w in sp.keywords)


def test_question_spec_all_133_deterministic():
    if not QUESTIONS.is_file():
        pytest.skip("questions.json not available")
    qs = json.loads(QUESTIONS.read_text())
    specs = [S.question_spec(x["question"]) for x in qs]
    assert specs == [S.question_spec(x["question"]) for x in qs]
    assert len(specs) == 133
    for sp in specs:
        assert sp.n_values is None or sp.n_values >= 1
        assert not sp.decimals or sp.n_values is None or len(sp.decimals) == sp.n_values
        assert all(isinstance(p, str) and (p[:2] in ("FY", "CY") or p[:4].isdigit()) for p in sp.periods)
    assert 20 <= sum(sp.pins_date for sp in specs) <= 40
    assert sum(sp.n_values and sp.n_values > 1 for sp in specs) >= 15


def test_question_spec_synthetic_rules():
    sp = S.question_spec("As reported in the March 1990 Treasury Bulletin, what were FY 1988 outlays? Round to two decimal places.")
    assert sp.pins_date and sp.periods == ("FY1988",) and sp.decimals == (2,)
    sp = S.question_spec("Using the most recently published figures, what was the total for calendar years 2001 through 2003?")
    assert not sp.pins_date and sp.periods == ("CY2001", "CY2002", "CY2003")
    sp = S.question_spec("Adjust using the CPI-U (1982-84=100) for the period from January 1984 to March 1984.")
    assert sp.periods == ("1984-01", "1984-02", "1984-03")
    sp = S.question_spec("How many months in 1975 exceeded 10 percent? Report the value as a percent value.")
    assert sp.value_type == "integer" and sp.percent_form == "percent"
    sp = S.question_spec("By how many percentage points did the rate change, rounded to the nearest hundredth?")
    assert sp.value_type == "number" and sp.decimals == (2,)
    assert S.canonical(" [$1,234.50, 12.3%] ") == "1234.50 12.3%"
    assert S.canonical("Deficit") == "deficit"

# ---------------------------------------------------------------------------------------------
# predicates and offers (synthetic events, stub tables, fake runner)
# ---------------------------------------------------------------------------------------------


class FakeRunner:
    """In-process executor for tests (the real runner is harness_core.runner.SandboxRunner)."""
    def __init__(self):
        self.calls = []

    def run(self, code, *, timeout_s=5.0):
        self.calls.append(code)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                exec(compile(code, "<calc>", "exec"), {})
        except Exception:                               # noqa: BLE001
            return False, ""
        return True, buf.getvalue()


class StubTables:
    def __init__(self, latest=None, columns=None, rows=None, identities=(), db=None):
        self.latest, self.columns, self.rows = latest or {}, columns or {}, rows or {}
        self.identities, self.db = list(identities), db

    def latest_printing(self, rec):
        return self.latest.get(rec["locator"])

    def table_at(self, fname, line):
        return {"file": fname, "hdr_line": int(line)}

    def column_cells(self, t, col):
        return self.columns.get((t["file"], t["hdr_line"], col), [])

    def row_cells(self, t, line):
        return self.rows.get((t["file"], t["hdr_line"], line), [])

    def analysis(self, t):
        return {"identities": self.identities}

    def con(self):
        return self.db


class StubPack(OfficeQAPack):
    def __init__(self, stub):
        super().__init__()
        self._tables = stub


def rec(loc, text, value, *, col="c2", column="The Judiciary", period="1987-01", row_period=None, flags=(),
        ref="f.txt:463", issue="1987_06", identity_fail=False, repair=""):
    attrs = {"oq.table": "FFO-3", "oq.table_ref": ref, "oq.file": loc.split(":")[0], "oq.issue": issue, "oq.part": 1,
             "oq.col": col, "oq.column": column, "oq.row": period, "oq.section": "", "oq.period": period,
             "oq.row_period": period if row_period is None else row_period, "oq.col_period": "",
             "oq.flags": list(flags), "oq.repair": repair, "oq.identity_fail": identity_fail, "oq.units": ""}
    return {"locator": loc, "text": text, "value": value, "attrs": attrs}


def ob(ref, r, call=1):
    return C.Observe(kind="observe", ref=ref, call=call, t=float(call), source="x1", locator=r["locator"], text=r["text"],
                     value=r["value"], attrs=dict(r["attrs"]))


def calc(ref, code, output, links, call=2):
    """Compute event as the ledger records it; `links` maps a literal's text to the observe ref it came from."""
    inputs = []
    for lit in code_literals(code):
        entry = {"literal": lit.text, "value": lit.value, "span": [lit.start, lit.end], "observe": links.get(lit.text), "match": None}
        if lit.trivial:
            entry["trivial"] = True
        elif entry["observe"]:
            entry["match"] = "exact"
        inputs.append(entry)
    return C.Compute(kind="compute", ref=ref, call=call, t=float(call), source="x2", code=code,
                     code_sha256=C.sha256_text(code), output=output, values=O.numbers(output), inputs=inputs)


def ctx_for(question, events, answer, stub, runner=None):
    pack = StubPack(stub)
    cand = C.Candidate(kind="candidate", ref="a1", call=9, t=9.0, answer=answer)
    return C.RuleContext(question=question, spec=pack.question_spec(question), events=list(events) + [cand],
                         candidate=cand, pack=pack, runner=runner or FakeRunner())


Q_JUD = "What were the outlays of the judiciary in January 1987, in millions of dollars? Round to one decimal place."


def test_linked_cells_via_ledger_links_fallback_and_typed_answers():
    r = rec("f.txt:482", "78", 78.0)
    e1 = ob("c1", r)
    k = calc("k1", "x = 78\nprint(x * 2)", "156", {"78": "c1"})
    ctx = ctx_for(Q_JUD, [e1, k], "156.0", StubTables())
    links = R.linked_cells(ctx)
    assert len(links) == 1 and links[0]["observe"].ref == "c1" and links[0]["compute"].ref == "k1" and links[0]["depth"] == 1
    k2 = calc("k1", "x = 78\nprint(x * 2)", "156", {})          # ledger left the input unlinked
    assert R.linked_cells(ctx_for(Q_JUD, [e1, k2], "156.0", StubTables()))[0]["observe"].ref == "c1"
    e3 = ob("c3", rec("f.txt:483", "781", 781.0))
    typed = R.linked_cells(ctx_for(Q_JUD, [e1, e3], "781", StubTables()))
    assert typed[0]["compute"] is None and typed[0]["cell"]["locator"] == "f.txt:483"
    assert R.linked_cells(ctx_for(Q_JUD, [e1], "78", StubTables())) == []      # < 3 significant digits: no typed link
    assert R.linked_cells(ctx_for(Q_JUD, [e3], "781.0", StubTables()))         # exact value, any format
    assert R.linked_cells(ctx_for(Q_JUD, [e3], "781.4", StubTables())) == []   # rounding is not a typed link
    k3 = calc("k2", "y = 156\nprint(y + 1)", "157", {"156": "k1"}, call=3)   # calc of a calc
    chain = R.linked_cells(ctx_for(Q_JUD, [e1, k, k3], "157", StubTables()))
    assert chain and chain[0]["depth"] == 2 and chain[0]["observe"].ref == "c1"


def test_cell_flags_never_on_plain_revision_marks():
    r = rec("f.txt:482", "r 781", 781.0, flags=["r", "p"])
    ctx = ctx_for(Q_JUD, [ob("c1", r)], "781", StubTables())
    assert R.cell_flags(ctx) == []
    assert R.cell_flags(ctx, flags=["r", "p"]) == []
    for flags, name, extra in ((["SHIFT+1"], "repair", {"repair": "SHIFT+1"}), (["YEAR_UNKNOWN"], "Y?", {}),
                               (["IDENTITY_FAIL"], "identity_fail", {"identity_fail": True})):
        r2 = rec("f.txt:482", "781", 781.0, flags=flags, **extra)
        got = R.cell_flags(ctx_for(Q_JUD, [ob("c1", r2)], "781", StubTables()))
        assert len(got) == 1 and got[0].values["flag"]["name"] == name and got[0].values["flag"]["check"]
    r3 = rec("f.txt:482", "781", 781.0, flags=["year+1 inferred"])
    assert R.cell_flags(ctx_for(Q_JUD, [ob("c1", r3)], "781", StubTables())) == []
    assert R.cell_flags(ctx_for(Q_JUD, [ob("c1", r3)], "781", StubTables()), flags=["year_inferred"])
    # a year x month matrix cell: 'Y?-11' column resolved by the row year is not an unknown year
    r4 = rec("f.txt:482", "781", 781.0, period="1932-11")
    r4["attrs"]["oq.col_period"] = "Y?-11"
    assert R.cell_flags(ctx_for(Q_JUD, [ob("c1", r4)], "781", StubTables())) == []


def test_later_printing_differs_and_offer_reruns_the_calc():
    r = rec("f.txt:482", "78", 78.0)
    latest = {"f.txt:482": {"issue": "1988_03", "locator": "g.txt:940", "text": "79", "value": 79.0, "ambiguous": False}}
    k = calc("k1", "x = 78\nprint(round(x * 1.5, 1))", "117.0", {"78": "c1"})
    runner = FakeRunner()
    ctx = ctx_for(Q_JUD, [ob("c1", r), k], "117.0", StubTables(latest), runner)
    got = R.later_printing_differs(ctx)
    assert len(got) == 1
    b = got[0].values
    assert b["printing"]["locator"] == "g.txt:940" and b["cell"]["locator"] == "f.txt:482" and b["compute"]["ref"] == "k1"
    res = R.substitute_printing(ctx, got[0])
    assert isinstance(res, C.OfferResult) and res.answer == "118.5" and "g.txt:940" in res.derivation
    assert runner.calls and "79" in runner.calls[0] and res.compute_sha256 == C.sha256_text(runner.calls[0])
    # same value in the latest printing, pinned questions, estimates, ambiguous printings: silent
    same = {"f.txt:482": {**latest["f.txt:482"], "value": 78.0, "text": "78"}}
    assert R.later_printing_differs(ctx_for(Q_JUD, [ob("c1", r), k], "117.0", StubTables(same))) == []
    pinned = "According to the June 1987 Treasury Bulletin, " + Q_JUD
    assert R.later_printing_differs(ctx_for(pinned, [ob("c1", r), k], "117.0", StubTables(latest))) == []
    est = rec("f.txt:482", "78", 78.0, period="FY1987 est")
    assert R.later_printing_differs(ctx_for(Q_JUD, [ob("c1", est), k], "117.0", StubTables(latest))) == []
    amb = {"f.txt:482": {**latest["f.txt:482"], "ambiguous": True}}
    assert R.later_printing_differs(ctx_for(Q_JUD, [ob("c1", r), k], "117.0", StubTables(amb))) == []


def test_offer_for_a_typed_answer_keeps_the_format():
    r = rec("f.txt:482", "1,078", 1078.0)
    latest = {"f.txt:482": {"issue": "1988_03", "locator": "g.txt:940", "text": "1,079", "value": 1079.0, "ambiguous": False}}
    q = "What were the outlays of the judiciary in January 1987, in millions of dollars?"
    ctx = ctx_for(q, [ob("c1", r)], "$1,078 million", StubTables(latest))
    got = R.later_printing_differs(ctx)
    res = R.substitute_printing(ctx, got[0])
    assert res.answer == "$1,079 million"
    # rescaled typing (billions): the substitution is rescaled the same way
    ctx2 = ctx_for(q, [ob("c1", r)], "1.078", StubTables(latest))
    res2 = R.substitute_printing(ctx2, R.later_printing_differs(ctx2)[0])
    assert res2.answer == "1.079"


def test_period_consistency_rules():
    P = R.parse_period
    assert not R.consistent(P("FY2010"), P("CY2010")) and R.consistent(P("2010"), P("FY2011"))
    assert R.consistent(P("2009-12"), P("FY2010")) and not R.consistent(P("2010-12"), P("FY2010"))
    assert R.consistent(P("1975-12"), P("FY1976")) and not R.consistent(P("1976-08"), P("FY1976"))
    assert R.consistent(P("1976-12"), P("FY1977")) and R.consistent(P("FY1970"), P("1970-03"))
    assert not R.consistent(P("1970-04"), P("1970-03")) and R.consistent(P("2011-09-30"), P("2011-09"))
    assert P("Y?-03") is None and P("FY2004 est") is None and P("FY2004 to date") is None


def test_period_mismatch_fy_vs_cy_with_alternative_cell():
    fy = rec("s.txt:2050", "5,000", 5000.0, period="FY2000", col="c8", column="(8)", ref="s.txt:2040")
    cy = rec("s.txt:2060", "5,100", 5100.0, period="CY2000", col="c8", column="(8)", ref="s.txt:2040")
    stub = StubTables(columns={("s.txt", 2040, 8): [fy, cy]})
    q = "What was the amount for calendar year 2000, in millions of dollars?"
    k = calc("k1", "a = 5000\nprint(a)", "5000", {"5000": "c1"})
    ctx = ctx_for(q, [ob("c1", fy), k], "5000", stub)
    got = R.period_mismatch(ctx)
    assert len(got) == 1 and got[0].values["alt"]["locator"] == "s.txt:2060" and got[0].values["question"]["period"] == "CY2000"
    assert R.substitute_period(ctx, got[0]).answer == "5100"
    assert R.period_mismatch(ctx_for(q, [ob("c1", cy), k], "5100", stub)) == []
    q2 = "What was the amount in the calendar year that the war began?"
    assert R.period_mismatch(ctx_for(q2 + " Use 2000 data.", [ob("c1", fy), k], "5000", stub)) == []
    no_alt = StubTables()
    assert R.period_mismatch(ctx_for(q, [ob("c1", fy), k], "5000", no_alt)) == []
    assert len(R.period_mismatch(ctx_for(q, [ob("c1", fy), k], "5000", no_alt), require_alt=False)) == 1


def test_better_table_and_row_coverage():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t(hdr_line INT, tid TEXT, tid_raw TEXT, title TEXT, subtitle TEXT, section TEXT, header TEXT,"
               " squash TEXT, kind TEXT, part INT, file TEXT)")
    rows = [(100, "PDO-1", None, "Maturity Schedule of Interest-Bearing Marketable Public Debt", "", "", "Date | Amount", "", "data", 1),
            (300, "SB-2", None, "Sales and Redemptions of Savings Bonds", "", "", "Period | Sales | Redemptions", "", "data", 1),
            (500, "SN-1", None, "Savings Notes Sales Redemptions and Amounts Outstanding", "", "", "Period | Sales | Redemptions | Outstanding",
             "", "data", 1)]
    db.executemany("INSERT INTO t VALUES (?,?,?,?,?,?,?,?,?,?,'f.txt')", rows)
    r = rec("f.txt:310", "310", 310.0, ref="f.txt:300", period="1982-07")
    q = "What was the average monthly amount of savings notes sales redemptions and amounts outstanding in 1982?"
    got = R.better_table(ctx_for(q, [ob("c1", r)], "310", StubTables(db=db)))
    assert len(got) == 1 and got[0].values["table"]["other"] == "SN-1" and got[0].values["table"]["used"] == "SB-2"
    assert R.better_table(ctx_for("What were savings bond sales in 1982?", [ob("c1", r)], "310", StubTables(db=db))) == []
    # row coverage
    cells = [rec("f.txt:741", str(v), float(v), col=f"c{i}", column=lab, period="2003-01", ref="f.txt:728")
             for i, (v, lab) in enumerate([(300, "Legislative branch (1)"), (400, "Judicial branch (2)"),
                                           (500, "Department of Commerce (4)"), (600, "Department of Energy (7)"),
                                           (9999, "Total outlays (37)")], start=1)]
    stub = StubTables(rows={("f.txt", 728, 741): cells}, identities=[{"total": 5, "run": [1, 2, 3, 4], "bad": []}])
    k = calc("k1", "print(300 + 400)", "700", {"300": "c1", "400": "c2"})
    q = "What were the total outlays for January 2003 across all listed agencies except the Department of Commerce?"
    got = R.row_coverage(ctx_for(q, [ob("c1", cells[0]), ob("c2", cells[1]), k], "700", stub))
    assert len(got) == 1
    row = got[0].values["row"]
    assert row["used"] == 2 and row["unused_n"] == 1 and "Energy" in row["unused"] and "Commerce" not in row["unused"]
    assert R.row_coverage(ctx_for("What was the difference in January 2003?", [ob("c1", cells[0]), ob("c2", cells[1]), k],
                                  "700", stub)) == []


def test_seed_rules_validate_and_render_through_the_core_engine():
    pack = OfficeQAPack()
    names_p = set(core_predicates.REGISTRY) | set(pack.predicates())
    names_o = set(core_offers.REGISTRY) | set(pack.offers())
    seeds = pack.seed_rules()
    assert [r.id for r in seeds] == ["period_match", "later_printing", "flagged_cells", "table_alternatives", "row_coverage"]
    for r in seeds:
        assert core_rules.validate_rule(r, predicates=names_p, offers=names_o) == [], r.id
        assert r.scope == "pack" and r.origin == "seed" and len(r.finding.encode()) <= 200
    assert not set(pack.predicates()) & set(core_predicates.REGISTRY)
    r = rec("f.txt:482", "78", 78.0)
    latest = {"f.txt:482": {"issue": "1988_03", "locator": "g.txt:940", "text": "79", "value": 79.0, "ambiguous": False}}
    k = calc("k1", "x = 78\nprint(round(x * 1.5, 1))", "117.0", {"78": "c1"})
    ctx = ctx_for(Q_JUD, [ob("c1", r), k], "117.0", StubTables(latest))
    findings, trace = RuleEngine().evaluate_trace(ctx, seeds)
    assert [f.rule for f in findings] == ["later_printing"]
    f = findings[0]
    assert "g.txt:940" in f.text and "f.txt:482" in f.text and "?" not in f.text and len(f.text.encode()) <= 200
    assert f.offer and f.offer.answer == "118.5"
    assert {t["rule"]: t["status"] for t in trace}["period_match"] == "silent"


def test_predicates_are_fast_on_long_ledgers():
    evs = []
    for i in range(1, 3001):
        evs.append(ob(f"c{i}", rec(f"f.txt:{i}", str(i), float(i))))
    evs.append(calc("k1", "print(17 + 2999)", "3016", {"17": "c17", "2999": "c2999"}, call=5))
    latest = {"f.txt:17": {"issue": "1988_03", "locator": "g.txt:1", "text": "18", "value": 18.0, "ambiguous": False}}
    ctx = ctx_for(Q_JUD, evs, "3016", StubTables(latest))
    t0 = time.time()
    for name, fn in R.PREDICATES.items():
        fn(ctx)
    assert time.time() - t0 < 2.0

# ---------------------------------------------------------------------------------------------
# seeds, conventions, leak screen, protocol
# ---------------------------------------------------------------------------------------------


def test_seed_text_notes_and_conventions():
    pack = OfficeQAPack()
    notes = pack.seed_notes()
    assert {n.id for n in notes} >= {"oq_latest_printing", "oq_period_match", "oq_flagged_cells", "oq_table_choice", "oq_row_coverage"}
    assert all(len(n.text.encode()) <= C.NOTE_MAX and n.origin == "seed" and n.triggers for n in notes)
    text = pack.seed_text()
    assert len(text.encode()) <= 4096 and all(n.text in text for n in notes)
    conv = pack.task_conventions()
    assert "maximum precision" in conv and "most up-to-date revision" in conv and "exact date" in conv
    budget = len(text.encode()) + sum(len(r.finding.encode()) for r in pack.seed_rules())
    assert budget < C.TEXT_BUDGET
    assert "find_tables" in pack.usage() and "--from-row" in pack.usage()


def test_leak_screen():
    pack = OfficeQAPack()
    assert pack.leak_screen("Prefer the latest printing of FY rows; round at the end.", ["12345.67", "0.123"]) == []
    found = pack.leak_screen("Use 12,345.67 from treasury_bulletin_2004_03.txt for q081.", ["12345.67"])
    assert any("file" in f for f in found) and any("question ordinal" in f for f in found)
    assert any("released answer #0" in f for f in found)
    assert all("12345" not in f.split("large number")[0] for f in found if "released" in f)
    assert pack.leak_screen("the answer is surplus overall", ["surplus overall"]) == ["the text of released answer #0"]
    assert pack.leak_screen("use 100 and 1990 and 12", ["100", "1990", "12"]) == []
    assert pack.leak_screen("thirteen categories", ["13"]) == ["a number equal to released answer #0"]


def test_pack_protocol_and_tools_resolve():
    import importlib
    import inspect
    from packs.officeqa import pack as pack_module
    pack = OfficeQAPack()
    assert pack.name == "officeqa" and pack.answer_kind == "text"
    for name, target in pack.tools.items():
        mod, fn = target.split(":")
        assert mod == "tools.cli" and name in cli.TOOLS
        assert callable(getattr(importlib.import_module(f"packs.officeqa.{mod}"), fn))
    for meth in ("question_spec", "extract_final", "canonical", "numbers", "observations", "predicates", "offers",
                 "seed_rules", "seed_text", "seed_notes", "usage", "leak_screen", "task_conventions"):
        assert callable(getattr(pack, meth)), meth
    # contracts v9.1 optional attributes the ledger reads
    assert pack.handles_truncation is True and pack.max_observations == 4000
    assert "raw_command" in inspect.signature(pack.observations).parameters
    assert pack.numbers("[1,234.5, 12%]") == [1234.5, 12.0]
    assert pack.canonical("[1,234.50]") == "1234.50"
    # the kit wraps tools/cli.py (multi-command: tool name first); nothing actor-side lives next to the seeds
    wrappers = pack_module.tool_wrappers()
    assert wrappers == {t: [str(ROOT / "packs/officeqa/tools/cli.py"), t] for t in cli.TOOLS}
    assert sorted(p.name for p in (ROOT / "packs/officeqa/tools").glob("*.py")) == ["__init__.py", "cli.py", "otables.py"]
    assert not list((ROOT / "packs/officeqa").glob("cli.py")) and not list((ROOT / "packs/officeqa").glob("otables.py"))


def test_pack_modules_are_stdlib_plus_harness_core():
    import ast as _ast
    allowed = set(sys.stdlib_module_names) | {"harness_core", "packs"}
    for p in (ROOT / "packs" / "officeqa").rglob("*.py"):
        tree = _ast.parse(p.read_text())
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, _ast.ImportFrom) and node.level == 0:
                mods = [node.module.split(".")[0]]
            else:
                continue
            assert all(m in allowed or m == "otables" for m in mods), (p.name, mods)

# ---------------------------------------------------------------------------------------------
# end to end: harness_core ledger + gate + this pack (real corpus, small index, fake runner)
# ---------------------------------------------------------------------------------------------


class Episode:
    """harness_core ledger + gate over this pack, fed with Responses items as the broker relays them."""
    def __init__(self, small_index, question):
        from harness_core.gate import Gate
        from harness_core.ledger import Ledger
        self.pack = OfficeQAPack(CORPUS, small_index)
        self.led = Ledger(self.pack, None)
        self.gate = Gate(self.pack, self.pack.seed_rules(), None, FakeRunner(), question)
        self.n = 0

    def run(self, cmd, out):
        self.n += 1
        cid = f"call_{self.n}"
        self.led.ingest_response(2 * self.n, {"output": [{"type": "function_call", "name": "exec_command", "call_id": cid,
                                                         "arguments": json.dumps({"cmd": cmd})}]}, float(self.n))
        self.led.ingest_request(2 * self.n + 1, [{"type": "function_call_output", "call_id": cid, "output": CODEX_HEAD + out}],
                                self.n + 0.5)

    def tool(self, argv):
        self.run(" ".join(argv), run_cli(self.pack.tables, argv))

    def calc(self, code):
        from harness_core.numeric import numbers_in
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(code, {})
        frame = f"{C.CALC_OPEN}{C.sha256_text(code)[:12]}⟧\n{code}\n{C.CALC_RESULT}\n{buf.getvalue().strip()}\n{C.CALC_END}"
        self.run("calc <<'PY'\n" + code + "\nPY", frame)
        return numbers_in(buf.getvalue())

    def observes(self):
        return [e for e in self.led.events if e.kind == "observe"]

    def submit(self, answer):
        t0 = time.time()
        res = self.gate.submit({"answer": answer}, self.led, 100.0, 99)
        return res, time.time() - t0


@needs_corpus
def test_end_to_end_gate_offers_the_latest_printing(small_index):
    ep = Episode(small_index, "What were the outlays of the judiciary in January 1987, in millions of dollars?")
    ep.tool(["show_table", "treasury_bulletin_1987_06.txt", "463", "--match", "judiciary"])
    jan = [e for e in ep.observes() if e.attrs.get("oq.period") == "1987-01"]
    assert len(jan) == 1
    ep.calc(f"x = {jan[0].text}\nprint(x * 1.0)")
    k = [e for e in ep.led.events if e.kind == "compute"][0]
    assert k.inputs[0]["observe"] == jan[0].ref
    res, dt = ep.submit(str(float(jan[0].value)))
    assert dt < 2.0 and res.status == "findings" and [f.rule for f in res.findings] == ["later_printing"]
    assert "treasury_bulletin_1988_03.txt:940" in res.message and res.findings[0].offer is not None
    assert "Accept: submit --answer" in res.message
    ep.pack.close()


@needs_corpus
def test_end_to_end_period_match_offers_the_calendar_year_row(small_index):
    ep = Episode(small_index, "What were Series E and EE savings bond sales in calendar year 1999, in millions of dollars?")
    ep.tool(["show_table", "treasury_bulletin_2001_03.txt", "2040"])
    fy = [e for e in ep.observes() if e.attrs.get("oq.period") == "FY1999" and e.value][0]
    cy = [e for e in ep.observes() if e.attrs.get("oq.period") == "CY1999" and e.attrs["oq.col"] == fy.attrs["oq.col"]][0]
    ep.calc(f"s = {fy.text.replace(',', '')}\nprint(s)")
    res, dt = ep.submit(fy.text.replace(",", ""))
    assert dt < 2.0 and [f.rule for f in res.findings] == ["period_match"]
    f = res.findings[0]
    assert fy.locator in f.text and cy.locator in f.text and "CY1999" in f.text
    assert f.offer is not None and ep.pack.numbers(f.offer.answer) == [cy.value]
    # accepting the offer: the calendar-year value passes the period check
    res2, _ = ep.submit(f.offer.answer)
    assert "period_match" not in [x.rule for x in res2.findings]
    ep.pack.close()


@needs_corpus
def test_end_to_end_flagged_cell_without_year(small_index):
    ep = Episode(small_index, "What was the average yield of Treasury bonds in March 1968, in percent?")
    ep.tool(["show_table", "treasury_bulletin_1970_06.txt", "4946"])
    yq = [e for e in ep.observes() if str(e.attrs.get("oq.period", "")).startswith("Y?") and e.value][0]
    res, dt = ep.submit(yq.text)
    assert dt < 2.0 and [f.rule for f in res.findings] == ["flagged_cells"]
    assert "year is not printed" in res.findings[0].text and "SIBLINGS" in res.findings[0].text
    ep.pack.close()

# ---------------------------------------------------------------------------------------------
# review fixes (S2-pack-review.md): bounded re-derivation, fragment verification, clip-safe findings
# ---------------------------------------------------------------------------------------------


@needs_corpus
def test_printings_scan_is_capped_and_says_so(tables, monkeypatch):
    monkeypatch.setattr(O, "PRINTINGS_SCAN_CAP", 2)
    full = tables.printings_data("judiciary", "FFO-3")
    assert full["hits"] > 2 and full["scanned"] == 2
    text = tables.printings("judiciary", "FFO-3")
    assert "2 table parts searched" in text and f"the first 2 of {full['hits']} matching table parts" in text
    monkeypatch.undo()
    # an explicit window far before a period row starts PRINTINGS_EARLY_YEARS before the period
    head = tables.printings("1987-01", "FFO-3", column="judiciary", years="1939-1988").split("\n")[0]
    assert f"in issues {1987 - O.PRINTINGS_EARLY_YEARS}..1988" in head


@needs_corpus
def test_range_alignment_is_indexed_and_file_capped(tables):
    files = [f for f in SMALL_FILES if (CORPUS / f).is_file()]
    junk = "\n".join(f"no such corpus text {i}" for i in range(2000))
    t0 = time.time()                                  # was O(seen x expected): ~6 s here before the fix
    got = obs(tables, "cat " + " ".join(files) + " | grep zzz", junk)
    assert time.time() - t0 < 1.0 and all(e.attrs.get("oq.via") == "raw" for e in got)
    lines = tables.lines("treasury_bulletin_1987_06.txt")
    picked = [n for n in range(460, 520) if lines[n - 1].strip()][:5]
    seen = "\n".join(lines[n - 1] for n in picked)
    evs = obs(tables, "sed -n '460,520p' treasury_bulletin_1987_06.txt | grep -v '^$'", seen)
    assert [e.locator for e in evs] == [f"treasury_bulletin_1987_06.txt:{n}" for n in picked]


@needs_corpus
def test_tool_reruns_are_deduplicated_capped_and_split_after_whitespace_collapse(tables, monkeypatch):
    calls = []
    real = cli.page
    monkeypatch.setattr(cli, "page", lambda tool, args, t: calls.append(tool) or real(tool, args, t))
    a = ["show_table", "treasury_bulletin_1987_06.txt", "463"]
    out = run_cli(tables, a)
    once = obs(tables, " ".join(a), out)
    calls.clear()
    assert len(obs(tables, "; ".join([" ".join(a)] * 5), out)) == len(once) and calls == ["show_table"]
    calls.clear()
    many = "; ".join(f"show_table treasury_bulletin_1987_06.txt {n}" for n in range(400, 420))
    obs(tables, many, "nothing")
    assert len(calls) == observe.MAX_TOOL_PAGES
    # the ledger collapses newlines: 'show_table a 1\nshow_table b 2' arrives as one simple command
    b = ["show_table", "treasury_bulletin_1970_06.txt", "4946"]
    evs = obs(tables, " ".join(a + b), out + run_cli(tables, b))
    assert {e.attrs["oq.file"] for e in evs} == {"treasury_bulletin_1987_06.txt", "treasury_bulletin_1970_06.txt"}
    # page headers beyond the fallback cap never become raw value lines
    heads = [tables.show_page(f, t["hdr_line"]).lines[0] for f in SMALL_FILES[:1]
             for t in tables.file_tables(f)[:observe.MAX_FALLBACK_PAGES + 4]]
    assert obs(tables, "for f in $L; do show_table $f | head -1; done", "\n".join(heads)) == []


@needs_corpus
def test_fragment_lines_must_show_numbers_of_that_corpus_line(tables):
    lines = tables.lines("treasury_bulletin_2004_03.txt")
    n, tok = next((i, m.group(0)) for i, ln in enumerate(lines, 1) for m in [re.search(r"\b\d,\d{3}\b", ln)] if m)
    frag = tok[-3:]                                   # '245' out of '1,245': not a number of that line
    assert float(frag) not in O.numbers(lines[n - 1])
    evs = obs(tables, "rg -o x", f"treasury_bulletin_2004_03.txt:{n}:{frag} ")
    assert all(":L" in e.locator for e in evs)
    evs = obs(tables, "rg -o x", f"treasury_bulletin_2004_03.txt:{n}:{tok}")
    assert [e.locator for e in evs] == [f"treasury_bulletin_2004_03.txt:{n}"]


def test_seed_findings_keep_their_decisive_values_at_worst_case_lengths():
    from harness_core.engine import render_template
    loc = "treasury_bulletin_1987_06.txt:12345"
    long_col = "Legislative and judicial branches > The Judiciary and related agencies > Courts of appeals (8)"
    assert R.short_column(long_col) == "Courts of appeals (8)" and len(R.short_column("x" * 90)) <= 24
    assert R.short_column("Stocks > Net purchases > (7)") == "Net purchases (7)"
    assert R.short_column("Stocks > Net foreign purchases > (7)") == "Net foreign purchas… (7)"
    vals = {"cell": {"locator": loc, "column": R.short_column("y" * 70 + " (12)"), "period": "FY1999 est", "text": "12,345,678.9r"},
            "question": {"period": "CY1999"}, "alt": {"period": "CY1999", "locator": loc, "text": "12,345,678.9"},
            "printing": {"locator": loc, "text": "12,345,679.1"},
            "flag": {"why": "a row failing the table's total identity (OCR?)",
                     "check": 'Compare: printings --table FFO-3 --row 1999-12 --col "(12)"'},
            "table": {"used_ref": loc, "other": "t" * 31 + "…", "other_ref": loc, "words": "w" * 26},
            "row": {"used": 12, "locator": loc, "period": "FY1999", "unused_n": 14, "unused": "u" * 70}}
    must = {"period_match": ["12,345,678.9.", "CY1999"], "later_printing": ["12,345,679.1"],
            "flagged_cells": ['--col "(12)"'], "table_alternatives": ["w" * 26 + "."], "row_coverage": ["14 other cells"]}
    for rule in OfficeQAPack().seed_rules():
        text = render_template(rule.finding, vals)
        assert len(text.encode()) <= 200 and all(m in text for m in must[rule.id]), (rule.id, text)


def test_period_mismatch_is_silent_on_the_base_period_of_a_change():
    base = rec("m.txt:110", "35.7", 35.7, period="1968-11", col="c3", column="CPI-U", ref="m.txt:100")
    same = rec("m.txt:122", "37.8", 37.8, period="1969-11", col="c3", column="CPI-U", ref="m.txt:100")
    stub = StubTables(columns={("m.txt", 100, 3): [base, same]})
    k = calc("k1", "a = 35.7\nprint(a)", "35.7", {"35.7": "c1"})
    yoy = "What was the year-over-year CPI-U inflation rate for November 1969, in percent?"
    assert R.period_mismatch(ctx_for(yoy, [ob("c1", base), k], "35.7", stub)) == []
    plain = "What was the CPI-U index level for November 1969?"
    assert len(R.period_mismatch(ctx_for(plain, [ob("c1", base), k], "35.7", stub))) == 1
    P = R.parse_period
    assert R.step_before(P("FY1989"), [P("FY1990")]) and R.step_before(P("1989"), [P("CY1990")])
    assert not R.step_before(P("FY1990"), [P("CY1990")]) and not R.step_before(P("1969-09"), [P("1969-11")])


# ---------------------------------------------------------------------------------------------
# wave-2 integration (S7): raw command, truncation-aware ledger path, answer span, allowed words, threads
# ---------------------------------------------------------------------------------------------


@needs_corpus
def test_raw_command_splits_script_lines_that_the_normalised_command_joins(tables):
    """S2-review 2: 'cd <dir><newline>sed -n a,bp file' is two commands; the ledger's normalised command
    joins them, so without the raw command the sed range is not recognised and the lines stay raw."""
    f = "treasury_bulletin_1987_06.txt"
    lines = tables.lines(f)
    rng = range(478, 486)
    out = CODEX_HEAD + "\n".join(lines[n - 1] for n in rng)
    raw = f"cd {CORPUS}\nsed -n '478,485p' {f}"
    joined = " ".join(raw.split())
    without = obs(tables, joined, out)
    assert without and {e.attrs["oq.via"] for e in without} == {"raw"}
    with_raw = observe.observations(joined, out, source="x1", call=3, t=1.5, next_ref=refs(), tables=tables,
                                    raw_command=raw)
    assert [e.locator for e in with_raw] == [f"{f}:{n}" for n in rng]
    assert {e.attrs["oq.via"] for e in with_raw} == {"range"}
    # a here-document body is data, not commands (an unbalanced quote in it no longer breaks parsing)
    heredoc = f"python3 - <<'PY'\nprint(\"don't\")\nPY\nsed -n '478,485p' {CORPUS}/{f}"
    evs = observe.observations(" ".join(heredoc.split()), out, source="x1", call=3, t=1.5, next_ref=refs(),
                               tables=tables, raw_command=heredoc)
    assert [e.locator for e in evs] == [f"{f}:{n}" for n in rng]
    assert observe.shell_script("show_table a 1\nshow_table b 2") == "show_table a 1 ; show_table b 2"
    assert observe.shell_script("cat a \\\n  b") == "cat a   b"
    assert observe.shell_script("echo 'x\ny' && ls") == "echo 'x\ny' && ls"
    # review fix: a here-string (<<<) is not a here-document; the lines after it are still commands
    assert observe.shell_script('grep -n x <<< "$v"\nsed -n 1,2p f.txt') == 'grep -n x <<< "$v" ; sed -n 1,2p f.txt'
    assert observe.shell_script("cat <<-EOF\n\tbody 1\n\tEOF\nls") == "cat <<-EOF ; ls"


@needs_corpus
def test_ledger_passes_raw_command_and_keeps_truncation_markers_for_the_pack(small_index):
    """S2-review 1/2 through harness_core.ledger.Ledger: Command.raw_cmd/tool are stored and handed to the
    pack; the pack gets the output with Codex's markers (a one-line read dict keeps its uncut lines)."""
    import officeqa_tools
    from harness_core.ledger import Ledger

    class NoTruncation(OfficeQAPack):
        handles_truncation = False
    tool = officeqa_tools.OfficeQACorpus(CORPUS, CORPUS / "unused.sqlite")
    t = repr(tool.read("treasury_bulletin_1944_03.txt", 4015, 12))
    mid = t.index("4020: ")
    cut = t[:mid + 10] + "…300 chars truncated…" + t[mid + 400:]
    located = {}
    for label, pack in (("aware", OfficeQAPack(CORPUS, small_index)), ("core", NoTruncation(CORPUS, small_index))):
        led = Ledger(pack, None)
        raw = "python3 - <<'PY'\nfrom officeqa import OfficeQACorpus\nprint(c.read(...))\nPY"
        led.ingest_response(2, {"output": [{"type": "function_call", "name": "exec_command", "call_id": "k1",
                                            "arguments": json.dumps({"cmd": raw})}]}, 1.0)
        led.ingest_request(3, [{"type": "function_call_output", "call_id": "k1", "output": CODEX_HEAD + cut}], 1.5)
        command = [e for e in led.events if e.kind == "command"][0]
        assert command.raw_cmd == raw and command.tool == "exec_command" and command.cmd == " ".join(raw.split())
        assert command.truncated
        located[label] = {e.locator for e in led.events if e.kind == "observe" and e.attrs.get("oq.via") == "read"}
        assert led.max_obs_per_output == 4000             # the pack's declared cap (default would be 400)
        pack.close()
    assert located["core"] == set()                      # the whole one-line dict was dropped
    got = {int(x.rsplit(":", 1)[1]) for x in located["aware"]}
    assert 4015 in got and 4020 not in got               # only the cut lines are lost


def test_extract_final_returns_the_answer_span():
    pack = OfficeQAPack()
    cases = {
        "12.3": "12.3",
        "In FY2004 the total was 12.3, up from 11.1": "In FY2004 the total was 12.3, up from 11.1",  # one line: as graded
        "I checked the table.\nThe total was 12.3 in FY2004.\n\n**12.3**": "12.3",
        "Final answer: [0.35, surplus]": "[0.35, surplus]",
        "Some text\n**Answer:** 1,234.5\nHope this helps!": "1,234.5",
        "<FINAL_ANSWER>42</FINAL_ANSWER>": "42",
        "Reasoning first.\nNo digits in this last line": "Reasoning first.\nNo digits in this last line".split("\n")[-1],
        "Computed with calc.\n- Answer: 1990": "1990",
        "`3.14`": "3.14",
        "Answer -5": "Answer -5",                   # review fix: '-' before a digit is a sign (was '5')
        "Work shown.\nANSWER - 12": "12",
        "Final answer: -5": "-5",
        "": "",
    }
    for message, span in cases.items():
        assert pack.extract_final(message) == span, message
    assert pack.extract_final("Totals 1 and 2.\nThe answer is 7.5 percent.\nThanks") == "The answer is 7.5 percent."
    assert len(pack.extract_final("9" * 5000)) == 1000


def test_allowed_words_let_quoted_values_fill_count_slots():
    """S2 request 3: Q103-style '[<gini>, 'surplus' or 'deficit']' answers are two values."""
    question = "What was the Gini coefficient of the receipts? Report [gini, 'surplus' or 'deficit'] in the format [a, b]."
    spec = S.question_spec(question)
    assert spec.n_values == 2 and spec.allowed_words == ("surplus", "deficit")
    assert S.allowed_words("the government's 'net' position and the nation's debt") == ("net",)
    assert S.allowed_words('Is it a "surplus" or a "deficit"?') == ("surplus", "deficit")
    assert S.allowed_words("What is the total for the Treasury's 1990 budget?") == ()
    pack = OfficeQAPack()

    def fires(answer):
        cand = C.Candidate(kind="candidate", ref="a1", call=1, t=0.0, answer=answer)
        ctx = C.RuleContext(question=question, spec=spec, events=[cand], candidate=cand, pack=pack, runner=None)
        return core_predicates.count_mismatch(ctx)
    assert fires("[0.35, surplus]") == [] and fires("[0.35, Deficit]") == []
    assert [b.values["count"] for b in fires("[0.35]")] == [{"expected": 2, "got": 1}]
    assert [b.values["count"] for b in fires("[0.35, surplus, deficit]")] == [{"expected": 2, "got": 3}]
    assert fires("[0.35, 0.40]") == []                   # numbers alone may still fill the count


@needs_corpus
def test_tables_are_usable_from_any_thread(small_index):
    """The broker relays each Codex request on a fresh handler thread and serves /v1/submit on another;
    with a same-thread sqlite connection every re-derivation after the first thread silently degraded
    table cells to raw lines (and index-backed rules errored)."""
    import threading
    pack = OfficeQAPack(CORPUS, small_index)
    argv = ["find_tables", "judiciary", "--years", "1987-1988"]
    out = run_cli(pack.tables, argv)                     # opens the connection on this thread
    here = obs(pack.tables, " ".join(argv), out)
    there = {}
    worker = threading.Thread(target=lambda: there.update(evs=obs(pack.tables, " ".join(argv), out),
                                                           one=pack.tables.con().execute("select 1").fetchone()))
    worker.start()
    worker.join(30)
    assert there["one"] == (1,)
    assert [(e.locator, e.attrs.get("oq.kind"), e.attrs.get("oq.via")) for e in there["evs"]] == \
           [(e.locator, e.attrs.get("oq.kind"), e.attrs.get("oq.via")) for e in here]
    pack.close()
