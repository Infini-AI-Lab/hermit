"""otables - table tools over the Treasury Bulletin TXT corpus (v9 OfficeQA pack, L1).

Stdlib only (Python >= 3.9). Read-only over the corpus; the only writer is `build_index`.
Hardened from officeqa-v9-design/understand/tables_proto/otables.py (see officeqa-v9-design/understand/tables.md).
Lives in packs/officeqa/tools/ with cli.py: the actor's kit grants these two files and a listing of this
directory only, so seed and rule files of the pack are neither readable nor listed (S1-review request 2).

Layers in this module
  * pure parsing/analysis: `parse_period`, `parse_cell`, `analyze(table)` (header realignment, panels,
    period fill-down, row repairs, identities).  No I/O, deterministic.
  * `file_tables(corpus, fname)`: every pipe table of one issue with its context (title, id, units, ...).
  * `build_index(corpus_dir, out_path)`: the one-off SQLite/FTS5 table index (written once, then opened
    read-only with `mode=ro&immutable=1`).
  * `Tables(corpus, index)`: `find_page`, `show_page`, `printings_page` return a `Page` (the exact text the
    CLI prints plus one `Cell` per displayed data cell, with the output line it sits on).  The ledger
    re-derives Observe events from these pages (observe.py), so the text and the cells can never drift.

v10 W0 (officeqa-v10-analysis/synthesis/W0-PROPOSAL.md §(d), W0-1..W0-12; every change is additive to the
printed format, so observe.py's frozen header recovery still works):
  * find_tables: title+header+row labels by default with prefix tokens (`--title-only` restores the old
    scope), a ranked/capped OR fallback on 0 hits, group lines first then parts per group until the budget,
    `Table N.` groups merged, `[snapshot]` tag, matched tokens per group, and a `[section]` block from the
    narrative section index (tables `s`/`fs`, INDEX_VERSION otables-index-v10; older indexes still open).
  * show_table: `--grep` repeatable / `a|b` / month forms, `--period FY2021`, 0-row and --from-row hints,
    non-table-line and part-continuation lines, NOTES under --raw, the strict header-shift repair shown as
    LABELSHIFT-1 next to the printed labels, date labels repaired from a sibling, sibling year anchoring.
  * printings: SUMMARY first and keyed by (part, panel, column, period, as-of), exact parsed values,
    column-index warning, `--section`, month rows exact unless `--all-periods`, no-match diagnostics.

Corpus facts the analysis relies on (census in tables.md): every table is a pandas `to_markdown` pipe
table - one header line, one `| --- |` line, body lines.  Multi-row PDF headers are flattened into one
line with " > " between levels, empty header cells become "Unnamed: k_level_j", duplicate labels get
".1", ".2" suffixes, empty body cells are "nan", numeric columns containing a blank became floats
("2085.0").  `show_table` never silently realigns: every repair is listed under NOTES and marked on the row.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import difflib
import functools
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

CORPUS_ENV = "OFFICEQA_CORPUS"
INDEX_ENV = "TABLES_INDEX"
INDEX_VERSION = "otables-index-v10"     # v10: + narrative section tables s/fs (W0-12); v9.1 indexes still open
PAGE_BUDGET = 8000            # bytes (UTF-8) per printed page, including the header and continuation lines
MIN_BUDGET = 1500
PRINTINGS_LISTING_CAP = 60
# printings analyses at most this many table parts (~6 ms each cold): bounds the tool and, above all, the
# broker's deterministic re-run of it in observe.py, which delays the relay of the next request
PRINTINGS_SCAN_CAP = 400
PRINTINGS_EARLY_YEARS = 2     # an explicit --years window starts at most this many years before a period row
# v10 W0 (officeqa-v10-analysis/synthesis/W0-PROPOSAL.md §(d)); plain constants the learner may tune
PREFIX_MIN = 4                # find_tables: query tokens this long match as prefixes ("dollar"* hits "dollars")
LIGHT_COLS = "rid, tid, tid_raw, title, subtitle, section, issue, kind"   # OR fallback: grouping/ranking columns
ALL_PARTS_CAP = 12            # find_tables --all: parts listed per group before '... N more parts'
RESERVED_BLOCKS = 3           # find_tables: groups whose part blocks are reserved before the group lines are cut
SECTION_HITS = 5              # find_tables: narrative [section] lines printed above the table groups
ANCHOR_SIBLINGS = 6           # show_table: sibling printings tried for year anchoring / label repair
STOPWORDS = frozenset("a an and are as at be by for from in is of on or the to was were with what which this that "
                      "its it their there these those than then into over under per each".split())


def default_corpus() -> Path:
    value = os.environ.get(CORPUS_ENV, "")
    if not value:
        raise ToolError(f"set {CORPUS_ENV} to the corpus directory (treasury_bulletin_*.txt)")
    return Path(value)


def default_index() -> Path:
    value = os.environ.get(INDEX_ENV, "")
    if not value:
        raise ToolError(f"set {INDEX_ENV} to the tables index (tables.sqlite)")
    return Path(value)


class ToolError(Exception):
    """A user-facing error (bad arguments, no such table); the CLI prints it and exits with code 2."""


# ----------------------------------------------------------------------------
# lexical helpers
# ----------------------------------------------------------------------------
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
          "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
MON = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
       r"|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
MONTHNAME = (r"(?:January|February|March|April|May|June|July|August|September|October|November|December"
             r"|Jan\.|Feb\.|Mar\.|Apr\.|Aug\.|Sept?\.|Oct\.|Nov\.|Dec\.)")
DASHES = "—–‒−"          # em, en, figure dash, minus

ID_RX = re.compile(r"^\s*(?:T\s?A\s?B\s?L\s?E|Table)\s*([A-Z0-9]{1,6}(?:-[IVXl1]{1,4})?-\s?\d{1,2}[A-Z]?)\s*"
                   r"(?:\.\s*)?(?:--|[-" + DASHES + r"]|\.)?\s*(.*)$")
NUM_TITLE_RX = re.compile(r"^\s*(?:T\s?A\s?B\s?L\s?E|Table)\s*(\d{1,2})\s*\.?\s*[-" + DASHES + r"]+\s*(.*)$")
TOC_ENTRY_RX = re.compile(r"^([A-Z]{1,5}(?:-[IVX1]{1,4})?-\d{1,2}[A-Z]?)\s*\.?\s*[-" + DASHES + r".]")
UNITS_RX = re.compile(r"^[\(\[].{0,200}(dollars|millions|thousands|billions|percent|ounces|par value|"
                      r"in units|number|position at|amounts? in)", re.I)
ASOF_RX = re.compile(r"(" + MONTHNAME + r"\s+\d{1,2}\s*,\s*(?:19|20)\d\d)", re.I)
PAGE_RX = re.compile(r"^(?:-?\d{1,3}-?|Treasury Bulletin|" + MONTHNAME + r"\s+(?:19|20)\d\d)$")
FOOT_RX = re.compile(r"^(?:\d{1,2}/|\*|Source|Note|NOTE|Back [Ff]igures|r Revised|p Preliminary|n\.a\.|"
                     r"Footnotes?|See footnote|\(Continued|Detail may not)")
CONT_RX = re.compile(r"(,\s*con\.?\s*$|continued|\(cont)", re.I)
SEP_RX = re.compile(r"^\|(\s*:?-{3,}:?\s*\|)+\s*$")
UNNAMED_RX = re.compile(r"^Unnamed: \d+(?:_level_\d+)?$")
DUP_RX = re.compile(r"^(.*?)\.(\d{1,2})$")
YEAR_RX = re.compile(r"^((?:18|19|20)\d\d)(?:\.0)?$")


def split_cells(line: str) -> list:
    s = line.rstrip("\n").strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    out = [c.strip() for c in s.split("|")]
    return ["" if c == "nan" else c for c in out]


def norm(s: str) -> str:
    """case/whitespace/hyphen-insensitive text for matching."""
    s = s.lower()
    s = re.sub(r"\.{2,}", " ", s)
    s = re.sub(r"[-" + DASHES + r"/]", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def canon_id(raw: str) -> str:
    """Canonical table id with the OCR confusions seen in the corpus undone
    (FF0-3 -> FFO-3, 1FS-2 -> IFS-2, CM-11-1 -> CM-II-1, FCP-V11-2 -> FCP-VII-2)."""
    s = raw.upper().replace(" ", "")
    parts = s.split("-")
    if len(parts) < 2:
        return s
    head, mids, last = parts[0], parts[1:-1], parts[-1]
    head = head.replace("0", "O")
    if head[:1] == "1":
        head = "I" + head[1:]
    if head == "FFQ":
        head = "FFO"
    fixed = []
    for m in mids:
        if re.fullmatch(r"[IVXL1]+", m):
            m = m.replace("1", "I").replace("L", "I")
        fixed.append(m.replace("0", "O"))
    return "-".join([head] + fixed + [last])


def clean_label(s: str) -> str:
    s = re.sub(r"\.{2,}", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.rstrip(" .:")


def issue_of(fname: str) -> str:
    m = re.search(r"(\d{4})_(\d{2})", fname)
    return f"{m.group(1)}_{m.group(2)}" if m else "?"


# ----------------------------------------------------------------------------
# periods
# ----------------------------------------------------------------------------
_P_STRIP = re.compile(r"(\s+\d{1,2}/)+$|\s+[rp]$|\s*\*$")


@functools.lru_cache(maxsize=200000)
def parse_period(raw: str):
    """Parse a stub/period label.  Returns dict(kind, year, month, day, est) or None.
    kinds: year, month (year may be None), day (weekly/daily continuation), fytd."""
    if not raw:
        return None
    s = raw.strip()
    s = re.sub(r"\.{2,}", "", s)
    s = re.sub(r"\((?:add|Add)[^)]*\)", "", s)
    s = _P_STRIP.sub("", s).strip()
    s = _P_STRIP.sub("", s).strip()
    low = s.lower().strip(" .:")
    est = bool(re.search(r"\best\b|\(est", low))
    low = re.sub(r"\s*[-(]?\s*\(?est\.?\)?\s*$", "", low).strip(" .-")
    m = re.match(r"^fiscal (?:year )?((?:19|20)\d\d) to date", low)
    if m:
        return {"kind": "fytd", "year": int(m.group(1)), "month": None, "day": None, "est": est}
    m = re.match(r"^(?:calendar year |fiscal year |fiscal |fy ?|cy ?)?((?:18|19|20)\d\d)(?:\.0)?$", low)
    if m:
        return {"kind": "year", "year": int(m.group(1)), "month": None, "day": None, "est": est}
    m = re.match(r"^((?:18|19|20)\d\d)\s*[-" + DASHES + r"]\s*(\d{2}|(?:18|19|20)\d\d)$", low)
    if m:
        y2 = m.group(2)
        y2 = int(m.group(1)[:2] + y2) if len(y2) == 2 else int(y2)
        return {"kind": "range", "year": int(m.group(1)), "year2": y2, "month": None, "day": None, "est": est}
    m = re.match(r"^((?:19|20)\d\d)\s*[-:,.]?\s*" + MON + r"(?:\s*(\d{1,2}))?\.?$", low)
    if m:
        return {"kind": "month", "year": int(m.group(1)), "month": MONTHS[m.group(2)[:3]],
                "day": int(m.group(3)) if m.group(3) else None, "est": est}
    m = re.match(r"^" + MON + r"\s*,?\s*((?:19|20)\d\d)$", low)
    if m:
        return {"kind": "month", "year": int(m.group(2)), "month": MONTHS[m.group(1)[:3]], "day": None, "est": est}
    m = re.match(r"^" + MON + r"(?:\s*(\d{1,2}))?\.?$", low)
    if m:
        return {"kind": "month", "year": None, "month": MONTHS[m.group(1)[:3]],
                "day": int(m.group(2)) if m.group(2) else None, "est": est}
    m = re.match(r"^(\d{1,2})\.+$", raw.strip())
    if m and 1 <= int(m.group(1)) <= 31:
        return {"kind": "day", "year": None, "month": None, "day": int(m.group(1)), "est": est}
    return None


def _pp(cell: str, dayctx: bool = False):
    """parse_period plus bare day numbers ('20') inside a column whose other cells are dated ('1972-July 13')."""
    p = parse_period(cell)
    if p is None and dayctx and re.fullmatch(r"\d{1,2}", cell.strip()) and 1 <= int(cell) <= 31:
        return {"kind": "day", "year": None, "month": None, "day": int(cell), "est": False}
    return p


def _has_days(vals):
    return any((parse_period(v) or {}).get("day") for v in vals if v)


def parse_query_period(q: str):
    """User-facing period spec: 2003-01, 2003-1, Jan 2003, January 2003, 2003, FY2003 (a day, as in
    2003-01-15 or '1939-Jan. 19', still yields the month: see query_day)."""
    q = q.strip()
    m = re.match(r"^((?:19|20)\d\d)[-/](\d{1,2})(?:[-/]\d{1,2})?$", q)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.match(r"^(?:fy|cy)?\s*((?:19|20)\d\d)$", q, re.I)
    if m:
        return (int(m.group(1)), None)
    p = parse_period(q)
    if p and p["year"] and p["month"]:
        return (p["year"], p["month"])
    return None


def query_day(q: str):
    """The day of a day-level period spec ('2003-01-15', '1939-Jan. 19', 'Jan 19, 1939' is not one), else None."""
    q = q.strip()
    m = re.match(r"^(?:19|20)\d\d[-/]\d{1,2}[-/](\d{1,2})$", q)
    if m:
        return int(m.group(1))
    p = parse_period(q)
    return p["day"] if p and p["year"] and p["month"] and p.get("day") else None


# ----------------------------------------------------------------------------
# cells
# ----------------------------------------------------------------------------
NUM = r"-?(?:\d[\d,]*(?:\.\d+)?|\.\d+)"


@functools.lru_cache(maxsize=400000)
def parse_cell(raw: str):
    """-> (value|None, flags tuple).  Flags: empty dash lt_half na r p fn:<k>/ paren pct
    float0 multi:<n> trail_digit? text date"""
    t = raw.strip()
    if t == "":
        return (None, ("empty",))
    if t in ("-", "--", "- -") or t in DASHES:
        return (0.0, ("dash",))
    if t in ("*", "-*", "*-", "(*)"):
        return (None, ("lt_half",))
    if re.fullmatch(r"n\.?\s?a\.?", t, re.I):
        return (None, ("na",))
    flags = []
    t = t.replace("$", "").strip()
    # footnote markers glued after a number: "3.91 4/", "1,913 1/2"(=fn 1/ + stray digit)
    while True:
        m = re.match(r"^(.*\d)\s+(\d{1,2})/(\d?)$", t)
        if not m:
            break
        flags.append(f"fn:{m.group(2)}/" + ("?" if m.group(3) else ""))
        t = m.group(1).strip()
    m = re.match(r"^([rp])\s*(" + NUM + r")$", t)
    if m:
        flags.append(m.group(1))
        t = m.group(2)
    m = re.match(r"^(" + NUM + r")\s*([rp])$", t)
    if m:
        flags.append(m.group(2))
        t = m.group(1)
    m = re.match(r"^\((" + NUM + r")\)?(\d)?$", t)
    if m:
        flags.append("paren")
        if m.group(2):
            flags.append("trail_digit?")
        return (-float(m.group(1).replace(",", "")), tuple(flags))
    m = re.match(r"^([+-]?\s?\d*\.?\d+)\s?%$", t)
    if m:
        flags.append("pct")
        return (float(m.group(1).replace(" ", "")), tuple(flags))
    m = re.match(r"^[+-]\s?(\d*\.\d+|\d+)$", t)
    if m:
        return (float(t.replace(" ", "")), tuple(flags))
    if re.fullmatch(NUM, t):
        if re.fullmatch(r"-?\d+\.0", t):
            flags.append("float0")
        return (float(t.replace(",", "")), tuple(flags))
    m = re.fullmatch(r"(" + NUM + r")((?:\s+" + NUM + r")+)", t)
    if m:
        parts = [m.group(1)] + m.group(2).split()
        return (None, tuple(flags + [f"multi:{len(parts)}"]))
    m = re.fullmatch(r"(" + NUM + r")\s(\d)", t)
    if m:
        return (float(m.group(1).replace(",", "")), tuple(flags + ["trail_digit?"]))
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}(?:-\d{2})?", t):
        return (None, tuple(flags + ["date"]))
    return (None, tuple(flags + ["text"]))


def cell_type(raw: str) -> str:
    if raw == "":
        return "empty"
    if parse_period(raw):
        return "period"
    v, f = parse_cell(raw)
    if "date" in f:
        return "date"
    if "pct" in f:
        return "pct"
    if v is not None or "lt_half" in f or "na" in f or any(x.startswith("multi") for x in f):
        return "num"
    return "text"


# ----------------------------------------------------------------------------
# file -> tables
# ----------------------------------------------------------------------------
FILE_RX = re.compile(r"^[A-Za-z0-9_.-]+\.txt$")


@functools.lru_cache(maxsize=64)
def load_lines(corpus: str, fname: str) -> tuple:
    """Lines of one corpus file (1-based line k is element k-1).  Only the basename of `fname` is used,
    so a path such as 'corpus/x.txt' reads 'x.txt' inside the corpus and never leaves it."""
    name = Path(str(fname).strip()).name
    if not FILE_RX.match(name):
        raise ToolError(f"not a corpus file name: {fname!r}")
    p = Path(corpus) / name
    if not p.is_file():
        raise ToolError(f"no such corpus file: {name}")
    return tuple(p.read_text(encoding="utf-8", errors="replace").split("\n"))


def _is_units(s: str) -> bool:
    # 1999_12..2001_06 issues split words into syllables ('mil lions of dol lars')
    return bool(UNITS_RX.match(s) or (s[:1] in "([" and UNITS_RX.match(s.replace(" ", ""))))


def _is_caps_heading(s: str) -> bool:
    letters = [ch for ch in s if ch.isalpha()]
    return (6 <= len(s) <= 140 and len(letters) >= 5 and not s.startswith("|")
            and sum(ch.isupper() for ch in letters) / len(letters) > 0.85
            and not s.upper().startswith("TABLE "))


@functools.lru_cache(maxsize=64)
def file_tables(corpus: str, fname: str) -> tuple:
    """All pipe tables of one issue with their context (title, id, units, as-of...).  Cached and shared:
    callers must not mutate the returned dicts."""
    lines = load_lines(corpus, fname)
    n = len(lines)
    tables = []
    i = 0
    while i < n:
        if not lines[i].startswith("|"):
            i += 1
            continue
        j = i
        while j < n and lines[j].startswith("|"):
            j += 1
        seps = [k for k in range(i + 1, j) if SEP_RX.match(lines[k])]
        if not seps:
            i = j
            continue
        for si, k in enumerate(seps):
            hdr = k - 1
            end = (seps[si + 1] - 2) if si + 1 < len(seps) else j - 1
            rows = [(r + 1, split_cells(lines[r])) for r in range(k + 1, end + 1)]
            tables.append({"file": Path(fname).name, "issue": issue_of(fname), "hdr_line": hdr + 1,
                           "end_line": end + 1, "header": split_cells(lines[hdr]), "rows": rows})
        i = j
    # context
    prev_end = 0
    last_titled = None
    for t in tables:
        a = t["hdr_line"] - 1                      # 0-based header index
        ctx = []
        k = a - 1
        while k >= prev_end and len(ctx) < 16:
            s = lines[k].strip()
            if s:
                ctx.append((k + 1, s))
            k -= 1
        tid = tid_raw = title = None
        title_line = None
        units = None
        subtitle = []
        plain = []
        only_furniture = True
        for ln, s in ctx:                         # nearest first
            if PAGE_RX.match(s):
                continue
            if FOOT_RX.match(s):
                continue
            m = ID_RX.match(s)
            if m and not re.match(r"^(summarizes|details|includes|presents|provides|shows|lists|is |are |covers)", m.group(2)):
                only_furniture = False
                if tid is None:
                    tid_raw = m.group(1).replace(" ", "")
                    tid = canon_id(tid_raw)
                    title = m.group(2).strip()
                    title_line = ln
                    if not title:                      # "Table FFO-3." / "— Budget Outlays by Agency"
                        nxt = [x for x in lines[ln:ln + 3] if x.strip()]
                        if nxt and nxt[0].strip()[:1] in "-" + DASHES:
                            title = nxt[0].strip().lstrip("-" + DASHES + " ")
                break
            m = NUM_TITLE_RX.match(s)
            if m:
                only_furniture = False
                tid_raw = "Table " + m.group(1)
                title = m.group(2).strip()
                title_line = ln
                break
            if _is_units(s):
                if units is None:
                    units = s
                continue
            if len(s) <= 160 and not s.startswith("|"):
                only_furniture = False
                plain.append((ln, s))
                if len(plain) >= 3:
                    break
                continue
            # long narrative line: stop looking further up
            only_furniture = False
            break
        if title is None and plain:
            # untitled era (1939-43) or subtitle-only: nearest short descriptive line(s)
            cand = [p for p in plain if not ASOF_RX.fullmatch(p[1])]
            if cand:
                title_line, title = cand[0]
            subtitle = [p[1] for p in plain if p[0] != title_line]
        elif plain:
            subtitle = [p[1] for p in plain]
        # the numbered/ID title may be followed by subtitle lines between it and the table
        if title_line:
            for ln in range(title_line + 1, t["hdr_line"]):
                s = lines[ln - 1].strip()
                if s and not _is_units(s) and not PAGE_RX.match(s) and s not in subtitle and len(s) < 200:
                    subtitle.append(s)
                if s and _is_units(s) and not units:
                    units = s
        cont = False
        inherited = False
        if (title is None and last_titled is not None and only_furniture):
            cont = inherited = True
        if title and CONT_RX.search(title):
            cont = True
        # a header of 0..n-1 means pandas saw no header: continuation of the previous table
        hdr = t["header"]
        header_missing = hdr == [str(x) for x in range(len(hdr))]
        if header_missing and last_titled is not None and (title is None or t["hdr_line"] - prev_end <= 12):
            if title and not tid:
                subtitle = [title] + subtitle
            cont = inherited = True
        if inherited:
            for key in ("tid", "tid_raw", "title", "title_line", "units", "asof", "section"):
                t[key] = last_titled.get(key)
        else:
            t.update({"tid": tid, "tid_raw": tid_raw, "title": title, "title_line": title_line, "units": units})
            # section: nearest ALL-CAPS heading above (running head or section name)
            sec = None
            for k2 in range((title_line or t["hdr_line"]) - 2, max(-1, (title_line or t["hdr_line"]) - 80), -1):
                s = lines[k2].strip()
                if _is_caps_heading(s):
                    sec = s
                    break
            t["section"] = sec
            blob = " ".join(x for x in [title or "", " ".join(subtitle), units or "", sec or ""])
            m = ASOF_RX.search(blob)
            t["asof"] = m.group(1) if m else None
        t["subtitle"] = subtitle
        t["cont"] = cont
        t["inherited"] = inherited
        t["header_missing"] = header_missing
        firsts = [r[1][0] for r in t["rows"] if r[1]]
        t["kind"] = "toc" if sum(bool(TOC_ENTRY_RX.match(c)) for c in firsts) >= 3 else "data"
        if not inherited and t.get("title"):
            last_titled = t
        elif last_titled is None:
            last_titled = t
        prev_end = t["end_line"]
    for t in tables:
        orphans = []
        k = t["end_line"]
        seen = 0
        while k < n and seen < 4:
            s = lines[k].strip()
            k += 1
            if not s:
                continue
            if s.startswith("|"):
                break
            seen += 1
            nums = re.findall(r"(?<![\w/])[-r]?\d[\d,]*(?:\.\d+)?", s)
            if len(nums) >= 4 and re.match(r"^[A-Za-z]", s) and not FOOT_RX.match(s):
                orphans.append(k)
        t["orphans"] = orphans
    # a header-only block (no body rows) right above a table is its real column header
    for idx in range(1, len(tables)):
        pv, t = tables[idx - 1], tables[idx]
        if not pv["rows"] and t["hdr_line"] - pv["end_line"] <= 6:
            t["pre_header"] = pv["header"]
    # part numbering for multi-part tables
    for idx, t in enumerate(tables):
        prev = tables[idx - 1] if idx else None
        if t["cont"] and prev is not None and (t["inherited"] or (t.get("tid") and t.get("tid") == prev.get("tid"))):
            t["part"] = prev["part"] + 1
        else:
            t["part"] = 1
    return tuple(tables)


def table_at_how(corpus: str, fname: str, line: int) -> tuple:
    """(table, how): the table whose header/body contains `line` ('inside'), whose title is at `line`
    ('title'), or the next one ('next'; W0-7 says so instead of snapping silently)."""
    ts = file_tables(corpus, fname)
    for t in ts:
        if t["hdr_line"] <= line <= t["end_line"]:
            return t, "inside"
    for t in ts:
        if t.get("title_line") == line:
            return t, "title"
    for t in ts:
        if t["hdr_line"] > line:
            return t, "next"
    raise ToolError(f"no table at or after {fname}:{line}")


def table_at(corpus: str, fname: str, line: int) -> dict:
    """table whose header/body contains `line`, or whose title is at `line`, or the next one."""
    return table_at_how(corpus, fname, line)[0]


FIGURE_RX = re.compile(r"^\s*(chart|figure)\b", re.I)


def _align_years(a: dict, sa: dict) -> tuple:
    """W0-10: anchor the YEAR_UNKNOWN rows of analysis `a` on sibling analysis `sa` (same table, dated).
    A row anchors when its parsed values equal exactly one dated sibling row of the same panel and month;
    every panel with unknown rows needs one anchor and all anchors of a panel must agree on the year of
    its first Jan..Dec cycle.  -> (rows copy, {panel: [years]}, agree, compared) or None."""
    def vals(o):
        return tuple(parse_cell(v)[0] for v in o["vals"])

    def block(o):
        m = re.match(r"Y\?(\d*)-(\d\d)(-\d\d)?( est)?$", o["period"] or "")
        return (int(m.group(1) or 0), int(m.group(2)), m.group(3) or "", m.group(4) or "") if m else None
    dated = collections.defaultdict(lambda: collections.defaultdict(list))
    for o in sa["rows"]:
        m = re.fullmatch(r"(\d{4})-(\d\d)", o["period"] or "")
        if m and sum(v is not None for v in vals(o)) >= 2:
            dated[o["panel"]][(vals(o), int(m.group(2)))].append(int(m.group(1)))
    year0 = {}
    panels = {o["panel"] for o in a["rows"] if "YEAR_UNKNOWN" in o["flags"]}
    for k in panels:
        cands = set()
        for o in a["rows"]:
            b = block(o)
            if o["panel"] != k or "YEAR_UNKNOWN" not in o["flags"] or not b:
                continue
            ys = dated[k].get((vals(o), b[1]), [])
            if len(set(ys)) == 1:
                cands.add(ys[0] - b[0])
        if len(cands) != 1:
            return None
        year0[k] = cands.pop()
    by_period = {(o["panel"], o["period"]): vals(o) for o in sa["rows"]}
    rows, years, agree, compared = [], collections.defaultdict(set), 0, 0
    for o in a["rows"]:
        b = block(o)
        if o["panel"] not in year0 or "YEAR_UNKNOWN" not in o["flags"] or not b:
            rows.append(o)
            continue
        y = year0[o["panel"]] + b[0]
        per = f"{y}-{b[1]:02d}{b[2]}{b[3]}"
        years[o["panel"]].add(y)
        sib = by_period.get((o["panel"], f"{y}-{b[1]:02d}"))
        if sib is not None and not b[2]:
            pairs = [(x, s) for x, s in zip(vals(o), sib) if x is not None and s is not None]
            compared += len(pairs)
            agree += sum(x == s for x, s in pairs)
        rows.append({**o, "period": per, "flags": [f for f in o["flags"] if f != "YEAR_UNKNOWN"] + ["anchored"]})
    if not compared or agree < 0.8 * compared:
        return None
    return rows, {k: sorted(v) for k, v in years.items()}, agree, compared


# ----------------------------------------------------------------------------
# analysis: header realignment, panels, year fill-down, row repair
# ----------------------------------------------------------------------------
def header_parts(h: str):
    """'A > Unnamed: 3_level_1 > B.1' -> (['A', 'B'], dup=1)"""
    dup = 0
    parts = []
    for p in h.split(" > "):
        p = p.strip()
        m = re.match(r"^(.*\D)\.(\d{1,2})$", p) or re.match(r"^((?:18|19|20)\d\d)\.(\d{1,2})$", p)
        if m:
            dup = max(dup, int(m.group(2)))
            p = m.group(1)
        if UNNAMED_RX.match(p) or p == "":
            continue
        if parts and norm(parts[-1]) == norm(p):
            continue
        parts.append(p)
    return parts, dup


PERIOD_WORDS = re.compile(r"^(period|date|fiscal year or month|calendar year or month|year or month|"
                          r"end of fiscal year or month|end of (calendar )?year or month|fiscal year|month|year)$", re.I)


def _year_label_row(cells):
    ne = [c for c in cells if c]
    if len(ne) < 2 or not all(YEAR_RX.match(c) for c in ne):
        return False
    ys = [int(float(c)) for c in ne]
    return ys == sorted(ys) and len(set(ys)) <= max(2, len(cells) // 2)


def pcols_guess(data, ncols):
    out = []
    for c in range(ncols):
        vals = [r["cells"][c] for r in data if r["cells"][c]]
        if len(vals) >= 2 and sum(parse_period(v) is not None for v in vals) >= 0.6 * len(vals):
            out.append(c)
    return out


def _classify_rows(t, ncols, labels):
    rows = []
    in_header = False
    hdr_norm = {norm(l) for l in labels if l}
    for ln, cells in t["rows"]:
        cells = (cells + [""] * ncols)[:ncols]
        ne = [c for c in cells if c]
        kind = "data"
        if not ne:
            kind = "blank"
        elif _year_label_row(cells):
            kind = "yearlabel"
        elif len(ne) >= 2 and len(set(ne)) == 1 and cell_type(ne[0]) == "text":
            kind = "section"
        elif ne == [cells[0]] and ncols > 1:
            p = parse_period(cells[0])
            kind = "yeargroup" if (p and p["kind"] == "year") else "section"
        else:
            types = [cell_type(c) for c in ne]
            textish = sum(x == "text" for x in types) / len(types)
            match_hdr = sum(norm(c) in hdr_norm for c in ne)
            if textish >= 0.6 and (PERIOD_WORDS.match(clean_label(cells[0]) or "x") or match_hdr >= 2):
                kind = "header"
            elif in_header and textish >= 0.8 and cells[0] == "":
                kind = "header"
        in_header = kind == "header"
        rows.append({"line": ln, "cells": cells, "kind": kind, "flags": []})
    return rows


def _column_groups(vcols, labels, hdr_year):
    """Repeated column groups sharing one stub (AY-1 style: 4 x (Treasury, Aa, municipal)).
    -> (list of column lists, reason) or (None, None)."""
    if len(vcols) < 4:
        return None, None
    ys = [hdr_year[c] for c in vcols]
    if sum(y is not None for y in ys) >= 0.6 * len(vcols):
        runs = []
        for c, y in zip(vcols, ys):
            if runs and (y == runs[-1][0] or y is None):
                runs[-1][1].append(c)
            else:
                runs.append([y, [c]])
        if len(runs) >= 2 and all(len(r[1]) >= 2 for r in runs[:-1]):
            return [r[1] for r in runs], "header year labels"
    base = [norm(re.sub(r"(\s*\d{1,2}/)+$", "", labels[c].split(" > ")[-1])) for c in vcols]
    if len(set(base)) <= 1:
        return None, None
    for P in range(2, len(vcols) // 2 + 1):
        pairs = [(base[i], base[i + P]) for i in range(len(vcols) - P)]
        if pairs and sum(a == b for a, b in pairs) >= 0.75 * len(pairs) and len(set(base[:P])) == P:
            return [vcols[i:i + P] for i in range(0, len(vcols), P)], f"labels repeat every {P} columns"
    return None, None


def _col_periods(labels, vcols, issue):
    """Periods printed in column headers (transposed tables, e.g. CM-I-3 '1999 r | 2000 > May | June p').
    Month columns without a printed year get the year of the previous month column (next year after a
    wrap) or, right after annual columns, last annual year + 1.  -> {col: (text, inferred?)}"""
    out = {}
    year = None
    last_m = None
    last_annual = None
    iy, im = map(int, issue.split("_"))
    for c in vcols:
        py = pm = None
        md = ASOF_RX.findall(labels[c])
        if md:
            # 'Sept. 30, 2011' balance-sheet date column; 'Oct. 1, 2011, through Dec. 31, 2011' flow column
            out[c] = (f"{md[0]} through {md[-1]}" if len(md) > 1 and "through" in labels[c].lower() else md[-1], False)
            continue
        for part in labels[c].split(" > "):
            p = parse_period(part)
            if not p:
                continue
            if p["kind"] == "year":
                py = p["year"]
            elif p["kind"] == "month":
                pm = p["month"]
                if p["year"]:
                    py = p["year"]
        if pm is None and py is not None:
            out[c] = (f"{py} (year)", False)
            last_annual = py
            last_m = None
            continue
        if pm is None:
            continue
        inferred = False
        if py is None:
            inferred = True
            if last_m is not None and year is not None:
                py = year + (1 if pm <= last_m else 0)
            elif last_annual is not None:
                py = last_annual + 1
        if py is not None and (py, pm) > (iy, im):
            out[c] = (f"{py}-{pm:02d}?", True)          # after the issue date: impossible
        else:
            out[c] = (f"{py}-{pm:02d}" if py else f"Y?-{pm:02d}", inferred)
        year, last_m = py, pm
    return out


def _check_identity(rows_k, c, run):
    ok = tot = 0
    bad = []
    for o in rows_k:
        vals = {}
        for q, cc in enumerate(o["cols"]):
            v, f = parse_cell(o["vals"][q])
            vals[cc] = 0.0 if "lt_half" in f else (v if "pct" not in f else None)
        if vals.get(c) is None or any(vals.get(x) is None for x in run):
            continue
        if vals[c] == 0 and all(vals[x] == 0 for x in run):
            continue
        tot += 1
        dec = 0
        for o_v in [o["vals"][o["cols"].index(x)] for x in run + [c]]:
            m = re.search(r"\.(\d+)", o_v)
            if m and m.group(1).strip("0"):
                dec = max(dec, len(m.group(1)))
        unit = 10 ** -min(dec, 3)
        if abs(sum(vals[x] for x in run) - vals[c]) <= unit * (0.5 * len(run) + 1):
            ok += 1
        else:
            bad.append(o["line"])
    return ok, tot, bad


def _identities(out_rows, panel_vcols, col_label, max_k=13):
    """columns that equal the sum of an adjacent run of columns (Total = parts) - verifies alignment."""
    found = []
    cols = panel_vcols
    vals = {}
    for o in out_rows:
        row = {}
        for q, c in enumerate(o["cols"]):
            v, f = parse_cell(o["vals"][q])
            if v is not None and "pct" not in f:
                row[c] = v
            elif "lt_half" in f or "empty" in f:
                row[c] = 0.0 if "lt_half" in f else None
        vals[o["line"]] = row
    for ci, c in enumerate(cols):
        best = None
        for direction in (1, -1):
            for k in range(2, max_k + 1):
                run = cols[ci + 1: ci + 1 + k] if direction == 1 else cols[max(0, ci - k): ci]
                if len(run) != k:
                    break
                ok = tot = 0
                bad = []
                for ln, row in vals.items():
                    if row.get(c) is None or any(row.get(x) is None for x in run):
                        continue
                    if all(row.get(x) == 0 for x in run) and row.get(c) == 0:
                        continue
                    tot += 1
                    s = sum(row[x] for x in run)
                    dec = max([len(str(abs(row[x])).split(".")[1].rstrip("0")) if "." in str(row[x]) else 0
                               for x in run + [c]] + [0])
                    unit = 10 ** -min(dec, 3)
                    if abs(s - row[c]) <= unit * (0.5 * k + 1):
                        ok += 1
                    else:
                        bad.append(ln)
                if tot >= 3 and ok >= 0.8 * tot and (best is None or ok > best[1]):
                    best = (run, ok, tot, bad)
        if best:
            run, ok, tot, bad = best
            found.append((c, run, ok, tot, bad))
    # keep the most informative: drop identities whose total column is itself a part of a bigger one
    found.sort(key=lambda x: (-len(x[1]), x[0]))
    return found[:6]


def _header_groups(labels, dups):
    """side-by-side header groups: labels repeating with period P (base label = label without footnote marks);
    falls back to pandas duplicate-suffix groups."""
    lab_cols = [c for c in range(len(labels)) if labels[c]]
    base = [norm(re.sub(r"(\s*\d{1,2}/)+", " ", labels[c])) for c in lab_cols]
    for P in range(2, len(lab_cols) // 2 + 1):
        pairs = [(base[i], base[i + P]) for i in range(len(lab_cols) - P)]
        if pairs and sum(a == b for a, b in pairs) >= 0.75 * len(pairs) and len(set(base[:P])) >= 2:
            return [lab_cols[i:i + P] for i in range(0, len(lab_cols), P)]
    groups = collections.OrderedDict()
    for c in lab_cols:
        groups.setdefault(dups[c], []).append(c)
    return list(groups.values())


def _panels_for(data, ncols, labels, dups, hdr_year, notes, tag, seg_labels=None, colflags=None):
    """period columns -> side-by-side panels; else repeated column groups; else one panel.
    `colflags` (col -> set) receives 'label_realigned' / 'label_unreliable' for the columns a note is about."""
    colflags = colflags if colflags is not None else collections.defaultdict(set)
    pcols = []
    for c in range(ncols):
        vals = [r["cells"][c] for r in data if r["cells"][c]]
        dc = _has_days(vals)
        if len(vals) >= 2 and sum(_pp(v, dc) is not None for v in vals) >= 0.6 * len(vals):
            pcols.append(c)
    if seg_labels is not None and len(pcols) < 2:
        extra = [c for c in range(ncols) if PERIOD_WORDS.match(clean_label(seg_labels[c].split(" > ")[0]) or "x")]
        if len(extra) >= 2:
            pcols = sorted(set(pcols) | set(extra))
    col_label = {}
    if len(pcols) >= 2:
        # date columns that are DATA (maturity date next to issue date) are not panel stubs: side-by-side panels
        # need a repeating header or equal-width blocks, each with value columns
        st = pcols if pcols[0] == 0 else [0] + pcols
        widths = [(st[i + 1] if i + 1 < len(st) else ncols) - st[i] for i in range(len(st))]
        hg = _header_groups(labels, dups)
        gl = [len(g) for g in hg[:len(st)]]
        repeating = len(hg) >= len(st) and min(gl) >= 2 and max(gl) - min(gl) <= 1
        if seg_labels is None and not repeating and \
                (min(widths[:-1]) < 2 or max(widths[:-1]) - min(widths[:-1]) > 1):
            pcols = pcols[:1]
    if len(pcols) >= 2:
        starts = pcols if pcols[0] == 0 else [0] + pcols
        panels = []
        for k, s in enumerate(starts):
            e = starts[k + 1] if k + 1 < len(starts) else ncols
            panels.append({"pcol": s if s in pcols else None,
                           "vcols": list(range(s + 1, e)) if s in pcols else list(range(s, e)),
                           "year0": None, "kind": "side-by-side"})
        # drop trailing all-empty columns from the last panel
        last = panels[-1]
        while last["vcols"] and all(r["cells"][last["vcols"][-1]] == "" for r in data):
            last["vcols"].pop()
        glist = _header_groups(labels, dups)
        if seg_labels is not None:
            # in-body header rows give one label per column: use them as printed
            for pn in panels:
                for c in pn["vcols"]:
                    col_label[c] = seg_labels[c] or "?"
        elif len(glist) >= len(panels):
            for k, pn in enumerate(panels):
                labs = [labels[c] for c in glist[k]]
                isper = [bool(PERIOD_WORDS.match(re.sub(r"(\s*\d{1,2}/)+$", "", x.split(" > ")[0]).strip()))
                         for x in labs]
                vl = [x for x, ip in zip(labs, isper) if not ip]
                if len(labs) == len(pn["vcols"]) + 1 and pn["pcol"] is not None and not any(isper):
                    vl = labs[1:]                # first label of the group belongs to the period column
                # 'Period > From 10 to 20 years': a value label slid left onto the period column
                slid = [x.split(" > ", 1)[1] for x, ip in zip(labs, isper) if ip and " > " in x]
                if slid and len(vl) + len(slid) == len(pn["vcols"]):
                    vl = slid + vl
                    notes.append(f"{tag}panel {k + 1}: sub-label {slid[0]!r} sat on the period column; moved to the "
                                 f"first value column - VERIFY with a sibling printing or an identity")
                    for c in pn["vcols"]:
                        colflags[c].add("label_realigned")
                shifted = False
                if len(vl) == len(pn["vcols"]) and not any(isper) and len(labs) == len(pn["vcols"]) \
                        and pn["pcol"] is not None:
                    shifted = True               # period label merged into the first value label
                vl = [re.sub(r"^(Period|Date|Fiscal year or month) > ", "", x) for x in vl]
                if len(vl) == len(pn["vcols"]):
                    for q, c in enumerate(pn["vcols"]):
                        col_label[c] = vl[q]
                    if (shifted or any(x != y for x, y in zip(vl, labs[1:]))) and not slid:
                        notes.append(f"{tag}panel {k + 1}: period label was merged into a value label; value labels "
                                     f"re-assigned left to right - VERIFY with a sibling printing or an identity")
                        for c in pn["vcols"]:
                            colflags[c].add("label_realigned")
                else:
                    for q, c in enumerate(pn["vcols"]):
                        col_label[c] = (vl[q] if q < len(vl) else "?") + " ?"
                    notes.append(f"{tag}panel {k + 1}: {len(pn['vcols'])} value columns but the header group has "
                                 f"{len(vl)} value labels - labels are NOT reliable here (a column is missing/merged); "
                                 f"VERIFY against a sibling printing or an identity")
                    for c in pn["vcols"]:
                        colflags[c].add("label_unreliable")
        else:
            notes.append(f"{tag}{len(panels)} side-by-side panels found from period columns but the header has "
                         f"{len(glist)} label groups - labels shown raw, VERIFY")
            for pn in panels:
                for c in pn["vcols"]:
                    colflags[c].add("label_unreliable")
        return panels, pcols, col_label
    panel = {"pcol": 0 if pcols[:1] == [0] else None, "vcols": list(range(1, ncols)), "year0": None, "kind": "single"}
    if panel["pcol"] == 0:
        grp, why = _column_groups(panel["vcols"], labels, hdr_year)
        if grp:
            panels = [{"pcol": 0, "vcols": g, "kind": "group",
                       "year0": collections.Counter([hdr_year[c] for c in g if hdr_year[c]]).most_common(1)[0][0]
                       if any(hdr_year[c] for c in g) else None} for g in grp]
            notes.append(f"{tag}{len(grp)} column groups share the period stub ({why}); each group is a different "
                         f"run of years - unstacked below as panels 1..{len(grp)}")
            return panels, pcols, col_label
    return [panel], pcols, col_label


def _basis(section, stub_label):
    for txt in (section or "", stub_label or ""):
        sq = norm(txt).replace(" ", "")
        if "fiscalyear" in sq or sq.startswith("fiscal"):
            return "FY"
        if "calendaryear" in sq or "calenderyear" in sq:
            return "CY"
    return ""


def _banner_year(seg_rows, pn, section, issue):
    """unlabeled month block under a banner such as 'Dec. 1999 - Dec. 2000': anchor the LAST month row on
    the banner's last year and count backwards.  -> {row line: year} or {}"""
    ys = [int(y) for y in re.findall(r"\b((?:19|20)\d\d)\b", section or "")]
    if not ys or pn["pcol"] is None:
        return {}
    months = []
    for r in seg_rows:
        if r["kind"] != "data":
            continue
        p = parse_period(r["cells"][pn["pcol"]])
        if p and p["kind"] == "month":
            if p["year"] is not None:
                return {}
            months.append((r["line"], p["month"]))
    if not months:
        return {}
    year = max(ys)
    out = {}
    prev = None
    for ln, m in reversed(months):
        if prev is not None and m >= prev:
            year -= 1
        out[ln] = year
        prev = m
    if min(out.values()) < min(ys):
        return {}
    return out


