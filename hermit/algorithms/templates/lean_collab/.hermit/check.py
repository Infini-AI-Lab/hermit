"""Template check: the candidate's exports still wire the team service.

exports.yaml is learnable (a tool's description, say), but the five team tools and the service's reactions are the
fixed code's: the candidate must still export each tool under its name with its entrypoint, and the reactions
entrypoint, or its exports.yaml goes back to the parent's.
"""
from __future__ import annotations

from pathlib import Path

import yaml

TOOLS = {name: f"collab.py:{name}" for name in ("check", "pull", "refute", "say", "plan")}
REACTIONS = "reactions.py:react"


def check(adapter_path: str) -> list[dict]:
    path = Path(adapter_path) / "exports.yaml"
    try:                                                # a missing exports.yaml exports nothing: the wiring is gone
        exports = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.is_file() else {}
    except (OSError, yaml.YAMLError):
        return []                                       # a broken exports.yaml is the library's check
    if not isinstance(exports, dict):
        return []
    declared = {t.get("name"): t.get("entrypoint") for t in exports.get("tools") or [] if isinstance(t, dict)}
    problems = [f"the {name} tool must stay exported with entrypoint {entry}"
                for name, entry in TOOLS.items() if declared.get(name) != entry]
    reactions = exports.get("reactions")
    if not isinstance(reactions, dict) or reactions.get("entrypoint") != REACTIONS:
        problems.append(f"the reactions entrypoint must stay {REACTIONS}")
    return [{"path": "exports.yaml", "problem": p} for p in problems]
