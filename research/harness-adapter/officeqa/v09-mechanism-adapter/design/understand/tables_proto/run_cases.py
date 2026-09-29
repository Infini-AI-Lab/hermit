#!/usr/bin/env python3
"""Structural tests of otables on tables named in officeqa-divergence-analysis/per-question/*.json.

Checks are about STRUCTURE (periods, labels, flags, identities, which issues print a row) - never about
answer values.  Renderings are written to out/<case>.txt for inspection (they contain raw corpus cells).
Run:  python3 run_cases.py
"""
from __future__ import annotations

import re
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import otables as O  # noqa: E402

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
results = []


def analysis(fname, line):
    t = O.table_at(fname, line)
    ts = list(O.file_tables(fname))
    k = next(i for i, x in enumerate(ts) if x["hdr_line"] == t["hdr_line"])
    return t, O.analyze(t, prev_header=ts[k - 1]["header"] if k else None)


def case(name, qids, what):
    def deco(fn):
        try:
            status, detail = fn()
        except Exception as e:  # noqa: BLE001
            status, detail = "ERROR", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
        results.append((name, qids, what, status, detail))
        return fn
    return deco


def save(name, text):
    (OUT / f"{name}.txt").write_text(text + "\n")


def mask(text):
    """printings output: hide the printed values (keep structure)."""
    text = re.sub(r"(= )\S+( \[[^\]]*\])?$", r"\1<v>\2", text, flags=re.M)
    return re.sub(r"^(      )\S+(: \d{4}_\d\d)", r"\1<v>\2", text, flags=re.M)


# --------------------------------------------------------------------------------------------------
@case("ffo3_2004_03_stitch", "Q81 Q16 Q86", "FFO-3 split into 3 OCR parts (13/12/12 columns) + year fill-down")
def _():
    out = O.show_table("treasury_bulletin_2004_03.txt", 728, stitch=True)
    save("ffo3_2004_03_stitch", out)
    t = O.table_at("treasury_bulletin_2004_03.txt", 728)
    parts = O._parts_of("treasury_bulletin_2004_03.txt", t, True)
    nums = set()
    for p in parts:
        a = O.analyze(p)
        for c in range(1, a["ncols"]):
            m = re.search(r"\((\d+)\)\s*$", a["col_label"][c])
            if m:
                nums.add(int(m.group(1)))
    t0, a0 = analysis("treasury_bulletin_2004_03.txt", 728)
    pers = [o["period"] for o in a0["rows"]]
    exp = ["FY1999", "FY2000", "FY2001", "FY2002", "FY2003", "FY2004 est", "2002-12"] + [f"2003-{m:02d}" for m in range(1, 13)] + ["FY2004 to date"]
    ok = len(parts) == 3 and nums == set(range(1, 38)) and pers == exp
    return ("PASS" if ok else "FAIL",
            f"parts={len(parts)} column numbers found={len(nums)}/37 periods_match={pers == exp}; "
            f"r-flag cells parsed e.g. {O.parse_cell('r 16,311')[1]}")


@case("ffo3_printings_2003_01_gsa", "Q81", "same row (Jan 2003, GSA column) across every FFO-3 printing")
def _():
    out = O.printings("2003-01", "FFO-3", column="general services")
    save("ffo3_printings_2003_01_gsa", mask(out))
    issues = sorted(set(re.findall(r"^  (\d{4}_\d\d)  ", out, re.M)))
    exp = ["2003_06", "2003_09", "2003_12", "2004_03"]      # Opus worklog: printed in exactly these four
    return ("PASS" if issues == exp else "FAIL", f"issues={issues} (Opus: {exp})")