def _fill(seg_rows, pn, k, section0, labels, issue="0000_00"):
    out = []
    pc = pn["pcol"]
    year = pn.get("year0")
    year_row = None
    last = None
    block = 0
    section = section0
    group = None
    basis = _basis(section0, labels[pc] if pc is not None else "")
    descending = False
    if pc is not None:
        ex = []
        for r in seg_rows:
            if r["kind"] == "data":
                p = parse_period(r["cells"][pc])
                if p and p["kind"] == "month" and p["year"]:
                    ex.append(p["year"] * 12 + p["month"])
        descending = len(ex) >= 2 and all(a > b for a, b in zip(ex, ex[1:]))
    prev_per = None
    dayctx = pc is not None and _has_days([r["cells"][pc] for r in seg_rows if r["kind"] == "data"])
    m0 = re.match(r"^((?:19|20)\d\d)\b", section0 or "")
    if m0:
        year_row = int(m0.group(1))                    # banner '1993-Con.' = continuation of the 1993 block
    banner = _banner_year(seg_rows, pn, section0, issue) if year is None else {}
    for r in seg_rows:
        if r["kind"] == "yearlabel":
            src = pn["vcols"] + ([pc] if pc is not None and pn["kind"] != "group" else [])
            ys = [int(float(r["cells"][c])) for c in src if r["cells"][c] and YEAR_RX.match(r["cells"][c])]
            if ys:
                year = collections.Counter(ys).most_common(1)[0][0]
                last = None
            continue
        if r["kind"] == "yeargroup":
            group = clean_label(r["cells"][0])
            gp = parse_period(r["cells"][0])
            if gp and gp["kind"] == "year":             # 'YYYY' row heading a block of month/day rows
                year_row, year, last = gp["year"], None, None
            continue
        if r["kind"] != "data":
            continue
        vals = [r["cells"][c] for c in pn["vcols"]]
        pcell = r["cells"][pc] if pc is not None else ""
        if not any(vals):
            continue
        per = None
        pflag = []
        if pc is not None:
            p = _pp(pcell, dayctx)
            if p is None and pcell:
                pflag.append("PERIOD?")
            elif p:
                if p["kind"] == "year":
                    per = f"{basis}{p['year']}"
                    year_row = p["year"]
                elif p["kind"] == "range":
                    per = f"{basis}{p['year']}-{p['year2']} (range)"
                elif p["kind"] == "fytd":
                    per = f"FY{p['year']} to date"
                elif p["kind"] == "month":
                    if p["year"] is not None:
                        if last and last[0] is not None and (p["year"], p["month"]) < (last[0], last[1]) and not descending:
                            pflag.append("YEAR_BACKWARDS")
                        year = p["year"]
                    elif r["line"] in banner:
                        year = banner[r["line"]]
                        pflag.append("year from section banner")
                    elif last is None and year is None and year_row is not None:
                        year = year_row                  # maturity-schedule style: 'YYYY' row heads the block
                        pflag.append("year from year row")
                    elif descending and last is not None and p["month"] > last[1]:
                        if year is not None:
                            year -= 1                    # newest-first table (OFS-2): Dec, Sept, June, Mar, <year-1> Dec
                            pflag.append("year-1 inferred (newest-first table)")
                        block += 1
                    elif not descending and last is not None and (p["month"] < last[1] or (p["month"] == last[1] and not p["day"])):
                        if year is not None:
                            year += 1
                            pflag.append("year+1 inferred")
                        block += 1
                    if year is None:
                        per = f"Y?{block}-{p['month']:02d}" if block else f"Y?-{p['month']:02d}"
                        pflag.append("YEAR_UNKNOWN")
                    else:
                        per = f"{year}-{p['month']:02d}"
                    if p["day"]:
                        per += f"-{p['day']:02d}"
                    last = (year, p["month"])
                elif p["kind"] == "day" and last:
                    per = f"{last[0] if last[0] else 'Y?'}-{last[1]:02d}-{p['day']:02d}"
                if p.get("est"):
                    per = (per or "") + " est"
            elif prev_per is not None and any(vals):
                per = prev_per                         # blank stub: same period as the row above
                pflag.append("stub blank: period of row above")
            prev_per = per if per else prev_per
        label = clean_label(pcell if pc is not None else r["cells"][0])
        labs = r.get("labels", labels)
        out.append({"line": r["line"], "panel": k + 1, "period": per, "label": label, "section": section,
                    "group": group, "cols": list(pn["vcols"]), "vals": vals, "flags": r["flags"] + pflag,
                    "labels": labs, "inbody": labs is not labels})
    return out


