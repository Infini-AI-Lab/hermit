#!/usr/bin/env python3
"""Asserts the protocol: verdict grammar parses, unparseable output
falls back to cannot-tell (which never confirms), and a mis-zoned
proposal is CAUGHT by two agreeing independent verdicts."""

import io
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run  # noqa: E402

# Grammar parses; unparseable output IS cannot-tell.
assert run.parse_verdict("VERDICT: agree") == ("agree", "")
assert run.parse_verdict("VERDICT: differ C-4") == ("differ", "C-4")
assert run.parse_verdict("VERDICT: cannot-tell no deciding field") == \
    ("cannot-tell", "no deciding field")
assert run.parse_verdict("Well, it is hard to say...") == \
    ("cannot-tell", "unparseable reply")
assert run.parse_verdict("") == ("cannot-tell", "unparseable reply")

buf = io.StringIO()
with redirect_stdout(buf):
    sound, caught, garbled = run.main()
printed = buf.getvalue()

# Sound proposal: both samples agree, step confirmed.
assert sound[0] == "confirmed" and sound[1] == "A-6", sound

# Mis-zoned proposal: two independent verdicts agree on a different
# value and outrank the proposer — a CATCH with the derived bench.
assert caught[0] == "CATCH" and caught[1] == "C-4", caught
assert all(v == ("differ", "C-4") for v in caught[2]), caught
assert "CATCH: proposal A-6 overruled by 2 independent verdicts: C-4" \
    in printed, printed

# Missing deciding field: the stub rambles, the parser falls back to
# cannot-tell, and cannot-tell never confirms — the step is unverified.
assert garbled[0] == "unverified", garbled
assert all(v == ("cannot-tell", "unparseable reply") for v in garbled[2])

print("OK: grammar parsed, unparseable fell back to cannot-tell, "
      "divergence caught by two agreeing verdicts")
