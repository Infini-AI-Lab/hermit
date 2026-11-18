"""Emit a Claude Code plugin, inside a local marketplace that `claude plugin marketplace add` accepts.

Claude Code's plugin format carries every part of an adapter but its hooks, which `refuse` names (deploy connects
them in a workspace):

- context: an output style with `force-for-plugin: true`, whose text Claude Code appends to the system prompt
  while the plugin is enabled. `keep-coding-instructions: true` keeps the default coding instructions.
- tools: the conventional `.mcp.json` at the plugin root. Claude Code expands `${CLAUDE_PLUGIN_ROOT}` and
  `${CLAUDE_PLUGIN_DATA}` in it, so the entry stays valid wherever the plugin is installed.
- roles: subagent files in the conventional `agents/` directory. Naming that directory in the manifest breaks it:
  as a string the manifest fails validation, as a list nothing loads.
- skills: the conventional `skills/` directory.
- the adapter's files: embedded under `adapter/`. In training and in a `hermit deploy` workspace the context says
  where the adapter's copy is and names the files no export connects (hermit.hosts.inputs.resources). An output
  style cannot say where the plugin is installed (Claude Code substitutes `${CLAUDE_PLUGIN_ROOT}` in skills,
  agents, hooks and servers, not in an output style), so a `SessionStart` hook says it: Claude Code adds a
  SessionStart hook's output to the context. The hook is a POSIX shell command: `sh` on macOS and Linux, Git Bash on
  Windows (where Claude Code falls back to PowerShell without Git Bash, and the hook fails).
"""
from __future__ import annotations

from pathlib import Path
import json

from hermit.hosts.inputs import other_files_section
from hermit.plugins.common import (ADAPTER_DIR, AUDIT_LOG, FRAMEWORK_VERSION, SERVER_COMMAND, clear, copy_skills,
                                           embed_adapter, folder_sentence, load, names_folder, prepare_output,
                                           record, refuse, server_args, subagent_file, write_json)

SCHEMA = "https://anthropic.com/claude-code/plugin.schema.json"
MARKETPLACE = ".claude-plugin/marketplace.json"
FILES_NOTE = "hermit-files.md"            # the list of the adapter's other files the hook prints
HOOKS = "hooks/hooks.json"


def folder_hook(bundle, plugin: Path, skills: list[str]) -> str | None:
    """Write the hook that tells the session where the adapter's files are, as the deployment's context does
    (hermit.hosts.inputs.folder_note and the "Other files" list), when the adapter has any the agent may be sent to:
    files no export connects, or skills and roles whose paths are relative to the adapter. Returns the hooks file."""
    others = bundle.other_files
    if not names_folder(bundle, skills):
        return None
    # the installed path is printf's argument, never part of the command; an adapter id has no % or quote
    command = f"printf '{folder_sentence(bundle, '%s')}\\n' \"$CLAUDE_PLUGIN_ROOT\" \"$CLAUDE_PLUGIN_ROOT\""
    if others:
        # Past 10,000 characters Claude Code keeps a hook's output in a file and shows its first 2,000, which hold the
        # folder; the list (at most LISTED_FILES entries, as a deployment's) is shorter for any ordinary adapter.
        (plugin / FILES_NOTE).write_text("\n" + other_files_section(bundle.adapter, others, None) + "\n")
        command += f" && cat \"$CLAUDE_PLUGIN_ROOT/{FILES_NOTE}\""
    write_json(plugin / HOOKS, {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}})
    return HOOKS


def output_style(name: str, description: str, context: str) -> str:
    return (f"---\nname: {json.dumps(name)}\ndescription: {json.dumps(description)}\n"
            f"force-for-plugin: true\nkeep-coding-instructions: true\n---\n\n{context.strip()}\n")


