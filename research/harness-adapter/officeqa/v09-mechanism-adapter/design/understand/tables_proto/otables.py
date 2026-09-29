#!/usr/bin/env python3
"""otables - table tools over the Treasury Bulletin TXT corpus (v9 prototype).

Stdlib only (Python >= 3.8).  Read-only over the corpus; writes only its cache.

  python otables.py build                               # one-off table index (sqlite)
  python otables.py find "judicial branch" --years 2003-2004
  python otables.py show treasury_bulletin_2004_03.txt 728 [--stitch] [--cols 1-3] [--grep Jan]
  python otables.py printings --table FFO-3 --row 2003-01 --col judicial

Design notes (see ../tables.md):
  * every table in the corpus is a pandas `to_markdown` pipe table: one header
    line, one `| --- |` line, body lines.  Multi-row PDF headers are flattened
    into one line with " > " between levels, empty header cells become
    "Unnamed: k_level_j", duplicate labels get ".1", ".2" suffixes, empty body
    cells are "nan", numeric columns containing a blank became floats ("2085.0").
  * show_table never silently realigns: every repair is listed under NOTES and
    marked on the row.
"""
from __future__ import annotations

import argparse
import collections
import functools
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = Path(os.environ.get(
    "OTABLES_CORPUS",
    "/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/opus-officeqa-133/corpus"))
CACHE = Path(os.environ.get("OTABLES_CACHE", HERE / "cache"))
INDEX = CACHE / "tables.sqlite"

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
    """User-facing period spec: 2003-01, 2003-1, Jan 2003, January 2003, 2003, FY2003."""
    q = q.strip()
    m = re.match(r"^((?:19|20)\d\d)[-/](\d{1,2})$", q)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.match(r"^(?:fy|cy)?\s*((?:19|20)\d\d)$", q, re.I)
    if m:
        return (int(m.group(1)), None)
    p = parse_period(q)
    if p and p["year"] and p["month"]:
        return (p["year"], p["month"])
    return None


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
@functools.lru_cache(maxsize=16)
def load_lines(fname: str) -> tuple:
    p = CORPUS / Path(fname).name
    return tuple(p.read_text(encoding="utf-8", errors="replace").split("\n"))


def _is_units(s: str) -> bool:
    # 1999_12..2001_06 issues split words into syllables ('mil lions of dol lars')
    return bool(UNITS_RX.match(s) or (s[:1] in "([" and UNITS_RX.match(s.replace(" ", ""))))


def _is_caps_heading(s: str) -> bool:
    letters = [ch for ch in s if ch.isalpha()]
    return (6 <= len(s) <= 140 and len(letters) >= 5 and not s.startswith("|")
            and sum(ch.isupper() for ch in letters) / len(letters) > 0.85
            and not s.upper().startswith("TABLE "))


@functools.lru_cache(maxsize=16)
def file_tables(fname: str) -> tuple:
    """All pipe tables of one issue with their context (title, id, units, as-of...)."""
    lines = load_lines(fname)
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


def table_at(fname: str, line: int) -> dict:
    """table whose header/body contains `line`, or whose title is at `line`, or the next one."""
    ts = file_tables(fname)
    for t in ts:
        if t["hdr_line"] <= line <= t["end_line"]:
            return t
    for t in ts:
        if t.get("title_line") == line:
            return t
    for t in ts:
        if t["hdr_line"] > line:
            return t
    raise SystemExit(f"no table at or after {fname}:{line}")


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


def _panels_for(data, ncols, labels, dups, hdr_year, notes, tag, seg_labels=None):
    """period columns -> side-by-side panels; else repeated column groups; else one panel."""
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
                else:
                    for q, c in enumerate(pn["vcols"]):
                        col_label[c] = (vl[q] if q < len(vl) else "?") + " ?"
                    notes.append(f"{tag}panel {k + 1}: {len(pn['vcols'])} value columns but the header group has "
                                 f"{len(vl)} value labels - labels are NOT reliable here (a column is missing/merged); "
                                 f"VERIFY against a sibling printing or an identity")
        else:
            notes.append(f"{tag}{len(panels)} side-by-side panels found from period columns but the header has "
                         f"{len(glist)} label groups - labels shown raw, VERIFY")
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


