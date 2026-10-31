"""`trace` — the raw record of a past episode, for hosts (2026-09-08).

User decision: hosts may read raw history. One name for every venue,
one rolling window per call: wm(trace, {address, from}) returns the
record behind a record address — the same file the meta-agent reads under
/evidence — from turn `from` on, as much as fits under WINDOW_CHARS, and
says where to continue. The first line names the file, so a host that
can reach a filesystem may read it there instead.

Address resolution is venue-neutral: the file under /evidence/blockNNN/
whose stem is the address, or whose stem starts with the address's
letters followed by its number zero-padded to three digits (q17 ->
q017_id9.json, h3 -> h003.json, e12 -> e012.json).
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

WINDOW_CHARS = 30_000
_ADDR = re.compile(r"^([A-Za-z]+)(\d+)$")


def record_path(evidence_dir: str, address: str) -> Optional[str]:
    """The record file for an address, or None. Searches every blockNNN/
    (and the evidence root) for a stem match; the first block wins."""
    address = (address or "").strip()
    if not address or not os.path.isdir(evidence_dir):
        return None
    m = _ADDR.match(address)
    padded = f"{m.group(1)}{int(m.group(2)):03d}" if m else None
    dirs = [evidence_dir] + sorted(
        os.path.join(evidence_dir, d) for d in os.listdir(evidence_dir)
        if d.startswith("block") and os.path.isdir(os.path.join(evidence_dir, d)))
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in names:
            stem, ext = os.path.splitext(fn)
            if ext != ".json":
                continue
            if stem == address or (padded and (
                    stem == padded or stem.startswith(padded + "_"))):
                candidate = os.path.join(d, fn)
                base = os.path.realpath(evidence_dir)
                if os.path.realpath(candidate).startswith(base + os.sep):
                    return candidate
    return None


def _mount_path(evidence_dir: str, path: str) -> str:
    return "/evidence/" + os.path.relpath(path, evidence_dir).replace(os.sep, "/")


def _turn_text(i: int, t: dict) -> str:
    lines = [f"turn {i}" + (f" [by: {t['by']}]" if t.get("by") else "")]
    r = t.get("response")
    if r is not None:
        lines.append("action: " + json.dumps(r, ensure_ascii=False))
    if t.get("reasoning"):
        lines.append("reasoning: " + str(t["reasoning"]).strip())
    o = t.get("observation")
    if o is not None:
        c = o.get("content") if isinstance(o, dict) and "content" in o else o
        lines.append("environment: " + (c if isinstance(c, str)
                                        else json.dumps(c, ensure_ascii=False)))
    return "\n".join(lines)


def render(evidence_dir: str, address: str, from_turn: int = 0,
           cap: int = WINDOW_CHARS) -> str:
    """The rolling window: header, turns from `from_turn` while the text
    stays under `cap`, then a continuation line when turns remain."""
    path = record_path(evidence_dir, address)
    if path is None:
        return f"no record for address {address!r}"
    try:
        rec = json.load(open(path))
    except (OSError, ValueError) as exc:
        return f"trace error: cannot read the record ({exc})"
    mount = _mount_path(evidence_dir, path)
    if not isinstance(rec, list):
        # a record that is not a turn list (another venue's shape): the file
        # itself, under the same cap
        body = json.dumps(rec, ensure_ascii=False, indent=1)
        head = (f"record {address}. The file is {mount} — if you can reach a "
                f"filesystem, you can also read it there yourself.\n")
        if len(head) + len(body) > cap:
            return head + body[:cap - len(head) - 40] + "\n[truncated at the window cap]"
        return head + body
    n = len(rec)
    try:
        start = max(0, int(from_turn or 0))
    except (TypeError, ValueError):
        start = 0
    head = (f"record {address}: {n} turns. The file is {mount} — if you can "
            f"reach a filesystem, you can also read it there yourself.")
    out = [head]
    size = len(head)
    i = start
    while i < n:
        block = _turn_text(i, rec[i] if isinstance(rec[i], dict) else {"response": rec[i]})
        if size + len(block) + 2 > cap and i > start:
            break
        if size + len(block) + 2 > cap:
            # one turn larger than the window: cut it rather than return nothing
            block = block[:cap - size - 60] + "\n[turn cut at the window cap]"
            out.append(block)
            i += 1
            break
        out.append(block)
        size += len(block) + 2
        i += 1
    if start >= n:
        out.append(f"no turn {start}: the record has {n} turns (0–{n - 1})")
    elif i < n:
        out.append(f"returned turns {start}–{i - 1} of {n}; continue with from={i}")
    return "\n\n".join(out)
