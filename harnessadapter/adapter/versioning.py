"""Git-backed revision history for adapter directories.

Every adapter directory the framework evolves is a git repository. The
framework makes the commits: one for the initial revision, one for every
evolution session whose candidate changed something. Learners never run git;
the evolution broker exposes the history read-only (`history` tool), and the
host driver that adopts a candidate receives the repository with it.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

AUTHOR = ["-c", "user.name=harnessadapter", "-c", "user.email=harnessadapter@localhost"]


def _git(root: Path, *args, check=True):
    return subprocess.run(["git", *AUTHOR, "-C", str(root), *args], capture_output=True, text=True, check=check)


def has_repo(root: Path) -> bool:
    return (Path(root) / ".git").is_dir()


def head(root: Path) -> str | None:
    if not has_repo(root):
        return None
    result = _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def ensure_repo(root: Path, message: str = "adapter: initial revision") -> str:
    """Initialise a repository with one commit when the directory has none."""
    root = Path(root)
    if not shutil.which("git"):
        raise RuntimeError("git is required for adapter revision history")
    if not has_repo(root):
        _git(root, "init", "-q")
        _git(root, "config", "core.autocrlf", "false")
        (root / ".gitignore").write_text("__pycache__/\n*.pyc\n") if not (root / ".gitignore").exists() else None
    if head(root) is None:
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", message, "--allow-empty")
    return head(root)


def commit_all(root: Path, message: str) -> str | None:
    """Commit every change under root; None when nothing changed."""
    root = Path(root)
    ensure_repo(root)
    _git(root, "add", "-A")
    if not _git(root, "status", "--porcelain", check=False).stdout.strip():
        return None
    _git(root, "commit", "-q", "-m", message)
    return head(root)


def copy_history(source: Path, target: Path) -> bool:
    """Carry the source repository's history into a copied directory."""
    src = Path(source) / ".git"
    if not src.is_dir():
        return False
    dst = Path(target) / ".git"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return True


def log(root: Path, n: int = 10) -> str:
    if not has_repo(root):
        return "(no revision history)"
    n = max(1, min(int(n), 50))
    return _git(root, "log", "--stat", "--date=short", f"-n{n}", "--format=%h %ad %s", check=False).stdout


def diff(root: Path, ref: str = "HEAD~1", path: str | None = None) -> str:
    if not has_repo(root):
        return "(no revision history)"
    if not all(ch.isalnum() or ch in "~^_-" for ch in ref) or len(ref) > 64:
        raise ValueError("ref must be a revision such as HEAD~1 or a short hash")
    args = ["diff", ref, "--", path] if path else ["diff", ref]
    result = _git(root, *args, check=False)
    text = result.stdout or result.stderr
    return text[:30000] + ("\n[diff truncated at 30000 characters]" if len(text) > 30000 else "")
