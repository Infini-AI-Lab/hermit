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

from pathlib import Path

ROOT = Path(__file__).resolve().parent
PARTS = {"skills": "skills.md", "memory": "memory.md", "subagents": "subagents.md"}
_EMPTY_PREFIX = ("(no ", "(none")


def _read(part: str) -> str:
    path = ROOT / PARTS[part]
    return path.read_text() if path.exists() else ""


def _is_empty(text: str) -> bool:
    """A seed/placeholder part ("(No skills yet.)") counts as empty, so the agent is not handed an empty section."""
    return not text.strip() or text.strip().lower().startswith(_EMPTY_PREFIX)


def _sections(text: str) -> list[dict]:
    """Split a part into {heading, body} sections by Markdown headings (#, ##, ###). Any text before the first
    heading becomes a leading section with heading None."""
    sections, heading, buf = [], None, []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            if heading is not None or any(b.strip() for b in buf):
                sections.append({"heading": heading, "body": "\n".join(buf).strip()})
            heading = line.lstrip().lstrip("#").strip()
            buf = []
        else:
            buf.append(line)
    if heading is not None or any(b.strip() for b in buf):
        sections.append({"heading": heading, "body": "\n".join(buf).strip()})
    return sections


def harness(name: str, args: dict | None = None) -> dict:
    """Pull one part of the harness. Returns {component, populated, content, ...}; `list` returns {overview}."""
    args = args or {}
    if name == "list":
        overview = {}
        for part in PARTS:
            text = _read(part)
            overview[part] = {"populated": not _is_empty(text),
                              "entries": [s["heading"] for s in _sections(text) if s["heading"]]
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
    """Each sub-agent in subagents.md (a `## name` section) becomes a hermit sub-agent: name = the heading,
    description = its first non-empty line, body = the section (the playbook). An empty/seed subagents.md -> none."""
    text = _read("subagents")
    if _is_empty(text):
        return []
    out, seen = [], set()
    for section in _sections(text):
        name, body = section["heading"], section["body"]
        if not name or not body or name in seen:
            continue
        description = next((line.strip() for line in body.splitlines() if line.strip()), "")
        if not description:
            continue
        seen.add(name)
        out.append({"name": name, "description": description, "body": body})
    return out
