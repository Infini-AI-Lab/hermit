#!/usr/bin/env python3
"""Deploy an adapter into a Claude Code workspace: the whole adapter under hermit/<id>/, and its exports connected to
Claude Code's own places: CLAUDE.md context, .mcp.json tools, roles, skills, and hooks in .claude/settings.json."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from hermit.adapter.loader import agent_tools, hook_definitions, load_manifest, not_portable
from hermit.hosts.context import PROJECT_CONTEXT, START, append_context
from hermit.hosts.inputs import (MARKER, PACKAGE_ROOT, _adapter_sandbox_env, adapter_files, adapter_folder,
                                 check_workspace, copy_adapter, copy_skills, folder_note, hook_commands, render_exports,
                                 retire_roles, role_body, server_name, source_label, subagent_markdown, subagent_name)


def prepare(adapter: Path, output: Path, python: str, *, learning_material: Path | None = None) -> dict:
    adapter, output = adapter.resolve(), output.resolve()
    refused = not_portable(load_manifest(adapter), "A Claude Code workspace")   # before anything is written
    if refused:
        raise ValueError(refused)
    previous = check_workspace(adapter, output, "claude-code")
    output.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(adapter)
    files = adapter_files(adapter, output, manifest, previous)
    hooks = hook_definitions(adapter, manifest)
    settings_path = output / ".claude" / "settings.json"     # the hooks' place; other settings in it are kept
    settings = json.loads(settings_path.read_text()) if settings_path.is_file() else {}
    if not isinstance(settings, dict):
        raise ValueError(f"{settings_path} is not a JSON object")
    exports = render_exports(adapter, python)
    converted = []
    for role in exports["agents"]:
        converted.append((role, subagent_name(role["name"]), role_body(adapter, role)))
    if len({name for _, name, _ in converted}) != len(converted):
        raise ValueError("Role names collide after native name conversion")
    context = folder_note(output, manifest) + "\n\n" + exports["context"]
    for role, native, _ in converted:
        context = context.replace(f"`{role['name']}`", f"`{native}`")
    context_path = output / "CLAUDE.md"
    existing = context_path.read_text() if context_path.exists() else PROJECT_CONTEXT
    if existing.startswith(START):
        existing = PROJECT_CONTEXT + "\n\n" + existing
    context_text = append_context(existing, context, source=source_label(manifest), version=manifest["version"],
                                  adapter_id=manifest["id"])
    has_tools = bool(agent_tools(manifest))     # the context tool is the harness's, not served
    if has_tools:
        subprocess.run([python, "-c", "import mcp.server"], check=True, capture_output=True)
    agents_dir = output / ".claude/agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    role_paths, name_map = {}, {}
    for role, native, instructions in converted:
        path = agents_dir / f"{native}.md"
        path.write_text(subagent_markdown(native, role["description"], instructions))
        role_paths[native] = str(path)
        name_map[role["name"]] = native
    audit = output / "tool-calls.jsonl"
    server = None
    if has_tools:
        server = {"type": "stdio", "command": python,
                  "args": ["-m", "hermit.runtime.mcp_server", "--adapter", str(adapter), "--audit-log", str(audit)],
                  "env": {"PYTHONPATH": PACKAGE_ROOT, **_adapter_sandbox_env(python)}}
        if learning_material is not None:
            server["args"].extend(["--learning-material", str(Path(learning_material).resolve())])
    (output / ".mcp.json").write_text(json.dumps({"mcpServers": {server_name(manifest): server} if has_tools else {}},
                                                 indent=2) + "\n")
    context_path.write_text(context_text)
    if hooks:
        settings["hooks"] = hook_commands(hooks, adapter_folder(output, manifest))["hooks"]
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    elif previous.get("hooks") and "hooks" in settings:
        settings.pop("hooks")
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    retire_roles(output, previous, role_paths)
    skills = copy_skills(adapter, manifest, output / ".claude" / "skills")
    copy_adapter(adapter, output, manifest, files, previous)
    result = {"host": "claude-code", "adapter_id": manifest["id"], "version": manifest["version"], "adapter": str(adapter),
              "workspace": str(output), "context_injected_via": "CLAUDE.md",
              "server_name": server_name(manifest) if has_tools else None, "server": server, "roles": role_paths,
              "role_name_map": name_map, "skills_dir": str(output / ".claude" / "skills") if skills else None,
              "skills": skills, "hooks": str(settings_path) if hooks else None,
              "audit_log": str(audit) if has_tools else None,
              "adapter_dir": str(adapter_folder(output, manifest)), "files": files}
    (output / MARKER).write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--workspace", "--output", dest="workspace", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    print(json.dumps(prepare(args.adapter, args.workspace, str(Path(args.python).absolute())), indent=2))
