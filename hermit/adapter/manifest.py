"""The adapter's metadata (.hermit/) and its exports (exports.yaml).

Two kinds of files in an adapter cannot be changed by a learner: those under `.hermit/`, and those the template's
`structure` fixes. Everything else is the adapter's content.

`.hermit/` holds the metadata: `adapter.yaml` (the template's identity, `structure`, `check`, `evolver_tools`), the
template's own files (its checker, `EVOLVER.md`, the code of its learner tools) and `CARD.md`, which only the
framework writes. Every adapter of a tree carries `.hermit/` as its root does, `CARD.md` aside.

`exports.yaml` declares what the acting agent receives (tools, subagents, skills). It is content: a learner may
change it unless the template fixes it. `flatten` returns the exports with the identity fields the runtime and the
hosts print.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

import yaml

META = ".hermit"                       # the metadata directory: a learner never changes it
MANIFEST = f"{META}/adapter.yaml"     # the template's identity, structure, check and learner tools
CARD = f"{META}/CARD.md"              # the adapter's card: written by the Trainer and `hermit eval` only
EXPORTS = "exports.yaml"               # what the acting agent receives: content, learnable unless the template fixes it
CONTEXT = "ADAPTER.md"                 # the acting agent's context: a fixed name, always exported, optional
SECTIONS = ("template", "structure", "check", "evolver_tools")


def is_meta(rel: str) -> bool:
    return rel == META or rel.startswith(META + "/")


def read_meta(root: Path) -> dict:
    """.hermit/adapter.yaml, validated."""
    path = Path(root) / MANIFEST
    if not path.is_file():
        if (Path(root) / "adapter.yaml").is_file():
            raise ValueError(f"{root} has the Hermit 0.1 layout (adapter.yaml at the root). Move it to {MANIFEST} "
                             f"and EVOLVER.md, CARD.md and the check file into {META}/; in the moved file, move the "
                             f"exports section into {EXPORTS} at the root, delete the origin section and point check "
                             f"into {META}/. See https://infini-ai-lab.github.io/hermit/docs/framework/"
                             "adapter-manifest.html#migrating-from-hermit-0-1")
        raise ValueError(f"Missing {MANIFEST} in {root}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{MANIFEST} must be a mapping")
    unknown = set(data) - set(SECTIONS)
    if unknown:
        raise ValueError(f"Unknown {MANIFEST} sections: {sorted(unknown)}")
    template = data.get("template")
    if not isinstance(template, dict):
        raise ValueError(f"{MANIFEST} needs a template section")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", str(template.get("id", ""))):
        raise ValueError("template.id must be a lowercase slug")
    if not isinstance(template.get("version"), str) or not template["version"].strip():
        raise ValueError("template.version must be a nonempty string")
    structure_of(data)
    if data.get("check") is not None and not re.fullmatch(r"[\w./-]+\.py:[A-Za-z_]\w*", str(data["check"])):
        raise ValueError("check must be 'file.py:function'")
    return data


def read_exports(root: Path) -> dict:
    """exports.yaml, validated; an adapter without one exports its context only."""
    path = Path(root) / EXPORTS
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text())
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{EXPORTS} must be a mapping")
    if data.get("context", CONTEXT) != CONTEXT:
        raise ValueError(f"the context is always {CONTEXT}; context may only name it")
    return data


def read_manifest(root: Path) -> dict:
    """The metadata with the exports under `exports`."""
    return {**read_meta(root), "exports": read_exports(root)}


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
        return is_meta(rel) or any(matches(rel, p) for p in self.fixed)

    def learnable_for(self, rel: str) -> Learnable | None:
        if self.is_fixed(rel):
            return None
        item = next((item for item in self.learnable if matches(rel, item.path)), None)
        return item or (Learnable(EXPORTS) if rel == EXPORTS else None)   # exports.yaml: learnable unless fixed


FORMAT_KEYS = {"max_bytes", "max_files", "text", "frontmatter"}


def structure_of(data: dict) -> Structure:
    raw = data.get("structure")
    if raw is None:                       # no structure section: every file is learnable
        return Structure((Learnable("**"),), ())
    if not isinstance(raw, dict):
        raise ValueError("structure must be a mapping")
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