def _identity_over(out, keys, c, run):
    """(ok, tot) of the identity c = sum(run) over every segment/panel whose rows carry those columns."""
    ok = tot = 0
    for key in keys:
        rows_k = [o for o in out if (o["segment"], o["panel"]) == key]
        if rows_k and c in rows_k[0]["cols"] and all(x in rows_k[0]["cols"] for x in run):
            ok2, tot2, _ = _check_identity(rows_k, c, list(run))
            ok += ok2
            tot += tot2
    return ok, tot


def _label_shift(ncols, col_label, empty_cols, out, keys, identities):
    """W0-8 rule 1, strict: the header has k+1 labels for k data-bearing columns, the labels carry the ordinals
    (1)..(k) with exactly one unnumbered parent-only label, the trailing labelled column is empty in every
    row, and a total identity FAILS with the labels as printed but holds once the labels right of the
    unnumbered one move one column to the left.  -> ({col: label after the shift}, note) or (None, None)."""
    if not empty_cols or empty_cols[-1] != ncols - 1 or not col_label[ncols - 1]:
        return None, None
    k = len([c for c in range(1, ncols) if c not in empty_cols])
    labelled = [c for c in range(1, ncols) if col_label[c]]
    if len(labelled) != k + 1:
        return None, None
    ords, unnum = {}, []
    for c in labelled:
        n = column_key(col_label[c])[0]
        if n is None:
            unnum.append(c)
        elif int(n) in ords:
            return None, None
        else:
            ords[int(n)] = c
    if sorted(ords) != list(range(1, k + 1)) or len(unnum) != 1:
        return None, None
    u = unnum[0]
    if not any(c != u and norm(col_label[c].split(" > ")[0]) == norm(col_label[u]) for c in labelled):
        return None, None                                  # not a parent-only label
    shifted = {n: (c - 1 if c > u else c) for n, c in ords.items()}
    inv = {c: n for n, c in shifted.items()}
    for idn in identities:
        c_t, run = idn["total"], idn["run"]
        if c_t not in inv or any(c not in inv for c in run):
            continue
        p_t, p_run = ords[inv[c_t]], [ords[inv[c]] for c in run]
        ok0, tot0 = _identity_over(out, keys, p_t, p_run)
        ok1, tot1 = _identity_over(out, keys, c_t, run)
        if not (tot0 >= 3 and ok0 >= 0.8 * tot0) and tot1 >= 3 and ok1 >= 0.8 * tot1:
            rs = f"c{run[0]}..c{run[-1]}" if len(run) > 2 else "+".join(f"c{x}" for x in run)
            note = (f"header realigned: {k + 1} labels for {k} numeric columns; identity c{c_t} = sum {rs} holds only "
                    f"after the shift (labels right of c{u} '{col_label[u][:40]}' move one column left; both mappings "
                    "shown in COLUMNS as LABELSHIFT-1)")
            return {c: col_label[c + 1] for c in range(u, ncols - 1)}, note
    return None, None


def _date_label_candidates(ncols, col_label, coltype, dups, pcols) -> list:
    """W0-8 rule 2: columns whose amount-like label carries a pandas '.n' suffix while the column holds dates
    and the label's base sits on a num column - the label belongs to a date column of a sibling printing."""
    out = []
    for c in range(1, ncols):
        if coltype[c] != "date" or not dups[c] or c in pcols:
            continue
        lab = norm(col_label[c])
        if not re.search(r"\b(amount|outstanding|millions|thousands|billions)\b", lab) or re.search(r"\bdate\b", lab):
            continue
        if any(b != c and coltype[b] == "num" and norm(col_label[b]) == lab for b in range(ncols)):
            out.append(c)
    return out


