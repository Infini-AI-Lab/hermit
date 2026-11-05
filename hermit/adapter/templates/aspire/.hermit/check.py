"""Template check for `aspire` adapters: the skill library is Markdown entries an actor can apply as written.

The surface:
    ADAPTER.md            the actor's instructions above the library, never empty
    skills/README.md      the index: links every skill file, and only files that exist
    skills/<topic>.md     frontmatter `description:`; entries under `## <title>`, each with **Failure signature.**,
                          **When to apply.** and **Provenance.**, each at the start of a line; every fenced snippet
                          runs under the CaP program rules (ASPIRE cap_harness/runtime.py `_ProgramValidator`: only
                          `import math`, no names or attributes starting with `_`, no class, with, global, nonlocal,
                          del or async)
"""
from __future__ import annotations

import ast
from pathlib import Path
import re

OWN = frozenset({"ADAPTER.md", "exports.yaml", "README.md", "interface.py"})
SKILL = re.compile(r"skills/([a-z0-9][a-z0-9_\-]*)\.md")
FIELDS = ("**Failure signature.**", "**When to apply.**", "**Provenance.**")
FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)            # every fenced block, labelled or not
LINK = re.compile(r"\]\(([A-Za-z0-9_\-]+\.md)\)")
# Privileged access a generated program must never use (ASPIRE's provenance rule), named in code or prose
PRIVILEGED = re.compile(r"\b(?:env\.sim|sim\.(?:data|model)|sim\.forward|check_success|get_reward|"
                        r"native_env|handle\.env|parsed_problem|eval_predicate|obj_body_id|getattr|setattr)\b")
FORBIDDEN = (ast.AsyncFunctionDef, ast.Await, ast.ClassDef, ast.Delete, ast.Global, ast.ImportFrom, ast.Nonlocal,
             ast.With)


def program_problem(src: str) -> str | None:
    """Why the CaP sandbox would refuse `src`, or None (the rules of ASPIRE's _ProgramValidator)."""
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return f"does not parse: {exc.msg} (line {exc.lineno})"
    for node in ast.walk(tree):
        if isinstance(node, FORBIDDEN):
            return f"{type(node).__name__} is not allowed in generated programs"
        if isinstance(node, ast.Import) and any(a.name != "math" or a.asname for a in node.names):
            return "only `import math` is allowed, without aliasing"
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            return "private and dunder attribute access is not allowed"
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            return "private and dunder names are not allowed"
    return None


def entries(body: str) -> list[tuple[str, str]]:
    """(title, text) of every `## ` entry of a skill file; a `## ` line inside a fenced block is code, not a heading."""
    lines, marks, fenced = body.split("\n"), [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("## "):
            marks.append(i)
    return [(lines[a][3:].strip(), "\n".join(lines[a:b])) for a, b in zip(marks, marks[1:] + [len(lines)])]


def problems(adapter: Path) -> list[dict]:
    issues = []
    issue = lambda path, problem: issues.append({"path": path, "problem": problem})
    if not (adapter / "ADAPTER.md").is_file() or not (adapter / "ADAPTER.md").read_text().strip():
        issue("ADAPTER.md", "ADAPTER.md is empty: it is the actor's instructions above the library")
    skills = {}
    for p in sorted(adapter.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or ".git" in p.parts:
            continue
        rel = p.relative_to(adapter).as_posix()
        if rel in OWN or rel.startswith(".hermit/") or rel == "skills/README.md":
            continue
        if rel == "skills/proposed.md":
            issue(rel, "skills/proposed.md stages proposals for a verifier only; admit verified entries into their "
                       "topic files and remove it")
            continue
        m = SKILL.fullmatch(rel)
        if not m:
            issue(rel, f"{rel} is off the library's surface (ADAPTER.md, skills/README.md, skills/<topic>.md)")
            continue
        skills[p.name] = p.read_text(encoding="utf-8", errors="replace")
    index_path = adapter / "skills" / "README.md"
    index = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    if not index.strip():
        issue("skills/README.md", "the index is missing or empty")
    linked = set(LINK.findall(index))
    for name in sorted(linked - set(skills)):
        issue("skills/README.md", f"the index links {name}, which is not in skills/")
    for name in sorted(set(skills) - linked):
        issue(f"skills/{name}", f"skills/{name} is not linked from skills/README.md, so its purpose is unnamed")
    for name, text in skills.items():
        rel = f"skills/{name}"
        if not re.search(r"^description:\s*\S", text[:600], re.M):
            issue(rel, f"{rel} needs a `description:` in its frontmatter")
        seen = set()
        for title, body in entries(text):
            if title in seen:
                issue(rel, f"{rel} has two entries titled {title!r}")
            seen.add(title)
            missing = [f for f in FIELDS if not any(line.startswith(f) for line in body.split("\n"))]
            if missing:
                issue(rel, f"{rel} § {title}: missing {', '.join(missing)}")
            found = PRIVILEGED.search(body)
            if found:
                issue(rel, f"{rel} § {title}: names privileged access ({found.group(0)}), which programs must not use")
            for snippet in FENCE.findall(body):
                why = program_problem(snippet)
                if why:
                    issue(rel, f"{rel} § {title}: the snippet would be refused by the program sandbox: {why}")
    return issues


def check(adapter_path: str) -> list[dict]:
    adapter = Path(adapter_path).resolve()
    if not (adapter / "skills").is_dir():
        # Never pass an adapter this check cannot see (under bubblewrap, /tmp is an empty tmpfs).
        return [{"path": "skills", "problem": f"no aspire adapter at {adapter} (skills/ missing or not visible)"}]
    return problems(adapter)
