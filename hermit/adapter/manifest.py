"""The adapter manifest, adapter.yaml: template identity, exports, structure (the specification), check, origin.

A template and every adapter of its tree share one manifest; adapters add `origin`. The runtime and the hosts only
need the exports, so `flatten` returns them together with the identity fields they print.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

import yaml

MANIFEST = "adapter.yaml"
CONTEXT = "ADAPTER.md"     # every adapter's context for the acting agent: a fixed name, always exported
# exports.context_tool: a declared tool the harness calls once per task, before the agent's first model call, with
# {query, task}; the `text` it returns is appended to the context for that task (per-task context, such as the
# retrieval of ReHarness). It is the harness's tool, never offered to the agent.
CONTEXT_TOOL = "context_tool"
CARD = "CARD.md"          # written by the Trainer; neither learnable nor fixed
FRAMEWORK_FILES = (MANIFEST, CARD)
SECTIONS = ("template", "exports", "structure", "check", "evolver_tools", "origin")
# Sections that every adapter of a tree must carry unchanged from its root (check 3 of check_spec).
FIXED_SECTIONS = ("template", "exports", "structure", "check", "evolver_tools")   # `origin` is the Trainer's


def read_manifest(root: Path) -> dict:
    path = Path(root) / MANIFEST
    if not path.is_file():
        raise ValueError(f"Missing {MANIFEST} in {root}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{MANIFEST} must be a mapping")
    unknown = set(data) - set(SECTIONS)
    if unknown:
        raise ValueError(f"Unknown {MANIFEST} sections: {sorted(unknown)}")
    template = data.get("template")
    if not isinstance(template, dict):
        raise ValueError("adapter.yaml needs a template section")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", str(template.get("id", ""))):
        raise ValueError("template.id must be a lowercase slug")
    if not isinstance(template.get("version"), str) or not template["version"].strip():
        raise ValueError("template.version must be a nonempty string")
    if not isinstance(data.get("exports"), dict):
        raise ValueError("adapter.yaml needs an exports section")
    if data["exports"].get("context", CONTEXT) != CONTEXT:
        raise ValueError(f"the context is always {CONTEXT}; exports.context may only name it")
    structure_of(data)
    if data.get("check") is not None and not re.fullmatch(r"[\w./-]+\.py:[A-Za-z_]\w*", str(data["check"])):
        raise ValueError("check must be 'file.py:function'")
    return data


def flatten(data: dict) -> dict:
    """The exports plus the identity fields the runtime and the hosts read (id, name, version)."""
    template = data["template"]
    out = dict(data["exports"])
    out["context"] = CONTEXT
    out["id"] = template["id"]
    out["name"] = template.get("name", template["id"])
    out["version"] = template["version"]
    return out


@dataclass(frozen=True)
class Learnable:
    path: str                    # glob relative to the adapter root; ** matches across directories
    format: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Structure:
    learnable: tuple[Learnable, ...]
    fixed: tuple[str, ...]

    def is_fixed(self, rel: str) -> bool:
        return rel in FRAMEWORK_FILES or any(matches(rel, p) for p in self.fixed)

    def learnable_for(self, rel: str) -> Learnable | None:
        if self.is_fixed(rel):
            return None
        return next((item for item in self.learnable if matches(rel, item.path)), None)


FORMAT_KEYS = {"max_bytes", "max_files", "text", "frontmatter"}


def structure_of(data: dict) -> Structure:
    raw = data.get("structure")
    if not isinstance(raw, dict):
        raise ValueError("adapter.yaml needs a structure section")
    learnable = []
    for item in raw.get("learnable") or []:
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("structure.learnable entries are paths or {path, format}")
        fmt = item.get("format") or {}
        if set(fmt) - FORMAT_KEYS:
            raise ValueError(f"Unknown format keys for {item['path']}: {sorted(set(fmt) - FORMAT_KEYS)}")
        learnable.append(Learnable(item["path"], dict(fmt)))
    fixed = raw.get("fixed") or []
    if not all(isinstance(p, str) for p in fixed):
        raise ValueError("structure.fixed must be a list of paths")
    return Structure(tuple(learnable), tuple(fixed))


def _regex(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif pattern.startswith("**", i):
            out.append(".*"); i += 2
        elif c == "*":
            out.append("[^/]*"); i += 1
        elif c == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(c)); i += 1
    return re.compile("".join(out) + r"\Z")


def matches(rel: str, pattern: str) -> bool:
    pattern = pattern.rstrip("/")
    if _regex(pattern).match(rel):
        return True
    # A directory pattern covers everything below it.
    return _regex(pattern + "/**").match(rel) is not None


def files_of(root: Path) -> list[str]:
    """Every file of an adapter directory, relative, excluding caches and git metadata."""
    root = Path(root)
    out = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir() or "__pycache__" in path.parts or rel.startswith(".git/") or rel == ".git":
            continue
        out.append(rel)
    return out