@case("ffo3_printings_1987_01_judiciary", "Q86", "vintage map; 1987_09 prints both FFO-3 bands in ONE pipe table")
def _():
    out = O.printings("1987-01", "FFO-3", column="judiciary")
    save("ffo3_printings_1987_01_judiciary", mask(out))
    issues = re.findall(r"^  (\d{4}_\d\d)  ", out, re.M)
    distinct = re.search(r"(\d+) distinct values", out)
    exp = ["1987_06", "1987_09", "1987_12", "1988_03"]      # Opus: same in first three, 1988_03 differs
    ok = issues == exp and distinct and distinct.group(1) == "2" and "REVISED" in out
    return ("PASS" if ok else "FAIL", f"issues={issues}; distinct={distinct.group(1) if distinct else None}; "
            "(before the per-row-label fix the second band's row was reported under 'The judiciary')")


@case("ay1_1970_06_years_lost", "Q11 Q18", "AY-1: 4 column groups x 3 Jan..Dec blocks, year labels lost")
def _():
    out = O.show_table("treasury_bulletin_1970_06.txt", 4946)
    save("ay1_1970_06_years_lost", out)
    t, a = analysis("treasury_bulletin_1970_06.txt", 4946)
    monthly = [o for o in a["rows"] if o["segment"] == 0]
    groups = sorted({o["panel"] for o in monthly})
    per_group = {g: len([o for o in monthly if o["panel"] == g]) for g in groups}
    unknown = all("YEAR_UNKNOWN" in o["flags"] for o in monthly)
    last_blk_g4 = [o for o in monthly if o["panel"] == 4 and (o["period"] or "").startswith("Y?2-")]
    sib = "SIBLINGS" in out and "1970_05" in out or "1970_07" in out
    ok = groups == [1, 2, 3, 4] and all(per_group[g] >= 29 for g in groups) and unknown and len(last_blk_g4) == 5 and sib
    return ("PASS" if ok else "FAIL",
            f"groups={groups} rows/group={per_group} all YEAR_UNKNOWN={unknown} group4 3rd block months={len(last_blk_g4)} "
            f"(ends May => issue-date anchor) siblings_listed={sib}")


@case("ay1_1970_05_years_anchored", "Q18", "sibling printing whose year-label rows anchor every block")
def _():
    out = O.show_table("treasury_bulletin_1970_05.txt", 4848)
    save("ay1_1970_05_years_anchored", out)
    t, a = analysis("treasury_bulletin_1970_05.txt", 4848)
    rng = {}
    for o in a["rows"]:
        if re.match(r"^\d{4}-\d\d$", o["period"] or ""):
            rng.setdefault(o["panel"], []).append(o["period"])
    spans = {g: (v[0], v[-1]) for g, v in rng.items()}
    exp = {1: ("1959-01", "1961-12"), 2: ("1962-01", "1964-12"), 3: ("1965-01", "1967-12"), 4: ("1968-01", "1970-04")}
    named = "Treasury bonds" in a["labels"][1]
    ok = spans == exp and named
    return ("PASS" if ok else "FAIL", f"group spans={spans}; column names from title block={named}")


@case("yields_1944_03_label_slide", "Q30", "3 side-by-side panels; 1943-Jan label slid into the middle panel")
def _():
    out = O.show_table("treasury_bulletin_1944_03.txt", 4015)
    save("yields_1944_03_label_slide", out)
    t, a = analysis("treasury_bulletin_1944_03.txt", 4015)
    flagged = {(o["line"], o["panel"]) for o in a["rows"] if any(f.startswith("SEQ") or f == "YEAR_BACKWARDS" for f in o["flags"])}
    unreliable = sum("NOT reliable" in n for n in a["notes"])
    weekly = [o for o in a["rows"] if "Weekly" in (o["section"] or "")]
    weekly_ok = weekly and all(re.match(r"^\d{4}-\d\d-\d\d$", o["period"] or "") for o in weekly)
    ok = (4031, 2) in flagged and (4031, 3) in flagged and unreliable >= 2 and weekly_ok
    return ("PASS" if ok else "FAIL",
            f"sequence breaks flagged at {sorted(flagged)}; 2-value panels marked unreliable={unreliable}; "
            f"weekly dates filled={bool(weekly_ok)}")


