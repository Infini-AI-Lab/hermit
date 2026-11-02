"""The pinned checkout supplies the original loop, prompts and public meta-skills."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

COMMIT = "4380d4bff673dd6e1d58e5babeb2aaa0fe527119"


def checkout(value: str | Path | None = None) -> Path:
    if importlib.util.find_spec("harbor") is None:
        from hermit.agent.evolver.dependencies import missing_dependency
        raise missing_dependency("coevoskills", "the pinned CoEvoSkills and Harbor packages", "coevoskills")
    root = Path(value or os.environ.get("COEVOSKILLS_ROOT", "../CoEvoSkills")).expanduser().resolve()
    if not (root / "libs/terminus_agent/agents/terminus_2/harbor_terminus_2_evolution.py").is_file():
        raise RuntimeError(f"Set COEVOSKILLS_ROOT to a CoEvoSkills checkout at {COMMIT}: {root}")
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if head != COMMIT:
        raise RuntimeError(f"CoEvoSkills revision {head}; required {COMMIT}")
    if subprocess.run(["git", "-C", str(root), "diff", "--quiet", "HEAD", "--", "libs", "scripts"]).returncode:
        raise RuntimeError("pinned upstream actor/evolution source was modified")
    # All calls happen in a dedicated worker process. Upstream uses
    # generic top-level package names; evict an already imported foreign package.
    for name in ("libs",):
        module = sys.modules.get(name)
        locations = list(getattr(module, "__path__", [])) if module else []
        if module is not None and not any(Path(p).resolve().is_relative_to(root) for p in locations):
            for key in list(sys.modules):
                if key == name or key.startswith(name + "."):
                    del sys.modules[key]
    # An installed wheel must not shadow the pinned source tree.
    if str(root) in sys.path:
        sys.path.remove(str(root))
    sys.path.insert(0, str(root))
    return root


def safe_name(value: str) -> str:
    import re
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", value) or value in {".", ".."}:
        raise ValueError(f"unsafe task id: {value!r}")
    return value


def real_tree(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"expected a real directory: {root}")
    for path in root.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError(f"links and special files are not allowed: {path}")
