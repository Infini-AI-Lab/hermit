#!/usr/bin/env python3
"""The planner must accept four requests with running counts and write
arguments, refuse the unknown seed bank, refuse the sixth item on an
exhausted allowance, and refuse a whole batch with an unparseable row."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import run  # noqa: E402

out = run.plan("sample_data/batch.jsonl", "sample_data/allowances.json")
assert out is not None
assert out[0].startswith("PLAN|1|SB-ALPHA|ACCEPT|used=4/5|write=book_bench("), out[0]
assert "ref='P-11'" in out[0], out[0]
assert out[1].startswith("PLAN|2|SB-BETA|ACCEPT|used=1/1|"), out[1]
assert out[2].startswith("PLAN|3|SB-ALPHA|ACCEPT|used=5/5|"), out[2]
assert out[3].startswith("PLAN|4|SB-GAMMA|ACCEPT|used=3/3|"), out[3]
assert out[4].startswith("CATCH: item 5 SB-DELTA refused"), out[4]
assert out[5].startswith("CATCH: item 6 SB-ALPHA refused"), out[5]
assert "5/5" in out[5], out[5]
assert len(out) == 6, out

bad = run.plan("sample_data/batch_bad.jsonl", "sample_data/allowances.json")
assert bad is None, "a batch with an unparseable row must be refused whole"

print("OK: running counts held, overshoot and unknown seed bank refused, "
      "broken batch refused whole")
