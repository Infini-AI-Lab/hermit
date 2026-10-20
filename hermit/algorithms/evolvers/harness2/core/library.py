# Derived from google-research/harness2 core/library.py, Copyright 2026 Google LLC, Apache-2.0.
"""The per-domain experience library: what earlier steps or tasks in this domain adopted.

    <root>/<domain>/INDEX.md
    <root>/<domain>/harnesses/<entry>/harness/<rel>    the full adopted tree
    <root>/<domain>/harnesses/<entry>/EXPERIENCE.md    why it was chosen, what each edit did, one lesson

The whole tree is carried, never a summary. Only an entry with a cue, a lesson and evidence is stored.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import surface as S
from .selection import has_experience
from .context import assert_tree_has_no_grader_verdicts


def slug(entry: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", entry.replace("/", "__")) or "entry"


def domain_dir(domain: str) -> str:
    return re.sub(r"\s*[/\\]\s*", "__", str(domain)).strip() or "default"


@dataclass
class Library:
    root: Path
    domain: str
    surface: S.SurfaceSpec

    @property
    def dir(self) -> Path:
        return Path(self.root) / domain_dir(self.domain)

    @property
    def index(self) -> Path:
        return self.dir / "INDEX.md"

    def entry_dir(self, entry: str) -> Path:
        return self.dir / "harnesses" / slug(entry)

    def exists(self) -> bool:
        return self.index.is_file()

    def context_dir(self) -> Path | None:
        """The directory handed to an improver session, or None while this domain is empty."""
        return self.dir if self.exists() and any(self.dir.rglob("EXPERIENCE.md")) else None

    def entries(self) -> list[str]:
        d = self.dir / "harnesses"
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []

    def tree_of(self, entry: str) -> S.Tree:
        return S.read_tree(self.surface, self.entry_dir(entry) / "harness")

    def add(self, entry: str, harness: S.Tree, *, why: str = "", label: str = "",
            experience: dict | None = None) -> Path | None:
        """Store one adopted harness plus why; rebuild INDEX.md. Refused (None) without a complete experience."""
        if not has_experience(experience):
            return None
        assert_tree_has_no_grader_verdicts(harness)
        assert_tree_has_no_grader_verdicts({"EXPERIENCE.md": str(experience) + why})
        d = self.entry_dir(entry)
        shutil.rmtree(d / "harness", ignore_errors=True)
        (d / "harness").mkdir(parents=True, exist_ok=True)
        S.write_tree(self.surface, harness, d / "harness", render=False)
        exp = experience or {}
        lines = [f"# {entry}", "", f"- adopted: `{label}`", f"- components: {S.inventory(self.surface, harness)}", ""]
        if why.strip():
            lines += ["## Why this was chosen", why.strip(), ""]
        if exp.get("edits"):
            lines += ["## What each edit did", str(exp["edits"]).strip(), ""]
        lines += ["## Transferable lesson"]
        lines += [f"- **{f}**: {str(exp[f]).strip()}" for f in ("cue", "lesson", "evidence", "confidence") if exp.get(f)]
        lines += [""]
        (d / "EXPERIENCE.md").write_text("\n".join(lines), encoding="utf-8")
        self.rebuild_index()
        return d

    def rebuild_index(self) -> Path:
        rows = [f"# Library — {self.domain}", "",
                "Each entry is an earlier task in THIS domain whose harness was chosen, stored with its FULL component "
                "tree and why it was picked. Browse this index, then open the entries that look relevant to the matter "
                "in front of you.", ""]
        names = self.entries()
        if not names:
            rows.append("_(empty — this is the first task in this domain)_")
        for name in names:
            ed = self.dir / "harnesses" / name
            tree = S.read_tree(self.surface, ed / "harness")
            rows += [f"## `harnesses/{name}/`",
                     f"- harness: `harnesses/{name}/harness/` — {S.inventory(self.surface, tree)}",
                     f"- why:     `harnesses/{name}/EXPERIENCE.md`"]
            cue = _field(ed / "EXPERIENCE.md", "cue")
            if cue:
                rows.append(f"- cue:     {cue}")
            rows.append("")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index.write_text("\n".join(rows), encoding="utf-8")
        return self.index


def _field(path: Path, name: str) -> str:
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(f"- **{name}**:"):
            return line.split(":", 1)[1].strip()
    return ""