@case("yields_1956_07_header_shift", "Q62", "header 'Period > From 10 to 20 years' slid one column left")
def _():
    out = O.show_table("treasury_bulletin_1956_07.txt", 2603)
    save("yields_1956_07_header_shift", out)
    t, a = analysis("treasury_bulletin_1956_07.txt", 2603)
    lay = a["layouts"][0]["labels"]
    c1, c2, c3 = lay.get(1, ""), lay.get(2, ""), lay.get(3, "")
    verify = any("VERIFY" in n for n in a["notes"])
    ok = "10 to 20" in c1 and "after" in c2 and "Moody" in c3 and verify
    t2, a2 = analysis("treasury_bulletin_1956_08.txt", 3176)
    inbody = [l for l in a2["layouts"] if "20 years and after" in l["labels"].get(2, "")]
    return ("PASS" if ok and inbody else "FAIL",
            f"1956_07 panel-1 labels: c1={c1!r} c2={c2!r} c3={c3!r} (VERIFY note={verify}); "
            f"1956_08 in-body header gives c2='..20 years and after' in {len(inbody)} layout(s)")


@case("ffo2_windows", "Q14 Q130", "13-month rolling window: which calendar/fiscal months an issue covers")
def _():
    res = []
    for fname, want in (("treasury_bulletin_2011_03.txt", [f"2010-{m:02d}" for m in range(1, 13)]),
                        ("treasury_bulletin_2018_12.txt", [f"2017-{m:02d}" for m in (10, 11, 12)] + [f"2018-{m:02d}" for m in range(1, 10)]),
                        ("treasury_bulletin_2011_12.txt", [f"2011-{m:02d}" for m in range(10, 13)])):
        hits = [ln for f, ln, *_ in O._db().execute("SELECT file, hdr_line FROM t WHERE tid='FFO-2' AND file=? ORDER BY hdr_line", (fname,)).fetchall()]
        t, a = analysis(fname, hits[0])
        pers = [o["period"] for o in a["rows"]]
        months = [p for p in pers if re.match(r"^\d{4}-\d\d$", p or "")]
        res.append((fname[18:25], all(w in pers for w in want), f"{months[0]}..{months[-1]}"))
        save(f"ffo2_{fname[18:25]}", O.show_table(fname, hits[0], max_rows=30))
    # Dec 2011 covers Sept 2010..Sept 2011 only (card C003 misuse in Q14): Oct-Dec 2011 must be absent
    ok = res[0][1] and res[1][1] and not res[2][1]
    return ("PASS" if ok else "FAIL", "; ".join(f"{i}: month span {span} contains target months={c}" for i, c, span in res)
            + "  (2011_12 must NOT contain Oct-Dec 2011)")


@case("pdo1_1972_03_row_shift", "Q9", "maturity schedule: 2 panels, rows slid left, garbled year labels")
def _():
    out = O.show_table("treasury_bulletin_1972_03.txt", 2076)
    save("pdo1_1972_03_row_shift", out)
    t, a = analysis("treasury_bulletin_1972_03.txt", 2076)
    shifted = sum("SHIFT+1" in o["flags"] for o in a["rows"])
    ident = [n for n in a["notes"] if n.startswith("identity")]
    fails_2085 = any("2085" in n and "2090" in n for n in ident)
    ok = shifted >= 20 and fails_2085
    return ("PARTIAL" if ok else "FAIL",
            f"rows repaired SHIFT+1={shifted}; identities: {ident[:2]}; garbled year labels ('1976' on 1972 rows) are "
            "NOT repaired - only the identity failures and the note expose the damage")


