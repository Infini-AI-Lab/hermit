# Derived from google-research/harness2 core/tree.py, Copyright 2026 Google LLC, Apache-2.0.
"""The editable component tree of a `harness2` adapter: read, validate, write, render, diff.

The surface is the MetaHarnessAdapter adapter layout, described by a `SurfaceSpec` read from the template's fixed
`harness2.yaml`:

    harness/systemprompt.md       c1  role and procedure         always on (composed into ADAPTER.md)
    harness/guardrails.md         c2  standing rules             always on
    harness/LongTermMEMORY.md     c3  durable domain facts       always on
    harness/tool_guidance.md      c4  how to use the tools       always on
    skills/<name>/SKILL.md        c6  a reusable workflow        inline (in ADAPTER.md) | index (hosts load it on demand)
    skills/<name>/scripts/*.py    c7  a helper carried with it   only when the actor can run scripts
    agents/<name>.md              c5  a delegated subagent       only when the actor can spawn one

`ADAPTER.md` is never edited directly: it is the rendering of the tree (`render_context`), and the template's
`check.py` enforces that identity. The edit contract is omitted-is-inherited: a proposal is the edited tree itself.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

Tree = dict[str, str]

ALWAYS_ON = ("harness/systemprompt.md", "harness/guardrails.md", "harness/LongTermMEMORY.md",
             "harness/tool_guidance.md")
SURFACE_FILE = "harness2.yaml"
# Files of an adapter that are never part of the tree (the framework's and the template's own).
FRAMEWORK_FILES = frozenset({"ADAPTER.md", "adapter.yaml", "CARD.md", "check.py", "EVOLVER.md", "README.md",
                             SURFACE_FILE, "skills/README.md", "agents/README.md"})
SKILL_MODES = ("inline", "index", "off")
_NAME = r"[a-z0-9][a-z0-9_\-]*"


@dataclass(frozen=True)
class SurfaceSpec:
    """What the acting agent can be given, and the caps the validators enforce."""
    name: str = "mha"
    always_on: tuple[str, ...] = ALWAYS_ON
    always_on_max: int = 24000
    context_max: int = 0            # bytes of the rendered ADAPTER.md, 0 = no cap of our own
    skills: str = "inline"          # inline: skill bodies are rendered into ADAPTER.md | index | off
    scripts: bool = False           # whether skills may carry scripts/*.py (the actor can run them)
    agents: bool = False            # whether agents/<name>.md roles exist for the actor
    skill_dir: str = "skills"
    agent_dir: str = "agents"
    autotrigger_file: str = "harness/systemprompt.md"

    @property
    def trigger_line(self) -> str:
        if self.skills == "inline":
            return "When {desc}, follow the `{name}` skill."
        return "When {desc}, load the `{name}` skill."

    @property
    def trigger_hint(self) -> str:
        return ("Add a line to the always-on text naming it, e.g. `When <situation>, "
                + ("follow" if self.skills == "inline" else "load") + " the `{name}` skill.`")


def load_surface(root: Path | str) -> SurfaceSpec:
    """The SurfaceSpec declared by `<root>/harness2.yaml` (a template or an adapter); defaults when absent."""
    path = Path(root) / SURFACE_FILE
    data = (yaml.safe_load(path.read_text()) or {}) if path.is_file() else {}
    s = dict(data.get("surface") or {})
    skills = str(s.get("skills", "inline"))
    if skills not in SKILL_MODES:
        raise ValueError(f"{SURFACE_FILE}: surface.skills must be one of {SKILL_MODES}, got {skills!r}")
    return SurfaceSpec(skills=skills, scripts=bool(s.get("scripts", False)), agents=bool(s.get("agents", False)),
                       always_on_max=int(data.get("always_on_max", 24000)),
                       context_max=int(data.get("context_max", 0) or 0))


def _rx(surface: SurfaceSpec) -> dict[str, re.Pattern]:
    sd, ad = re.escape(surface.skill_dir), re.escape(surface.agent_dir)
    return {"skill": re.compile(rf"{sd}/({_NAME})/SKILL\.md"),
            "skill_script": re.compile(rf"{sd}/({_NAME})/scripts/[A-Za-z0-9_.\-]+"),
            "agent": re.compile(rf"{ad}/({_NAME})\.md"),
            "script_ref": re.compile(rf"(?:python3?\s+)((?:{sd}/[\w\-]+/)?scripts/[\w.\-]+\.py)")}


def normalize_rel(rel: str) -> str:
    r = str(rel).strip().replace("\\", "/")
    if r.startswith("/") or ".." in r.split("/"):
        raise ValueError(f"path escapes the harness: {rel!r}")
    while r.startswith("./"):
        r = r[2:]
    return r.strip("/")


def is_allowed(surface: SurfaceSpec, rel: str) -> bool:
    """Whether this path is on the editable surface. A predicate: it never raises."""
    try:
        rel = normalize_rel(rel)
    except ValueError:
        return False
    if rel in surface.always_on:
        return True
    r = _rx(surface)
    if surface.skills != "off" and r["skill"].fullmatch(rel):
        return True
    if surface.skills != "off" and surface.scripts and r["skill_script"].fullmatch(rel):
        return True
    return surface.agents and bool(r["agent"].fullmatch(rel))


def skill_names(surface: SurfaceSpec, tree: Tree) -> list[str]:
    rx = _rx(surface)["skill"]
    return sorted({m.group(1) for rel in tree if (m := rx.fullmatch(rel))})


def agent_names(surface: SurfaceSpec, tree: Tree) -> list[str]:
    rx = _rx(surface)["agent"]
    return sorted({m.group(1) for rel in tree if (m := rx.fullmatch(rel))})


def read_tree(surface: SurfaceSpec, d: Path | str, *, rejected: list[str] | None = None) -> Tree:
    """rel -> content for every file on the editable surface; `rejected` collects what is on disk but off it
    (framework and template files excepted)."""
    d = Path(d)
    out: Tree = {}
    if not d.is_dir():
        return out
    for p in sorted(d.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or ".git" in p.parts:
            continue
        rel = p.relative_to(d).as_posix()
        if rel in FRAMEWORK_FILES or rel.startswith(".") or p.suffix == ".pyc":
            continue
        if not is_allowed(surface, rel):
            if rejected is not None:
                rejected.append(rel)
            continue
        out[rel] = p.read_text(encoding="utf-8", errors="replace")
    return out


def write_tree(surface: SurfaceSpec, tree: Tree, dest: Path | str, *, render: bool = True) -> Path:
    """Materialise an exact tree under `dest`: editable files already there are removed first, files off the
    surface are preserved, the always-on files always exist, and ADAPTER.md is re-rendered when `render`."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for p in sorted(dest.rglob("*"), reverse=True):
        if p.is_file() and ".git" not in p.parts:
            rel = p.relative_to(dest).as_posix()
            if rel not in FRAMEWORK_FILES and is_allowed(surface, rel):
                p.unlink()
        elif p.is_dir() and p.name != ".git" and not any(p.iterdir()):
            p.rmdir()
    for a in surface.always_on:
        p = dest / a
        p.parent.mkdir(parents=True, exist_ok=True)
        if a not in tree:
            p.write_text("", encoding="utf-8")
    for rel, content in tree.items():
        rel = normalize_rel(rel)
        if not is_allowed(surface, rel):
            raise ValueError(f"{rel} is off the editable surface")
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    if render:
        (dest / "ADAPTER.md").write_text(render_context(surface, tree), encoding="utf-8")
    return dest


