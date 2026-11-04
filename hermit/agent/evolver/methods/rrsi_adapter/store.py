"""The candidate store's file operations, as Meta-Harness Adapter's Evolver performs them (its evolver.py: the same
store layout, state/candidates/<id>/adapter/, and the same rules), kept here so that this method depends on no other
method's private names.

Everything done to the store happens outside the proposer's sandbox, which can write the run's state and scratch: a
path that a session could have replaced with a link is checked (`_real`) or written without following one (`_write`).
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil

CANDIDATES = "candidates"
KEEP = (".git", ".hermit")                 # what a candidate never brings into the working copy


def _copy_tree(source: Path, target: Path) -> None:
    """`source`'s files at `target`, without its root's .git and .hermit/, or byte code anywhere."""
    if target.exists():
        shutil.rmtree(target)

    def ignore(directory, names):
        skip = {"__pycache__", *(n for n in names if n.endswith(".pyc"))}
        return skip | ({n for n in names if n in KEEP} if Path(directory) == source else set())
    shutil.copytree(source, target, symlinks=False, ignore=ignore)


def _real(path: Path, root: Path) -> bool:
    """Whether `path` lies under `root` through real directories only: no component below `root` is a link."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            return False
    return path.resolve().is_relative_to(root.resolve())


def _write(path: Path, text: str) -> None:
    """Write a file the proposer's sandbox could have replaced with a link (or a named pipe, whose opening would
    wait for a reader), without following one."""
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists() and not path.is_file():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _store(ctx) -> Path:
    """state/candidates/ as a real directory: a store replaced by a link (or a file) is removed and made afresh, and
    the candidates it held then fail their digest."""
    store = ctx.workspace.state / CANDIDATES
    if store.is_symlink() or (store.exists() and not store.is_dir()):
        store.unlink()
    store.mkdir(parents=True, exist_ok=True)
    return store


def _install(candidate: Path, adapter: Path) -> None:
    """The candidate's files in the working copy: everything but .git and .hermit/ replaced."""
    for entry in adapter.iterdir():
        if entry.name in KEEP:
            continue
        shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
    for entry in candidate.iterdir():
        if entry.name in KEEP or entry.name == "__pycache__":
            continue
        target = adapter / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(entry, target)
