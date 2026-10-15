#!/usr/bin/env python3
"""The search must window its output (10 hits + MORE line), close the
last page without MORE, refuse over-cap queries with guidance, and
refuse empty queries and out-of-range pages."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import run  # noqa: E402

p1 = run.search(run.LOG, "SB-ALPHA", 1)
assert p1[0] == "PAGE|1/3|showing 1-10 of 25", p1[0]
assert len([ln for ln in p1 if ln.startswith("HIT|")]) == 10, p1
assert p1[-1] == "MORE|15 more; narrow the query or request page 2", p1[-1]
assert all("SB-ALPHA" in ln for ln in p1 if ln.startswith("HIT|")), p1

p3 = run.search(run.LOG, "SB-ALPHA", 3)
assert p3[0] == "PAGE|3/3|showing 21-25 of 25", p3[0]
assert len([ln for ln in p3 if ln.startswith("HIT|")]) == 5, p3
assert not any(ln.startswith("MORE|") for ln in p3), "last page has no MORE"

broad = run.search(run.LOG, "GRM", 1)
assert len(broad) == 1 and broad[0].startswith("CATCH:"), broad
assert "150 rows" in broad[0] and "narrow" in broad[0], broad[0]

empty = run.search(run.LOG, "   ", 1)
assert empty[0].startswith("CATCH: empty"), empty

far = run.search(run.LOG, "SB-ALPHA", 9)
assert far[0].startswith("CATCH: page 9 out of range"), far

none = run.search(run.LOG, "SB-OMEGA", 1)
assert none == ["PAGE|1/1|0 matches for 'SB-OMEGA'"], none

print("OK: windowing, last-page close, cap refusal, and bad-input "
      "refusals all correct")
