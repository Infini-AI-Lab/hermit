"""Template check: the candidate's host package still renders, and its exports still serve the wm tool.

The old port's `validate()` rendered the context entrypoint before a candidate was adopted; a learner edit that
breaks the package (a memory entry the catalog cannot parse, a brief the cap code rejects) would otherwise reach
every host of the next block. Tool bundles are not checked here: an incomplete or failing bundle is parked by
the Evolver's admission step, as natively, not reverted. Issues name the adapter file a traceback points at, so repair can revert that file
alone; an issue without a path reverts the whole session, as a failed validation discarded it natively.

exports.yaml is learnable, but hosts reach the world model only through the wm tool: the candidate must still
export a tool named wm whose entrypoint is interface.py:wm, or its exports.yaml goes back to the parent's.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import traceback
import types
import uuid

import yaml

WM_ENTRYPOINT = "interface.py:wm"


def _interface(adapter: Path):
    """The candidate's own interface.py, loaded as a package so its relative imports resolve."""
    name = "_mwm_check_" + uuid.uuid4().hex
    package = types.ModuleType(name)
    package.__path__ = [str(adapter)]
    sys.modules[name] = package
    spec = importlib.util.spec_from_file_location(name + ".interface", adapter / "interface.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _path_in(adapter: Path, text: str) -> str:
    for path in sorted(adapter.rglob("*")):
        if path.is_file() and str(path) in text:
            return path.relative_to(adapter).as_posix()
    return ""


def _wm_tool(adapter: Path) -> str:
    """Why the candidate's exports no longer serve the wm tool, or "" when they do."""
    try:
        exports = yaml.safe_load((adapter / "exports.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ""                                       # a missing or broken exports.yaml is the library's check
    tools = exports.get("tools") if isinstance(exports, dict) else None
    wm = [t for t in tools or [] if isinstance(t, dict) and t.get("name") == "wm"]
    if not wm:
        return "exports.yaml no longer exports the wm tool"
    if wm[0].get("entrypoint") != WM_ENTRYPOINT:
        return f"the wm tool's entrypoint must stay {WM_ENTRYPOINT}"
    return ""


def check(adapter_path: str) -> list[dict]:
    adapter = Path(adapter_path).resolve()
    issues = []
    problem = _wm_tool(adapter)
    if problem:
        issues.append({"path": "exports.yaml", "problem": problem})
    try:
        _interface(adapter).context({})
    except Exception:                                   # noqa: BLE001
        text = traceback.format_exc()
        issues.append({"path": _path_in(adapter, text),
                       "problem": "the host package does not render: " + text.strip().splitlines()[-1]})
    return issues