def analyze(t: dict, repair: bool = True, prev_header=None) -> dict:
    ncols = len(t["header"])
    notes = []
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
        panels, pcols, labs_fix = _panels_for(sdata, ncols, labels, dups, hdr_year, notes, tag, inb)
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
        if re.search(r"\b(bid|yield|rate|percent|discount)\b", lab) and coltype[c] == "date":
            notes.append(f"c{c} labelled '{col_label[c]}' holds dates - header probably shifted")

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
    for (c, run), (ok, tot, badl, ks) in [x for x in best.values()][:4]:
        rs = f"c{run[0]}..c{run[-1]}" if len(run) > 2 else "+".join(f"c{x}" for x in run)
        notes.append(f"identity: c{c} = sum {rs} on {ok}/{tot} rows ({len(ks)} segment/panel(s))"
                     + (f"; FAILS at lines {','.join(map(str, badl[:10]))} - OCR misread or shifted cell?" if badl else ""))
    return {"ncols": ncols, "labels": labels, "col_label": col_label, "coltype": coltype, "panels": all_panels,
            "pcols": sorted(all_pcols), "rows": out, "notes": notes, "raw_rows": rows, "colper": colper,
            "segments": len(segs), "layouts": seg_layouts}


# ----------------------------------------------------------------------------
# index (sqlite, built once)
# ----------------------------------------------------------------------------
def build_index(verbose=True):
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE t(rid INTEGER PRIMARY KEY, file TEXT, issue TEXT, hdr_line INT, end_line INT,"
                " title_line INT, tid TEXT, tid_raw TEXT, title TEXT, subtitle TEXT, units TEXT, asof TEXT,"
                " section TEXT, cont INT, part INT, ncols INT, nrows INT, kind TEXT, header TEXT,"
                " first_period TEXT, last_period TEXT, squash TEXT)")
    con.execute("CREATE VIRTUAL TABLE f USING fts5(title, header, rowlabels, content='', tokenize='unicode61')")
    files = sorted(CORPUS.glob("treasury_bulletin_*.txt"))
    n = 0
    for i, p in enumerate(files):
        for t in file_tables(p.name):
            labs = []
            seen = set()
            pers = []
            for _, cells in t["rows"]:
                c0 = clean_label(cells[0]) if cells else ""
                pp = parse_period(c0) if c0 else None
                if pp:
                    pers.append(c0)
                    continue
                k = norm(c0)
                if k and k not in seen and len(k) > 2:
                    seen.add(k)
                    labs.append(k)
            hdr = " | ".join(" > ".join(header_parts(h)[0]) for h in t["header"])
            squash = norm(" ".join(x for x in [t.get("title") or "", " ".join(t.get("subtitle") or []),
                                                t.get("section") or "", hdr])).replace(" ", "")
            con.execute("INSERT INTO t VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (t["file"], t["issue"], t["hdr_line"], t["end_line"], t.get("title_line"), t.get("tid"),
                         t.get("tid_raw"), t.get("title"), " / ".join(t.get("subtitle") or []), t.get("units"),
                         t.get("asof"), t.get("section"), int(t["cont"]), t["part"], len(t["header"]),
                         len(t["rows"]), t["kind"], hdr, pers[0] if pers else None, pers[-1] if pers else None,
                         squash))
            rid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            ttl = " ".join(x for x in [t.get("tid") or "", t.get("tid_raw") or "", t.get("title") or "",
                                        " ".join(t.get("subtitle") or []), t.get("units") or "",
                                        t.get("section") or ""])
            con.execute("INSERT INTO f(rowid, title, header, rowlabels) VALUES (?,?,?,?)",
                        (rid, norm(ttl), norm(hdr), " ; ".join(labs)[:1500]))
            n += 1
        file_tables.cache_clear()
        load_lines.cache_clear()
        if verbose and i % 100 == 0:
            print(f"  {i}/{len(files)} files, {n} tables", file=sys.stderr)
    con.commit()
    con.close()
    tmp.replace(INDEX)
    return n


