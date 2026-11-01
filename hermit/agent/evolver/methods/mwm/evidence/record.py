"""Limits on the raw record the learner reads, shared across venues.

How much of the environment's response a learner can see is a property of the
METHOD's information channel, not of the venue. When each venue driver picked
its own number the method was differently informed at different venues, which
is a confound in exactly the comparison the venues exist to support — and the
numbers were undocumented, so nothing said whether a difference was deliberate.

The defect that produced this module: FlywheelBench clipped every tool
observation at 800 characters while the benchmark itself clips nothing, so the
host saw a `get_product_details` response in full (median 1649 characters, and
100% of them over 800) and the learner saw about half. The learner attributed
the cut to the environment, wrote "the flags sit past the 800-char observation
cap" into its claims, and spent eight blocks recording that the cap rather than
its own reasoning was the binding constraint on narrowing.

A venue may be stricter, but only in the driver, in the open, with the reason
written down — dabstep is, because its observations are code-execution output
with no natural bound.
"""

from __future__ import annotations

import os

# Per tool observation, in characters, in the RAW RECORD the learner reads.
# 2000 (user decision 2026-09-15): measured on banking b4, a third of the record's bytes were the
# host's own wm reads of memory entries copied back verbatim (one read up to
# 10,552 chars), and the learner re-read them after having read the same
# entries in the repository. Every venue's raw writer clips through
# clip_obs(); the host itself sees the full observation.
OBS_CAP = int(os.environ.get("MW_OBS_CAP", "2000"))


def clip_obs(text, cap: int = None) -> str:
    """The observation as the record keeps it: the first `cap` characters
    and a line saying how much was left out."""
    s = text if isinstance(text, str) else str(text)
    cap = OBS_CAP if cap is None else cap
    if len(s) <= cap:
        return s
    return s[:cap] + f"\n\n[... {len(s) - cap} more characters truncated]"

