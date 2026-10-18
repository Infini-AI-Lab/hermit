"""Template check (check 6): every tool bundle under tools/ follows the rules in interface.py.

The Trainer loads this file from the template root (as `python -c` under bubblewrap, no network, 120 s) and passes the
candidate adapter's path. It parses TOOL.json files and reads sources; it never runs or imports a bundle. Every issue
names an existing file — the bundle's TOOL.json, or the stray file itself — so the Trainer's repair removes that file
alone and the rest of the step (ADAPTER.md, the other tools) survives. interface.py is loaded by file path from this
file's own directory: it is a sibling, not a package.
"""
from __future__ import annotations

from pathlib import Path
import types
import uuid


def _interface():
    """The sibling interface.py as a module, executed from source so no bytecode cache lands beside it."""
    path = Path(__file__).resolve().parent / "interface.py"
    module = types.ModuleType("_toolsmith_interface_" + uuid.uuid4().hex)
    module.__file__ = str(path)
    exec(compile(path.read_text("utf-8"), str(path), "exec"), module.__dict__)
    return module


def check(adapter_path: str) -> list[dict]:
    adapter = Path(adapter_path).resolve()
    tools = adapter / "tools"
    if not tools.is_dir():
        return []
    iface = _interface()
    issues: list[dict] = []
    seen: set[str] = set()
    for entry in sorted(tools.iterdir()):
        if entry.is_dir() and not entry.name.startswith(("_", ".")) and (entry / "TOOL.json").is_file():
            issues.extend(iface.validate_bundle(adapter, entry.name))
            key = entry.name.casefold()
            if key in seen:
                issues.append({"path": f"tools/{entry.name}/TOOL.json", "check": 6,
                               "problem": f"duplicate tool name {entry.name!r}"})
            seen.add(key)
            continue
        # Not a tool (a loose file, a directory without TOOL.json, a '_' or '.' name): ignored, except that no file
        # under tools/ may carry a page. tools/README.md is the template's own fixed text (check 1 guards it).
        files = [entry] if entry.is_file() else sorted(p for p in entry.rglob("*") if p.is_file())
        for path in files:
            rel = path.relative_to(adapter).as_posix()
            if rel == "tools/README.md" or "__pycache__" in path.parts or path.is_symlink():
                continue
            marker = iface.html_marker(path.read_text("utf-8", errors="replace"))
            if marker:
                issues.append({"path": rel, "check": 6, "problem": marker})
    return issues