def _db():
    if not INDEX.exists():
        print("building table index (one-off)...", file=sys.stderr)
        build_index()
    return sqlite3.connect(f"file:{INDEX}?mode=ro", uri=True)


def _years(spec):
    if not spec:
        return None
    m = re.match(r"^(\d{4})(?:\s*[-:]\s*(\d{4}))?$", str(spec))
    if not m:
        raise SystemExit("--years takes YYYY or YYYY-YYYY")
    a = int(m.group(1))
    b = int(m.group(2) or a)
    return a, b


def _id_like(s):
    return re.fullmatch(r"[A-Za-z0-9]{1,6}(?:-[A-Za-z0-9]{1,4})?-\d{1,2}[A-Za-z]?", s.strip()) is not None


def find_tables(phrase: str, years=None, limit=25, rows=False, all_hits=False):
    yr = _years(years)
    con = _db()
    where = []
    args = []
    if _id_like(phrase):
        where.append("t.tid = ?")
        args.append(canon_id(phrase))
        sql = "SELECT t.* FROM t WHERE " + " AND ".join(where)
    else:
        toks = norm(phrase).split()
        if not toks:
            raise SystemExit("empty phrase")
        cols = "{title header rowlabels}" if rows else "{title header}"
        q = " AND ".join(f'{cols} : "{tk}"' for tk in toks)
        sql = "SELECT t.* FROM f JOIN t ON t.rid = f.rowid WHERE f MATCH ?"
        args.append(q)
    if yr:
        sql += (" AND " if " WHERE " in sql else " WHERE ") + "substr(t.issue,1,4) BETWEEN ? AND ?"
        args += [str(yr[0]), str(yr[1])]
    sql += " ORDER BY t.issue, t.hdr_line"
    cur = con.execute(sql, args)
    names = [d[0] for d in cur.description]
    hits = [dict(zip(names, r)) for r in cur.fetchall()]
    if not _id_like(phrase):
        # OCR splits words ('Cal en dar', 'Lia bilities'): also match with all spaces removed
        sq = norm(phrase).replace(" ", "")
        sql2 = "SELECT t.* FROM t WHERE t.squash LIKE ?"
        args2 = [f"%{sq}%"]
        if yr:
            sql2 += " AND substr(t.issue,1,4) BETWEEN ? AND ?"
            args2 += [str(yr[0]), str(yr[1])]
        have = {h["rid"] for h in hits}
        extra = [dict(zip(names, r)) for r in con.execute(sql2, args2).fetchall()]
        hits += [h for h in extra if h["rid"] not in have]
        hits.sort(key=lambda h: (h["issue"], h["hdr_line"]))
    con.close()
    ph = norm(phrase)
    for h in hits:
        where_ = []
        if ph in norm(" ".join(x for x in [h["tid"] or "", h["title"] or "", h["subtitle"] or ""])):
            where_.append("title")
        if ph in norm(h["header"] or ""):
            where_.append("header")
        h["where"] = ",".join(where_) or ("id" if _id_like(phrase) else "tokens")
        h["score"] = (3 if "title" in where_ else 0) + (2 if "header" in where_ else 0) + (1 if h["kind"] == "data" else -5)
    # group by table identity (id + normalized title) across issues
    groups = collections.OrderedDict()
    for h in hits:
        ttl = re.sub(CONT_RX, "", h["title"] or h["subtitle"] or "")
        ttl = re.sub(ASOF_RX, "", ttl)
        ttl = re.sub(r"\b(preliminary|revised)\b", "", ttl, flags=re.I)
        ident = h["tid"] or (f"{h['tid_raw']} in {h['section'] or '?'}" if h["tid_raw"] else "")
        key = (ident, norm(ttl).replace(" ", "")[:60])
        h["ident"] = ident
        groups.setdefault(key, []).append(h)
    for key in [k for k in groups if not k[0]]:
        twin = next((k for k in groups if k[0] and k[1] == key[1]), None)
        if twin:
            groups[twin] += groups.pop(key)
    ranked = sorted(groups.items(), key=lambda kv: (-max(x["score"] for x in kv[1]), -len(kv[1])))
    lines = [f"find_tables({phrase!r}, years={years}): {len(hits)} table parts in {len(groups)} groups"]
    for (tid, _), hs in ranked[:limit]:
        tid = tid[:60]
        issues = sorted({x["issue"] for x in hs})
        h0 = hs[0]
        ttl = (h0["title"] or h0["subtitle"] or "(untitled)")[:110]
        lines.append(f"[{tid or '-'}] {ttl}  | {len(issues)} issues {issues[0]}..{issues[-1]} | match:{h0['where']}"
                     + (f" | kind:{h0['kind']}" if h0["kind"] != "data" else ""))
        show = hs if all_hits else hs[-3:]
        for x in show:
            per = f" rows {x['first_period']}..{x['last_period']}" if x["first_period"] else ""
            asof = f" as-of {x['asof']}" if x["asof"] else ""
            lines.append(f"    {x['file']}:{x['hdr_line']} part{x['part']}{' cont' if x['cont'] else ''}"
                         f" {x['ncols']}c x {x['nrows']}r{per}{asof}")
        if not all_hits and len(hs) > 3:
            lines.append(f"    ... {len(hs) - 3} more (use --all)")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# show_table
