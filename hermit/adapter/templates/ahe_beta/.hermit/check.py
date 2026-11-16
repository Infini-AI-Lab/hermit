"""Validate the template's Codex hook surface without running learner code."""

import json
from pathlib import Path

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
            for group in groups:
                if event == "Stop" and group.get("matcher"):
                    raise ValueError(
                        "The interactive Stop dispatcher requires unconditional hooks"
                    )
                for hook in group["hooks"]:
                    if hook.get("type") != "command" or not isinstance(
                        hook.get("command"), str
                    ):
                        raise ValueError("Only command hooks are supported")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append({"path": ".codex/hooks.json", "problem": str(exc)})
    for script in root.glob("scripts/*.py"):
        try:
            compile(script.read_text(), str(script), "exec")
        except (SyntaxError, ValueError) as exc:
            errors.append(
                {"path": script.relative_to(root).as_posix(), "problem": str(exc)}
            )
    return errors
