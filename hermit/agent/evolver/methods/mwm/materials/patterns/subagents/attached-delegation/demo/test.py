#!/usr/bin/env python3
"""Asserts the protocol: the attachment rides verbatim, the delegate
reports the two planted contradictions, and the same delegate given only
the retelling answers the wrong problem confidently."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run  # noqa: E402

# The SPEC's first line is the trigger line.
spec_first = open(os.path.join(run.DATA, "triage.spec")).readline()
assert spec_first.startswith("trigger:"), spec_first

# With the attachment: the raw log text is inside the request verbatim,
# and the delegate holds the claim, reporting both contradictions.
request, reply, bare = run.main()
log_body = open(os.path.join(run.DATA, "germination_log.txt")).read()
assert log_body in request, "attachment must ride along verbatim"
assert "RAW MATERIAL: germination_log.txt" in request
assert reply.startswith("TRIAGE: hold"), reply
assert reply.count("CONTRADICTION:") == 2, reply
assert "9 trays" in reply and "2" in reply, reply

# Without the attachment: the failure this form prevents — a confident
# triage of the misstated claim, no contradiction visible.
assert "CONTRADICTION" not in bare, bare
assert bare.startswith("TRIAGE: accept 9 trays"), bare

print("OK: attachment carried verbatim; contradictions reported; "
     "retelling-only call shows the prevented failure")
