"""Template check for `ace` adapters: ADAPTER.md carries ACE's playbook block, and the playbook is well formed.

ADAPTER.md:
    <any text>                       kept as it is (for example the task skill the adapter started from)
    <!-- ace:playbook -->            once
    <generator instructions>
    <!-- ace:bullets -->             once
    ## SECTION ...                   the playbook: `## ` section headers and bullets, one per line,
    [slug-00001] helpful=N harmful=N :: content          each id of the form slug-NNNNN and unique
    <!-- /ace:playbook -->           once
A line in the playbook that is neither is one ACE's parser would never read; a repeated id would make the Reflector's
tags and the counters ambiguous.
"""
from __future__ import annotations

from pathlib import Path
import re

BEGIN, BULLETS, END = "<!-- ace:playbook -->", "<!-- ace:bullets -->", "<!-- /ace:playbook -->"
LINE = re.compile(r"\[([^\]]+)\]\s*helpful=(\d+)\s*harmful=(\d+)\s*::\s*(.*)")
ID = re.compile(r"[a-z]+-\d{5}")


def problems(text: str) -> list[str]:
    errs = [f"ADAPTER.md must contain {m} exactly once (found {text.count(m)})"
            for m in (BEGIN, BULLETS, END) if text.count(m) != 1]
    if errs:
        return errs
    if not text.index(BEGIN) < text.index(BULLETS) < text.index(END):
        return [f"the markers must come in the order {BEGIN}, {BULLETS}, {END}"]
    body = text[text.index(BULLETS) + len(BULLETS):text.index(END)]
    seen = set()
    for n, line in enumerate(body.strip("\n").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = LINE.match(s)
        if not m:
            errs.append(f"playbook line {n} is neither a `## section` header nor a bullet "
                        f"`[id] helpful=N harmful=N :: content`: {s[:120]!r}")
        elif not ID.fullmatch(m.group(1)):
            errs.append(f"playbook line {n}: bullet id {m.group(1)!r} is not of the form slug-00001")
        elif m.group(1) in seen:
            errs.append(f"playbook line {n}: bullet id {m.group(1)} appears twice")
        else:
            seen.add(m.group(1))
        if len(errs) >= 10:
            break
    return errs


def check(adapter_path: str) -> list[dict]:
    f = Path(adapter_path) / "ADAPTER.md"
    if not f.is_file():
        return [{"path": "ADAPTER.md", "problem": "ADAPTER.md must stay: it holds the playbook"}]
    return [{"path": "ADAPTER.md", "problem": p} for p in problems(f.read_text(encoding="utf-8", errors="replace"))]
