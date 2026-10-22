#!/usr/bin/env python3
"""The gate must print GO only when every checklist item is satisfied,
list ALL missing items on a block, refuse unknown action types, and
treat unparseable facts as NO-GO."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import run  # noqa: E402

CL = "sample_data/checklists.json"

go = run.main(CL, "sample_data/facts_go.json")
assert go == ("GO|transplant|satisfied=bed_prepared,hardening_off_done,"
              "pest_check_passed"), go

blocked = run.main(CL, "sample_data/facts_missing.json")
assert blocked.startswith("CATCH: transplant blocked - missing:"), blocked
assert "hardening_off_done" in blocked and "pest_check_passed" in blocked, blocked
assert "bed_prepared" not in blocked, blocked

unknown = run.main(CL, "sample_data/facts_unknown.json")
assert unknown.startswith("CATCH: unknown action type 'repaint'"), unknown

bad = run.main(CL, "sample_data/facts_bad.json")
assert bad.startswith("CATCH: facts input unparseable"), bad

print("OK: GO on full checklist; missing, unknown, and unparseable "
      "all resolve to NO-GO")
