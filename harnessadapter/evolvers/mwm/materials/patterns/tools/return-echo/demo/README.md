# Demo: two toy tools sharing a worksheet echo

`python3 test.py` — a nursery hand logs a newly sown tray batch, then
marks an earlier one benched. Both tool returns end with the same
compact echo block (open batch ids, benched count, last action), and the
second return's echo already reflects the first call's change —
freshness by construction, because the echo renders from the worksheet
at return time. The test then corrupts the worksheet file and asserts
the next call refuses with a CATCH line instead of echoing stale memory.

Form (keep when re-domaining): the `with_echo` funnel every tool return
passes through, rendering from persistent state at return time, the
compact fixed-shape block, and refuse-on-unreadable.

Content (replace): the worksheet fields, the two tool verbs, and which
three or four numbers your domain's decisions actually hinge on — echo
those, and nothing more.