@case("mq1_1982_03_header_shift", "Q15", "two-panel bill quotations; header labels shifted right of the data")
def _():
    out = O.show_table("treasury_bulletin_1982_03.txt", 4363)
    save("mq1_1982_03_header_shift", out)
    t, a = analysis("treasury_bulletin_1982_03.txt", 4363)
    warn = [n for n in a["notes"] if "header probably shifted" in n]
    return ("PARTIAL" if warn else "FAIL",
            f"type/label mismatch warnings={len(warn)} ({warn[:1]}); labels are NOT realigned "
            "(needs a type-guided alignment of label list to non-empty columns)")


@case("cm_column_periods", "Q89 Q13", "month columns whose year is printed only on the first/annual columns")
def _():
    checks = []
    t, a = analysis("treasury_bulletin_2000_12.txt", 3043)
    checks.append(("2000_12 CM-I-3", {c: v[0] for c, v in a["colper"].items()},
                   a["colper"].get(5, ("",))[0] == "2000-06"))
    t, a = analysis("treasury_bulletin_2001_12.txt", 3267)
    cp = {c: v[0] for c, v in a["colper"].items()}
    june = [c for c, v in cp.items() if v == "2001-06"]
    checks.append(("2001_12 CM-I-2", cp, len(june) == 1))
    t, a = analysis("treasury_bulletin_2013_03.txt", 2247)
    cp3 = {c: v[0] for c, v in a["colper"].items()}
    checks.append(("2013_03 CM-I-1", cp3, cp3.get(2) == "2012-06" and cp3.get(8) == "2012-12"))
    save("cm_i_2_2001_12", O.show_table("treasury_bulletin_2001_12.txt", 3267, max_rows=12))
    save("cm_i_1_2013_03", O.show_table("treasury_bulletin_2013_03.txt", 2247, max_rows=12))
    ok = all(x[2] for x in checks)
    return ("PASS" if ok else "FAIL", "; ".join(f"{n}: {cp}" for n, cp, _ in checks))


@case("fd1_2000_09_identities", "Q101 Q8", "FD-1 in a syllable-split OCR issue; column identities prove alignment")
def _():
    out = O.show_table("treasury_bulletin_2000_09.txt", 957)
    save("fd1_2000_09_identities", out)
    t, a = analysis("treasury_bulletin_2000_09.txt", 957)
    ids = [n for n in a["notes"] if n.startswith("identity")]
    want = ["c1 = sum c2+c3", "c4 = sum c5+c6", "c7 = sum c8+c9"]
    ok = t.get("tid") == "FD-1" and all(any(w in n for n in ids) for w in want)
    found = O.find_tables("summary of federal debt", years="2000")
    return ("PASS" if ok and "[FD-1]" in found else "FAIL",
            f"id={t.get('tid')} title={t.get('title')!r}; identities={ids}; find by phrase hits FD-1={'[FD-1]' in found}")


@case("months_by_year_1950_02", "Q17", "year x month matrix with Total column; OCR-misread cells")
def _():
    out = O.show_table("treasury_bulletin_1950_02.txt", 369)
    save("months_by_year_1950_02", out)
    t, a = analysis("treasury_bulletin_1950_02.txt", 369)
    ids = [n for n in a["notes"] if n.startswith("identity")]
    fails = re.findall(r"FAILS at lines ([\d,]+)", " ".join(ids))
    lines = [int(x) for x in fails[0].split(",")] if fails else []
    lab = {o["line"]: (o["section"], o["period"]) for o in a["rows"]}
    hit = [lab[l] for l in lines if l in lab]
    exp_hit = any(s and "expenditures" in s.lower() and p.endswith("1942") for s, p in hit) and \
        any(s and "expenditures" in s.lower() and p.endswith("1947") for s, p in hit)
    return ("PASS" if exp_hit else "FAIL",
            f"row-total identity failures at {[(s[:22] if s else s, p) for s, p in hit]} (Opus found misreads in the "
            "1942 and 1947 expenditure rows of this printing)")


