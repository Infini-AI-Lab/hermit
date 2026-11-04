"""The pinned upstream checkout: its prompts, meta-skills and `scripts/`, which the installed package does not carry."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys

COMMIT = "4380d4bff673dd6e1d58e5babeb2aaa0fe527119"
AGENT_FILE = "libs/terminus_agent/agents/terminus_2/harbor_terminus_2_evolution.py"
UPSTREAM_DIRS = ("libs", "scripts", "meta_skills")    # the loop's code, its helpers, the meta-skills it injects
HARBOR_COMMIT = "3f28e5ce2acbff36d8b5df431e35e050ac13bef6"   # upstream's pyproject pins Harbor here


def harbor_commit() -> str | None:
    """The Git commit the installed Harbor was built from (its direct_url.json), or None."""
    import importlib.metadata
    import json
    try:
        record = importlib.metadata.distribution("harbor").read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None
    return ((json.loads(record) if record else {}).get("vcs_info") or {}).get("commit_id")


def verify(value: str | Path | None = None) -> Path:
    """The checkout `value` (default $COEVOSKILLS_ROOT) when Harbor is installed at upstream's pinned commit and the
    checkout is the pinned commit with its loop unmodified; raises otherwise. Imports nothing of upstream, so the
    Trainer's process can call it."""
    if importlib.util.find_spec("harbor") is None:
        from hermit.agent.evolver.dependencies import missing_dependency
        raise missing_dependency("coevoskills_adapter", "the pinned CoEvoSkills and Harbor packages", "coevoskills")
    harbor = harbor_commit()
    if harbor != HARBOR_COMMIT:
        raise RuntimeError(f"Harbor is installed at {harbor or 'an unknown revision'}; upstream pins {HARBOR_COMMIT} "
                           f"(install the coevoskills dependency group)")
    root = Path(os.path.expandvars(str(value or os.environ.get("COEVOSKILLS_ROOT") or "../CoEvoSkills")))
    root = root.expanduser().resolve()
    if not (root / AGENT_FILE).is_file():
        raise RuntimeError(f"no CoEvoSkills checkout at {root}: set evolver.params.upstream_root or COEVOSKILLS_ROOT "
                           f"to a checkout at {COMMIT}")
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if head != COMMIT:
        raise RuntimeError(f"the CoEvoSkills checkout at {root} is at {head or 'no commit'}; {COMMIT} is required")
    changed = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all", "--",
                              *UPSTREAM_DIRS], capture_output=True, text=True).stdout.strip()
    if changed:
        raise RuntimeError(f"the CoEvoSkills checkout at {root} has local changes or untracked files in "
                           f"{', '.join(UPSTREAM_DIRS)}: {changed.splitlines()[0]}")
    return root


def checkout(value: str | Path | None = None) -> Path:
    """verify(), then put the checkout first on sys.path, so its `libs` and `scripts` are the ones imported (an
    installed package of the same top-level name is evicted). For the worker process only."""
    root = verify(value)
    for name in ("libs", "scripts"):
        module = sys.modules.get(name)
        locations = list(getattr(module, "__path__", [])) if module else []
        if module is not None and not any(Path(p).resolve().is_relative_to(root) for p in locations):
            for key in list(sys.modules):
                if key == name or key.startswith(name + "."):
                    del sys.modules[key]
    if str(root) in sys.path:
        sys.path.remove(str(root))
    sys.path.insert(0, str(root))
    return root


def safe_name(value: str) -> str:
    """A task id usable as a file name; raises otherwise."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", value) or value in {".", ".."}:
        raise ValueError(f"unsafe task id: {value!r}")
    return value


def real_tree(root: Path) -> None:
    """Raises unless `root` is a directory of plain files and directories (no links, no devices)."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"expected a real directory: {root}")
    for path in root.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError(f"links and special files are not allowed: {path}")
