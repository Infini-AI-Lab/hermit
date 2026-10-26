"""The continual_harness adapter's interface: how an acting agent *reaches* the evolved harness.

The Continual Harness Refiner maintains four learnable parts — base policy `p` (policy.md), sub-agents `G`
(subagents.md), skills `K` (skills.md), memory `M` (memory.md) — and re-renders them into ADAPTER.md, which is the
agent's context (the *push* channel: the whole harness, baked into the system prompt).

This file adds the *pull* channel and sub-agent dispatch, so the harness is not just text the agent reads but
things the agent invokes — using hermit's own export hooks (`exports.tools` / `exports.agents`) and nothing else.
It is **self-contained**: it reads only its own adapter files (next to this module) and imports no other template's
code.

- `harness(name, args)` — the tool the agent calls at act time to pull one part of the harness on demand:
    name="skills"    -> the skills K      (optionally filtered by args.query)
    name="memory"    -> the memory M      (optionally filtered by args.query)
    name="subagents" -> the sub-agents G  catalogue
    name="list"      -> an overview: which parts are populated and the headings of their entries
- `agent_definitions()` — exposes each sub-agent in subagents.md as a hermit sub-agent the agent can delegate to.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PARTS = {"skills": "skills.md", "memory": "memory.md", "subagents": "subagents.md"}
_EMPTY_PREFIX = ("(no ", "(none")


def _slug(text: str) -> str:
    """A valid hermit sub-agent/role name (hosts require `[a-z][a-z0-9-]{0,63}`): lowercase, runs of other
    characters collapsed to a single hyphen, must start with a letter. Empty if the heading has no usable letters."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    s = re.sub(r"^[^a-z]+", "", s)
    return s[:64].rstrip("-")


def _read(part: str) -> str:
    path = ROOT / PARTS[part]
    return path.read_text() if path.exists() else ""


def _is_empty(text: str) -> bool:
    """A seed/placeholder part ("(No skills yet.)") counts as empty, so the agent is not handed an empty section."""
    return not text.strip() or text.strip().lower().startswith(_EMPTY_PREFIX)


def _sections(text: str) -> list[dict]:
    """Split a part into {heading, level, body} sections by Markdown headings, ignoring any `#` line that falls inside
    a ``` fenced code block (so a shell comment like `# build the index` is not mistaken for a heading). `level` is
    the number of leading `#` (so entries written as `## name` are level 2); text before the first heading becomes a
    leading section with heading None and level 0."""
    sections, heading, level, buf, in_fence = [], None, 0, [], False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            buf.append(line)
            continue
        if not in_fence and stripped.startswith("#"):
            if heading is not None or any(b.strip() for b in buf):
                sections.append({"heading": heading, "level": level, "body": "\n".join(buf).strip()})
            level = len(stripped) - len(stripped.lstrip("#"))
            heading = stripped.lstrip("#").strip()
            buf = []
        else:
            buf.append(line)
    if heading is not None or any(b.strip() for b in buf):
        sections.append({"heading": heading, "level": level, "body": "\n".join(buf).strip()})
    return sections


def harness(name: str, args: dict | None = None) -> dict:
    """Pull one part of the harness. Returns {component, populated, content, ...}; `list` returns {overview}."""
    args = args or {}
    if name == "list":
        overview = {}
        for part in PARTS:
            text = _read(part)
            overview[part] = {"populated": not _is_empty(text),
                              "entries": [s["heading"] for s in _sections(text) if s["level"] == 2]
                              if not _is_empty(text) else []}
        return {"component": "list", "overview": overview}
    if name not in PARTS:
        return {"error": f"unknown part {name!r}; use one of: skills, memory, subagents, list"}
    text = _read(name)
    if _is_empty(text):
        return {"component": name, "populated": False, "content": ""}
    query = (args.get("query") or "").strip().lower()
    if query:
        hits = [s for s in _sections(text)
                if query in (s["heading"] or "").lower() or query in s["body"].lower()]
        if hits:
            content = "\n\n".join(f"## {s['heading']}\n{s['body']}" if s["heading"] else s["body"] for s in hits)
            return {"component": name, "populated": True, "query": query, "matched": len(hits), "content": content}
        return {"component": name, "populated": True, "query": query, "matched": 0, "content": "",
                "note": "no section matched; call without a query to get the whole part"}
    return {"component": name, "populated": True, "content": text.strip()}


def agent_definitions() -> list[dict]:
    """Each sub-agent in subagents.md (a `## name` section) becomes a hermit sub-agent: name = the heading slugified
    to the `[a-z][a-z0-9-]{0,63}` a host requires, description = its first non-empty line, body = the section (the
    playbook). Sections whose heading has no usable letters, or with no body, are skipped; an empty/seed
    subagents.md -> none."""
    text = _read("subagents")
    if _is_empty(text):
        return []
    out, seen = [], set()
    for section in _sections(text):
        if section["level"] != 2:          # only `## name` entries are sub-agents (not a `# Title` or leading text)
            continue
        heading, body = section["heading"], section["body"]
        if not heading or not body:
            continue
        name = _slug(heading)
        if not name or name in seen:
            continue
        description = next((line.strip() for line in body.splitlines() if line.strip()), "")
        if not description:
            continue
        seen.add(name)
        out.append({"name": name, "description": description, "body": body})
    return out