@case("tso3_1963_03_spurious_label", "Q43", "survey-of-ownership by issue; spurious extra header label")
def _():
    out = O.show_table("treasury_bulletin_1963_03.txt", 3762)
    save("tso3_1963_03", out)
    t, a = analysis("treasury_bulletin_1963_03.txt", 3762)
    ids = [n for n in a["notes"] if n.startswith("identity")]
    ncol_lab = sum(bool(l) for l in a["labels"][1:])
    nonempty = [c for c in range(1, a["ncols"]) if a["coltype"][c] != "empty"]
    spur = any("spurious extra label" in n for n in a["notes"])
    return ("PARTIAL" if ids and spur else "FAIL",
            f"header labels={ncol_lab} vs non-empty value columns={len(nonempty)}; spurious-label warning={spur}; "
            f"identities={ids[:1]} (the tool says labels are shifted but cannot tell WHICH label is spurious)")


@case("sbn3_2001_03_blocks", "Q122", "savings-bond table: FY vs CY annual rows, unlabeled month block, OCR row shift")
def _():
    hits = O._db().execute("SELECT hdr_line FROM t WHERE tid='SBN-3' AND file='treasury_bulletin_2001_03.txt' ORDER BY hdr_line").fetchall()
    t, a = analysis("treasury_bulletin_2001_03.txt", hits[0][0])
    save("sbn3_2001_03", O.show_table("treasury_bulletin_2001_03.txt", hits[0][0], max_rows=60))
    pers = [o["period"] for o in a["rows"] if o["period"]]
    fy_cy = any(p.startswith("FY") for p in pers) and any(p.startswith("CY") for p in pers)
    mar = [o for o in a["rows"] if o["label"].startswith("Mar")]
    banner = mar and mar[0]["period"] == "2000-03" and "year from section banner" in mar[0]["flags"]
    out = O.printings("2000-03", "SBN-3", column="(8)", years="2000-2001")
    save("sbn3_printings_2000_03", mask(out))
    iso = re.search(r"ISOLATED: 2001_03", out)
    ok = fy_cy and banner and iso
    return ("PASS" if ok else "FAIL",
            f"annual rows tagged FY/CY from section banners={fy_cy}; unlabeled 'Mar' -> {mar[0]['period'] if mar else None} "
            f"flagged={banner}; printings marks the 2001_03 printing of col (8) as an ISOLATED outlier={bool(iso)} "
            "(its month block is shifted one row against the 2000_06..2001_06 printings)")


@case("expenditures_1956_08_two_bands", "Q60", "agencies table split in two column bands; parent header spans sub-columns")
def _():
    t = O.table_at("treasury_bulletin_1956_08.txt", 250)
    out = O.show_table("treasury_bulletin_1956_08.txt", t["hdr_line"], stitch=True)
    save("expenditures_1956_08", out)
    parts = O._parts_of("treasury_bulletin_1956_08.txt", t, True)
    labs = []
    for p in parts:
        a = O.analyze(p)
        labs += [l for l in a["labels"] if "efense" in l]
    return ("PASS" if any(">" in l for l in labs) else "PARTIAL",
            f"parts={len(parts)} Defense labels={labs[:3]} (parent > child kept in the label)")


@case("ffo5_1981_12_garbled_cell", "Q88", "total row fell out of the pipe table; 'r' OCR'd as '1'")
def _():
    t = O.table_at("treasury_bulletin_1981_12.txt", 905)
    out = O.show_table("treasury_bulletin_1981_12.txt", 905)
    save("ffo5_1981_12", out)
    t, a = analysis("treasury_bulletin_1981_12.txt", 905)
    orphan = any("fell out of the pipe table" in n and "916" in n for n in a["notes"])
    typ = O.cell_type("1-156")
    return ("PARTIAL" if orphan and typ == "text" else "FAIL",
            f"orphan-row note for line 916={orphan}; '1-156' typed as {typ!r} (not silently a number). The tool does not "
            "decode '2/155,598' (footnote 2/ + OCR'd r + value) - printings() against the previous issue is the check")