def always_on_text(surface: SurfaceSpec, tree: Tree) -> str:
    return "\n\n".join(tree.get(a, "") for a in surface.always_on)


def parse_frontmatter(text: str) -> tuple[dict | None, str]:
    """(frontmatter, body) exactly as the framework's check_spec reads it (`---\\n` first, YAML up to the next
    `\\n---`); the frontmatter is None when there is none or it is not a YAML mapping."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---", 4)
    if end < 0:
        return None, text
    nl = text.find("\n", end + 4)
    body = text[nl + 1:] if nl >= 0 else ""
    try:
        head = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None, body
    return (head if isinstance(head, dict) else None), body


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """A SKILL.md or agent file's frontmatter as strings, and its body ({} when there is no valid frontmatter)."""
    head, body = parse_frontmatter(text)
    return ({str(k): ("" if v is None else str(v)) for k, v in (head or {}).items()}), body


def repair_frontmatter(surface: SurfaceSpec, tree: Tree) -> list[str]:
    """Quote single-line frontmatter values that break YAML (a description containing `: ` is the usual case);
    return the files repaired. Nothing else is touched."""
    fixed = []
    rx = _rx(surface)["skill"]
    for rel in sorted(tree):
        text = tree[rel]
        if not rx.fullmatch(rel) or not text.startswith("---\n"):
            continue
        end = text.find("\n---", 4)
        if end < 0 or parse_frontmatter(text)[0] is not None:
            continue
        lines = []
        for line in text[4:end].splitlines():
            m = re.match(r"^([A-Za-z_][\w-]*):[ \t]*(.*?)\s*$", line)
            if m and m.group(2) and not m.group(2).startswith(('"', "'", "|", ">")):
                line = f"{m.group(1)}: {json.dumps(m.group(2), ensure_ascii=False)}"
            lines.append(line)
        candidate = "---\n" + "\n".join(lines) + text[end:]
        if parse_frontmatter(candidate)[0] is not None:
            tree[rel] = candidate
            fixed.append(rel)
    return fixed


