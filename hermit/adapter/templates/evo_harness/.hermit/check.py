"""Template check for `evo-harness` adapters: ADAPTER.md carries the block once, every skill has a usable frontmatter
(`name` equal to its directory, a one-line `description` Codex can parse), a topic skill's directory says its topic, and
the block names every skill, so that what the agent reads and what the Evolver keeps cannot drift apart."""
from __future__ import annotations

import json
from pathlib import Path
import re

BEGIN, END = "<!-- evo-harness:general -->", "<!-- /evo-harness:general -->"
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
TOPIC = re.compile(r"[a-z0-9-]+--[a-z0-9-]+")
MAX_NAME = 64                                          # Codex skips a skill with a longer name, in its log only


def _skill(d: Path, rel: str) -> list[str]:
    text = (d / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    m = re.match(r"---\nname: (.*)\ndescription: (.*)\n---\n", text)
    if not m:
        return [f"{rel}: the file must start with a frontmatter of `name` and `description`, one line each"]
    errs = []
    if m.group(1) != d.name:
        errs.append(f"{rel}: name {m.group(1)!r} is not its directory's")
    try:
        if not isinstance(json.loads(m.group(2)), str) or not json.loads(m.group(2)).strip():
            errs.append(f"{rel}: description must be a non-empty string")
    except ValueError:
        errs.append(f"{rel}: description must be JSON-quoted")
    return errs


def problems(root: Path) -> list[dict]:
    f = root / "ADAPTER.md"
    if not f.is_file():
        return [{"path": "ADAPTER.md", "problem": "ADAPTER.md must stay: it holds the skill block"}]
    text = f.read_text(encoding="utf-8", errors="replace")
    out = [{"path": "ADAPTER.md", "problem": f"ADAPTER.md must contain {m} exactly once (found {text.count(m)})"}
           for m in (BEGIN, END) if text.count(m) != 1]
    if out:
        return out
    if text.index(BEGIN) > text.index(END):
        return [{"path": "ADAPTER.md", "problem": f"{BEGIN} must come before {END}"}]
    block = text[text.index(BEGIN):text.index(END)]
    for level, pattern in (("general", NAME), ("skills", TOPIC)):
        for d in sorted(p for p in (root / level).glob("*") if p.is_dir()):
            rel = f"{level}/{d.name}/SKILL.md"
            if not pattern.fullmatch(d.name):
                out.append({"path": rel, "problem": f"directory name {d.name!r} does not fit {pattern.pattern}"})
            if level == "skills" and len(d.name) > MAX_NAME:
                out.append({"path": rel, "problem": f"name is longer than {MAX_NAME} characters: Codex would not load it"})
            if not (d / "SKILL.md").is_file():
                out.append({"path": rel, "problem": "a skill directory needs its SKILL.md"})
                continue
            out += [{"path": rel, "problem": p} for p in _skill(d, rel)]
            shown = f"### {d.name}\n" if level == "general" else f"`skills/{d.name}/SKILL.md`"
            if shown not in block:
                out.append({"path": "ADAPTER.md", "problem": f"the block does not name {rel}"})
    return out


def check(adapter_path: str) -> list[dict]:
    return problems(Path(adapter_path))
