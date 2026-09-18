#!/usr/bin/env python3
"""The classifier must accept the clean rows, reject the over-cap row,
hold the untagged wilted row, refuse the retyped row with a CATCH
line, and refuse the whole run when the rules file is unparseable."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import run  # noqa: E402

out = run.main("sample_data/germination_log.txt", "sample_data/rules.json")
assert out is not None
assert out[0].startswith("VERDICT|G-001|ACCEPT|"), out[0]
assert out[1].startswith("VERDICT|G-002|ACCEPT|"), out[1]
assert out[2].startswith("VERDICT|G-003|REJECT|"), out[2]
assert out[3].startswith("CATCH: line 4"), out[3]
assert out[4].startswith("VERDICT|G-004|HOLD|"), out[4]
assert out[5].startswith("VERDICT|G-005|ACCEPT|"), out[5]
assert len(out) == 6, out

bad = run.main("sample_data/germination_log.txt", "sample_data/rules_bad.json")
assert bad is None, "unparseable rules must refuse the whole run"

print("OK: verdicts correct; retyped row and broken rules both refused")
