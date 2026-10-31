#!/usr/bin/env python3
"""windowed-output demo: searching a large germination log, rationed.

The search tool paginates its own output: at most WINDOW hits per
return, a MORE line naming both escape routes (narrow, or next page),
and a HARD_CAP beyond which the query is refused with guidance instead
of executed. Empty queries and out-of-range pages are refused.

Usage: python3 run.py <query> [page]
"""

import math
import sys

LOG = "sample_data/germination_log.txt"
WINDOW = 10
HARD_CAP = 60


def search(log_path, query, page=1):
    if not isinstance(query, str) or not query.strip():
        return ["CATCH: empty or non-text query; refusing "
                "(state a substring to look for)"]
    if not isinstance(page, int) or page < 1:
        return [f"CATCH: page must be a positive integer, got {page!r}"]
    rows = [ln.rstrip("\n") for ln in open(log_path) if ln.strip()]
    matches = [r for r in rows if query in r]
    if len(matches) > HARD_CAP:
        return [f"CATCH: query {query!r} matches {len(matches)} rows "
                f"(cap {HARD_CAP}); refusing to dump - narrow the query"]
    if not matches:
        return [f"PAGE|1/1|0 matches for {query!r}"]
    pages = math.ceil(len(matches) / WINDOW)
    if page > pages:
        return [f"CATCH: page {page} out of range; "
                f"{query!r} has pages 1-{pages}"]
    start = (page - 1) * WINDOW
    window = matches[start:start + WINDOW]
    out = [f"PAGE|{page}/{pages}|showing {start + 1}-{start + len(window)} "
           f"of {len(matches)}"]
    out += [f"HIT|{r}" for r in window]
    remaining = len(matches) - (start + len(window))
    if remaining:
        out.append(f"MORE|{remaining} more; narrow the query or "
                   f"request page {page + 1}")
    return out


def main(query, page=1):
    out = search(LOG, query, page)
    for line in out:
        print(line)
    return out


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "SB-ALPHA"
    p = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    sys.exit(1 if main(q, p)[0].startswith("CATCH:") else 0)