def render_context(surface: SurfaceSpec, tree: Tree) -> str:
    """ADAPTER.md: the always-on files, then the skills (inline bodies or an index), then the subagent index."""
    parts = [tree.get(a, "").strip() for a in surface.always_on]
    parts = [p for p in parts if p]
    skills = skill_names(surface, tree) if surface.skills != "off" else []
    if skills:
        lines = ["## Skills"]
        for name in skills:
            meta, body = split_frontmatter(tree[f"{surface.skill_dir}/{name}/SKILL.md"])
            desc = meta.get("description", "").strip()
            if surface.skills == "inline":
                lines.append(f"### {name}" + (f"\n\n{desc}" if desc else "") + f"\n\n{body.strip()}")
            else:
                lines.append(f"- `{name}`: {desc}")
        parts.append("\n\n".join(lines) if surface.skills == "inline" else "\n".join(lines))
    agents = agent_names(surface, tree) if surface.agents else []
    if agents:
        lines = ["## Subagents"]
        for name in agents:
            meta, _ = split_frontmatter(tree[f"{surface.agent_dir}/{name}.md"])
            lines.append(f"- `{name}`: {meta.get('description', '').strip()}")
        parts.append("\n".join(lines))
    return ("\n\n".join(parts) + "\n") if parts else ""


def autotrigger(surface: SurfaceSpec, tree: Tree) -> list[str]:
    """Give every skill a trigger line in always-on text; return the names repaired. A line that would push the
    always-on text over the cap is skipped instead."""
    fixed: list[str] = []
    for name in skill_names(surface, tree):
        if name in always_on_text(surface, tree):
            continue
        meta, _ = split_frontmatter(tree.get(f"{surface.skill_dir}/{name}/SKILL.md", ""))
        desc = meta.get("description", "").strip().rstrip(".") or f"the {name} workflow applies"
        desc = re.sub(r"^(?:use (?:this skill )?)?when\s+", "", desc, flags=re.I) or desc
        line = surface.trigger_line.format(name=name, desc=desc)
        if len(always_on_text(surface, tree)) + len(line) + 2 > surface.always_on_max:
            continue
        cur = tree.get(surface.autotrigger_file, "")
        tree[surface.autotrigger_file] = (cur.rstrip() + "\n" + line + "\n") if cur.strip() else line + "\n"
        fixed.append(name)
    return fixed


def _py_compiles(rel: str, src: str) -> str:
    try:
        compile(src, rel, "exec")
    except (SyntaxError, ValueError) as exc:
        detail = (f"{type(exc).__name__}: {exc.msg} (line {exc.lineno})" if isinstance(exc, SyntaxError)
                  else f"{type(exc).__name__}: {exc}")
        return f"{rel} does not compile: {detail[:160]}"
    return ""


