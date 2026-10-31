"""The host agent's deployments besides `hermit deploy`'s (`agent.params.deploy`):

tree    a Codex tree as upstream installs it (google-research/harness2, `CodexSubstrate`): `AGENTS.md`, `.agents/`,
        `scripts/`, `.codex/` and `memory/` at the workspace root, and `sub_agents/<name>/AGENT.md` as agent roles
        in the session's home. An adapter that is context rather than a tree (no `AGENTS.md`: the prompt-only and
        reharness templates) gets `AGENTS.md` holding its context for the task.
files   the adapter as it is: its context (`ADAPTER.md`, or what its context tool renders for the task) as
        `AGENTS.md`, and every other file outside `.hermit/` at the same path in the workspace. A root name the
        session uses (`AGENTS.md`, `.codex/`, and the task's `reserved` names) is refused, so that a run never uses a
        silently different adapter.

Each returns what it deployed; the agent records it with the run.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil

TREE_ENTRIES = ("AGENTS.md", ".agents", "scripts", ".codex",      # upstream's codex_root install (CODEX_ROOT_ENTRIES)
                "memory")                                         # the continual-harness template's notes
FILES_SKIP = (".hermit", ".git", "ADAPTER.md", "__pycache__")
FILES_RESERVED = ("AGENTS.md", ".codex")      # the session's instructions; Codex's project configuration and hooks


def frontmatter(text: str) -> tuple[dict[str, str], str]:
    """A file's YAML-ish frontmatter as strings, and its body ({} without one)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    head: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and re.fullmatch(r"[A-Za-z_][\w-]*", key.strip()):
            value = value.strip()
            if value.startswith('"') and value.endswith('"') and len(value) >= 2:   # a double-quoted YAML scalar
                try:
                    value = str(json.loads(value))
                except ValueError:
                    value = value.strip('"')
            else:
                value = value.strip("\"'")
            head[key.strip()] = value
    body = text[end + 4:]
    return head, body[body.find("\n") + 1:] if "\n" in body else ""


def _write_context(work: Path, context: str | None) -> bool:
    if context and context.strip():
        (work / "AGENTS.md").write_text(context.rstrip("\n") + "\n", encoding="utf-8")
        return True
    return False


def tree(adapter: Path | None, work: Path, context: str | None = None) -> dict:
    """The adapter's Codex tree into the workspace root; `sub_agents` (name, description, instructions) are returned
    for the session's home. `context` becomes `AGENTS.md` when the adapter has none."""
    out: dict = {"agents_md": False, "skills": [], "scripts": [], "hooks": False, "sub_agents": [], "memory": False,
                 "context": False}
    if adapter is None:
        return out
    adapter = Path(adapter)
    for entry in TREE_ENTRIES:
        src = adapter / entry
        if src.is_dir():
            shutil.copytree(src, work / entry, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif src.is_file():
            shutil.copy2(src, work / entry)
    if not (work / "AGENTS.md").is_file():
        out["context"] = _write_context(work, context)
    out["agents_md"] = (work / "AGENTS.md").is_file()
    out["skills"] = sorted(p.parent.name for p in (work / ".agents" / "skills").glob("*/SKILL.md"))
    out["scripts"] = sorted(p.name for p in (work / "scripts").glob("*.py"))
    out["hooks"] = (work / ".codex" / "hooks.json").is_file()
    out["memory"] = (work / "memory" / "NOTES.md").is_file()
    for agent_md in sorted((adapter / "sub_agents").glob("*/AGENT.md")):
        meta, body = frontmatter(agent_md.read_text(encoding="utf-8"))
        name = re.sub(r"[^a-z0-9_]", "_", agent_md.parent.name.lower())
        out["sub_agents"].append({"name": name, "description": meta.get("description") or name,
                                  "instructions": body.strip()})
    return out


def files(adapter: Path | None, work: Path, context: str | None = None, reserved: tuple = ()) -> dict:
    """The adapter as it is; `context` (the task's context: ADAPTER.md and what a context tool renders) is
    `AGENTS.md`, else `ADAPTER.md` itself."""
    out: dict = {"agents_md": False, "files": [], "sub_agents": [], "memory": False, "hooks": False}
    if adapter is None:
        return out
    adapter = Path(adapter)
    entries = sorted(p for p in adapter.iterdir() if p.name not in FILES_SKIP)
    taken = [p.name for p in entries if p.name in (*FILES_RESERVED, *reserved)]
    if taken:
        raise ValueError(f"the adapter has {', '.join(taken)} at its root, a name the session uses: rename it")
    doc = adapter / "ADAPTER.md"
    if _write_context(work, context):
        out["agents_md"] = True
    elif doc.is_file() and doc.read_text(encoding="utf-8").strip():
        shutil.copy2(doc, work / "AGENTS.md")
        out["agents_md"] = True
    for src in entries:
        if src.is_dir():
            shutil.copytree(src, work / src.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, work / src.name)
    out["files"] = sorted(str(p.relative_to(work)) for p in work.rglob("*") if p.is_file() and p.name != "AGENTS.md")
    return out
