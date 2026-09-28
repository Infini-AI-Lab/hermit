"""The text surface of an adapter: which files a text optimizer may rewrite.

GEPA optimizes a dict of named texts; SkillOpt optimizes one document. An
adapter is a directory. A TextSurface maps between the two: it reads the
chosen files into a candidate {relative path: text} and materializes a
candidate as a complete, loadable adapter directory (every other file copied
unchanged from the base adapter). For the prompt-only architecture the
surface is its context document, ADAPTER.md.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from harnessadapter.adapter.loader import inside, load_manifest

IGNORED = {".git", "__pycache__", ".DS_Store"}


def candidate_hash(candidate: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(candidate, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def copy_adapter(source: Path, destination: Path) -> None:
    """Copy an adapter without VCS metadata; symlinks are refused because the
    acting harness treats the adapter as a sealed, read-only snapshot."""
    source = Path(source).resolve()
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in IGNORED for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"Adapter symlinks are not supported: {relative}")
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


class TextSurface:
    """The optimizable text files of an adapter, by relative path."""

    def __init__(self, base_adapter: Path, components: list[str] | None = None):
        self.base = Path(base_adapter).resolve()
        manifest = load_manifest(self.base)
        if components is None:
            context = manifest.get("context")
            if not isinstance(context, str):
                raise ValueError("This adapter renders its context in code; name the text components explicitly")
            components = [context]
        if not components:
            raise ValueError("A text surface needs at least one component")
        for name in components:
            inside(self.base, name)
            if name in ("adapter.json",):
                raise ValueError("The manifest is not an optimizable text")
        self.components = list(dict.fromkeys(components))

    def read(self, adapter: Path | None = None) -> dict[str, str]:
        root = Path(adapter).resolve() if adapter else self.base
        return {name: inside(root, name).read_text() for name in self.components}

    def check(self, candidate: dict[str, str]) -> dict[str, str]:
        if set(candidate) != set(self.components):
            raise ValueError(f"Candidate components {sorted(candidate)} differ from the surface {self.components}")
        if not all(isinstance(v, str) for v in candidate.values()):
            raise ValueError("Candidate texts must be strings")
        return {name: candidate[name] for name in self.components}

    def materialize(self, candidate: dict[str, str], destination: Path) -> Path:
        """Write a complete adapter: the base's files with the candidate's
        texts in place. The destination must not exist yet."""
        candidate = self.check(candidate)
        destination = Path(destination)
        if destination.exists():
            raise ValueError(f"Refusing to overwrite {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=destination.parent))
        try:
            copy_adapter(self.base, staging)
            for name, text in candidate.items():
                (staging / name).write_text(text)
            load_manifest(staging)
            os.replace(staging, destination)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return destination.resolve()


class CandidateStore:
    """Materialized candidates by content hash, so a candidate that an
    optimizer evaluates many times is written once and its directory name
    identifies it in every rollout record."""

    def __init__(self, surface: TextSurface, root: Path):
        self.surface = surface
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, candidate: dict[str, str]) -> Path:
        digest = candidate_hash(self.surface.check(candidate))[:16]
        destination = self.root / f"cand-{digest}"
        if not destination.exists():
            try:
                self.surface.materialize(candidate, destination)
            except (ValueError, OSError):
                if not destination.exists():   # else another thread won the race
                    raise
        return destination
