"""Template check for `continual-harness` adapters: the tree is the Continual Harness state H = (p, G, K, M) in the
layout Codex reads, nothing lies off that surface, and ADAPTER.md is AGENTS.md (the framework's context export is
the always-on text; AGENTS.md is the file Codex reads).

The surface:
    AGENTS.md                              p, the one always-on file: at most 8,000 characters, never empty
    .agents/skills/<name>/SKILL.md         K, frontmatter `description:` required; `name:` optional, must match the dir
    .agents/skills/<name>/scripts/<file>   a helper carried with its skill; a .py must compile
    sub_agents/<name>/AGENT.md             G, frontmatter `description:` required
    memory/NOTES.md                        M, notes under `## <topic>` headings; present, possibly a placeholder
A script a skill or AGENTS.md invokes must be in the tree. The on-demand directories' README.md placeholders are
not part of the tree. The rules for skills and sub-agents are Codex's own loading rules (a skill without a
description is dropped silently; a `name:` that differs from the directory renames it at load).
"""
from __future__ import annotations

from pathlib import Path
import re

ALWAYS_ON = "AGENTS.md"
ALWAYS_ON_MAX = 8_000
MEMORY = "memory/NOTES.md"
# The framework's and the template's own files, and the placeholder READMEs: not part of the harness tree.
OWN = frozenset({"ADAPTER.md", "adapter.yaml", "CARD.md", "check.py", "EVOLVER.md", "README.md"})
STUBS = frozenset({"sub_agents/README.md", ".agents/skills/README.md"})
NAME = r"[a-z0-9][a-z0-9_\-]*"
RX = {"skill": re.compile(rf"\.agents/skills/({NAME})/SKILL\.md"),
      "skill_script": re.compile(rf"\.agents/skills/{NAME}/scripts/[A-Za-z0-9_.\-]+"),
      "subagent": re.compile(rf"sub_agents/({NAME})/AGENT\.md"),
      "script_ref": re.compile(r"(?:python3?\s+)(\.agents/skills/[\w\-]+/scripts/[\w.\-]+\.py)")}


def allowed(rel: str) -> bool:
    return rel in (ALWAYS_ON, MEMORY) or any(RX[k].fullmatch(rel) for k in ("skill", "skill_script", "subagent"))


def read_tree(adapter: Path) -> tuple[dict[str, str], list[str]]:
    """rel -> text for every file on the surface, and the files that are off it."""
    tree, off = {}, []
    for p in sorted(adapter.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or ".git" in p.parts or p.suffix == ".pyc":
            continue
        rel = p.relative_to(adapter).as_posix()
        if rel in OWN or rel in STUBS:
            continue
        if allowed(rel):
            tree[rel] = p.read_text(encoding="utf-8", errors="replace")
        else:
            off.append(rel)
    return tree, off


def problems(tree: dict[str, str]) -> list[str]:
    """Every problem of the tree, in actionable prose."""
    errs: list[str] = []
    body = tree.get(ALWAYS_ON, "")
    if len(body) > ALWAYS_ON_MAX:
        errs.append(f"AGENTS.md is {len(body)} chars, over the {ALWAYS_ON_MAX} cap; it is injected whole into EVERY "
                    f"turn, and Codex truncates it silently at 32,768 bytes")
    if not body.strip():
        errs.append("AGENTS.md is empty: it is the only always-on file, so an empty one is a harness that does not exist")
    if MEMORY not in tree:
        errs.append(f"{MEMORY} is missing: the memory is part of the harness even when it holds no note yet")
    for rel in sorted(tree):
        m = RX["skill"].fullmatch(rel)
        if not m:
            continue
        name, fm = m.group(1), tree[rel][:600]
        if not re.search(r"^description:\s*\S", fm, re.M):
            errs.append(f"{rel} needs a `description:` in its frontmatter, or the skill is dropped SILENTLY at load")
        m_name = re.search(r"^name:\s*(\S+)\s*$", fm, re.M)
        if m_name and m_name.group(1) != name:
            errs.append(f"{rel} frontmatter `name: {m_name.group(1)}` OVERRIDES the directory name at load; drop the "
                        f"`name:` line or make it `name: {name}`")
    for rel in sorted(tree):
        m = RX["subagent"].fullmatch(rel)
        if m and not re.search(r"^description:\s*\S", tree[rel][:600], re.M):
            errs.append(f"{rel} needs a `description:` in its frontmatter")
    for rel, src in tree.items():
        if rel.endswith(".py") and RX["skill_script"].fullmatch(rel):
            try:
                compile(src, rel, "exec")
            except (SyntaxError, ValueError) as exc:
                detail = (f"{type(exc).__name__}: {exc.msg} (line {exc.lineno})" if isinstance(exc, SyntaxError)
                          else f"{type(exc).__name__}: {exc}")
                errs.append(f"{rel} does not compile: {detail[:160]}")
    have = {rel for rel in tree if RX["skill_script"].fullmatch(rel)}
    for rel, src in tree.items():
        if not (RX["skill"].fullmatch(rel) or rel == ALWAYS_ON):
            continue
        for ref in RX["script_ref"].findall(src):
            if ref not in have:
                errs.append(f"{rel} invokes `{ref}` but that file is not in the harness")
    return errs


def check(adapter_path: str) -> list[dict]:
    adapter = Path(adapter_path).resolve()
    if not (adapter / ALWAYS_ON).is_file():
        # Never pass an adapter this check cannot see (under bubblewrap, /tmp is an empty tmpfs).
        return [{"path": ALWAYS_ON, "problem": f"no continual-harness adapter at {adapter} (AGENTS.md missing or not "
                                               f"visible)"}]
    tree, off = read_tree(adapter)
    issues = [{"path": rel, "problem": f"{rel} is off the harness surface (AGENTS.md, .agents/skills/<name>/SKILL.md "
                                       f"and its scripts/, sub_agents/<name>/AGENT.md, memory/NOTES.md)"}
              for rel in off]
    issues += [{"path": "", "problem": p} for p in problems(tree)]
    context = (adapter / "ADAPTER.md").read_text(encoding="utf-8") if (adapter / "ADAPTER.md").is_file() else ""
    if context != tree.get(ALWAYS_ON, ""):
        issues.append({"path": "ADAPTER.md", "problem": "ADAPTER.md is not AGENTS.md: it is the always-on text as the "
                       "framework exports it, so edit AGENTS.md and copy it"})
    return issues