def problems(surface: SurfaceSpec, tree: Tree) -> list[tuple[str, str]]:
    """Every problem of a tree as (the file to blame, or "" for the whole tree; actionable prose)."""
    out: list[tuple[str, str]] = []
    r = _rx(surface)
    body = always_on_text(surface, tree)
    if len(body) > surface.always_on_max:
        out.append(("", f"always-on content is {len(body)} chars, over the {surface.always_on_max} cap; every "
                        f"always-on character is in context on every run, so cut text or move it into a skill"))
    if surface.context_max:
        size = len(render_context(surface, tree).encode("utf-8"))
        if size > surface.context_max:
            out.append(("", f"the rendered prompt block (always-on text plus skills) is {size} bytes, over the "
                            f"{surface.context_max} cap; cut text"))
    for rel in tree:
        if not is_allowed(surface, rel):
            out.append((rel, f"{rel} is off the editable surface"))
    for name in skill_names(surface, tree):
        rel = f"{surface.skill_dir}/{name}/SKILL.md"
        head, _ = parse_frontmatter(tree[rel])
        meta, body_text = split_frontmatter(tree[rel])
        if head is None and tree[rel].startswith("---\n"):
            out.append((rel, f"{rel} frontmatter is not valid YAML: put each value on one line and quote it when it "
                             f"contains `: ` (description: \"...\")"))
        elif not meta.get("description"):
            out.append((rel, f"{rel} needs a `description:` in YAML frontmatter that starts on the first line "
                             f"(`---\\ndescription: ...\\n---`), or the skill is dropped silently at load"))
        if meta.get("name") and meta["name"] != name:
            out.append((rel, f"{rel} frontmatter `name: {meta['name']}` must match its directory `{name}`, or be "
                             f"left out"))
        if not body_text.strip():
            out.append((rel, f"{rel} has no body: a skill is the procedure itself"))
        if name not in body:
            out.append((rel, f"skill '{name}' is never named in the always-on text, so the agent will not know to "
                             f"use it; an untriggered skill is dead weight. " + surface.trigger_hint.format(name=name)))
    for name in agent_names(surface, tree):
        rel = f"{surface.agent_dir}/{name}.md"
        meta, _ = split_frontmatter(tree[rel])
        if meta.get("name", name) != name or not meta.get("description"):
            out.append((rel, f"{rel} needs frontmatter `name: {name}` and `description:`"))
    for rel, src in tree.items():
        if r["skill_script"].fullmatch(rel) and rel.endswith(".py"):
            err = _py_compiles(rel, src)
            if err:
                out.append((rel, err))
    have = {rel for rel in tree if r["skill_script"].fullmatch(rel)}
    for rel, src in tree.items():
        if not (r["skill"].fullmatch(rel) or rel in surface.always_on):
            continue
        for ref in r["script_ref"].findall(src):
            if not surface.scripts:
                out.append((rel, f"{rel} invokes `{ref}`, but the acting agent here cannot run scripts; state the "
                                 f"procedure in words instead"))
            elif ref not in have:
                out.append((rel, f"{rel} invokes `{ref}` but that file is not in the harness"))
    return out


def validate(surface: SurfaceSpec, tree: Tree) -> dict:
    """Raise ValueError listing every problem (actionable prose, fed back to the editor); return a census."""
    found = problems(surface, tree)
    if found:
        raise ValueError(" | ".join(message for _, message in found))
    r = _rx(surface)
    return {"always_on_chars": len(always_on_text(surface, tree)), "n_files": len(tree),
            "skills": len(skill_names(surface, tree)), "agents": len(agent_names(surface, tree)),
            "scripts": sum(1 for rel in tree if r["skill_script"].fullmatch(rel)),
            "context_bytes": len(render_context(surface, tree).encode("utf-8"))}


def diff_summary(surface: SurfaceSpec, before: Tree, after: Tree) -> dict:
    return {"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after)),
            "modified": sorted(rel for rel in set(before) & set(after) if before[rel] != after[rel]),
            "always_on_char_delta": len(always_on_text(surface, after)) - len(always_on_text(surface, before))}


def changed(summary: dict) -> bool:
    return bool(summary["added"] or summary["removed"] or summary["modified"])


def unified_diff(before: Tree, after: Tree, *, fromlabel: str = "current", tolabel: str = "candidate") -> str:
    return "".join(line for name in sorted(before.keys() | after.keys())
                   for line in difflib.unified_diff(before.get(name, "").splitlines(keepends=True),
                                                    after.get(name, "").splitlines(keepends=True),
                                                    fromfile=f"{fromlabel}/{name}", tofile=f"{tolabel}/{name}"))


def diversity(trees: dict[str, Tree]) -> dict:
    """Pairwise line-Jaccard over candidate trees, so collapse to one body is visible."""
    names = sorted(trees)
    if len(names) < 2:
        return {"n": len(names), "n_distinct": len(names), "mean_line_jaccard": 1.0}

    def lines(t: Tree) -> set[str]:
        return {f"{rel}:{ln}" for rel, c in t.items() for ln in c.splitlines() if ln.strip()}
    sets = {n: lines(trees[n]) for n in names}
    sims = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            u = sets[a] | sets[b]
            sims.append(len(sets[a] & sets[b]) / len(u) if u else 1.0)
    distinct = len({tuple(sorted(t.items())) for t in trees.values()})
    return {"n": len(names), "n_distinct": distinct, "mean_line_jaccard": round(sum(sims) / len(sims), 4)}


def inventory(surface: SurfaceSpec, tree: Tree) -> str:
    """A one-line census of the components actually in this harness."""
    n_sk, n_ag = len(skill_names(surface, tree)), len(agent_names(surface, tree))
    n_sc = sum(1 for rel in tree if _rx(surface)["skill_script"].fullmatch(rel))
    bits = [f"{v} {k}" for k, v in (("skills", n_sk), ("agents", n_ag), ("scripts", n_sc)) if v] or ["always-on text only"]
    return f"{', '.join(bits)}; {len(always_on_text(surface, tree)):,} always-on chars"