# ----------------------------------------------------------------------------
def _parts_of(fname, t, stitch):
    ts = list(file_tables(fname))
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


def _fmt_grid(rows, width_cap=14):
    w = [max(len(str(r[c])) for r in rows) for c in range(len(rows[0]))]
    w = [min(x, width_cap if c > 3 else 44) for c, x in enumerate(w)]
    out = []
    for r in rows:
        out.append("  ".join(str(v)[:w[c]].ljust(w[c]) if c <= 3 else str(v)[:w[c]].rjust(w[c]) for c, v in enumerate(r)).rstrip())
    return out


def _col_filter(spec, ncols):
    if not spec:
        return None
    keep = set()
    for part in spec.split(","):
        part = part.strip().lstrip("c")
        if "-" in part:
            a, b = part.split("-")
            keep.update(range(int(a), int(b) + 1))
        elif part:
            keep.add(int(part))
    return keep


def _siblings(t, span=6, max_n=3):
    """same table (id or title) in nearby issues with the same width - for header/year anchoring."""
    if not INDEX.exists():
        return []
    con = _db()
    y, m = map(int, t["issue"].split("_"))
    lo, hi = f"{y - 1}_{m:02d}", f"{y + 1}_{m:02d}"
    if t.get("tid"):
        cur = con.execute("SELECT file, hdr_line, issue FROM t WHERE tid=? AND ncols=? AND issue BETWEEN ? AND ? "
                          "AND issue != ? AND part=? ORDER BY issue", (t["tid"], len(t["header"]), lo, hi, t["issue"], t["part"]))
    else:
        cur = con.execute("SELECT file, hdr_line, issue FROM t WHERE title=? AND ncols=? AND issue BETWEEN ? AND ? "
                          "AND issue != ? ORDER BY issue", (t.get("title"), len(t["header"]), lo, hi, t["issue"]))
    res = cur.fetchall()
    con.close()
    res.sort(key=lambda r: abs((int(r[2][:4]) * 12 + int(r[2][5:])) - (y * 12 + m)))
    return res[:max_n]


def _row_label_for(a, o, c):
    """label of source column c for output row o (in-body header labels win)."""
    if o.get("inbody"):
        x = o["labels"][c]
        return x or a["col_label"][c]
    return a["col_label"][c]


