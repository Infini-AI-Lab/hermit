# Derived from google-research/harness2 core/selection.py, Copyright 2026 Google LLC, Apache-2.0.
"""Anonymised ids for blind selection and the parsers of what the selector and the reflector reply."""
from __future__ import annotations

import hashlib
import random
import re
from typing import Sequence

_CHOICE = re.compile(r"```choice[ \t]*\n?\s*([^\n`]+?)\s*\n?\s*```", re.S)
_WHY = re.compile(r"<why>(.*?)</why>", re.S)
_EXP = re.compile(r"<experience>(.*?)</experience>", re.S)
_EDITS = re.compile(r"<edits>(.*?)</edits>", re.S)
_NOTES = re.compile(r"<notes>(.*?)</notes>", re.S)


def anon_id(index: int) -> str:
    """A..Z, AA..AZ, BA... -- always accepted by the choice parser."""
    out = ""
    n = int(index) + 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def assign_ids(labels: Sequence[str], *, seed_key: str) -> dict[str, str]:
    """anon id -> label, by canonical sort then a seeded shuffle: stable across reruns."""
    seed = int(hashlib.sha1(seed_key.encode()).hexdigest()[:12], 16)
    ordered = sorted(labels)
    random.Random(seed).shuffle(ordered)
    return {anon_id(i): lbl for i, lbl in enumerate(ordered)}


def _normal_id(raw: str) -> str:
    rid = raw.strip().strip("*`'\"").strip()
    rid = re.sub(r"(?i)^candidate[ _-]*", "", rid)
    rid = re.sub(r"(?i)\.md$", "", rid)
    return rid.strip().upper()


def parse_choice(text: str, id_map: dict[str, str]) -> tuple[str, str]:
    """(anon id, why) of a selector reply: the LAST ```choice block (a corrected choice wins), with decorations
    such as **B**, `B`, b or B.md tolerated. ValueError when it chose nothing or something unknown."""
    found = _CHOICE.findall(text or "")
    if not found:
        raise ValueError("no ```choice block in the reply")
    anon = _normal_id(found[-1])
    if anon not in id_map:
        raise ValueError(f"chose {found[-1]!r}, which is not one of {', '.join(sorted(id_map))}")
    w = _WHY.findall(text)
    return anon, (w[-1].strip() if w else "")[:2000]


def parse_notes(text: str) -> str:
    """The reply's <notes> block; without one, the reply minus its file blocks."""
    found = _NOTES.findall(text or "")
    if found:
        return found[-1].strip()
    stripped = re.sub(r"(?ms)^[ \t]*<<<FILE.*?^[ \t]*(?:FILE>>>|>>>FILE|<<<END>>>)[ \t]*$", "[file block]", text or "")
    return stripped.strip()


_KEY = re.compile(r"^\W*(cue|lesson|evidence|confidence)\W*:\s*(.*)$", re.I)


def parse_reflection(text: str) -> tuple[str, dict]:
    """(edit rows, experience) of a reflector reply. The experience is {} unless cue, lesson and evidence are all
    present; NO_ENTRY is a valid reply. Keys may be decorated (**cue**:, - Cue:) and values may continue on the
    following lines."""
    text = (text or "").replace("\r\n", "\n")
    edits = _EDITS.findall(text)
    rows = edits[-1].strip() if edits else ""
    blocks = _EXP.findall(text)
    if not blocks:
        return rows, {}
    out: dict = {}
    key = None
    for line in blocks[-1].strip().splitlines():
        m = _KEY.match(line)
        if m:
            key = m.group(1).lower()
            out[key] = m.group(2).strip()
        elif key and line.strip():
            out[key] = (out[key] + " " + line.strip()).strip()
    out = {k: v for k, v in out.items() if v}
    if not all(out.get(k) for k in ("cue", "lesson", "evidence")):
        return rows, {}
    if rows:
        out["edits"] = rows
    return rows, out


def has_experience(experience: dict | None) -> bool:
    return isinstance(experience, dict) and all(
        isinstance(experience.get(key), str) and experience[key].strip() for key in ("cue", "lesson", "evidence"))
