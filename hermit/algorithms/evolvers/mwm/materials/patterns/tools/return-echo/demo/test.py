#!/usr/bin/env python3
"""Every tool return must END with an echo block that reflects the
state as of THAT return (freshness), and a corrupted worksheet must
make the next call refuse instead of echoing stale memory."""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import run  # noqa: E402

work = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
shutil.copy("sample_data/worksheet_start.json", work)

out1 = run.tool_log_sowing(work, "TB-9", "2 trays chives")
lines1 = out1.split("\n")
assert lines1[-1] == run.ECHO_END and run.ECHO_TOP in out1, out1
assert "open: 2 ['TB-7', 'TB-9']" in out1, out1
assert "last: log_sowing(TB-9)" in out1, out1

out2 = run.tool_mark_benched(work, "TB-7")
lines2 = out2.split("\n")
assert lines2[-1] == run.ECHO_END, out2
# freshness: the echo reflects THIS call's change, not the earlier state
assert "open: 1 ['TB-9']" in out2, out2
assert "benched: 1" in out2, out2
assert "last: mark_benched(TB-7)" in out2, out2

with open(work, "w") as f:
    f.write("{ corrupted mid-write")
out3 = run.tool_mark_benched(work, "TB-9")
assert out3.startswith("CATCH: worksheet unreadable"), out3
assert run.ECHO_TOP not in out3, "must not echo stale state"

os.unlink(work)
print("OK: echo present and fresh on every return; corrupt worksheet refused")
