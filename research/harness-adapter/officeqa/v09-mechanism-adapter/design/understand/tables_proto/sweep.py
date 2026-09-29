#!/usr/bin/env python3
"""Robustness sweep: analyze() every table of one issue per year; count crashes and note types by decade."""
import collections, re, sys, time, traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import otables as O

files = sorted(O.CORPUS.glob("treasury_bulletin_*.txt"))
pick = {}
for f in files:
    y = f.name[18:22]
    pick.setdefault(y, f.name)                     # first issue of every year
stats = collections.defaultdict(collections.Counter)
errs = []
t0 = time.time()
for y, fname in sorted(pick.items()):
    era = y[:3] + "0s"
    ts = O.file_tables(fname)
    for i, t in enumerate(ts):
        st = stats[era]
        st["tables"] += 1
        try:
            a = O.analyze(t, prev_header=ts[i - 1]["header"] if i else None)
        except Exception as e:
            errs.append((fname, t["hdr_line"], repr(e)[:120]))
            st["CRASH"] += 1
            continue
        st["rows"] += len(a["rows"])
        if a["pcols"]:
            st["has_period_stub"] += 1
        if any(len(l["panels"]) > 1 and l["panels"][0]["kind"] == "side-by-side" for l in a["layouts"]):
            st["side_by_side_panels"] += 1
        if any(l["panels"][0]["kind"] == "group" for l in a["layouts"] if l["panels"]):
            st["column_groups"] += 1
        fl = [f for o in a["rows"] for f in o["flags"]]
        st["rows_year_filled"] += sum(1 for o in a["rows"] if re.match(r"^\d{4}-\d\d", o["period"] or "") and not re.match(r"^\d{4}", o["label"]))
        st["rows_YEAR_UNKNOWN"] += fl.count("YEAR_UNKNOWN")
        st["rows_SEQ_break"] += sum(1 for f in fl if f.startswith("SEQ") or f == "YEAR_BACKWARDS")
        st["rows_SHIFT+1"] += fl.count("SHIFT+1")
        st["rows_SPLIT"] += sum(1 for f in fl if f.startswith("SPLIT"))
        st["rows_year+1_inferred"] += fl.count("year+1 inferred")
        n = " ".join(a["notes"])
        st["tables_with_identity"] += "identity:" in n
        st["tables_identity_FAILS"] += "FAILS at" in n
        st["tables_header_warn"] += ("VERIFY" in n or "NOT reliable" in n or "probably shifted" in n or "spurious" in n)
        st["tables_inbody_header"] += "in-body header" in n
        st["tables_orphan_row"] += "fell out of the pipe table" in n
        st["tables_colper_inferred"] += "column periods without a printed year" in n
        st["tables_header_missing"] += "header row missing" in n
print(f"{time.time() - t0:.1f}s, {len(pick)} issues")
keys = ["tables", "rows", "has_period_stub", "side_by_side_panels", "column_groups", "rows_year_filled", "rows_YEAR_UNKNOWN",
        "rows_year+1_inferred", "rows_SEQ_break", "rows_SHIFT+1", "rows_SPLIT", "tables_with_identity", "tables_identity_FAILS",
        "tables_header_warn", "tables_inbody_header", "tables_header_missing", "tables_orphan_row", "tables_colper_inferred", "CRASH"]
print("era    " + " ".join(k.replace("tables_", "t_").replace("rows_", "r_")[:14].rjust(14) for k in keys))
for era in sorted(stats):
    print(era.ljust(6), " ".join(str(stats[era][k]).rjust(14) for k in keys))
for e in errs[:10]:
    print("ERR", e)
