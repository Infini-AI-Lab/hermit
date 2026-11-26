"""Validate the template's Codex hook surface without running learner code."""

import json
from pathlib import Path
import re

import yaml

EVENTS = {
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "PreCompact",
    "PostCompact",
    "SessionStart",
    "SubagentStart",
    "SubagentStop",
    "UserPromptSubmit",
    "Stop",
}


def check(adapter_path):
    root = Path(adapter_path)
    errors = []
    if (
        not (root / "ADAPTER.md").is_file()
        or not (root / "ADAPTER.md").read_text().strip()
    ):
        errors.append(
            {
                "path": "ADAPTER.md",
                "problem": "Project instructions must remain nonempty",
            }
        )
    try:
        value = json.loads((root / ".codex/hooks.json").read_text())
        hooks = value["hooks"]
        if not isinstance(hooks, dict) or set(hooks) - EVENTS:
            raise ValueError("Unknown hook event")
        for event, groups in hooks.items():
            # Hermit loads the hooks only in this shape (hermit.adapter.loader): any other fails every session
            if not isinstance(groups, list) or not all(
                isinstance(g, dict) and isinstance(g.get("hooks"), list) for g in groups
            ):
                raise ValueError(f'{event} must be a list of {{"hooks": [...]}} groups')
            for group in groups:
                if event == "Stop" and group.get("matcher"):
                    raise ValueError(
                        "The interactive Stop dispatcher requires unconditional hooks"
                    )
                for hook in group["hooks"]:
                    if not isinstance(hook, dict) or hook.get("type") != "command" or not (
                        isinstance(hook.get("command"), str) and hook["command"].strip()
                    ):
                        raise ValueError("Only command hooks with a command are supported")
                    timeout = hook.get("timeout")
                    if timeout is not None and (
                        isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3600
                    ):  # whole seconds, as Codex and the Stop dispatcher read it: else every session fails
                        raise ValueError(f"A hook's timeout is whole seconds, an integer in [1, 3600], not {timeout!r}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append({"path": ".codex/hooks.json", "problem": str(exc)})
    try:  # the roles as Hermit reads them; Codex takes them as lowercase identifiers, `-` read as `_`, each its own
        from hermit.adapter.loader import subagent_files
        roles = subagent_files(root, "agents")
        names = [str(role["name"]).replace("-", "_") for role in roles]
        for role, name in zip(roles, names):  # each role's own file: what Hermit's repair restores
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or names.count(name) > 1:
                errors.append({"path": role["instructions"], "problem": "A role's name must be a lowercase "
                               f"identifier (letters, digits, - or _), distinct from the others: not {role['name']!r}"})
    except (OSError, ValueError, ImportError, yaml.YAMLError):  # a malformed role file: Hermit's deploy check
        pass                                                        # reports it (a folder named here, its repair
                                                                    # could not restore)
    for script in root.glob("scripts/*.py"):
        try:
            compile(script.read_text(), str(script), "exec")
        except (SyntaxError, ValueError) as exc:
            errors.append(
                {"path": script.relative_to(root).as_posix(), "problem": str(exc)}
            )
    return errors