def analyze(t: dict, repair: bool = True, prev_header=None) -> dict:
    """Pure analysis of one table part (see module docstring).  Returns labels, per-row periods and flags,
    NOTES, column periods, and the structured facts the rules use: `identities` (total column, run,
    support, failing lines) and `colflags` (column -> label_realigned / label_unreliable / header_shift)."""
    ncols = len(t["header"])
    notes = []
    colflags = collections.defaultdict(set)
    header = list(t["header"])
    if t.get("header_missing") and prev_header and len(prev_header) == ncols:
        header = list(prev_header)
        notes.append("header row missing (pandas 0..n-1): header borrowed from the previous table part")
    elif t.get("header_missing"):
        notes.append("header row missing (pandas 0..n-1) and no same-width previous part: columns unlabeled")
        header = ["" for _ in range(ncols)]
    hp = [header_parts(h) for h in header]
    firsts = {p[0] for p, _ in hp if p}
    if ncols > 2 and len(firsts) == 1 and all(len(p) >= 2 for p, _ in hp if p) and sum(bool(p) for p, _ in hp) >= ncols - 1:
        banner = next(iter(firsts))
        hp = [(p[1:], d) for p, d in hp]
        notes.append(f"every header cell starts with the same banner {banner[:70]!r}: stripped from the labels")
    labels = [" > ".join(p) for p, _ in hp]
    dups = [d for _, d in hp]
    hdr_year = [None] * ncols
    for c, (p, _) in enumerate(hp):
        for x in p:
            if YEAR_RX.match(x):
                hdr_year[c] = int(float(x))
    # a header-only block just above (title block) carries the real column names
    pre = t.get("pre_header")
    if pre and all(not l or YEAR_RX.match(l.split(" > ")[-1]) or l.split(" > ")[0] == labels[0].split(" > ")[0]
                   for l in labels[1:]):
        base = [" > ".join(header_parts(h)[0]) for h in pre[1:]]
        uniq = []
        for b in base:
            if norm(b) not in [norm(u) for u in uniq]:
                uniq.append(b)
        runs = []
        for c in range(1, ncols):
            if runs and hdr_year[c] == runs[-1][0]:
                runs[-1][1].append(c)
            else:
                runs.append([hdr_year[c], [c]])
        if uniq and all(len(r[1]) <= len(uniq) for r in runs):
            for y, cs in runs:
                for q, c in enumerate(cs):
                    labels[c] = uniq[q] + (f" > {y}" if y else "")
            notes.append("column names taken from the header-only block just above (title block); "
                         "year in the label = the group's first year")
    rows = _classify_rows(t, ncols, labels)
    eff = labels
    pending = []
    for r in rows:
        if r["kind"] == "header":
            pending.append(r["cells"])
            continue
        if pending:
            new = []
            for c in range(ncols):
                ps = []
                for hc in pending:
                    x = clean_label(hc[c])
                    if x and (not ps or norm(ps[-1]) != norm(x)):
                        ps.append(x)
                new.append(" > ".join(ps))
            if sum(bool(x) for x in new) >= max(2, ncols // 2):
                eff = new
                notes.append(f"in-body header rows before line {r['line']}: column labels replaced from there on")
            pending = []
        r["labels"] = eff
    # stacked rows: two table rows OCR'd into one line, most cells holding 2 values ('91 182', '3,524 3,040')
    if repair:
        new_rows = []
        nsplit = 0
        for r in rows:
            if r["kind"] != "data":
                new_rows.append(r)
                continue
            vals = r["cells"][1:]
            ne = [c for c in vals if c]
            ks = []
            for c in ne:
                f = parse_cell(c)[1]
                mk = [x for x in f if x.startswith("multi:")]
                if mk:
                    ks.append(int(mk[0].split(":")[1]))
            k = collections.Counter(ks).most_common(1)[0][0] if ks else 0
            if k >= 2 and len(ne) >= 3 and ks.count(k) >= 0.5 * len(ne):
                pieces = []
                for c in r["cells"]:
                    sp = c.replace("$", "").split() if any(x == f"multi:{k}" for x in parse_cell(c)[1]) else None
                    if sp is None and c:
                        dp = re.split(r"\s+(?=(?:19|20)\d\d\s*-|(?:Jan|Feb|Mar|Apr|May|June?|July?|Aug|Sept?|Oct|Nov|Dec)\b)", c)
                        sp = dp if len(dp) == k else None
                    pieces.append(sp)
                for i in range(k):
                    cells = []
                    for c, sp in zip(r["cells"], pieces):
                        cells.append(sp[i] if sp else (c if (i == 0 or c is r["cells"][0]) else ""))
                    cells[0] = r["cells"][0] if i == 0 else ""
                    new_rows.append({"line": r["line"], "cells": cells, "kind": "data", "flags": [f"ROWSPLIT {i + 1}/{k}"],
                                     "labels": r.get("labels")})
                nsplit += 1
            else:
                new_rows.append(r)
        if nsplit:
            rows = new_rows
            notes.append(f"{nsplit} lines held two table rows each (stacked-row OCR); split into sub-rows marked "
                         "ROWSPLIT i/k - single-valued cells kept on the first sub-row, VERIFY")
    data = [r for r in rows if r["kind"] == "data"]
    coltype = []
    for c in range(ncols):
        cnt = collections.Counter(cell_type(r["cells"][c]) for r in data)
        cnt.pop("empty", None)
        coltype.append(cnt.most_common(1)[0][0] if cnt else "empty")
    if repair and data:
        for r in data:
            cells = r["cells"]
            for c in range(ncols):
                v, f = parse_cell(cells[c])
                mk = [x for x in f if x.startswith("multi:")]
                if not mk:
                    continue
                k = int(mk[0].split(":")[1])
                trailing = 0
                for x in reversed(cells):
                    if x:
                        break
                    trailing += 1
                if trailing >= k - 1 and all(coltype[c + q] in ("num", "pct") for q in range(k) if c + q < ncols):
                    cells = cells[:c] + cells[c].replace("$", "").split() + cells[c + 1:ncols - (k - 1)]
                    r["flags"].append(f"SPLIT c{c}")
                    break
            r["cells"] = cells
        # rows that lost their first cell (everything slid one column left): compare each row's type
        # signature with the signature of rows whose first cell is a period (the header-conformant rows)
        refrows = [r for r in data if cell_type(r["cells"][0]) == "period"]
        if len(refrows) >= 3:
            ref = []
            for c in range(ncols):
                cnt = collections.Counter(cell_type(r["cells"][c]) for r in refrows)
                cnt.pop("empty", None)
                ref.append(cnt.most_common(1)[0][0] if cnt else "empty")
            for r in data:
                cells = r["cells"]
                if not cells[0] or cell_type(cells[0]) == "period" or cells[-1] != "":
                    continue
                s0 = sum(cell_type(cells[c]) == ref[c] for c in range(ncols) if cells[c])
                s1 = sum(cell_type(cells[c]) == ref[c + 1] for c in range(ncols - 1) if cells[c])
                if s1 >= s0 + 2:
                    r["cells"] = [""] + cells[:-1]
                    r["flags"].append("SHIFT+1")
    coltype = []                                   # recomputed after repairs
    for c in range(ncols):
        cnt = collections.Counter(cell_type(r["cells"][c]) for r in data)
        cnt.pop("empty", None)
        coltype.append(cnt.most_common(1)[0][0] if cnt else "empty")
    if t.get("orphans"):
        notes.append("text line(s) just after the table look like a table row that fell out of the pipe table "
                     f"(label + 4+ numbers) at line(s) {','.join(map(str, t['orphans']))}: read them with --raw/read")
    empty_cols = [c for c in range(ncols) if data and all(r["cells"][c] == "" for r in data)]
    if empty_cols:
        notes.append("columns empty in every data row: " + ",".join(f"c{c}" for c in empty_cols))
        tail = [c for c in empty_cols if c > ncols - 1 - len(empty_cols) and labels[c]]
        if tail and tail[-1] == ncols - 1 and len(pcols_guess(data, ncols)) <= 1:
            notes.append(f"the LAST labelled column(s) {','.join(f'c{c}' for c in tail)} ('{labels[tail[-1]][:50]}') never "
                         "hold data: the header probably has a spurious extra label, so labels right of it sit one column "
                         "too far right - pin columns with an identity or a sibling printing")
            for c in range(1, ncols):
                if c not in empty_cols:
                    colflags[c].add("label_unreliable")
    # ---- segments: a section banner or an in-body header starts a new layout
    segs = []
    cur = {"rows": [], "section": None}
    for r in rows:
        if r["kind"] in ("section", "header"):
            if r["kind"] == "section":
                ne = [c for c in r["cells"] if c]
                sec = clean_label(ne[0]) if ne else None
            else:
                sec = cur["section"]
            if any(x["kind"] == "data" for x in cur["rows"]):
                segs.append(cur)
                cur = {"rows": [], "section": sec}
            elif r["kind"] == "section":
                # consecutive banners ('Assets' / 'U.S. dollars:' / 'Held with Treasury:') form a path
                cur["section"] = f"{cur['section']} > {sec}" if cur["section"] and sec else sec
            continue
        cur["rows"].append(r)
    segs.append(cur)
    segs = [s for s in segs if any(x["kind"] == "data" for x in s["rows"])]
    col_label = list(labels)
    out = []
    seg_layouts = []
    all_panels = []
    all_pcols = set()
    for si, sg in enumerate(segs):
        sdata = [r for r in sg["rows"] if r["kind"] == "data"]
        tag = f"[{(sg['section'] or 'segment %d' % (si + 1))[:40]}] " if len(segs) > 1 else ""
        inb = next((r["labels"] for r in sdata if r.get("labels") is not None and r["labels"] is not labels), None)
        panels, pcols, labs_fix = _panels_for(sdata, ncols, labels, dups, hdr_year, notes, tag, inb, colflags)
        all_pcols.update(pcols)
        if not all_panels or len(panels) > len(all_panels):
            for c, l in labs_fix.items():
                col_label[c] = l
        seg_layouts.append({"section": sg["section"], "panels": panels,
                            "labels": {c: labs_fix.get(c, (inb or labels)[c]) for pn in panels for c in pn["vcols"]},
                            "pcols": pcols})
        seg_out = []
        for k, pn in enumerate(panels):
            seg_out += _fill(sg["rows"], pn, k, sg["section"], labels, t["issue"])
        for o in seg_out:
            o["segment"] = si
            if labs_fix and not o["inbody"]:
                o["labels"] = [labs_fix.get(c, labels[c]) for c in range(ncols)]
                o["inbody"] = True
        out += seg_out
        if len(panels) > len(all_panels):
            all_panels = panels
    if not all_panels:
        all_panels = [{"pcol": 0, "vcols": list(range(1, ncols)), "year0": None, "kind": "single"}]
    colper = _col_periods(labels, list(range(1, ncols)), t["issue"])
    if any(v[1] for v in colper.values()):
        notes.append("column periods without a printed year were inferred (after annual columns: last year + 1; "
                     "months wrap -> +1); check against the issue date " + t["issue"])
    for c in range(ncols):
        lab = norm(col_label[c])
        if not lab or coltype[c] == "empty" or c in all_pcols:
            continue
        if re.search(r"\bdate\b", lab) and coltype[c] in ("num", "pct"):
            notes.append(f"c{c} labelled '{col_label[c]}' holds {coltype[c]} values - header probably shifted")
            for c2 in range(1, ncols):
                colflags[c2].add("header_shift")
        if re.search(r"\b(bid|yield|rate|percent|discount)\b", lab) and coltype[c] == "date":
            notes.append(f"c{c} labelled '{col_label[c]}' holds dates - header probably shifted")
            for c2 in range(1, ncols):
                colflags[c2].add("header_shift")

    def mindex(p):
        m = re.match(r"^(\d{4})-(\d\d)$", p or "")
        return int(m.group(1)) * 12 + int(m.group(2)) - 1 if m else None
    keys = sorted({(o["segment"], o["panel"]) for o in out})
    for key in keys:
        seq = [o for o in out if (o["segment"], o["panel"]) == key]
        steps = []
        seq = [o for o in seq if not any(f.startswith("stub blank") or (f.startswith("ROWSPLIT") and not f.startswith("ROWSPLIT 1/"))
                                         for f in o["flags"])]
        for a, b in zip(seq, seq[1:]):
            ia, ib = mindex(a["period"]), mindex(b["period"])
            if ia is not None and ib is not None:
                steps.append((b, ib - ia))
        if len(steps) >= 4:
            mode = collections.Counter(d for _, d in steps).most_common(1)[0][0]
            for i, (b, d) in enumerate(steps):
                if d == mode:
                    continue
                prev_d = steps[i - 1][1] if i else None
                next_d = steps[i + 1][1] if i + 1 < len(steps) else None
                # a run of a different step (year-end rows before the monthly rows) is a block, not a break
                if d > 0 and (prev_d == d or next_d == d or i == 0):
                    continue
                b["flags"].append(f"SEQ step {d:+d} (usual {mode:+d})")
    unknown = sum("YEAR_UNKNOWN" in o["flags"] for o in out)
    if unknown:
        nblk = max([int(m.group(1)) for o in out for m in [re.match(r"Y\?(\d+)-", o["period"] or "")] if m] + [0]) + 1
        notes.append(f"{unknown} month rows carry no year in this printing ({nblk} Jan..Dec cycle(s) per panel): "
                     "anchor the years from a sibling printing (SIBLINGS) or the issue date, never by guessing")
    bad = [o for o in out if any(f.startswith("SEQ") or f == "YEAR_BACKWARDS" for f in o["flags"])]
    if bad:
        notes.append(f"{len(bad)} rows break the period sequence (OCR label/row damage?) at lines "
                     + ",".join(str(o["line"]) for o in bad[:12]) + " - VERIFY those rows")
    rep = [o for o in out if any(f == "SHIFT+1" or f.startswith("SPLIT") for f in o["flags"])]
    if rep:
        notes.append(f"{len(rep)} rows repaired (SHIFT+1 / SPLIT); compare with --raw at lines "
                     + ",".join(str(o["line"]) for o in rep[:12]))
    idents = []
    for key in keys:
        rows_k = [o for o in out if (o["segment"], o["panel"]) == key]
        if rows_k:
            for c, run, ok, tot, badl in _identities(rows_k, rows_k[0]["cols"], col_label):
                idents.append((key, c, run, ok, tot, badl))
    # candidate identities found strictly in some segment are then checked in every segment with the
    # same columns (a block with several OCR misreads would not reach 80% on its own)
    agg = collections.OrderedDict()
    for key, c, run, ok, tot, badl in idents:
        sig = (c, tuple(run))
        if sig in agg:
            continue
        a_ = agg.setdefault(sig, [0, 0, [], set()])
        for key2 in keys:
            rows_k = [o for o in out if (o["segment"], o["panel"]) == key2]
            if not rows_k or c not in rows_k[0]["cols"] or any(x not in rows_k[0]["cols"] for x in run):
                continue
            ok2, tot2, bad2 = _check_identity(rows_k, c, list(run))
            if tot2 and ok2 >= 0.5 * tot2:
                a_[0] += ok2
                a_[1] += tot2
                a_[2] += bad2
                a_[3].add(key2)
    # c3 = c4 + c5 also makes c5 ~ c3 + c4 when c4 is tiny: keep one identity per column set, the best-supported
    best = {}
    for (c, run), v in agg.items():
        if not v[1]:
            continue
        key = frozenset((c,) + run)
        if key not in best or v[0] / v[1] > best[key][1][0] / best[key][1][1]:
            best[key] = ((c, run), v)
    identities = []
    for (c, run), (ok, tot, badl, ks) in [x for x in best.values()][:4]:
        rs = f"c{run[0]}..c{run[-1]}" if len(run) > 2 else "+".join(f"c{x}" for x in run)
        notes.append(f"identity: c{c} = sum {rs} on {ok}/{tot} rows ({len(ks)} segment/panel(s))"
                     + (f"; FAILS at lines {','.join(map(str, badl[:10]))} - OCR misread or shifted cell?" if badl else ""))
        identities.append({"total": c, "run": list(run), "ok": ok, "tot": tot, "bad": sorted(set(badl))})
    # W0-8: both label mappings are kept; `labelshift` is printed next to the labels as printed, never instead
    labelshift, shift_note = _label_shift(ncols, col_label, empty_cols, out, keys, identities)
    if labelshift:
        notes.append(shift_note)
        for c in labelshift:
            colflags[c].add("label_shift")
    date_label_fix = _date_label_candidates(ncols, col_label, coltype, dups, all_pcols)
    return {"ncols": ncols, "labels": labels, "col_label": col_label, "coltype": coltype, "panels": all_panels,
            "pcols": sorted(all_pcols), "rows": out, "notes": notes, "raw_rows": rows, "colper": colper,
            "segments": len(segs), "layouts": seg_layouts, "identities": identities,
            "colflags": {c: sorted(v) for c, v in sorted(colflags.items())},
            "labelshift": labelshift or {}, "date_label_fix": date_label_fix}



# ----------------------------------------------------------------------------
# numbers in displayed text (table-aware: '2,659r', 'r 16,311', 'r-144,112', '(1,234)')
# ----------------------------------------------------------------------------
_NUM_TEXT_RX = re.compile(r"(?<![\d.])([-−]?)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?:[eE]([-+]?\d{1,3}))?(?![\d])"
                          r"|(?<![\d.])([-−]?)\.(\d+)(?![\d])")


def numbers(text: str, limit: int = 400) -> list:
    """Numbers as displayed, left to right: thousands separators joined (only exact 3-digit groups),
    glued flags ignored ('2,659r' -> 2659.0), unicode minus handled.  Used for Observe.attrs['values']."""
    out = []
    for m in _NUM_TEXT_RX.finditer(text or ""):
        try:
            if m.group(2) is not None:
                v = float(m.group(2).replace(",", "") + (m.group(3) or "") + (("e" + m.group(4)) if m.group(4) else ""))
                neg = bool(m.group(1))
            else:
                v = float("0." + m.group(6))
                neg = bool(m.group(5))
        except ValueError:
            continue
        if v != v or v in (float("inf"), float("-inf")):
            continue
        out.append(-v if neg else v)
        if len(out) >= limit:
            break
    return out


# ----------------------------------------------------------------------------
# index (sqlite + FTS5, built once; opened read-only and immutable)
# ----------------------------------------------------------------------------
_SCHEMA = (
    "CREATE TABLE t(rid INTEGER PRIMARY KEY, file TEXT, issue TEXT, hdr_line INT, end_line INT,"
    " title_line INT, tid TEXT, tid_raw TEXT, title TEXT, subtitle TEXT, units TEXT, asof TEXT,"
    " section TEXT, cont INT, part INT, ncols INT, nrows INT, kind TEXT, header TEXT,"
    " first_period TEXT, last_period TEXT, squash TEXT)",
    "CREATE VIRTUAL TABLE f USING fts5(title, header, rowlabels, content='', tokenize='unicode61')",
    "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE INDEX t_tid ON t(tid, issue)",
    "CREATE INDEX t_file ON t(file, hdr_line)",
    # W0-12: narrative sections (upper-case headings with their sub-headings); absent from v9 indexes,
    # which the tools still accept (the [section] block is then skipped)
    "CREATE TABLE s(sid INTEGER PRIMARY KEY, file TEXT, issue TEXT, line INT, end_line INT, heading TEXT, subs TEXT)",
    "CREATE VIRTUAL TABLE fs USING fts5(heading, subs, content='', tokenize='unicode61')",
    "CREATE INDEX s_file ON s(file, line)",
)
SECTIONS_PER_FILE = 500
SUBS_PER_SECTION = 12


def _is_subheading(lines, j: int) -> bool:
    """A short Title-case line (2+ words, mostly lower case, no final punctuation) followed within three
    lines by a prose line of 120+ characters: a narrative sub-heading such as 'Auction of 2-Year Notes'."""
    s = lines[j].strip()
    letters = [ch for ch in s if ch.isalpha()]
    if not (4 <= len(s) <= 80 and " " in s and len(letters) >= 4 and s[0].isupper() and not s.startswith("|")):
        return False
    if s[-1] in ".:;," or PAGE_RX.match(s) or FOOT_RX.match(s) or _is_units(s) or ID_RX.match(s) or NUM_TITLE_RX.match(s):
        return False
    if sum(ch.isupper() for ch in letters) / len(letters) > 0.6 or len(re.findall(r"\d[\d,.]*", s)) >= 3:
        return False
    if "$" in s or s.split()[-1].lower() in STOPWORDS:      # a wrapped prose line, not a heading
        return False
    for k in range(j + 1, min(len(lines), j + 4)):
        nxt = lines[k].strip()
        if nxt:
            return len(nxt) >= 120 and not nxt.startswith("|")
    return False


def narrative_sections(lines) -> list:
    """[(line, end_line, heading, [(line, sub-heading)])] of one issue: every multi-word upper-case heading
    outside the pipe tables; a heading repeated as the running head of the next page extends its section
    (one-word banners such as a month name break nothing); a section runs to the next heading."""
    heads = []
    for i, s in enumerate(lines):
        st = s.strip()
        if not st or st.startswith("|") or " " not in st or not _is_caps_heading(st):
            continue
        if PAGE_RX.match(st) or FOOT_RX.match(st) or _is_units(st):
            continue
        if heads and norm(heads[-1][1]) == norm(st):
            continue
        heads.append((i + 1, st))
        if len(heads) >= SECTIONS_PER_FILE:
            break
    out = []
    for k, (ln, h) in enumerate(heads):
        end = heads[k + 1][0] - 1 if k + 1 < len(heads) else len(lines)
        subs = []
        for j in range(ln, min(end, ln + 600)):
            if _is_subheading(lines, j):
                subs.append((j + 1, lines[j].strip()))
                if len(subs) >= SUBS_PER_SECTION:
                    break
        out.append((ln, end, h, subs))
    return out


def corpus_files(corpus_dir) -> list:
    return sorted(Path(corpus_dir).glob("treasury_bulletin_*.txt"))


def build_index(corpus_dir, out_path, *, files=None, verbose: bool = False) -> dict:
    """Index every pipe table of the corpus (or of `files`, basenames inside corpus_dir) into a fresh
    SQLite file at out_path (written to '<out>.tmp', then renamed; mode 0444).  Deterministic content:
    files in sorted order, no timestamps.  Returns build statistics (tables, files, seconds, bytes)."""
    corpus = Path(corpus_dir).resolve()
    out = Path(out_path).resolve()
    paths = corpus_files(corpus) if files is None else sorted(corpus / Path(f).name for f in files)
    missing = [p.name for p in paths if not p.is_file()]
    if not paths or missing:
        raise ToolError(f"corpus files missing under {corpus}: {missing[:3] or 'none found'}")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    t0 = time.time()
    con = sqlite3.connect(tmp)
    n = nsec = 0
    manifest = []
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        for stmt in _SCHEMA:
            con.execute(stmt)
        for i, p in enumerate(paths):
            data = p.read_bytes()
            manifest.append({"file": p.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            for t in file_tables(str(corpus), p.name):
                labs, seen, pers = [], set(), []
                for _, cells in t["rows"]:
                    c0 = clean_label(cells[0]) if cells else ""
                    if c0 and parse_period(c0):
                        pers.append(c0)
                        continue
                    k = norm(c0)
                    if k and k not in seen and len(k) > 2:
                        seen.add(k)
                        labs.append(k)
                hdr = " | ".join(" > ".join(header_parts(h)[0]) for h in t["header"])
                squash = norm(" ".join(x for x in [t.get("title") or "", " ".join(t.get("subtitle") or []),
                                                    t.get("section") or "", hdr])).replace(" ", "")
                cur = con.execute("INSERT INTO t VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (t["file"], t["issue"], t["hdr_line"], t["end_line"], t.get("title_line"), t.get("tid"),
                                   t.get("tid_raw"), t.get("title"), " / ".join(t.get("subtitle") or []), t.get("units"),
                                   t.get("asof"), t.get("section"), int(t["cont"]), t["part"], len(t["header"]),
                                   len(t["rows"]), t["kind"], hdr, pers[0] if pers else None, pers[-1] if pers else None,
                                   squash))
                ttl = " ".join(x for x in [t.get("tid") or "", t.get("tid_raw") or "", t.get("title") or "",
                                            " ".join(t.get("subtitle") or []), t.get("units") or "", t.get("section") or ""])
                con.execute("INSERT INTO f(rowid, title, header, rowlabels) VALUES (?,?,?,?)",
                            (cur.lastrowid, norm(ttl), norm(hdr), " ; ".join(labs)[:1500]))
                n += 1
            for ln, end, heading, subs in narrative_sections(load_lines(str(corpus), p.name)):
                subs_text = " ; ".join(f"{s} (:{k})" for k, s in subs)
                cur = con.execute("INSERT INTO s VALUES (NULL,?,?,?,?,?,?)",
                                  (p.name, issue_of(p.name), ln, end, heading, subs_text))
                con.execute("INSERT INTO fs(rowid, heading, subs) VALUES (?,?,?)",
                            (cur.lastrowid, norm(heading), norm(" ".join(s for _, s in subs))))
                nsec += 1
            if verbose and i % 100 == 0:
                print(f"  {i}/{len(paths)} files, {n} tables", flush=True)
        meta = {"version": INDEX_VERSION, "files": len(paths), "tables": n, "sections": nsec,
                "corpus_manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()}
        con.executemany("INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v)) for k, v in sorted(meta.items())])
        con.commit()
    finally:
        con.close()
    os.replace(tmp, out)
    out.chmod(0o444)
    return {**meta, "seconds": round(time.time() - t0, 1), "bytes": out.stat().st_size, "path": str(out)}


def connect_ro(index) -> sqlite3.Connection:
    """Read-only, immutable connection (no locks, no journal: the index never changes after build).
    Usable from any thread: the broker relays every Codex request on a fresh handler thread and runs
    /v1/submit on another, all sharing the pack's one Tables (a same-thread connection made every
    later re-derivation fail silently and degrade table cells to raw lines). Safe because the index is
    read-only and the sqlite library serialises access in its default threading mode (threadsafety 3)."""
    p = Path(index).resolve()
    if not p.is_file():
        raise ToolError(f"tables index not found: {p} (build it with `python -m packs.officeqa.tools.cli build`)")
    return sqlite3.connect(p.as_uri() + "?mode=ro&immutable=1", uri=True,
                           check_same_thread=sqlite3.threadsafety != 3)


def _years(spec):
    if not spec:
        return None
    m = re.match(r"^(\d{4})(?:\s*[-:]\s*(\d{4}))?$", str(spec).strip())
    if not m:
        raise ToolError("--years takes YYYY or YYYY-YYYY")
    a = int(m.group(1))
    b = int(m.group(2) or a)
    return min(a, b), max(a, b)


def _id_like(s):
    return re.fullmatch(r"[A-Za-z0-9]{1,6}(?:-[A-Za-z0-9]{1,4})?-\d{1,2}[A-Za-z]?", s.strip()) is not None


def _fts_phrase(tokens, cols, prefix=False, op="AND"):
    """FTS5 query: every token (AND) or any token (OR) inside the column filter `cols`; with `prefix`,
    tokens of PREFIX_MIN+ characters match as prefixes ('"canadian"*' also hits 'canadians')."""
    return f" {op} ".join(f'{cols} : "{tk}"' + ("*" if prefix and len(tk) >= PREFIX_MIN else "") for tk in tokens)


def _tok_match(tok: str, words) -> bool:
    """Does query token `tok` match one of `words` the way the FTS query does (prefix from PREFIX_MIN chars)?"""
    return any(w == tok or (len(tok) >= PREFIX_MIN and w.startswith(tok)) for w in words)


def _strip_table_n(title: str) -> str:
    """'Table 1. Summary of ...' -> 'Summary of ...' (group key only; the title is printed as stored)."""
    return re.sub(r"^\s*table\s*\d{1,2}\s*[.\-" + DASHES + r"]*\s*", "", title or "", flags=re.I)


# ----------------------------------------------------------------------------
# cell records shared by the pages (observe.py) and the rules (rules.py)
# ----------------------------------------------------------------------------
_ASOF_PARTS = re.compile(r"(" + MONTHNAME + r")\s+(\d{1,2})\s*,\s*((?:19|20)\d\d)", re.I)


def display_name(t: dict) -> str:
    """Table id, else 'Table N. Title', else the title; single line, no '|', at most 70 chars."""
    if t.get("tid"):
        name = t["tid"]
    elif t.get("tid_raw"):
        name = f"{t['tid_raw']}. {t.get('title') or ''}".strip(" .")
    else:
        name = t.get("title") or "(untitled)"
    return re.sub(r"\s+", " ", str(name).replace("|", "/")).strip()[:70] or "(untitled)"


def _date_period(text: str) -> str:
    """'Sept. 30, 2011' -> '2011-09-30'; anything else unchanged."""
    m = _ASOF_PARTS.fullmatch(text.strip())
    if not m:
        return text
    mon = MONTHS.get(m.group(1).lower()[:3])
    return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}" if mon else text


def effective_period(row_period: str, colper) -> tuple:
    """(period used for matching, extra flags).  Row periods win; a column period supplies the period
    for label rows (transposed tables) and the month for year x month matrices."""
    row_period = row_period or ""
    if not colper:
        return row_period, []
    text, inferred = colper
    flags = ["col period inferred"] if inferred else []
    if text.endswith("?"):
        flags.append("col period after issue date")
    base = _date_period(text.rstrip("?"))
    m = re.fullmatch(r"Y\?-(\d\d)", base)
    if m and re.fullmatch(r"(FY|CY)?\d{4}", row_period):
        return f"{row_period[-4:]}-{m.group(1)}", flags
    if not re.match(r"^((FY|CY)?\d{4}|Y\?)", row_period):
        m = re.fullmatch(r"(\d{4}) \(year\)", base)
        return (m.group(1) if m else base), flags
    return row_period, flags


REPAIR_PREFIXES = ("SHIFT+1", "SPLIT", "ROWSPLIT")


def column_key(label: str) -> tuple:
    """(printed column number or None, whitespace-free label without footnote marks) - the identity of a
    column across printings."""
    lab = re.sub(r"\s*\?$", "", str(label or "")).strip()
    m = re.search(r"\((\d{1,2})\)\s*$", lab)
    base = re.sub(r"\(\d{1,2}\)\s*$", "", lab).strip()
    base = re.sub(r"(?:\s+\d{1,2}/?)+$", "", base)
    return (m.group(1) if m else None, norm(base).replace(" ", ""))


def _label_has(label, want: str) -> bool:
    """Normalised substring match that also ignores intra-word spaces (OCR 'To tal as sets') and footnote
    marks on the label side; `want` is already norm()ed."""
    lab = norm(label or "")
    if want in lab:
        return True
    sq = want.replace(" ", "")
    return bool(sq) and sq in re.sub(r"(?:\s+\d{1,2}/?)+$", "", lab).replace(" ", "")


_BANNER_NOISE_RX = re.compile(r"\b(?:(?:19|20)\d\d|calendar|fiscal|years?|months?|period|continued|cont|through|thru|ending|ended"
                              r"|january|february|march|april|june|july|august|september|october|november|december"
                              r"|jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec|\d+)\b")
_GLUED_NOISE = ("calendaryears", "calendaryear", "fiscalyears", "fiscalyear")


def panel_stub(section: str) -> str:
    """W0-3 panel key: the section banner without footnote marks, case, spaces, punctuation, years, month
    names and period words - so 'Calendar years', 'Calendaryears' and 'Dec. 1999 - Dec. 2000' (basis and year
    banners, not panels) all key as '' while 'Budget expenditures 2/' keys as 'budgetexpenditures'.  Noise
    words are stripped as whole words ('Contract authority' keeps its 'cont', 'Marketable' its 'mar')."""
    words = norm(re.sub(r"(?:\s*\d{1,2}/)+", " ", section or ""))
    words = " ".join(w for w in _BANNER_NOISE_RX.sub(" ", words).split() if w not in _GLUED_NOISE)
    return words.replace(" ", "")[:40]


def same_panel(a: str, b: str) -> bool:
    """Two panel stubs of one series: equal, or near-identical strings (an OCR variant such as
    'monthlyseries' / 'monthlyaeries'; the ratio is difflib's over the two stubs)."""
    if a == b:
        return True
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def _period_sort(per: str) -> tuple:
    """Sort key of a filled period ('FY2003', '2003-01', '2003-01-15'): (year, month, day)."""
    m = re.match(r"^(?:FY|CY)?(\d{4})(?:-(\d\d))?(?:-(\d\d))?", per or "")
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)) if m else (0, 0, 0)


PERIOD_LOOKBACK = 60          # printings diagnostics: a row period more than this many years before its issue is OCR damage


def _period_range(periods: list) -> tuple:
    """(earliest, latest) of the filled periods a scan saw, ignoring OCR-damaged years: a period whose year
    lies after its issue's year + 1 or more than PERIOD_LOOKBACK years before it (the same mis-read row
    repeats in every printing, so frequency cannot tell).  `periods` holds (period, issue) pairs; () when
    nothing plausible remains."""
    ok = {p for p, i in periods if int(i[:4]) - PERIOD_LOOKBACK <= _period_sort(p)[0] <= int(i[:4]) + 1}
    # ties on the (year, month, day) key ('FY1935-1953', 'CY1935-1951') break on the label text: a set's
    # iteration order follows the per-process hash seed, and the printed range must not vary between runs
    keyed = sorted(ok, key=lambda p: (_period_sort(p), p))
    return (keyed[0], keyed[-1]) if keyed else ()


def exact_value(raw: str):
    """W0-4: the exact parsed value of a printed cell (Decimal of the text with '$', commas, r/p flags,
    footnote marks and a percent sign stripped; parentheses = negative), or the stripped text when it is not
    a number.  '5.0' and '5' are one value; 6+ digit values never round."""
    val, _ = parse_cell(raw)
    t = raw.strip().replace("$", "").replace(",", "")
    t = re.sub(r"(\s+\d{1,2}/\d?)+$", "", t).strip()
    t = re.sub(r"^[rp]\s*|\s*[rp]$", "", t).strip().rstrip("%").strip()
    m = re.fullmatch(r"\((-?\d*\.?\d+)\)\d?", t)
    if m:
        t = "-" + m.group(1)
    t = t.replace(" ", "")
    if val is None:
        return raw.strip()
    try:
        return Decimal(t)
    except InvalidOperation:
        return Decimal(repr(val))


def row_label_for(a, o, c):
    """label of source column c for output row o (in-body header labels win)."""
    if o.get("inbody"):
        x = o["labels"][c]
        return x or a["col_label"][c]
    return a["col_label"][c]


def cell_record(a: dict, t: dict, o: dict, c: int, raw: str, *, units=None, name=None) -> dict:
    """Everything the ledger and the rules need about one displayed cell.  `a` = analyze(t), `o` = one of
    a['rows'], `c` = source column index in t.  Keys: locator, text, value, attrs (namespaced 'oq.*')."""
    val, cfl = parse_cell(raw)
    lab = row_label_for(a, o, c)
    colper = a["colper"].get(c)
    period, pflags = effective_period(o.get("period") or "", colper)
    rowf = list(o.get("flags") or [])
    flags = [f for f in cfl if f not in ("float0", "empty")] + rowf + pflags + list(a.get("colflags", {}).get(c, []))
    if lab.endswith(" ?") and "label_unreliable" not in flags:
        flags.append("label_unreliable")
    ident = any(o["line"] in idn["bad"] and (c == idn["total"] or c in idn["run"]) for idn in a.get("identities", []))
    if ident:
        flags.append("IDENTITY_FAIL")
    repair = " ".join(f for f in rowf if f.startswith(REPAIR_PREFIXES))
    attrs = {"oq.table": name or display_name(t), "oq.table_ref": f"{t['file']}:{t['hdr_line']}",
             "oq.file": t["file"], "oq.issue": t["issue"], "oq.part": t["part"], "oq.col": f"c{c}",
             "oq.column": lab, "oq.row": o.get("label") or "", "oq.section": o.get("section") or "",
             "oq.period": period, "oq.row_period": o.get("period") or "",
             "oq.col_period": (colper[0] + ("~" if colper[1] else "")) if colper else "",
             "oq.flags": flags, "oq.repair": repair, "oq.identity_fail": ident,
             "oq.units": (units if units is not None else (t.get("units") or ""))[:80]}
    return {"locator": f"{t['file']}:{o['line']}", "text": raw, "value": val, "attrs": attrs}


# ----------------------------------------------------------------------------
# pages
# ----------------------------------------------------------------------------
@dataclass
class Cell:
    """One displayed data cell: `line` is the 0-based index of the output line that shows it."""
    line: int
    locator: str
    text: str
    value: float | None
    attrs: dict = field(default_factory=dict)


@dataclass
class Page:
    kind: str                      # 'show' | 'find' | 'printings'
    lines: list
    cells: list
    meta: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _bytes(lines) -> int:
    return sum(len(x.encode("utf-8")) + 1 for x in lines)


def _fit_lines(fixed_head, flexible, fixed_tail, avail, what):
    """Keep fixed_head + as many flexible lines (in order) as fit + fixed_tail within avail bytes."""
    if _bytes(fixed_head) + _bytes(flexible) + _bytes(fixed_tail) <= avail:
        return fixed_head + flexible + fixed_tail
    marker = f"  (… {{n}} {what} lines cut to fit the {PAGE_BUDGET}-byte page; narrow with --cols / --match)"
    room = avail - _bytes(fixed_head) - _bytes(fixed_tail) - _bytes([marker.format(n=len(flexible))])
    kept = []
    for x in flexible:
        if _bytes(kept + [x]) > room:
            break
        kept.append(x)
    return fixed_head + kept + [marker.format(n=len(flexible) - len(kept))] + fixed_tail


def _paginate(header_fn, pre, entries, total, from_row, budget, cont_hint, footer):
    """Assemble one page: header line, preamble, as many whole row entries as fit, continuation line,
    footer.  An entry is (lines, [(offset, Cell)]).  At least one entry is always shown when available."""
    def cont(nxt):
        return (f"... PAGE ENDS after row {nxt} of {total} ({budget}-byte page): continue with --from-row {nxt}"
                f" (same other options){cont_hint}")
    reserve = _bytes([header_fn(total, total), cont(total)] + ([footer] if footer else []))
    kept, used = [], _bytes(pre) + reserve
    for e in entries:
        s = _bytes(e[0])
        if kept and used + s > budget:
            break
        kept.append(e)
        used += s
    shown = len(kept)
    a, b = (from_row + 1, from_row + shown) if shown else (0, 0)
    lines = [header_fn(a, b)] + list(pre)
    cells = []
    for e_lines, e_cells in kept:
        base = len(lines)
        lines += e_lines
        for off, c in e_cells:
            c.line = base + off
            cells.append(c)
    if from_row + shown < total:
        lines.append(cont(from_row + shown))
    if footer:
        lines.append(footer)
    return lines, cells, (a, b)


def _cap_text(lines, cells, budget, note):
    """Cut a line list at `budget` bytes (whole lines), dropping cells on cut lines; adds `note`."""
    if _bytes(lines) <= budget:
        return lines, cells
    room = budget - _bytes([note])
    kept, size = [], 0
    for x in lines:
        s = len(x.encode("utf-8")) + 1
        if size + s > room:
            break
        kept.append(x)
        size += s
    return kept + [note], [c for c in cells if c.line < len(kept)]


def _col_filter(spec, ncols):
    if not spec:
        return None
    keep = set()
    for part in str(spec).split(","):
        part = part.strip().lstrip("cv")
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                keep.update(range(int(a), int(b) + 1))
            elif part:
                keep.add(int(part))
        except ValueError:
            raise ToolError(f"--cols takes column numbers such as '1-3,7', got {spec!r}") from None
    return keep


FOOTER = ("(period = filled-down row period, FY/CY from banners; 'Y?' = year not printed; <..~> = inferred "
          "column period; raw cells keep r/p flags and footnote marks)")
GREP_HINT = ("--grep is a case-insensitive substring of the filled period ({example}), the raw row label or the "
             "section, not a regex and not a column filter (use --match for columns)")


def grep_hint(a: dict) -> str:
    """GREP_HINT with the table's own first filled period as the example (a neutral YYYY-MM when none)."""
    example = next((o["period"].split(" ")[0] for o in a["rows"] if re.match(r"^(FY|CY)?\d{4}", o["period"] or "")), "YYYY-MM")
    return GREP_HINT.format(example=example)


def grep_terms(grep) -> list:
    """W0-6: one or several --grep values, each split at '|': the row is kept when ANY term matches."""
    if not grep:
        return []
    values = grep if isinstance(grep, (list, tuple)) else [grep]
    return [p.strip() for g in values for p in str(g).split("|") if p.strip()]


def row_matches(o: dict, terms: list) -> bool:
    """A term matches the label, the filled period or the section as a normalised substring; a month-name
    form ('1969-Nov', 'Nov 1969') or a year also matches the filled period through parse_query_period."""
    per = o.get("period") or ""
    for g in terms:
        gn = norm(g)
        if gn and (gn in norm(o.get("label") or "") or gn in norm(per) or gn in norm(o.get("section") or "")):
            return True
        qp = parse_query_period(g)
        if qp and qp[1] is not None and per.startswith(f"{qp[0]}-{qp[1]:02d}"):
            return True
        if qp and qp[1] is None and re.match(rf"^(FY|CY)?{qp[0]}(\b|-)", per):
            return True
    return False


def period_window(spec: str) -> tuple:
    """'FY2021' -> ('FY', 2021, (2020, 10), (2021, 9)); 'CY1996' / '1996' -> ('CY', 1996, (1996, 1), (1996, 12))."""
    m = re.fullmatch(r"\s*(fy|cy)?\s*((?:19|20)\d\d)\s*", str(spec or ""), re.I)
    if not m:
        raise ToolError("--period takes FY2021 or CY1996 (a fiscal or calendar year)")
    basis, y = (m.group(1) or "CY").upper(), int(m.group(2))
    return (basis, y, (y - 1, 10), (y, 9)) if basis == "FY" else (basis, y, (y, 1), (y, 12))


def in_period_window(per: str, window: tuple) -> bool:
    """Month rows inside the window, plus the year row of that basis ('FY2021', 'CY1996', '1996')."""
    basis, y, lo, hi = window
    m = re.match(r"^(\d{4})-(\d\d)", per or "")
    if m:
        return lo <= (int(m.group(1)), int(m.group(2))) <= hi
    core = (per or "").split(" ")[0]
    return core in (f"{basis}{y}", str(y)) or (basis == "CY" and core == f"CY{y}")


def _ym(pair):
    return f"{pair[0]}-{pair[1]:02d}"


@functools.lru_cache(maxsize=512)
def analysis_at(corpus: str, fname: str, hdr_line: int) -> dict:
    """analyze() of the table part whose header is at fname:hdr_line (cached; do not mutate)."""
    ts = file_tables(corpus, fname)
    for k, x in enumerate(ts):
        if x["hdr_line"] == hdr_line:
            return analyze(x, prev_header=ts[k - 1]["header"] if k > 0 else None)
    raise ToolError(f"no table part with its header at {fname}:{hdr_line}")


class Tables:
    """The L1 table tools over one corpus + index.  All outputs are deterministic functions of the
    arguments, the corpus and the index."""

    def __init__(self, corpus=None, index=None):
        self.corpus = str(Path(corpus).resolve()) if corpus else str(default_corpus().resolve())
        self._index = Path(index) if index else None
        self._con = None
        self._derived: dict = {}          # (file, hdr_line, what) -> sibling-derived facts (bounded, see _memo)

    def _memo(self, key, fn):
        """Small per-instance cache for sibling look-ups (anchoring, label repair): deterministic, so the
        broker's re-runs of one page cost the index once."""
        if key not in self._derived:
            if len(self._derived) > 2000:
                self._derived.clear()
            self._derived[key] = fn()
        return self._derived[key]

    @property
    def index(self) -> Path:
        if self._index is None:
            self._index = default_index()
        return self._index

    def con(self) -> sqlite3.Connection:
        if self._con is None:
            self._con = connect_ro(self.index)
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    # ---- tables
    def file_tables(self, fname) -> tuple:
        return file_tables(self.corpus, Path(str(fname).strip()).name)

    def lines(self, fname) -> tuple:
        return load_lines(self.corpus, Path(str(fname).strip()).name)

    def table_at(self, fname, line: int) -> dict:
        return table_at(self.corpus, Path(str(fname).strip()).name, int(line))

    def table_containing(self, fname, line: int):
        """The table part whose header/body contains fname:line, else None (no fallback to the next table)."""
        for t in self.file_tables(fname):
            if t["hdr_line"] <= line <= t["end_line"]:
                return t
        return None

    def analysis(self, t: dict) -> dict:
        return analysis_at(self.corpus, t["file"], t["hdr_line"])

    def parts_of(self, fname, t, stitch):
        ts = list(self.file_tables(fname))
        i = next(k for k, x in enumerate(ts) if x["hdr_line"] == t["hdr_line"])
        parts = [ts[i]]
        if stitch:
            # walk back to part 1 and forward over continuation parts of the same table
            while i > 0 and ts[i]["cont"] and ts[i - 1].get("tid") == ts[i].get("tid") and ts[i].get("tid") is not None:
                i -= 1
            parts = [ts[i]]
            k = i + 1
            while k < len(ts) and ts[k]["cont"] and ts[k].get("tid") == ts[i].get("tid") and ts[i].get("tid") is not None:
                parts.append(ts[k])
                k += 1
            if ts[i].get("tid") is None:
                k = i + 1
                while k < len(ts) and ts[k]["inherited"]:
                    parts.append(ts[k])
                    k += 1
        return parts

    def siblings(self, t, max_n=3):
        """same table (id or title) in nearby issues with the same width - for header/year anchoring."""
        try:
            con = self.con()
        except ToolError:
            return []
        y, m = map(int, t["issue"].split("_"))
        lo, hi = f"{y - 1}_{m:02d}", f"{y + 1}_{m:02d}"
        if t.get("tid"):
            cur = con.execute("SELECT file, hdr_line, issue FROM t WHERE tid=? AND ncols=? AND issue BETWEEN ? AND ? "
                              "AND issue != ? AND part=? ORDER BY issue, hdr_line",
                              (t["tid"], len(t["header"]), lo, hi, t["issue"], t["part"]))
        else:
            cur = con.execute("SELECT file, hdr_line, issue FROM t WHERE title=? AND ncols=? AND issue BETWEEN ? AND ? "
                              "AND issue != ? ORDER BY issue, hdr_line", (t.get("title"), len(t["header"]), lo, hi, t["issue"]))
        res = cur.fetchall()
        res.sort(key=lambda r: abs((int(r[2][:4]) * 12 + int(r[2][5:])) - (y * 12 + m)))
        return res[:max_n]

    def nearby_parts(self, t, years=1, same_width=True, max_n=ANCHOR_SIBLINGS) -> list:
        """[(file, hdr_line, issue)] of the same table id in issues within +-`years`, nearest first, any part."""
        if not t.get("tid"):
            return []
        try:
            con = self.con()
        except ToolError:
            return []
        y, m = map(int, t["issue"].split("_"))
        lo, hi = f"{y - years}_{m:02d}", f"{y + years}_{m:02d}"
        sql = "SELECT file, hdr_line, issue FROM t WHERE tid=? AND issue BETWEEN ? AND ? AND issue != ?"
        args = [t["tid"], lo, hi, t["issue"]]
        if same_width:
            sql += " AND ncols=?"
            args.append(len(t["header"]))
        res = con.execute(sql + " ORDER BY issue, hdr_line", args).fetchall()
        res.sort(key=lambda r: (abs((int(r[2][:4]) * 12 + int(r[2][5:])) - (y * 12 + m)), r[2], r[1]))
        return res[:max_n]

    def anchored(self, t: dict) -> dict:
        """W0-10: analysis(t) with the years of YEAR_UNKNOWN rows anchored from the nearest same-width sibling
        printing that aligns cell for cell (see _align_years); the pure analysis is returned unchanged when
        nothing is unknown.  A note says which sibling anchored, or that none aligns."""
        a = self.analysis(t)
        if not any("YEAR_UNKNOWN" in o["flags"] for o in a["rows"]):
            return a
        return self._memo((t["file"], t["hdr_line"], "anchored"), lambda: self._anchor(t, a))

    def _anchor(self, t: dict, a: dict) -> dict:
        tried = 0
        for fname, hl, _ in self.nearby_parts(t):
            try:
                sa = self.analysis(self.table_at(fname, hl))
            except ToolError:
                continue
            if any("YEAR_UNKNOWN" in o["flags"] for o in sa["rows"]) or len(sa["panels"]) != len(a["panels"]):
                continue
            tried += 1
            res = _align_years(a, sa)
            if not res:
                continue
            rows, years, agree, compared = res
            last = [o for o in rows if o["panel"] == max(years) and "anchored" in o["flags"]]
            tail = ""
            if last and not (last[-1]["period"] or "").endswith("-12"):
                tail = f" (partial, ends {last[-1]['period']})"
            desc = "; ".join(f"panel{k} = {','.join(map(str, ys))}" for k, ys in sorted(years.items()))
            note = f"YEARS ANCHORED from {fname}:{hl} ({agree}/{compared} cells agree): {desc}{tail}"
            return {**a, "rows": rows, "notes": a["notes"] + [note]}
        if tried:
            return {**a, "notes": a["notes"] + ["no sibling aligns - years unknown"]}
        return a

    def shift_sibling(self, t: dict, a: dict):
        """W0-8 rule 1: a nearby printing (any width) whose printed ordinals sit where the shifted mapping
        puts them -> 'file:line', else None.  Read from the index's stored header (the same ' | '-joined
        labels the analysis prints), so no sibling is parsed: a printing that carries the extra label puts
        every ordinal one column off and cannot confirm."""
        if not a.get("labelshift"):
            return None

        def find():
            want = {column_key(l)[0]: c for c, l in a["labelshift"].items()}
            for fname, hl, _, header in self._nearby_headers(t):
                theirs = {column_key(l)[0]: c for c, l in enumerate((header or "").split(" | ")) if column_key(l)[0]}
                if all(theirs.get(n) == c for n, c in want.items()):
                    return f"{fname}:{hl}"
            return None
        return self._memo((t["file"], t["hdr_line"], "shift_sibling"), find)

    def _nearby_headers(self, t: dict, years: int = 1) -> list:
        """[(file, hdr_line, issue, header)] of the same table id within +-`years`, nearest issue first."""
        if not t.get("tid"):
            return []
        try:
            con = self.con()
        except ToolError:
            return []
        y, m = map(int, t["issue"].split("_"))
        rows = con.execute("SELECT file, hdr_line, issue, header FROM t WHERE tid=? AND issue BETWEEN ? AND ? AND issue != ?",
                           (t["tid"], f"{y - years}_{m:02d}", f"{y + years}_{m:02d}", t["issue"])).fetchall()
        rows.sort(key=lambda r: (abs((int(r[2][:4]) * 12 + int(r[2][5:])) - (y * 12 + m)), r[2], r[1]))
        return rows

    def date_labels(self, t: dict, a: dict) -> dict:
        """W0-8 rule 2: {col: (sibling 'file:line', sibling label)} for the columns in a['date_label_fix'],
        from the nearest printing (+-3 years, any width) whose date column with the same leaf label carries
        a date word."""
        if not a.get("date_label_fix"):
            return {}

        def find():
            out = {}
            leaves = {c: norm(a["col_label"][c].split(" > ")[-1]) for c in a["date_label_fix"]}
            for fname, hl, _ in self.nearby_parts(t, years=3, same_width=False):
                try:
                    sa = self.analysis(self.table_at(fname, hl))
                except ToolError:
                    continue
                for c, leaf in leaves.items():
                    if c in out:
                        continue
                    for sc, lab in enumerate(sa["col_label"]):
                        if (sa["coltype"][sc] == "date" and norm(lab.split(" > ")[-1]) == leaf
                                and re.search(r"\b(date|dated|issued)\b", norm(lab))):
                            out[c] = (f"{fname}:{hl}", lab)
                            break
                if len(out) == len(leaves):
                    break
            return out
        return self._memo((t["file"], t["hdr_line"], "date_labels"), find)

    def row_cells(self, t: dict, line: int) -> list:
        """cell records of every non-empty cell on output rows at `line` of table part t (all panels)."""
        a = self.anchored(t)
        name = display_name(t)
        out = []
        for o in a["rows"]:
            if o["line"] != line:
                continue
            for q, c in enumerate(o["cols"]):
                if o["vals"][q] != "":
                    out.append(cell_record(a, t, o, c, o["vals"][q], name=name))
        return out

    def column_cells(self, t: dict, col: int) -> list:
        """cell records of every non-empty cell of source column `col` in table part t."""
        a = self.anchored(t)
        name = display_name(t)
        out = []
        for o in a["rows"]:
            if col in o["cols"]:
                v = o["vals"][o["cols"].index(col)]
                if v != "":
                    out.append(cell_record(a, t, o, col, v, name=name))
        return out

    # ---- find_tables
    def _t_rows(self, sql: str, args) -> list:
        """Rows of table t as dicts."""
        cur = self.con().execute(sql, args)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def _find_hits(self, phrase: str, toks: list, yr, scope: str) -> list:
        """Table parts matching every token (id lookup, FTS AND over `scope`, plus the space-squashed title
        match for OCR-split words), oldest first."""
        yq = " AND substr(t.issue,1,4) BETWEEN ? AND ?" if yr else ""
        ya = [str(yr[0]), str(yr[1])] if yr else []
        if _id_like(phrase):
            return self._t_rows("SELECT t.* FROM t WHERE t.tid = ?" + yq + " ORDER BY t.issue, t.hdr_line",
                                [canon_id(phrase)] + ya)
        hits = self._t_rows("SELECT t.* FROM f JOIN t ON t.rid = f.rowid WHERE f MATCH ?" + yq,
                            [_fts_phrase(toks, scope, prefix=True)] + ya)
        have = {h["rid"] for h in hits}
        sq = norm(phrase).replace(" ", "")
        hits += [h for h in self._t_rows("SELECT t.* FROM t WHERE t.squash LIKE ?" + yq, [f"%{sq}%"] + ya)
                 if h["rid"] not in have]
        hits.sort(key=lambda h: (h["issue"], h["hdr_line"]))
        return hits

    def _token_rids(self, toks: list, yr, fields: str) -> dict:
        """{token: set of rowids} for one FTS field group, prefix matching, inside the issue window."""
        yq = " AND substr(t.issue,1,4) BETWEEN ? AND ?" if yr else ""
        ya = [str(yr[0]), str(yr[1])] if yr else []
        out = {}
        for tk in toks:
            cur = self.con().execute("SELECT f.rowid FROM f JOIN t ON t.rid = f.rowid WHERE f MATCH ?" + yq,
                                     [_fts_phrase([tk], fields, prefix=True)] + ya)
            out[tk] = {r[0] for r in cur.fetchall()}
        return out

    def _rows_by_rid(self, rids: list, cols: str = "t.*") -> list:
        """Rows of table t for these rowids, fetched in chunks of 500 (SQLite's parameter limit)."""
        out = []
        for i in range(0, len(rids), 500):
            chunk = rids[i:i + 500]
            out += self._t_rows(f"SELECT {cols} FROM t WHERE t.rid IN ({','.join('?' * len(chunk))})", chunk)
        return out

    def _find_partial(self, toks: list, yr, title_only: bool) -> list:
        """W0-1 ranked OR fallback: EVERY table part matching some of the content tokens (a plain OR floods:
        60-86 k parts on the pilot), as light rows (LIGHT_COLS only, `header` empty) carrying 'ntok'
        (distinct tokens matched), 'th_toks' (matched in title/header) and 'rl_toks' (row labels only).
        Nothing is cut here: the groups are ranked on the whole match and `_hydrate` fetches the full rows
        of the groups that make the page, so issue spans, latest parts and match counts are exact."""
        th = self._token_rids(toks, yr, "{title header}")
        rl = self._token_rids(toks, yr, "{rowlabels}") if not title_only else {tk: set() for tk in toks}
        rids = set()
        for tk in toks:
            rids |= th[tk] | rl[tk]
        hits = self._rows_by_rid(sorted(rids), LIGHT_COLS)
        for h in hits:
            h["header"] = ""
            h["th_toks"] = [tk for tk in toks if h["rid"] in th[tk]]
            h["rl_toks"] = [tk for tk in toks if h["rid"] in rl[tk] and h["rid"] not in th[tk]]
            h["ntok"] = len(h["th_toks"]) + len(h["rl_toks"])
        return hits

    def _hydrate(self, ranked: list) -> list:
        """The ranked groups of light rows with their full index rows (match fields carried over), each
        group oldest part first."""
        carry = ("th_toks", "rl_toks", "ntok", "where", "score", "ident")
        full = {r["rid"]: r for r in self._rows_by_rid([h["rid"] for _, hs in ranked for h in hs])}
        out = []
        for key, hs in ranked:
            rows = [{**full[h["rid"]], **{k: h[k] for k in carry}} for h in hs if h["rid"] in full]
            rows.sort(key=lambda h: (h["issue"], h["hdr_line"]))
            out.append((key, rows))
        return out

    def _group_tokens(self, hs: list, toks: list, title_only: bool) -> tuple:
        """Which query tokens match a group, and where: (title/header tokens, row-label-only tokens).
        Title/header words come from the stored columns; row labels need one FTS probe per remaining token."""
        if hs and "th_toks" in hs[0]:                    # partial hits carry the answer already
            th = [tk for tk in toks if any(tk in h["th_toks"] for h in hs)]
            return th, [tk for tk in toks if tk not in th and any(tk in h["rl_toks"] for h in hs)]
        words = set()
        for h in hs:
            words.update(norm(" ".join(str(h[k] or "") for k in ("tid", "tid_raw", "title", "subtitle", "units",
                                                                  "section", "header"))).split())
        th = [tk for tk in toks if _tok_match(tk, words)]
        rl = []
        rids = [h["rid"] for h in hs][:300]
        for tk in toks:
            if tk in th or title_only or not rids:
                continue
            q = f"SELECT f.rowid FROM f WHERE f MATCH ? AND f.rowid IN ({','.join('?' * len(rids))}) LIMIT 1"
            if self.con().execute(q, [_fts_phrase([tk], "{rowlabels}", prefix=True)] + rids).fetchone():
                rl.append(tk)
        return th, rl

    @staticmethod
    def _group_hits(hits: list, phrase: str) -> "collections.OrderedDict":
        """Score every part (title > header > kind) and group the parts by table id / numbered title
        (W0-2: a leading 'Table N.' inside the title is ignored for the key; untitled twins merge)."""
        ph = norm(phrase)
        partial = bool(hits) and "th_toks" in hits[0]        # OR-fallback rows: no whole-phrase match by construction
        for h in hits:
            where_ = []
            if not partial and ph in norm(" ".join(x for x in [h["tid"] or "", h["title"] or "", h["subtitle"] or ""])):
                where_.append("title")
            if not partial and ph in norm(h["header"] or ""):
                where_.append("header")
            h["where"] = ",".join(where_) or ("id" if _id_like(phrase) else "tokens")
            h["score"] = (3 if "title" in where_ else 0) + (2 if "header" in where_ else 0) + (1 if h["kind"] == "data" else -5)
        groups = collections.OrderedDict()
        keys = {}                                              # group key per distinct (id, title, section): many parts share one
        for h in hits:
            sig = (h["tid"], h["tid_raw"], h["title"], h["subtitle"], h["section"])
            if sig not in keys:
                ttl = re.sub(CONT_RX, "", _strip_table_n(h["title"] or h["subtitle"] or ""))
                ttl = re.sub(ASOF_RX, "", ttl)
                ttl = re.sub(r"\b(preliminary|revised)\b", "", ttl, flags=re.I)
                ident = h["tid"] or (f"{h['tid_raw']} in {h['section'] or '?'}" if h["tid_raw"] else "")
                keys[sig] = (ident, norm(ttl).replace(" ", "")[:60])
            h["ident"] = keys[sig][0]
            groups.setdefault(keys[sig], []).append(h)
        for key in [k for k in groups if not k[0]]:
            twin = next((k for k in groups if k[0] and k[1] == key[1]), None)
            if twin:
                groups[twin] += groups.pop(key)
        return groups

    @staticmethod
    def _group_line(tid: str, hs: list, th: list, rl: list, partial: bool, ntoks: int) -> str:
        """W0-2 group line: id, title, issue span, row-period span, as-of span / snapshot tag, match detail."""
        issues = sorted({x["issue"] for x in hs})
        h0 = hs[0]
        ttl = re.sub(r"\s+", " ", (h0["title"] or h0["subtitle"] or "(untitled)"))[:110]
        dated = [x for x in hs if x["first_period"]]
        span = f" | rows {dated[0]['first_period']}..{dated[-1]['last_period']}"[:60] if dated else ""
        asofs = [x["asof"] for x in hs if x["asof"]]
        snap = " | [snapshot]" if asofs and not dated else ""
        asof = f" | as-of {asofs[0]}..{asofs[-1]}" if asofs and (len(set(asofs)) > 1) else (f" | as-of {asofs[0]}" if asofs else "")
        detail = "; ".join(x for x in [f"title: {', '.join(th)}" if th else "", f"rowlabels: {', '.join(rl)}" if rl else ""] if x)
        if partial:
            match = f"match:{len(th) + len(rl)} of {ntoks} tokens({detail})"
        elif h0["where"] == "tokens" and detail:
            match = f"match:tokens({detail})"
        else:                                            # whole phrase in the title/header, or an id lookup
            match = f"match:{h0['where']}"
        return (f"[{tid[:60] or '-'}] {ttl}  | {len(issues)} issues {issues[0]}..{issues[-1]}{span}{snap}{asof} | {match}"
                + (f" | kind:{h0['kind']}" if h0["kind"] != "data" else ""))

    @staticmethod
    def _part_lines(tid: str, ttl: str, hs: list, all_hits: bool) -> tuple:
        """([lines], [(offset, Cell)]) of one group's parts: the latest 3, or with --all up to ALL_PARTS_CAP."""
        show = hs[-ALL_PARTS_CAP:] if all_hits else hs[-3:]
        what = f"all {len(hs)}" if len(show) == len(hs) else f"latest {len(show)} of {len(hs)}"
        lines, cells = [f"  [{tid[:60] or '-'}] parts ({what}):"], []
        for x in show:
            per = f" rows {x['first_period']}..{x['last_period']}" if x["first_period"] else ""
            asof = f" as-of {x['asof']}" if x["asof"] else ""
            lines.append(f"    {x['file']}:{x['hdr_line']} part{x['part']}{' cont' if x['cont'] else ''}"
                         f" {x['ncols']}c x {x['nrows']}r{per}{asof}")
            cells.append((len(lines) - 1, Cell(0, f"{x['file']}:{x['hdr_line']}", lines[-1].strip()[:200], None,
                                              {"oq.kind": "table_hit", "oq.table": (tid or ttl)[:70], "oq.file": x["file"],
                                               "oq.issue": x["issue"], "oq.part": x["part"],
                                               "oq.table_ref": f"{x['file']}:{x['hdr_line']}"})))
        if len(show) < len(hs):
            lines.append(f"    ... {len(hs) - len(show)} more parts ({'add --years' if all_hits else 'use --all'})")
        return lines, cells

    def find_page(self, phrase: str, years=None, limit=25, rows=False, all_hits=False, budget=PAGE_BUDGET,
                  title_only=False) -> Page:
        """W0-1/W0-2 listing: title, header and row labels searched by default (`rows` is accepted as a no-op,
        `title_only` restores the title+header scope), prefix tokens, a ranked OR fallback on 0 hits, group
        lines first (every ranked group up to `limit`), then parts per group in rank order until the budget."""
        phrase = str(phrase or "").strip()
        yr = _years(years)
        limit = max(1, min(int(limit), 200))
        budget = max(MIN_BUDGET, min(int(budget or PAGE_BUDGET), PAGE_BUDGET))
        toks = [] if _id_like(phrase) else norm(phrase).split()
        if not toks and not _id_like(phrase):
            raise ToolError("find_tables needs a phrase or a table id such as FFO-3")
        scope = "{title header}" if title_only else "{title header rowlabels}"
        hits = self._find_hits(phrase, toks, yr, scope)
        content = [tk for tk in toks if tk not in STOPWORDS and len(tk) > 1]
        partial = False
        if not hits and content:
            hits = self._find_partial(content, yr, title_only)
            partial = bool(hits)
        groups = self._group_hits(hits, phrase)
        if partial:
            ranked = sorted(groups.items(), key=lambda kv: (-max(x["ntok"] for x in kv[1]),
                                                            -max(len(x["th_toks"]) for x in kv[1]),
                                                            -max(x["score"] for x in kv[1]), -len(kv[1])))
        else:
            ranked = sorted(groups.items(), key=lambda kv: (-max(x["score"] for x in kv[1]), -len(kv[1])))
        ranked = self._hydrate(ranked[:limit]) if partial else ranked[:limit]
        sections = self._sections(content, yr) if content else []
        head = [f"find_tables({phrase!r}, years={years}): {len(hits)} table parts in {len(groups)} groups"
                + ("" if hits else " - try fewer/other words, a table id (FFO-3), or a wider --years")]
        if partial:
            best = max(x["ntok"] for _, hs in ranked for x in hs)
            head.append(f"no table has all {len(content)} words; best partial matches (matched {best} of {len(content)}):")
        if not hits and sections:
            head.append(f"no table matched; {len(sections)} narrative sections match these words (read with sed -n)")
        head += [self._section_line(s) for s in sections[:SECTION_HITS]]
        glines, blocks = [], []
        for (tid, _), hs in ranked:
            th, rl = self._group_tokens(hs, content or toks, title_only)
            glines.append(self._group_line(tid, hs, th, rl, partial, len(content)))
            blocks.append(self._part_lines(tid, hs[0]["title"] or hs[0]["subtitle"] or "", hs, all_hits))
        if ranked and content and not any(self._group_tokens(hs, content, True)[0] for _, hs in ranked):
            head.append(f"no title match for {' '.join(content)}; row-label matches only:")
        return self._assemble_find(head, glines, blocks, len(groups), budget, phrase, years, len(hits))

    @staticmethod
    def _assemble_find(head, glines, blocks, ngroups, budget, phrase, years, nhits) -> Page:
        """Group lines first, then part blocks in rank order while they fit; one trailer says what was cut.
        The part blocks of the first RESERVED_BLOCKS groups are reserved before the group lines are laid out
        (at most half the page), so the best groups always show their file:line parts and as-of tags (the
        replay put the pilot's target at rank 2-3 whenever the top block alone was reserved)."""
        trailer = (f"... OUTPUT CUT at {budget} bytes: {len(glines)} of {ngroups} groups listed, parts shown for the first "
                   f"{len(blocks)}; narrow with --years, more words, or --limit")
        room = budget - _bytes([trailer])
        reserve = min(sum(_bytes(b[0]) for b in blocks[:RESERVED_BLOCKS]), budget // 2)
        lines = list(head)
        kept_groups = 0
        for g in glines:
            if _bytes(lines + [g]) > room - reserve:
                break
            lines.append(g)
            kept_groups += 1
        cells, shown = [], 0
        for b_lines, b_cells in blocks[:kept_groups]:
            if _bytes(lines + b_lines) > room:
                if shown:
                    break
                b_lines, b_cells = Tables._cut_block(b_lines, b_cells, room - _bytes(lines))   # the top block, in part
                if not b_lines:
                    break
            base = len(lines)
            lines += b_lines
            for off, c in b_cells:
                c.line = base + off
                cells.append(c)
            shown += 1 if len(b_lines) > 1 else 0
        if kept_groups < len(glines) or shown < len(blocks):
            lines.append(f"... OUTPUT CUT at {budget} bytes: {kept_groups} of {ngroups} groups listed, parts shown for the "
                         f"first {shown}; narrow with --years, more words, or --limit")
        return Page("find", lines, cells, {"phrase": phrase, "years": years, "hits": nhits})

    @staticmethod
    def _cut_block(b_lines: list, b_cells: list, avail: int) -> tuple:
        """The head of one part block that fits in `avail` bytes: its heading, the part lines that fit and a
        '... N more parts' line; ([], []) when not even one part line fits."""
        more = "    ... {n} more parts (narrow with --years)"
        kept = [b_lines[0]]
        parts = [x for x in b_lines[1:] if not x.lstrip().startswith("...")]
        for x in parts:
            if _bytes(kept + [x, more.format(n=len(parts))]) > avail:
                break
            kept.append(x)
        if len(kept) == 1:
            return [], []
        kept.append(more.format(n=len(parts) - (len(kept) - 1)))
        return kept, [(off, c) for off, c in b_cells if off < len(kept) - 1]

    # ---- narrative sections (W0-12; the index table `s` exists from INDEX_VERSION otables-index-v10 on)
    def _sections(self, toks: list, yr) -> list:
        """Narrative sections whose heading (or sub-headings) match >= 2 content tokens, best first (most
        tokens, then most in the heading itself, then newest issue); every FTS match is scored (the table
        holds ~28 k sections, so no LIMIT is needed).  Issues that repeat one heading (a boilerplate title
        page, a recurring section name) collapse into one entry carrying the issue span and the latest
        location.  [] on an index without the section table."""
        if len(toks) < 2:
            return []
        yq = " AND substr(issue,1,4) BETWEEN ? AND ?" if yr else ""
        ya = [str(yr[0]), str(yr[1])] if yr else []
        try:
            cur = self.con().execute("SELECT s.file, s.line, s.end_line, s.heading, s.subs, s.issue FROM fs JOIN s ON s.sid = fs.rowid "
                                     "WHERE fs MATCH ?" + yq, [_fts_phrase(toks, "{heading subs}", True, "OR")] + ya)
            rows = cur.fetchall()
        except sqlite3.OperationalError:
            return []
        by = collections.OrderedDict()
        for fname, line, end, heading, subs, issue in rows:
            hw = set(norm(heading).split())
            sw = set(norm(subs or "").split())
            n_h = sum(_tok_match(tk, hw) for tk in toks)
            n = sum(_tok_match(tk, hw | sw) for tk in toks)
            if n >= 2:                                   # one entry per heading, dates and numbers ignored
                by.setdefault(panel_stub(heading) or norm(heading), []).append((issue, line, fname, end, heading, subs, n, n_h))
        out = []
        for occ in by.values():
            occ.sort()
            best = max(x[6] for x in occ)
            issue, line, fname, end, heading, subs, n, n_h = next(x for x in reversed(occ) if x[6] == best)
            out.append({"file": fname, "line": line, "end": end, "heading": heading, "subs": subs, "n": n, "n_h": n_h,
                        "issues": sorted({x[0] for x in occ})})
        out.sort(key=lambda s: (s["n"], s["n_h"], s["issues"][-1]), reverse=True)
        return out

    @staticmethod
    def _section_line(s: dict) -> str:
        subs = [x for x in (s["subs"] or "").split(" ; ") if x][:4]
        tail = f" | sub-headings: {'; '.join(subs)}"[:150] if subs else ""
        where = f"{s['file']}:{s['line']}-{s['end']}"
        if len(s["issues"]) > 1:
            where = f"{len(s['issues'])} issues {s['issues'][0]}..{s['issues'][-1]}, latest {where}"
        return f"[section] {s['heading'][:90]} | {where}{tail}"

    def find_tables(self, *args, **kw) -> str:
        return self.find_page(*args, **kw).text

    # ---- show_table
    def _locate_lines(self, fname: str, line: int, t: dict, how: str) -> list:
        """W0-7: say how `line` resolved when it is not a table header (never snap silently)."""
        out = []
        if how == "next":
            ts = self.file_tables(fname)
            nxt = [x for x in ts if x["hdr_line"] > t["hdr_line"]][:1]
            near = f"; nearby: :{nxt[0]['hdr_line']} {(nxt[0].get('title') or '(untitled)')[:50]}" if nxt else ""
            out.append(f"  no table starts at {fname}:{line}; showing the next table at :{t['hdr_line']} "
                       f"({(t.get('title') or '(untitled)')[:60]}){near}")
            src = self.lines(fname)
            for k in range(max(1, line - 1), min(len(src), line + 2) + 1):
                if FIGURE_RX.match(src[k - 1]):
                    out.append(f"  figure caption at :{k}; plotted values are not in the text corpus")
                    break
        elif how == "inside" and line > t["hdr_line"] + 1:
            out.append(f"  line {line} is row {line - t['hdr_line'] - 1} of TABLE {display_name(t)} starting at {t['hdr_line']}")
        return out

    def _continuation_line(self, fname: str, t: dict, parts: list) -> list:
        """W0-7: an unstitched part with continuation parts after it says where the table goes on."""
        if len(parts) > 1:
            return []
        allp = self.parts_of(fname, t, True)
        i = next((k for k, x in enumerate(allp) if x["hdr_line"] == t["hdr_line"]), None)
        if i is None or i + 1 >= len(allp):
            return []
        return [f"  this part has {len(t['rows'])} rows; the table continues in part {allp[i + 1]['part']} at "
                f":{allp[i + 1]['hdr_line']} (use --stitch)"]

    def _page_notes(self, head: dict, analyses: list, siblings: bool) -> list:
        """NOTES of every part (W0-8 rule 1's sibling confirmation appended), SIBLINGS when something needs
        verifying, panel and stitching remarks."""
        notes = []
        a0 = analyses[0]
        sib_ok = self.shift_sibling(head, a0) if a0.get("labelshift") else None
        for pi, a in enumerate(analyses):
            pre = f"part{pi + 1}: " if len(analyses) > 1 else ""
            for n_ in a["notes"]:
                if pi == 0 and sib_ok and n_.startswith("header realigned:"):
                    n_ = n_ + f"; sibling {sib_ok} agrees"
                notes.append(pre + n_)
            if len(a["panels"]) > 1 and a["panels"][0]["kind"] == "side-by-side":
                notes.append(pre + f"{len(a['panels'])} side-by-side panels (period columns "
                             + ", ".join(f"c{pn['pcol']}" for pn in a["panels"] if pn["pcol"] is not None)
                             + ") unstacked: all rows of panel 1, then panel 2, ...")
        if len(analyses) > 1:
            base = [o["period"] or o["label"] for o in a0["rows"]]
            for pi, a in enumerate(analyses[1:], 2):
                other = [o["period"] or o["label"] for o in a["rows"]]
                if other != base:
                    notes.append(f"part{pi}: rows differ from part 1 ({len(other)} vs {len(base)}) - stitched by position, VERIFY")
        if siblings and any(("VERIFY" in n_ or "no year" in n_ or "shown raw" in n_) for n_ in notes):
            sib = self.siblings(head)
            if sib:
                notes.append("SIBLINGS (same table, same width, nearby issues; use them to anchor years/headers): "
                             + "; ".join(f"{f}:{ln}" for f, ln, _ in sib))
        return notes

    @staticmethod
    def _grid(raw_entries, slots, grid_head) -> tuple:
        """Grid rows of the ROWS view: (entries, rows_head) as _paginate wants them."""
        grid = [grid_head] + [r for sep, row, _, _ in raw_entries for r in ([sep] if sep else []) + [row]]
        ncol = len(grid_head)
        widths = []
        for c in range(ncol):
            vals = [str(r[c]) for r in grid]
            if c <= 3:
                widths.append(min(max(len(v) for v in vals), 44))
            elif c == ncol - 1:
                widths.append(0)
            else:
                numeric = [len(v) for v in vals[1:] if v and cell_type(v) in ("num", "pct", "period", "date")]
                text_ = [len(v) for v in vals[1:] if v and cell_type(v) not in ("num", "pct", "period", "date")]
                widths.append(max([len(vals[0])] + numeric + [min(max(text_ or [0]), 14)]))

        def fmt(r):
            out_, spans = [], []
            for c, v in enumerate(r):
                v = str(v)
                w = widths[c]
                if c == ncol - 1:
                    s = v
                elif len(v) > w:
                    s = (v[:max(1, w - 1)] + "…") if c > 3 else v[:w]
                else:
                    s = v.ljust(w) if c <= 3 else v.rjust(w)
                out_.append(s)
                spans.append(s)
            return "  ".join(out_).rstrip(), spans
        entries = []
        for sep, row, rcells, o in raw_entries:
            e_lines = []
            if sep:
                e_lines.append(fmt(sep)[0])
            text_, spans = fmt(row)
            e_lines.append(text_)
            e_cells = []
            for si, rec, src in rcells:
                shown = spans[4 + si].strip()
                e_cells.append((len(e_lines) - 1, Cell(0, rec["locator"], shown[:200], rec["value"], rec["attrs"])))
            entries.append((e_lines, e_cells))
        return entries, ["ROWS:", fmt(grid_head)[0]]

    def show_page(self, fname, line, cols=None, grep=None, stitch=False, raw=False, max_rows=400,
                  siblings=True, long=False, match=None, from_row=0, budget=PAGE_BUDGET, period=None) -> Page:
        """One page of a table.  `grep` is a substring or a list of substrings (OR; W0-6), `period` a fiscal
        or calendar year window; the analysis is the W0-10 anchored one; NOTES are printed in --raw too (W0-9)."""
        fname = Path(str(fname).strip()).name
        from_row = max(0, int(from_row or 0))
        max_rows = max(1, min(int(max_rows or 400), 2000))
        budget = max(MIN_BUDGET, min(int(budget or PAGE_BUDGET), PAGE_BUDGET))
        t, how = table_at_how(self.corpus, fname, int(line))
        parts = self.parts_of(fname, t, stitch)
        head = parts[0]
        name = display_name(head)
        units = head.get("units") or ""
        ctx = [f"  {(head.get('title') or '(untitled)')[:160]} | issue {head['issue']} | lines "
               f"{parts[0]['hdr_line']}-{parts[-1]['end_line']}"
               + (" | continuation part of a multi-part table" if head["cont"] or head["part"] > 1 else "")
               + (f" | +{len(parts) - 1} continuation parts stitched" if len(parts) > 1 else "")]
        for key, lab in (("subtitle", "subtitle"), ("units", "units"), ("asof", "as-of"), ("section", "section")):
            v = head.get(key)
            if v:
                ctx.append(f"  {lab}: " + (" / ".join(v) if isinstance(v, list) else v)[:300])
        ctx += self._locate_lines(fname, int(line), t, how) + self._continuation_line(fname, t, parts)
        meta = {"file": fname, "hdr_line": head["hdr_line"], "part": head["part"], "table": name}

        def header_fn(a, b, total_):
            return f"TABLE {name} | {fname}:{head['hdr_line']} | part {head['part']} | page rows {a}-{b} of {total_}"
        terms = grep_terms(grep)
        window = period_window(period) if period else None
        analyses = [self.anchored(p) for p in parts]
        a0 = analyses[0]
        notes = self._page_notes(head, analyses, siblings)
        if notes and from_row == 0:
            note_lines = ["NOTES:"] + ["  - " + x for x in notes]
        elif notes:
            note_lines = [f"NOTES: {len(notes)} (shown on the first page, --from-row 0)"]
        else:
            note_lines = []
        if raw:
            return self._raw_page(fname, parts, terms, from_row, max_rows, budget, ctx, note_lines, header_fn, meta)
        keep = _col_filter(cols, a0["ncols"])
        mt = [norm(x) for x in str(match).split(",") if norm(x)] if match else None

        def wanted(a, c):
            if keep is not None and c not in keep:
                return False
            if mt is not None and not any(m in norm(a["col_label"][c]) for m in mt):
                return False
            return True
        lays = a0.get("layouts") or [{"panels": a0["panels"]}]
        nslot = max([len(o["cols"]) for o in a0["rows"]] + [1])
        slots = [(0, q) for q in range(nslot)
                 if any(q < len(pn["vcols"]) and wanted(a0, pn["vcols"][q]) for lay in lays for pn in lay["panels"])]
        legend = []
        for pi, a in enumerate(analyses[1:], 1):
            if len(a["panels"]) > 1:
                legend.append(f"  (part {pi + 1} has several panels; only its first panel is stitched)")
            slots += [(pi, c) for c in a["panels"][0]["vcols"] if wanted(a, c)]
        pc0 = a0["panels"][0]["pcol"]
        stub = pc0 if pc0 is not None else 0
        legend.append(f"  c{stub:<3} {a0['col_label'][stub][:90] or '(stub)'} "
                      f"[{'period' if stub in a0['pcols'] else a0['coltype'][stub]}] -> shown as 'period' (filled) and 'label' (raw)")
        layouts = []
        for lay in a0.get("layouts") or [{"section": None, "panels": a0["panels"], "labels": {}}]:
            sig = (tuple(tuple(pn["vcols"]) for pn in lay["panels"]), tuple(sorted(lay["labels"].items())))
            if layouts and layouts[-1][0] == sig:
                layouts[-1][1].append(lay["section"])
            else:
                layouts.append((sig, [lay["section"]], lay))
        shifted = a0.get("labelshift") or {}
        repaired = self.date_labels(head, a0)
        for li, (sig, secs, lay) in enumerate(layouts):
            if len(layouts) > 1:
                legend.append(f"  -- layout {li + 1} (sections: {'; '.join((x or '-')[:40] for x in secs)[:160]})")
            for pi, q in [x for x in slots if x[0] == 0]:
                desc = []
                for k, pn in enumerate(lay["panels"]):
                    if q < len(pn["vcols"]):
                        c = pn["vcols"][q]
                        per = a0["colper"].get(c)
                        pers = f" <{per[0]}{'~' if per[1] else ''}>" if per else ""
                        lab = lay["labels"].get(c, a0["col_label"][c])
                        extra = f"  LABELSHIFT-1 -> {shifted[c][:60]}" if c in shifted and li == 0 else ""
                        if c in repaired and li == 0:
                            extra += f" (label repaired from sibling {repaired[c][0]}: {repaired[c][1][:50]})"
                        desc.append((f"p{k + 1}:" if len(lay["panels"]) > 1 else "")
                                    + f"c{c} {lab[:70] or '(no label)'}{pers} [{a0['coltype'][c]}]{extra}")
                if desc:
                    legend.append(f"  v{q + 1:<3} = " + " | ".join(desc))
        for pi, q in slots:
            if pi:
                a = analyses[pi]
                legend.append(f"  c{pi + 1}.{q:<3} {a['col_label'][q][:90] or '(no label)'} [{a['coltype'][q]}]  (part {pi + 1})")
        # ---- rows
        body = a0["rows"]
        if terms:
            body = [o for o in body if row_matches(o, terms)]
        if window:
            body = [o for o in body if in_period_window(o["period"] or "", window)]
            edge = {_ym((window[2][0], window[2][1] - 1) if window[2][1] > 1 else (window[2][0] - 1, 12)),
                    _ym((window[3][0], window[3][1] + 1) if window[3][1] < 12 else (window[3][0] + 1, 1))}
            excl = sorted({(o["period"] or "").split(" ")[0] for o in a0["rows"]} & edge)
            ctx.append(f"  {window[0]}{window[1]} = {_ym(window[2])}..{_ym(window[3])}: {len(body)} rows kept"
                       + (f" (excluded: {', '.join(excl)})" if excl else ""))
        total = len(body)
        if not total and (terms or window):
            ctx.append(f"  0 rows: {grep_hint(a0)}" if terms else "  0 rows fall in --period " + str(period))
            for g in terms:
                hit = next((c for c in range(1, a0["ncols"]) if norm(g) and norm(g) in norm(a0["col_label"][c])), None)
                if hit is not None:
                    ctx.append(f"  '{g}' matches column c{hit} {a0['col_label'][hit][:60]} - use --match {g!r}")
                    break
        if total and from_row >= total:
            ctx.append(f"  --from-row {from_row} is past the last filtered row ({total}); rows are 0-based")
        pos = {id(o): i for i, o in enumerate(a0["rows"])}
        names = [display_name(p) for p in parts]
        grid_head = ["line", "pnl", "period", "label"] + [f"v{q + 1}" if pi == 0 else f"c{pi + 1}.{q}" for pi, q in slots] + ["flags"]
        raw_entries = []                       # (sep_row | None, row, [(slot index, cell dict)], [long lines])
        last_sec = object()
        for o in body[from_row:from_row + max_rows]:
            sep = None
            if o["section"] != last_sec:
                sep = ["", "", "==", (o["section"] or "")[:44]] + [""] * (len(slots) + 1)
                last_sec = o["section"]
            row = [str(o["line"]), str(o["panel"]), o["period"] or "", (o["label"] or "")[:44]]
            flags = list(o["flags"])
            i = pos[id(o)]
            rcells = []
            for si, (pi, q) in enumerate(slots):
                v, src, oo = "", None, o
                if pi == 0:
                    if q < len(o["cols"]):
                        v, src = o["vals"][q], (0, o["cols"][q])
                else:
                    rows_p = analyses[pi]["rows"]
                    if i < len(rows_p) and q in rows_p[i]["cols"]:
                        oo = rows_p[i]
                        v, src = oo["vals"][oo["cols"].index(q)], (pi, q)
                row.append(v)
                if v != "" and src is not None:
                    rec = cell_record(analyses[src[0]], parts[src[0]], oo, src[1], v, units=units, name=names[src[0]])
                    rcells.append((si, rec, src))
            for pi in range(1, len(analyses)):
                if i < len(analyses[pi]["rows"]):
                    flags += [f"p{pi + 1}:{f}" for f in analyses[pi]["rows"][i]["flags"]]
            row.append(" ".join(flags))
            raw_entries.append((sep, row, rcells, o))
        cont_hint = ", or narrow with --cols / --match / --grep"
        if long:
            entries = []
            for sep, row, rcells, o in raw_entries:
                e_lines, e_cells = [], []
                for si, rec, src in rcells:
                    at = rec["attrs"]
                    per = at["oq.col_period"]
                    val, fl = parse_cell(rec["text"])
                    fl = [f for f in fl if f != "float0"]
                    e_lines.append(f"  {o['line']} p{o['panel']} {o['period'] or '-'} {(o['label'] or '')[:40]!r} | "
                                   f"c{src[0] + 1}.{src[1]} {at['oq.column'][:70]!r}{' <' + per + '>' if per else ''} = {rec['text']}"
                                   f"{' [' + ','.join(fl) + ']' if fl else ''}")
                    e_cells.append((len(e_lines) - 1, Cell(0, rec["locator"], rec["text"][:200], rec["value"], rec["attrs"])))
                if not e_lines:
                    e_lines.append(f"  {o['line']} p{o['panel']} {o['period'] or '-'} {(o['label'] or '')[:40]!r} | (no cells in the selected columns)")
                entries.append((e_lines, e_cells))
            rows_head = ["CELLS (line panel period 'row label' | c<part>.<col> 'column label' <column period> = raw [parsed flags]):"]
            grid_entries, _ = self._grid(raw_entries, slots, grid_head)
            if _bytes([x for e in grid_entries for x in e[0]]) <= budget // 2:
                cont_hint = f"; these {total} rows fit one page without --long, or with --cols" + cont_hint
        else:
            entries, rows_head = self._grid(raw_entries, slots, grid_head)
        flexible = legend + note_lines
        first = _bytes(entries[0][0]) if entries else 0
        reserve = _bytes([header_fn(total, total, total), FOOTER, "x" * 200])
        pre = _fit_lines(ctx + ["COLUMNS:"], flexible, rows_head, budget - reserve - first, "legend/NOTES")
        lines, cells, (a, b) = _paginate(lambda a, b: header_fn(a, b, total), pre, entries, total, from_row, budget,
                                         cont_hint, FOOTER)
        for c in cells:
            c.attrs = {**c.attrs, "oq.page": f"{a}-{b}/{total}"}
        return Page("show", lines, cells, {**meta, "rows": total, "page": (a, b), "raw": False})

    def _raw_page(self, fname, parts, terms, from_row, max_rows, budget, ctx, note_lines, header_fn, meta) -> Page:
        """--raw: the original lines of the part(s), NOTES above them (W0-9), --grep as a substring of the line."""
        lines_ = self.lines(fname)
        body = []
        for p in parts:
            for k in range(p["hdr_line"], p["end_line"] + 1):
                if not terms or any(norm(g) and norm(g) in norm(lines_[k - 1]) for g in terms):
                    body.append((k, p))
        total = len(body)
        entries = []
        for k, p in body[from_row:from_row + max_rows]:
            txt = lines_[k - 1]
            cell = Cell(0, f"{fname}:{k}", txt[:200], None,
                        {"values": numbers(txt), "oq.table": display_name(p), "oq.table_ref": f"{fname}:{p['hdr_line']}",
                         "oq.file": fname, "oq.issue": p["issue"], "oq.raw": True})
            entries.append(([f"{k:>6}: {txt}"], [(0, cell)]))
        first = _bytes(entries[0][0]) if entries else 0
        reserve = _bytes([header_fn(total, total, total), "x" * 200])
        pre = _fit_lines(ctx, note_lines, ["RAW LINES:"], budget - reserve - first, "NOTES")
        lines, cells, (a, b) = _paginate(lambda a, b: header_fn(a, b, total), pre, entries, total, from_row, budget, "", "")
        for c in cells:
            c.attrs["oq.page"] = f"{a}-{b}/{total}"
        return Page("show", lines, cells, {**meta, "rows": total, "page": (a, b), "raw": True})

    def show_table(self, *args, **kw) -> str:
        return self.show_page(*args, **kw).text

    # ---- printings
    def _printing_parts(self, table_hint, yr, newest_first=False):
        con = self.con()
        if _id_like(table_hint):
            sql, args = "SELECT file, hdr_line, issue, part FROM t WHERE tid=?", [canon_id(table_hint)]
        else:
            toks = norm(table_hint).split()
            if not toks:
                raise ToolError("printings needs --table (an id such as FFO-3 or title words)")
            sql = "SELECT t.file, t.hdr_line, t.issue, t.part FROM f JOIN t ON t.rid=f.rowid WHERE f MATCH ?"
            args = [_fts_phrase(toks, "title")]
        if yr:
            sql += " AND substr(issue,1,4) BETWEEN ? AND ?"
            args += [str(yr[0]), str(yr[1])]
        sql += " ORDER BY issue DESC, hdr_line" if newest_first else " ORDER BY issue, hdr_line"
        return con.execute(sql, args).fetchall()

    def printings_data(self, row_label: str, table_hint: str, period=None, column=None, years=None,
                       max_issues=400, section=None, all_periods=False) -> dict:
        """Every printed cell of one row (period or label) and optional column substring across the issues
        that print table_hint (prototype semantics; window: period year .. +3 (month) / +12 (year); an explicit
        window starts at most PRINTINGS_EARLY_YEARS before a period row; at most PRINTINGS_SCAN_CAP parts, oldest
        first, are analysed and the page says so when the cap cuts).  W0: a month row matches only the month
        row unless `all_periods`; `section` keeps one panel; labels match with intra-word spaces deleted;
        'rows_matched' / 'diag' feed the no-match diagnostics of printings_page."""
        row_label = str(row_label or "").strip()
        table_hint = str(table_hint or "").strip()
        if not row_label or not table_hint:
            raise ToolError("printings needs --row (a period such as 2003-01 / FY2003, or a label) and --table")
        yr = _years(years)
        qp0 = parse_query_period(period or row_label) if (period or parse_query_period(row_label)) else None
        explicit = bool(yr)
        if not yr and qp0:
            yr = (qp0[0], qp0[0] + (3 if qp0[1] else 12))
        elif yr and qp0 and yr[0] < qp0[0] - PRINTINGS_EARLY_YEARS:
            # an issue cannot print a period that lies years after it (estimates aside): start the window
            # PRINTINGS_EARLY_YEARS before the period instead of scanning decades of irrelevant parts
            yr = (qp0[0] - PRINTINGS_EARLY_YEARS, max(yr[1], qp0[0] - PRINTINGS_EARLY_YEARS))
        hits = self._printing_parts(table_hint, yr)
        max_parts = max(1, min(int(max_issues) * 4, PRINTINGS_SCAN_CAP))
        qp = parse_query_period(row_label)
        if period:
            qp = parse_query_period(period) or qp
        want_label = None if (qp and parse_query_period(row_label)) else norm(row_label)
        want_b = re.match(r"^\s*(fy|cy)", (period or row_label), re.I)
        qday = query_day(period if period and parse_query_period(period) else row_label) if qp else None
        col_hint = norm(column) if column else None
        col_num = re.fullmatch(r"\((\d{1,2})\)", str(column or "").strip())   # '(8)': the printed column number
        sec_hint = norm(section) if section else None
        found, rows_matched, hidden, days = [], 0, 0, set()
        month = f"{qp[0]}-{qp[1]:02d}" if qp and qp[1] else ""
        diag = {"explicit_years": explicit, "periods": [], "first": None, "labels": None, "columns": None}
        for fname, hl, issue, part in hits[:max_parts]:
            t = self.table_at(fname, hl)
            a = self.anchored(t)
            name = display_name(t)
            if diag["first"] is None:
                diag["first"] = (f"{fname}:{hl}", a)
            for o in a["rows"]:
                per = o["period"] or ""
                if re.match(r"^(FY|CY)?\d{4}", per):
                    diag["periods"].append((per.split(" ")[0], issue))
                ok = True
                if qp:
                    if qp[1] is None:
                        core = re.sub(r"^(FY|CY)", "", per.split(" ")[0])
                        ok = core == str(qp[0]) and (not want_b or per.upper().startswith(want_b.group(1).upper()))
                    elif qday:                                   # a day-level --row matches that day's row only
                        ok = per.split(" ")[0] == f"{month}-{qday:02d}"
                    elif all_periods:
                        ok = per.startswith(month)
                    else:
                        ok = per.split(" ")[0] == month
                    if not ok and not all_periods and per.startswith(month + "-"):
                        hidden += 1                              # a daily/weekly row of the month, not listed
                        days.add(per.split(" ")[0])
                if want_label:
                    ok = ok and _label_has(o["label"], want_label) or (ok and want_label == norm(o["section"] or ""))
                if sec_hint:
                    ok = ok and _label_has(o["section"], sec_hint)
                if not ok:
                    continue
                rows_matched += 1
                if diag["columns"] is None:
                    diag["columns"] = (f"{fname}:{o['line']}", [(c, row_label_for(a, o, c)) for c in o["cols"]])
                for q, c in enumerate(o["cols"]):
                    lab = row_label_for(a, o, c)
                    if col_num and column_key(lab)[0] != col_num.group(1):
                        continue
                    if col_hint and not col_num and not _label_has(lab, col_hint):
                        continue
                    v = o["vals"][q]
                    if v == "" and col_hint is None:
                        continue
                    rec = cell_record(a, t, o, c, v, name=name)
                    found.append({"issue": issue, "file": fname, "line": o["line"], "part": part, "period": o["period"],
                                  "label": o["label"], "col": c, "col_label": lab, "raw": v, "rec": rec,
                                  "section": o["section"] or "", "asof": t.get("asof") or "",
                                  "snapshot": bool(t.get("asof")) and not a["pcols"]})
        return {"hits": len(hits), "scanned": min(len(hits), max_parts), "window": yr, "found": found,
                "rows_matched": rows_matched, "hidden": hidden, "month": month, "days": sorted(days, key=_period_sort),
                "day": f"{month}-{qday:02d}" if (qday and month) else "", "diag": diag}

    # ---- no-match diagnostics (W0-5)
    @staticmethod
    def _hint_where(table_hint: str) -> tuple:
        """(SQL condition on table t, args) selecting the parts --table names: the id, or title words (FTS)."""
        if _id_like(table_hint):
            return "tid=?", [canon_id(table_hint)]
        toks = norm(table_hint).split() or ["zzzz"]
        return "rid IN (SELECT rowid FROM f WHERE f MATCH ?)", [_fts_phrase(toks, "title")]

    def _table_span(self, table_hint: str):
        """(first issue, last issue, parts, 'file:line' of the last part, earliest period, latest period) of
        the table over the whole index (id or title words), or None when nothing matches --table."""
        cond, args = self._hint_where(table_hint)
        row = self.con().execute(f"SELECT min(issue), max(issue), count(*) FROM t WHERE {cond}", args).fetchone()
        if not row or not row[2]:
            return None
        first, last, n = row
        parts = self.con().execute(f"SELECT file, hdr_line FROM t WHERE {cond} AND issue IN (?, ?) ORDER BY issue, hdr_line",
                                   args + [first, last]).fetchall()
        pers = []
        for fname, hl in (parts[:4] + parts[-4:]):           # every part of the first and of the last issue
            a = self.analysis(self.table_at(fname, hl))
            pers += [o["period"].split(" ")[0] for o in a["rows"] if re.match(r"^(FY|CY)?\d{4}", o["period"] or "")]
        keyed = sorted(pers, key=_period_sort)
        return first, last, n, f"{parts[-1][0]}:{parts[-1][1]}", (keyed[0] if keyed else "?"), (keyed[-1] if keyed else "?")

    def _discontinued_note(self, table_hint: str, last_issue: str):
        """The sentence of the issue after `last_issue` that says 'discontinu' within 50 lines of a mention of
        the id (a sentence naming the id itself wins), as 'file:line: sentence', or None."""
        nxt = self.con().execute("SELECT DISTINCT file FROM t WHERE issue > ? ORDER BY issue LIMIT 1", (last_issue,)).fetchone()
        if not nxt:
            return None
        tid = canon_id(table_hint)
        src = self.lines(nxt[0])
        at = [i for i, s in enumerate(src) if tid in s.upper()]
        best = None
        for i, s in enumerate(src):
            if "discontinu" not in s.lower() or not at:
                continue
            dist = min(abs(i - k) for k in at)
            if dist > 50:
                continue
            sent = next((x.strip() for x in re.split(r"(?<=[.;])\s+", s) if "discontinu" in x.lower() and tid in x.upper()),
                        next((x.strip() for x in re.split(r"(?<=[.;])\s+", s) if "discontinu" in x.lower()), s.strip()))
            rank = (tid not in sent.upper(), dist)
            if best is None or rank < best[0]:
                best = (rank, f"{nxt[0]}:{i + 1}: {sent[:240]}")
        return best[1] if best else None

    def _no_match_lines(self, data: dict, row_label: str, table_hint: str, period, column, section) -> list:
        """W0-5: why nothing matched, from what the index and the scanned parts already know."""
        out = ["  no matching row/column found (check the row label/period and --col; try find_tables first)"]
        yr, diag = data["window"], data["diag"]
        span = self._table_span(table_hint)
        tid = canon_id(table_hint) if _id_like(table_hint) else repr(table_hint)
        if not data["hits"]:
            if span is None:
                out.append(f"  no table matches --table {table_hint!r}: try find_tables with " +
                           ("title words or check the id" if _id_like(table_hint) else "fewer or other title words"))
                return out
            first, last, n, loc, p0, p1 = span
            out.append(f"  {tid} is printed in issues {first}..{last} ({n} parts); no issue in {yr[0]}..{yr[1]} prints it")
            if yr and str(yr[0]) > last[:4]:
                out.append(f"  {tid} prints periods {p0} .. {p1}; the last issue printing it is {last} ({loc}); "
                           f"later issues do not print {tid}")
                if diag["explicit_years"]:
                    out.append(f"  rows of {tid} were last printed in {last}; --years {yr[0]}-{yr[1]} starts after that")
                note = self._discontinued_note(table_hint, last) if _id_like(table_hint) else None
                if note:
                    out.append(f"  note in {note}")
            elif yr and str(yr[1]) < first[:4]:
                out.append(f"  the first issue printing {tid} is {first}; {yr[0]}..{yr[1]} lies before it")
            return out
        if not data["rows_matched"]:
            pers = _period_range(diag["periods"])
            if data.get("hidden") and data.get("day"):
                out.append(f"  no row is dated {data['day']}; the daily/weekly rows of {data['month']} are "
                           f"{', '.join(data['days'][:8])}{', ...' if len(data['days']) > 8 else ''} "
                           f"(--row one of them, or --row {data['month']} --all-periods for all)")
            elif data.get("hidden"):
                out.append(f"  only daily/weekly rows exist for that month ({data['hidden']} rows): use --all-periods, "
                           "or --row YYYY-MM-DD for one day")
            elif pers and (period or parse_query_period(row_label)):
                out.append(f"  the scanned parts print periods {pers[0]} .. {pers[-1]}; --row {row_label!r}"
                           f"{' --period ' + repr(period) if period else ''} is not among them")
            loc, a = diag["first"] or ("?", None)
            if a is not None:
                if not a["pcols"] and len(a["colper"]) >= 2:
                    cp = next(iter(a["colper"].values()))[0]
                    out.append(f"  this table has periods as columns: use --col \"{cp}\" --row <label>")
                dcols = [c for c in range(1, a["ncols"]) if a["coltype"][c] == "date"]
                if not a["pcols"] and dcols:
                    out.append(f"  rows of {tid} are keyed by c{dcols[0]} {a['col_label'][dcols[0]][:40]}; pass that date as --row")
                labs = [o["label"] for o in a["rows"] if o["label"]][:6]
                if labs and not parse_query_period(row_label):
                    out.append(f"  no row label of {loc} contains {row_label!r}; labels there start: " + " | ".join(x[:30] for x in labs))
                if section:
                    out.append(f"  --section {section!r} kept no rows; sections of {loc}: "
                               + " | ".join(sorted({(o['section'] or '-')[:40] for o in a['rows']})[:6]))
            return out
        loc, cols = diag["columns"] or ("?", [])
        shown = " | ".join(f"c{c} {re.sub(r'(\s+\d{1,2}/?)+$', '', lab)[:40]}" for c, lab in cols)[:700]
        out.append(f"  no column matches {column!r}; columns of {loc} are: {shown}")
        return out

    # ---- SUMMARY (W0-3, W0-4, W0-11)
    @staticmethod
    def _col_key(f: dict) -> str:
        """SUMMARY column key: the printed '(n)' number when there is one, else the squashed label."""
        m = re.search(r"\((\d{1,2})\)\s*$", f["col_label"])
        return f"col ({m.group(1)})" if m else norm(f["col_label"]).replace(" ", "")[:60]

    @staticmethod
    def _panel_keys(found: list) -> tuple:
        """({id(cell): panel key}, {panel key: [raw banners merged into it]}) for the SUMMARY.  Within one
        (part, column, period, as-of) series the banner stubs are merged when no issue prints two of them
        (the banner then varies between printings - an OCR variant such as 'series'/'aeries', or a banner
        missing in some issues - and is not a panel); on a real multi-panel table only near-identical stubs
        merge (same_panel).  The most frequent stub names the merged panel."""
        series = collections.defaultdict(list)
        for f in found:
            series[(f["part"], Tables._col_key(f), f["period"], f["asof"] if f["snapshot"] else "")].append(f)
        keys, merged = {}, collections.defaultdict(set)
        for xs in series.values():
            stubs = collections.Counter(panel_stub(f["section"]) for f in xs)
            per_issue = collections.defaultdict(set)
            for f in xs:
                per_issue[f["issue"]].add(panel_stub(f["section"]))
            canon = {}
            if all(len(s) <= 1 for s in per_issue.values()):
                top = stubs.most_common(1)[0][0]
                canon = {s: top for s in stubs}
            else:
                for s, _ in stubs.most_common():
                    canon[s] = next((c for c in dict.fromkeys(canon.values()) if same_panel(c, s)), s)
            for f in xs:
                keys[id(f)] = canon[panel_stub(f["section"])]
                if len(set(canon.values())) < len(stubs):           # some stubs of this series were merged
                    merged[keys[id(f)]].add(f["section"])
        return keys, merged

    def _summary_lines(self, found: list, add_cell) -> list:
        """SUMMARY keyed by (part, panel stub, column, period, as-of for snapshot tables); exact parsed values;
        column-index warning and ISOLATED suppressed for minority indexes; 'also printed under panel'."""
        keys, merged = self._panel_keys(found)
        bycol = collections.OrderedDict()
        for f in found:
            bycol.setdefault((f["part"], keys[id(f)], self._col_key(f), f["period"], f["asof"] if f["snapshot"] else ""), []).append(f)
        lines = ["SUMMARY (per column & period: value -> issues; first and latest printing):"]
        if any(f["snapshot"] for f in found):
            lines.append("  snapshot table (as-of in title, no period column): one printing per as-of date; keyed by as-of")
        for (part, stub, ckey, per, asof), xs in bycol.items():
            idx = collections.Counter(f["col"] for f in xs)
            major = idx.most_common(1)[0][0]
            if len(idx) > 1:
                lines.append(f"  column index differs across issues ({'; '.join(f'c{c}: {n} issues' for c, n in idx.most_common())}) "
                             f"for {ckey} [{per or '-'}]: the minority may be a mis-parsed header; verify with show_table --long")
            clab = f"part{part}{' panel ' + repr(xs[-1]['section'][:40]) if stub else ''}{' as-of ' + asof if asof else ''} " \
                   f"{ckey} ({xs[-1]['col_label'][:40]})"
            vals = collections.OrderedDict()
            for f in xs:
                vals.setdefault(exact_value(f["raw"]), []).append(f)
            tag = "REVISED/DIFFERS across printings" if len(vals) > 1 else "same in all printings"
            seq = [(exact_value(f["raw"]), f["issue"], f"{f['file']}:{f['line']}", f["col"]) for f in xs]
            iso = [seq[i] for i in range(1, len(seq) - 1)
                   if seq[i][0] != seq[i - 1][0] and seq[i - 1][0] == seq[i + 1][0] and seq[i][1] not in (seq[i - 1][1], seq[i + 1][1])
                   and seq[i][3] == major]
            lines.append(f"  [{per or '-'}] {clab[:110]}: {len(xs)} printings, {len(vals)} distinct values - {tag}")
            banners = sorted({x["section"] for x in xs}, key=lambda s: (-sum(x["section"] == s for x in xs), s))
            if stub in merged and len(banners) > 1:
                lines.append("      panel banner varies across printings (" + "; ".join(f"{b[:30]!r}" for b in banners[:3])
                             + (f"; +{len(banners) - 3} more" if len(banners) > 3 else "") + "): one panel per issue, merged")
            others = sorted({(k[1], x["section"]) for k, v in bycol.items() for x in v[:1]
                             if k[2] == ckey and k[3] == per and k[1] != stub and k[0] == part})
            for ostub, sec in others[:3]:
                n = len({x["issue"] for k, v in bycol.items() if k[2] == ckey and k[3] == per and k[1] == ostub for x in v})
                lines.append(f"      also printed under panel {sec[:40]!r} in {n} issues (not merged)")
            if len(others) > 3:
                lines.append(f"      ... and under {len(others) - 3} more panels (not merged; --section keeps one)")
            for vkey, fs in vals.items():
                loc = f" {fs[0]['file']}:{fs[0]['line']}" if len(xs) == 1 else ""      # one printing: locator here
                lines.append(f"      {fs[0]['raw']}: {fs[0]['issue']}..{fs[-1]['issue']} ({len(fs)}){loc}")
                add_cell(fs[-1], {"oq.summary": True}, len(lines) - 1)
            for v_, issue, loc, _ in iso:
                lines.append(f"      ISOLATED: {issue} ({loc}) disagrees with the printings before and after it, which agree "
                             f"- likely OCR/row shift in that issue, not a revision")
            if len(xs) > 1:
                lines.append(f"      first printing {xs[0]['issue']} ({xs[0]['file']}:{xs[0]['line']}), latest printing "
                             f"{xs[-1]['issue']} ({xs[-1]['file']}:{xs[-1]['line']})")
        return lines

    def printings_page(self, row_label: str, table_hint: str, period=None, column=None, years=None,
                       max_issues=400, budget=PAGE_BUDGET, section=None, all_periods=False) -> Page:
        """First line (frozen format), scan-cap line, SUMMARY first (W0-11: its bytes are reserved), then the
        listing cut to what fits ('... N more listing lines omitted; SUMMARY complete')."""
        data = self.printings_data(row_label, table_hint, period, column, years, max_issues, section, all_periods)
        yr, found = data["window"], data["found"]
        key = f"{table_hint}|{period or row_label}|{column or '*'}"
        lines = [f"printings(row={row_label!r}, table={table_hint!r}, period={period!r}, col={column!r}): "
                 f"{data['scanned']} table parts searched in issues {yr[0] if yr else 'all'}..{yr[1] if yr else ''}"
                 + (f" | section={section!r}" if section else "") + (" | all-periods" if all_periods else "")]
        if data["scanned"] < data["hits"]:
            lines.append(f"  (scan capped: the first {data['scanned']} of {data['hits']} matching table parts, oldest issues "
                         "first - narrow with --years or a table id)")
        if data["hidden"] and found:
            lines.append(f"  {data['hidden']} daily/weekly rows of {data['month']} not listed (the month row only; "
                         "--all-periods lists them, --row YYYY-MM-DD one day)")
        budget = max(MIN_BUDGET, min(int(budget or PAGE_BUDGET), PAGE_BUDGET))
        if not found:
            lines += self._no_match_lines(data, row_label, table_hint, period, column, section)
            lines, _ = _cap_text(lines, [], budget, f"... OUTPUT CUT at {budget} bytes")
            return Page("printings", lines, [], {"key": key, "found": 0})
        cells, pending = [], []

        def add_cell(f, extra, at):
            rec = f["rec"]
            attrs = {**rec["attrs"], "oq.printing_of": key, "oq.issue": f["issue"], **(extra or {})}
            pending.append((at, Cell(0, rec["locator"], rec["text"][:200], rec["value"], attrs)))
        summary = self._summary_lines(found, add_cell)
        summary, _ = _cap_text(summary, [], budget - _bytes(lines) - 200, f"... SUMMARY CUT at {budget} bytes: narrow with --col / --years")
        base = len(lines)
        lines += summary
        for at, c in pending:
            if at < len(summary):
                c.line = base + at
                cells.append(c)
        listing = ["  issue    file:line                          part period       label | col = value [flags]"]
        if any(f["section"] for f in found) and len({f["section"] for f in found}) > 1:
            listing[0] += " @ panel"
        lcells = []
        for f in found:
            val, fl = parse_cell(f["raw"])
            listing.append(f"  {f['issue']}  {f['file']}:{f['line']:<6} p{f['part']} {f['period'] or '-':<12} "
                           f"{(f['label'] or '')[:28]!r} | c{f['col']} {f['col_label'][:40]!r} = {f['raw']}"
                           f"{' [' + ','.join(fl) + ']' if fl and fl != ('float0',) else ''}"
                           + (f" @ {f['section'][:30]!r}" if listing[0].endswith("@ panel") else ""))
            lcells.append((len(listing) - 1, f))
        room = budget - _bytes(lines)
        kept = listing if _bytes(listing) <= room else []
        if not kept:
            marker = f"  ... {{n}} more listing lines omitted; SUMMARY complete (narrow with --col / --years)"
            room -= _bytes([marker.format(n=len(listing))])
            for x in listing:
                if _bytes(kept + [x]) > room:
                    break
                kept.append(x)
            kept.append(marker.format(n=len(listing) - len(kept)))
        base = len(lines)
        lines += kept
        listed = []
        for at, f in lcells:
            if at < len(kept) - (0 if kept is listing else 1):
                rec = f["rec"]
                listed.append(Cell(base + at, rec["locator"], rec["text"][:200], rec["value"],
                                   {**rec["attrs"], "oq.printing_of": key, "oq.issue": f["issue"]}))
        # listing cells first (as in v9, where the listing preceded the SUMMARY): the ledger keeps the first
        # of two cells with one locator and text, so the SUMMARY duplicates never displace a listing cell
        return Page("printings", lines, listed + cells, {"key": key, "found": len(found)})

    def printings(self, *args, **kw) -> str:
        return self.printings_page(*args, **kw).text

    # ---- latest printing of one cell (rules; newest-first scan, bounded, deterministic)
    def latest_printing(self, rec: dict, *, max_parts: int = 48) -> dict | None:
        """The newest printing (issue after rec's issue) of the same row period and column as cell record
        `rec`.  Row: the row period (FY/CY year or YYYY-MM), exactly; column: the label without footnote
        marks and, when printed, the same '(n)' column number (numbers alone are renumbered across issues).
        Scans the table's parts newest issue first (window: rec's issue .. period year +3 for months / +12
        for years, at least rec's issue year +3), stops at the first issue printing the row, at most
        `max_parts` parts (deterministic bound).  Returns {'issue','locator','text','value','ambiguous',
        'flags'} or None."""
        at = rec["attrs"]
        tfile, thdr = at["oq.table_ref"].rsplit(":", 1)
        tid_rows = self.con().execute("SELECT tid, title FROM t WHERE file=? AND hdr_line=?", (tfile, int(thdr))).fetchone()
        if not tid_rows:
            return None
        hint = tid_rows[0] or (tid_rows[1] or "")
        rp = at.get("oq.row_period") or ""
        m = re.fullmatch(r"(FY|CY)?(\d{4})(?:-(\d\d))?", rp)
        ckey = column_key(at.get("oq.column") or "")
        stub = panel_stub(at.get("oq.section") or "")        # W0-3: the same panel, never a neighbouring one
        if not hint.strip() or not m or not ckey[1]:
            return None
        year, iy = int(m.group(2)), int(str(at["oq.issue"])[:4])
        yr = (iy, max(year + (3 if m.group(3) else 12), iy + 3))
        parts = [p for p in self._printing_parts(hint, yr, newest_first=True) if p[2] > at["oq.issue"]]
        by_issue = collections.OrderedDict()
        for fname, hl, issue, part in parts[:max_parts]:
            if by_issue and issue != next(reversed(by_issue)):
                if by_issue[next(reversed(by_issue))]:
                    break                      # the newest issue that prints the row is complete
            by_issue.setdefault(issue, [])
            t = self.table_at(fname, hl)
            a = self.anchored(t)
            for o in a["rows"]:
                if (o["period"] or "") != rp:
                    continue
                theirs = panel_stub(o["section"] or "")
                if stub and theirs != stub and not same_panel(stub, theirs):
                    continue
                for q, c in enumerate(o["cols"]):
                    if o["vals"][q] != "" and column_key(row_label_for(a, o, c)) == ckey:
                        by_issue[issue].append((theirs == stub, cell_record(a, t, o, c, o["vals"][q])))
        for issue, recs in by_issue.items():
            if not recs:
                continue
            exact = [r for same, r in recs if same]              # the same panel stub wins over an OCR variant of it
            recs = exact or [r for _, r in recs]
            vals = {r["value"] for r in recs}
            r0 = recs[0]
            return {"issue": issue, "locator": r0["locator"], "text": r0["text"], "value": r0["value"],
                    "ambiguous": len(vals) > 1, "flags": r0["attrs"]["oq.flags"]}
        return None