def show_table(fname: str, line: int, cols=None, grep=None, stitch=False, raw=False, max_rows=400,
               siblings=True, long=False, match=None, from_row=0, budget=None) -> str:
    text = _show_table(fname, line, cols, grep, stitch, raw, max_rows, siblings, long, match, from_row)
    if budget is None or len(text) <= budget:
        return text
    # never let the shell/agent truncate silently: cut at a row boundary and say how to continue
    lines = text.split("\n")
    head_end = next((i for i, l in enumerate(lines) if l.startswith(("ROWS:", "CELLS"))), len(lines) - 1) + 2
    keep = lines[:head_end]
    size = sum(len(l) + 1 for l in keep)
    shown = 0
    for l in lines[head_end:-1]:
        if size + len(l) + 1 > budget - 300:
            break
        keep.append(l)
        size += len(l) + 1
        if re.match(r"^\s*\d+ ", l):
            shown += 1
    keep.append(f"... OUTPUT CUT BY otables at {budget} chars after {shown} rows: continue with --from-row "
                f"{from_row + shown}, or narrow with --cols / --match / --grep")
    return "\n".join(keep)


def _show_table(fname, line, cols=None, grep=None, stitch=False, raw=False, max_rows=400,
                siblings=True, long=False, match=None, from_row=0) -> str:
    fname = Path(fname).name
    t = table_at(fname, line)
    parts = _parts_of(fname, t, stitch)
    head = parts[0]
    out = [f"TABLE [{head.get('tid') or head.get('tid_raw') or '-'}] {head.get('title') or '(untitled)'}",
           f"  file {fname}  issue {head['issue']}  lines {parts[0]['hdr_line']}-{parts[-1]['end_line']}"
           f"  part {head['part']}{' of a multi-part table' if head['cont'] or head['part'] > 1 else ''}"
           f"{'  +' + str(len(parts) - 1) + ' continuation parts stitched' if len(parts) > 1 else ''}"]
    for key, lab in (("subtitle", "subtitle"), ("units", "units"), ("asof", "as-of"), ("section", "section")):
        v = head.get(key)
        if v:
            out.append(f"  {lab}: " + (" / ".join(v) if isinstance(v, list) else v)[:300])
    if raw:
        lines = load_lines(fname)
        for p in parts:
            out += [f"{k:>6}: {lines[k - 1]}" for k in range(p["hdr_line"], p["end_line"] + 1)]
        return "\n".join(out)
    ts = list(file_tables(fname))
    analyses = []
    for p in parts:
        k = next(i for i, x in enumerate(ts) if x["hdr_line"] == p["hdr_line"])
        analyses.append(analyze(p, prev_header=ts[k - 1]["header"] if k > 0 else None))
    a0 = analyses[0]
    keep = _col_filter(cols, a0["ncols"])
    mt = [norm(x) for x in match.split(",")] if match else None

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
    for pi, a in enumerate(analyses[1:], 1):
        if len(a["panels"]) > 1:
            out.append(f"  (part {pi + 1} has several panels; only its first panel is stitched)")
        slots += [(pi, c) for c in a["panels"][0]["vcols"] if wanted(a, c)]
    # ---- legend
    out.append("COLUMNS:")
    pc0 = a0["panels"][0]["pcol"]
    stub = pc0 if pc0 is not None else 0
    out.append(f"  c{stub:<3} {a0['col_label'][stub][:90] or '(stub)'} [{'period' if stub in a0['pcols'] else a0['coltype'][stub]}]"
               f" -> shown as 'period' (filled) and 'label' (raw)")
    layouts = []
    for lay in a0.get("layouts") or [{"section": None, "panels": a0["panels"], "labels": {}}]:
        sig = (tuple(tuple(pn["vcols"]) for pn in lay["panels"]), tuple(sorted(lay["labels"].items())))
        if layouts and layouts[-1][0] == sig:
            layouts[-1][1].append(lay["section"])
        else:
            layouts.append((sig, [lay["section"]], lay))
    for li, (sig, secs, lay) in enumerate(layouts):
        if len(layouts) > 1:
            out.append(f"  -- layout {li + 1} (sections: {'; '.join((x or '-')[:40] for x in secs)[:160]})")
        for pi, q in [x for x in slots if x[0] == 0]:
            desc = []
            for k, pn in enumerate(lay["panels"]):
                if q < len(pn["vcols"]):
                    c = pn["vcols"][q]
                    per = a0["colper"].get(c)
                    pers = f" <{per[0]}{'~' if per[1] else ''}>" if per else ""
                    lab = lay["labels"].get(c, a0["col_label"][c])
                    desc.append((f"p{k + 1}:" if len(lay["panels"]) > 1 else "")
                                + f"c{c} {lab[:70] or '(no label)'}{pers} [{a0['coltype'][c]}]")
            if desc:
                out.append(f"  v{q + 1:<3} = " + " | ".join(desc))
    for pi, q in slots:
        if pi == 0:
            continue
        else:
            a = analyses[pi]
            out.append(f"  c{pi + 1}.{q:<3} {a['col_label'][q][:90] or '(no label)'} [{a['coltype'][q]}]  (part {pi + 1})")
    # ---- notes
    notes = []
    for pi, a in enumerate(analyses):
        pre = f"part{pi + 1}: " if len(analyses) > 1 else ""
        notes += [pre + n_ for n_ in a["notes"]]
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
        sib = _siblings(head)
        if sib:
            notes.append("SIBLINGS (same table, same width, nearby issues; use them to anchor years/headers): "
                         + "; ".join(f"{f}:{ln}" for f, ln, _ in sib))
    if notes:
        out.append("NOTES:")
        out += ["  - " + x for x in notes]
    # ---- rows
    body = a0["rows"]
    if grep:
        g = norm(grep)
        body = [o for o in body if g in norm(o["label"] or "") or g in norm(o["period"] or "") or g in norm(o["section"] or "")]
    body = body[from_row:from_row + max_rows]
    pos = {id(o): i for i, o in enumerate(a0["rows"])}
    grid = [["line", "pnl", "period", "label"] + [f"v{q + 1}" if pi == 0 else f"c{pi + 1}.{q}" for pi, q in slots] + ["flags"]]
    cells_out = []
    last_sec = object()
    for o in body:
        if o["section"] != last_sec:
            grid.append(["", "", "==", (o["section"] or "")[:44]] + [""] * (len(slots) + 1))
            last_sec = o["section"]
        row = [o["line"], o["panel"], o["period"] or "", (o["label"] or "")[:44]]
        flags = list(o["flags"])
        i = pos[id(o)]
        for pi, q in slots:
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
                a = analyses[src[0]]
                lab = _row_label_for(a, oo, src[1])
                per = a["colper"].get(src[1])
                if per and per[0].startswith("Y?-") and re.fullmatch(r"(FY|CY)?\d{4}", o["period"] or ""):
                    per = (o["period"][-4:] + per[0][2:], False)  # year x month matrix: row year + column month
                val, fl = parse_cell(v)
                cells_out.append(f"  {o['line']} p{o['panel']} {o['period'] or '-'} {(o['label'] or '')[:40]!r} | "
                                 f"c{src[0] + 1}.{src[1]} {lab[:70]!r}{' <' + per[0] + '>' if per else ''} = {v}"
                                 f"{' [' + ','.join(fl) + ']' if fl else ''}")
        for pi in range(1, len(analyses)):
            if i < len(analyses[pi]["rows"]):
                flags += [f"p{pi + 1}:{f}" for f in analyses[pi]["rows"][i]["flags"]]
        row.append(" ".join(flags))
        grid.append(row)
    if long:
        out.append("CELLS (line panel period 'row label' | c<part>.<col> 'column label' <column period> = raw [parsed flags]):")
        out += cells_out
    else:
        out.append("ROWS:")
        out += _fmt_grid([[str(x) for x in r] for r in grid])
    out.append(f"({len(body)} of {len(a0['rows'])} rows; period = YYYY-MM filled down from the first row of each "
               f"year block; 'Y?' = year not printed; 'year+1 inferred' = rollover without a label; <..~> = inferred "
               f"column period; raw cells kept, so r/p flags and footnote marks stay visible)")
    return "\n".join(out)