def build(adapter: Path, output: Path, python: str, *, allow_lossy: bool = False) -> dict:
    bundle = load(adapter, python)
    omitted = refuse(bundle, "Claude Code", {}, allow_lossy=allow_lossy)
    output = prepare_output(output, bundle.adapter, MARKETPLACE)
    plugin = output / "plugins" / bundle.id
    plugin.mkdir(parents=True, exist_ok=True)
    clear(plugin, "agents", "output-styles", "skills", ".mcp.json", "hooks", FILES_NOTE)

    write_json(output / MARKETPLACE, {
        "name": f"{bundle.id}-adapters", "owner": {"name": "hermit"},
        "plugins": [{"name": bundle.id, "source": f"./plugins/{bundle.id}", "description": bundle.name}]})
    write_json(plugin / ".claude-plugin" / "plugin.json", {
        "$schema": SCHEMA, "name": bundle.id, "version": bundle.version, "description": bundle.name})

    roles = {}
    for role in bundle.roles:
        native, text = subagent_file(bundle, role)
        (plugin / "agents").mkdir(exist_ok=True)
        (plugin / "agents" / f"{native}.md").write_text(text)
        roles[role["name"]] = native

    context = bundle.context
    for name, native in roles.items():
        context = context.replace(f"`{name}`", f"`{native}`")   # as `deploy --host claude-code` does
    if bundle.has_context:
        (plugin / "output-styles").mkdir(exist_ok=True)
        (plugin / "output-styles" / f"{bundle.id}.md").write_text(
            output_style(bundle.id, f"Context of the {bundle.name} harness adapter", context))

    skills = copy_skills(bundle, plugin / "skills")
    hooks = folder_hook(bundle, plugin, skills)
    embed_adapter(bundle, plugin)
    record(bundle, plugin)
    if bundle.tools:
        write_json(plugin / ".mcp.json", {"mcpServers": {bundle.server: {
            "type": "stdio", "command": SERVER_COMMAND,
            "args": server_args(f"${{CLAUDE_PLUGIN_ROOT}}/{ADAPTER_DIR}", f"${{CLAUDE_PLUGIN_DATA}}/{AUDIT_LOG}")}}})
    (plugin / "README.md").write_text(_readme(bundle, output, skills, roles, omitted, hooks))
    return {"target": "claude-code", "adapter_id": bundle.id, "version": bundle.version,
            "framework": FRAMEWORK_VERSION, "output": str(output),
            "plugin": str(plugin), "install": [f"claude plugin marketplace add {output}",
                                               f"claude plugin install {bundle.id}@{bundle.id}-adapters"],
            "context": f"output-styles/{bundle.id}.md" if bundle.has_context else None,
            "skills": skills, "roles": roles, "server_name": bundle.server if bundle.tools else None,
            "folder_hook": hooks, "unnamed_files": [], "omitted": omitted}


def _readme(bundle, output: Path, skills: list, roles: dict, omitted: list, hooks: str | None) -> str:
    return "\n".join([
        f"# {bundle.name}", "",
        f"A Claude Code plugin generated by `hermit convert` from the `{bundle.id}` adapter, version "
        f"{bundle.version}.", "", "```",
        f"claude plugin marketplace add {output}", f"claude plugin install {bundle.id}@{bundle.id}-adapters", "```", "",
        "The tools run as `hermit serve`, so `hermit` must be installed and on PATH.", "",
        f"- Context: the output style `{bundle.id}`, applied automatically while the plugin is enabled "
        "(`force-for-plugin`)." if bundle.has_context else "- Context: none",
        (f"- The adapter's files: under `{ADAPTER_DIR}/`, which a `SessionStart` hook names to the session"
         + (f", with the {len(bundle.other_files)} files no export connects" if bundle.other_files else ""))
        if hooks else "- The adapter's files: under `adapter/`, for its tools; none is named to the session",
        f"- Skills: {', '.join(skills) or 'none'}", f"- Roles: {', '.join(roles.values()) or 'none'}",
        f"- Tools: {', '.join(t['name'] for t in bundle.tools) or 'none'}",
        f"- Left out (--allow-lossy): {', '.join(omitted)}" if omitted else "- Left out: nothing", ""])