@case("ofs2_2013_03_newest_first", "Q105 Q44", "OFS-2 lists quarters newest-first: 2012 - Dec, Sept, June, Mar, 2011 - Dec")
def _():
    out = O.show_table("treasury_bulletin_2013_03.txt", 1853, max_rows=20)
    save("ofs2_2013_03", out)
    t, a = analysis("treasury_bulletin_2013_03.txt", 1853)
    pers = [o["period"] for o in a["rows"] if o["period"]][:9]
    exp = ["2012-12", "2012-09", "2012-06", "2012-03", "2011-12", "2011-09", "2011-06", "2011-03", "2010-12"]
    bad = [o for o in a["rows"] if any(f.startswith("SEQ") or f == "YEAR_BACKWARDS" for f in o["flags"])]
    return ("PASS" if pers == exp and not bad else "FAIL",
            f"first periods={pers} (a naive fill-down gives 2013-09 for the 'Sept' under '2012 - Dec'); false sequence flags={len(bad)}")


@case("ofs1_2007_09_stitch", "Q44 Q77 Q101", "OFS-1 in two column bands; marketable + nonmarketable = total")
def _():
    out = O.show_table("treasury_bulletin_2007_09.txt", 1797, stitch=True)
    save("ofs1_2007_09", out)
    has_june06 = "2006-06" in out
    ids = [l for l in out.splitlines() if "identity" in l]
    ok = has_june06 and any("c3 = sum c4+c5" in l for l in ids) and "+1 continuation parts stitched" in out
    return ("PASS" if ok else "FAIL", f"2006-06 row present={has_june06}; identities={ids}")


@case("esf1_2012_06_balance_sheet", "Q39", "item rows x date columns; units banner inside the header; section paths")
def _():
    out = O.show_table("treasury_bulletin_2012_06.txt", 4722)
    save("esf1_2012_06", out)
    t, a = analysis("treasury_bulletin_2012_06.txt", 4722)
    cp = {c: v[0] for c, v in a["colper"].items()}
    secs = sorted({o["section"] for o in a["rows"] if o["section"]})
    ok = cp.get(1, "").startswith("Sept. 30") and "through" in cp.get(2, "") and cp.get(3, "").startswith("Dec. 31") \
        and any(" > " in x for x in secs)
    return ("PASS" if ok else "FAIL", f"column dates={cp}; section paths={secs[:3]} (indentation is lost: whether "
            "'Accounts receivable' sits under 'Foreign exchange and securities:' cannot be recovered - shown as nearest banner)")


@case("find_tables_recall", "Q109 Q101 Q16", "phrase -> table lookup incl. OCR-split words and lowercase labels")
def _():
    r1 = O.find_tables("sales and redemptions", years="1982")
    r2 = O.find_tables("judicial branch", years="2013")
    r3 = O.find_tables("summary of federal debt", years="2000")
    r4 = O.find_tables("calendar year", years="2000", limit=40)
    ok = "[SN-1]" in r1 and "[SB-2]" in r1 and "[FFO-3]" in r2 and "[FD-1]" in r3 and "CM-I-3" in r4
    save("find_tables_examples", "\n\n".join([r1, r2, r3]))
    return ("PASS" if ok else "FAIL", f"SN-1 & SB-2 listed together={'[SN-1]' in r1 and '[SB-2]' in r1}; "
            f"'judicial branch' -> FFO-3 header={'[FFO-3]' in r2}; syllable-split FD-1 title={'[FD-1]' in r3}; "
            f"'Cal en dar year' header found via squashed match={'CM-I-3' in r4}")


if __name__ == "__main__":
    w = max(len(r[0]) for r in results)
    for name, qids, what, status, detail in results:
        print(f"{status:<8} {name:<{w}}  [{qids}] {what}\n         {detail}\n")
    cnt = {}
    for r in results:
        cnt[r[3]] = cnt.get(r[3], 0) + 1
    print("SUMMARY:", cnt)
