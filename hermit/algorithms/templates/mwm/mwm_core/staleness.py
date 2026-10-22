"""Which memory entries have gone unverified for a long stretch.

For every memory entry it reads how long since it was last confirmed (its
own last_confirmed_t)
and lists the ones not confirmed for a long stretch. That is all it does
since 2026-09-06 (Part 3.3, user rule): it reads the world model only —
the learner's own entry files — never the evidence record. The former
evidence-side half (outcome rates of the episodes an entry's scope
matched) was removed with every other wrapper-computed statistic over the
record; the learner computes such things itself.
"""

from __future__ import annotations

import os
import re
from typing import List

OLD_TICKS_FRAC = 0.4      # "old" = last confirmed more than 40% of the run ago




def _entries(wrepo: str):
    """(name, body, last_confirmed_t) for every live memory entry."""
    from .wm_tools import memory_dir
    cdir = str(memory_dir(wrepo))
    out = []
    for fn in sorted(os.listdir(cdir)) if os.path.isdir(cdir) else []:
        if not fn.endswith(".md") or fn.startswith("_"):
            continue
        body = open(os.path.join(cdir, fn)).read()
        st = re.search(r"^status:\s*(\w+)", body, re.M)
        if st and st.group(1) == "retired":
            continue
        m = re.search(r"^last_confirmed_t:\s*(\d+)", body, re.M)
        if not m:
            continue
        out.append((fn[:-3], body, int(m.group(1))))
    return out


def age_lines(wrepo: str, now_t: int) -> List[str]:
    """GENERAL (reads the world model only): entries whose last confirmation
    is more than OLD_TICKS_FRAC of the run ago. Rendered in the shared
    status block for every venue; "" when now_t is unknown."""
    if not now_t:
        return []
    out = []
    for name, _body, last in _entries(wrepo):
        age = now_t - last
        if age > OLD_TICKS_FRAC * max(now_t, 1):
            out.append(f"  {name}: last confirmed t={last} ({age} ticks "
                       f"ago), unverified since")
    return out[:12]
