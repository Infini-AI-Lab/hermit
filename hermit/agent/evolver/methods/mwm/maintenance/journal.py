"""Wrapper-managed journal rolling (V8.5).

The journal is the model's OPEN STATE, not a chronicle — but v83 measured
82% chronicle (build lists, stamp confirmations, bookkeeping) and 73-153KB
files that every opening re-reads. Self-management was already the failure
mode of the comment files, so the discipline lives in the HARNESS:

  meta/journal.md = non-session preamble sections (seed text)
                  + "## Older sessions (digests)"  — one line per rolled
                    session, authored by the session itself (its Digest:
                    line; first content line as mechanical fallback)
                  + the last KEEP session entries in full

Rolled entries' full text moves to meta/journal-archive.md (readable on
demand, never required reading); git additionally keeps every version.
The roller runs before each staged session, in the same wrapper commit
as the agenda render, so nothing here is attributed to the learner.
"""

from __future__ import annotations

import os
import re
from typing import List

KEEP = 5
DIGEST_HEAD = "## Older sessions (digests)"
# Tolerant of "## Session block023" and zero padding: one v91 learner
# rendered the contract's `<block>` placeholder as the block LABEL and its
# journal never rolled (2026-08-27 incident). The number is what matters.
_SESSION = re.compile(r"^## Session (?:block\s*)?0*(\d+)\b", re.I)

_OPENISH = re.compile(r"\b(open|unresolved|pending|awaiting|candidate)\b",
                      re.I)


def _hyp_enabled() -> bool:
    from . import hypothesis as _H
    return _H.ENABLED


def _docket_enabled() -> bool:
    from . import docket as _D
    return _D.ENABLED


def _digest(n: int, body: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s.lower().startswith("digest:"):
            return f"- Session {n}: {s[7:].strip()}"
    first = next((l.lstrip('-# ').strip()
                  for l in body.splitlines() if l.strip()), "")
    return f"- Session {n}: {first[:160]}"


def roll(wrepo: str, keep: int = KEEP) -> List[str]:
    """Backwards-compatible wrapper: roll and discard the notice."""
    return roll_with_notice(wrepo, keep)[0]


def roll_with_notice(wrepo: str, keep: int = KEEP):
    """Collapse all but the newest `keep` session entries to digest lines.

    Returns (changed_paths, notice). `notice` is one sentence when an
    entry being archived still mentions open-ish work — the last call
    before that text stops being required reading; empty otherwise.
    Non-session sections and text before the first header stay in place;
    a learner writing several `## Session N` blocks for one N rolls them
    together (each contributes a digest line).
    """
    p = os.path.join(wrepo, "meta", "journal.md")
    try:
        text = open(p).read()
    except OSError:
        return [], ""
    parts = re.split(r"(?m)^(## .+)\n?", text)
    pre = parts[0]
    segs = [(parts[i], parts[i + 1] if i + 1 < len(parts) else "")
            for i in range(1, len(parts), 2)]
    sess, others, old_digests = [], [], ""
    for h, b in segs:
        m = _SESSION.match(h)
        if m:
            sess.append((int(m.group(1)), h, b))
        elif h.strip() == DIGEST_HEAD:
            old_digests += b
        else:
            others.append((h, b))
    numbers = sorted({n for n, _h, _b in sess})
    if len(numbers) <= keep:
        return [], ""
    cutoff = numbers[-keep]
    old = [(n, h, b) for n, h, b in sess if n < cutoff]
    recent = [(n, h, b) for n, h, b in sess if n >= cutoff]
    if not old:
        return [], ""
    apath = os.path.join(wrepo, "meta", "journal-archive.md")
    with open(apath, "a") as f:
        for n, h, b in sorted(old):
            f.write(h + "\n" + b.rstrip("\n") + "\n\n")
    out = pre
    for h, b in others:
        out += h + "\n" + b
    digests = (old_digests.rstrip("\n") + "\n" if old_digests.strip() else "")
    digests += "".join(_digest(n, b) + "\n" for n, _h, b in sorted(old))
    out += DIGEST_HEAD + "\n" + digests + "\n"
    for n, h, b in sorted(recent):
        out += h + "\n" + b
    if not out.endswith("\n"):
        out += "\n"
    open(p, "w").write(out)
    def _live_open(body: str) -> bool:
        # Closure lines legitimately contain the word "open" ("RESOLVED
        # open items: ..."); only non-closure lines count as live.
        return any(_OPENISH.search(l) for l in body.splitlines()
                   if "RESOLVED" not in l and "resolved" not in l)
    flagged = sorted({n for n, _h, b in old if _live_open(b)})
    notice = ""
    if flagged:
        notice = (f"Roll notice: archived session entr"
                  f"{'y' if len(flagged) == 1 else 'ies'} "
                  f"{', '.join(str(n) for n in flagged)} still mention"
                  f"{'s' if len(flagged) == 1 else ''} open/unresolved "
                  f"work — if any of it is a live question, it must "
                  + (f"be an open hypothesis under meta/hypothesis/ "
                     if _hyp_enabled() else
                     f"have its own file under meta/cases/ "
                     if _docket_enabled() else
                     f"live in its memory entry as a bracket or candidate "
                     f"set ")
                  + f"(archived text is no longer required reading).")
    return ["meta/journal.md", "meta/journal-archive.md"], notice