# ----------------------------------------------------------------------------
# printings
# ----------------------------------------------------------------------------
def printings(row_label: str, table_hint: str, period=None, column=None, years=None, max_issues=400) -> str:
    """The same row (and optionally column) across every issue that prints table_hint."""
    yr = _years(years)
    con = _db()
    if _id_like(table_hint):
        sql = "SELECT file, hdr_line, issue, part FROM t WHERE tid=?"
        args = [canon_id(table_hint)]
    else:
        toks = norm(table_hint).split()
        q = " AND ".join(f'title : "{tk}"' for tk in toks)
        sql = "SELECT t.file, t.hdr_line, t.issue, t.part FROM f JOIN t ON t.rid=f.rowid WHERE f MATCH ?"
        args = [q]
    qp0 = parse_query_period(period or row_label) if (period or parse_query_period(row_label)) else None
    if not yr and qp0:
        # a month row is reprinted for ~1-2 years, an annual row for up to ~10: default search window
        yr = (qp0[0], qp0[0] + (3 if qp0[1] else 12))
    if yr:
        sql += " AND substr(issue,1,4) BETWEEN ? AND ?"
        args += [str(yr[0]), str(yr[1])]
    sql += " ORDER BY issue, hdr_line"
    hits = con.execute(sql, args).fetchall()
    con.close()
    qp = parse_query_period(row_label)
    if period:
        qp = parse_query_period(period) or qp
    want_label = None if (qp and parse_query_period(row_label)) else norm(row_label)
    col_hint = norm(column) if column else None
    lines = [f"printings(row={row_label!r}, table={table_hint!r}, period={period!r}, col={column!r}): "
             f"{len(hits)} table parts searched in issues {yr[0] if yr else 'all'}..{yr[1] if yr else ''}"]
    seen_files = {}
    found = []
    for fname, hl, issue, part in hits[:max_issues * 4]:
        t = table_at(fname, hl)
        ts = list(file_tables(fname))
        k = next(i for i, x in enumerate(ts) if x["hdr_line"] == t["hdr_line"])
        a = analyze(t, prev_header=ts[k - 1]["header"] if k > 0 else None)
        for o in a["rows"]:
            ok = True
            if qp:
                per = o["period"] or ""
                if qp[1] is None:
                    want_b = re.match(r"^\s*(fy|cy)", (period or row_label), re.I)
                    core = re.sub(r"^(FY|CY)", "", per.split(" ")[0])
                    ok = core == str(qp[0]) and (not want_b or per.upper().startswith(want_b.group(1).upper()))
                else:
                    ok = per.startswith(f"{qp[0]}-{qp[1]:02d}")
            if want_label:
                ok = ok and (want_label in norm(o["label"] or "") or want_label == norm(o["section"] or ""))
            if not ok:
                continue
            for q, c in enumerate(o["cols"]):
                lab = _row_label_for(a, o, c)         # in-body header bands relabel columns mid-table
                if col_hint and col_hint not in norm(lab):
                    continue
                v = o["vals"][q]
                if v == "" and col_hint is None:
                    continue
                found.append((issue, fname, o["line"], part, o["period"], o["label"], c, lab, v))
    if not found:
        lines.append("  no matching row/column found (check the row label/period and --col; try find_tables first)")
        return "\n".join(lines)
    lines.append("  issue    file:line                          part period       label | col = value [flags]")
    listing_cap = 60
    if len(found) > listing_cap:
        lines.append(f"  ({len(found)} cells found; listing the first {listing_cap} - the SUMMARY below covers all; "
                     "narrow with --col)")
    for issue, fname, ln, part, per, lab, c, clab, v in found[:listing_cap]:
        val, fl = parse_cell(v)
        lines.append(f"  {issue}  {fname}:{ln:<6} p{part} {per or '-':<12} {lab[:28]!r} | c{c} {clab[:40]!r} = {v}"
                     f"{' [' + ','.join(fl) + ']' if fl and fl != ('float0',) else ''}")
    # summary per (col label): distinct values in issue order
    bycol = collections.OrderedDict()
    for issue, fname, ln, part, per, lab, c, clab, v in found:
        # same column across issues: printed column number '(8)' if any, else the label without spaces
        m = re.search(r"\((\d{1,2})\)\s*$", clab)
        ckey = f"col ({m.group(1)})" if m else norm(clab).replace(" ", "")[:60]
        bycol.setdefault((part, ckey, per), []).append((issue, v, f"{fname}:{ln}", clab))
    lines.append("SUMMARY (per column & period: value -> issues; first and latest printing):")
    for (part, ckey, per), xs in bycol.items():
        clab = f"part{part} {ckey} ({xs[-1][3][:40]})"
        vals = collections.OrderedDict()
        for issue, v, loc, _ in xs:
            val, fl = parse_cell(v)
            key = "%g" % val if val is not None else v
            vals.setdefault(key, []).append(issue)
        tag = "REVISED/DIFFERS across printings" if len(vals) > 1 else "same in all printings"
        # a value printed once, between printings that agree with each other, is more likely OCR damage or a
        # row/column shift in that one issue than a revision
        seq = []
        for issue, v, loc, _ in xs:
            val, fl = parse_cell(v)
            seq.append(("%g" % val if val is not None else v, issue, loc))
        iso = [seq[i] for i in range(1, len(seq) - 1)
               if seq[i][0] != seq[i - 1][0] and seq[i - 1][0] == seq[i + 1][0] and seq[i][1] not in (seq[i - 1][1], seq[i + 1][1])]
        lines.append(f"  [{per or '-'}] {clab[:80]}: {len(xs)} printings, {len(vals)} distinct values - {tag}")
        for key, iss in vals.items():
            lines.append(f"      {key}: {iss[0]}..{iss[-1]} ({len(iss)})")
        for v_, issue, loc in iso:
            lines.append(f"      ISOLATED: {issue} ({loc}) disagrees with the printings before and after it, which agree "
                         f"- likely OCR/row shift in that issue, not a revision")
        lines.append(f"      first printing {xs[0][0]} ({xs[0][2]}), latest printing {xs[-1][0]} ({xs[-1][2]})")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    f = sub.add_parser("find")
    f.add_argument("phrase")
    f.add_argument("--years")
    f.add_argument("--limit", type=int, default=25)
    f.add_argument("--rows", action="store_true", help="also search row labels")
    f.add_argument("--all", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("file")
    s.add_argument("line", type=int)
    s.add_argument("--cols")
    s.add_argument("--grep")
    s.add_argument("--stitch", action="store_true")
    s.add_argument("--raw", action="store_true")
    s.add_argument("--long", action="store_true")
    s.add_argument("--max-rows", type=int, default=400)
    s.add_argument("--match", help="comma list of column-label substrings")
    s.add_argument("--from-row", type=int, default=0)
    s.add_argument("--budget", type=int, default=9000, help="max output chars (0 = unlimited)")
    p = sub.add_parser("printings")
    p.add_argument("--table", required=True)
    p.add_argument("--row", required=True)
    p.add_argument("--period")
    p.add_argument("--col")
    p.add_argument("--years")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        print(f"indexed {build_index()} tables -> {INDEX}")
    elif a.cmd == "find":
        print(find_tables(a.phrase, a.years, a.limit, a.rows, a.all))
    elif a.cmd == "show":
        print(show_table(a.file, a.line, a.cols, a.grep, a.stitch, a.raw, a.max_rows, long=a.long, match=a.match,
                         from_row=a.from_row, budget=a.budget or None))
    elif a.cmd == "printings":
        print(printings(a.row, a.table, a.period, a.col, a.years))


if __name__ == "__main__":
    main()
