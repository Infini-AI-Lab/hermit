"""Emit one plugin directory that both Claude Code and Codex install.

Both hosts read the same marketplace file, so a single artifact serves both. They do not agree on everything:

- Claude Code expands `${CLAUDE_PLUGIN_ROOT}` inside the MCP config and exports it to the server it spawns, so
  its entry stays relocatable.
- Codex expands nothing and resolves nothing: a relative command, `./cmd`, `${CODEX_PLUGIN_ROOT}` and
  `${CLAUDE_PLUGIN_ROOT}` are all passed through literally, and the server is spawned with the user's working
  directory. Its adapter path is therefore written absolute, and moving the artifact invalidates it.
- A Codex plugin carries skills and MCP servers only. Roles have no place in it, so an adapter that exports
  them is refused rather than quietly shipped without them.

Each host reads its own manifest, so each manifest points at its own MCP file in the same directory.
"""
from __future__ import annotations

from pathlib import Path
import json

from harnessadapter.plugins.common import (ADAPTER_DIR, AUDIT_LOG, SKILL, Bundle, Unsupported, context_skill,
                                           copy_skills, embed_adapter, load, mcp_entry, subagent_file, write_json)

CLAUDE_ROOT = "${CLAUDE_PLUGIN_ROOT}"


def _marketplace(bundle: Bundle, output: Path, plugin_dir: Path) -> None:
    write_json(output / ".claude-plugin" / "marketplace.json", {
        "name": f"{bundle.id}-adapters",
        "owner": {"name": "harnessadapter"},
        "plugins": [{"name": bundle.id,
                     "source": "./" + plugin_dir.relative_to(output).as_posix(),
                     "description": bundle.manifest.get("name", bundle.id)}],
    })


def build(adapter: Path, output: Path, python: str, *, target: str = "claude-code",
          allow_lossy: bool = False) -> dict:
    """Emit the artifact both hosts install. `target` is the host the caller asked for: it decides only whether
    an export the other host cannot represent is a refusal or a documented omission."""
    bundle = load(adapter, python)
    output = output.resolve()
    if output.exists() and any(output.iterdir()) and not (output / ".claude-plugin" / "marketplace.json").is_file():
        raise ValueError("Refusing to write into a nonempty directory this framework did not create")
    plugin = output / "plugins" / bundle.id
    plugin.mkdir(parents=True, exist_ok=True)

    # Claude Code carries roles; a Codex plugin does not. Only refuse when Codex is the host that was asked for.
    skipped = {}
    if bundle.roles and target == "codex" and not allow_lossy:
        raise Unsupported(
            f"A Codex plugin holds skills and MCP servers only, but {bundle.id} exports "
            f"{len(bundle.roles)} role(s): {', '.join(r['name'] for r in bundle.roles)}. "
            "Use `harnessadapter deploy --host codex`, which supports roles, pass --allow-lossy to emit the "
            "plugin with the roles reachable from Claude Code only, or convert with --target claude-code.")

    # Skills: the adapter's own, plus its context, which a plugin cannot inject any other way.
    skills_dir = plugin / "skills"
    skills = copy_skills(bundle, skills_dir)
    name, body = context_skill(bundle)
    (skills_dir / name).mkdir(parents=True, exist_ok=True)
    (skills_dir / name / SKILL).write_text(body)
    skills = [name] + skills

    roles = {}
    for role in bundle.roles:
        native, text = subagent_file(bundle, role)
        (plugin / "agents").mkdir(parents=True, exist_ok=True)
        (plugin / "agents" / f"{native}.md").write_text(text)
        roles[role["name"]] = native
    if roles:
        skipped["codex"] = sorted(roles.values())

    embed_adapter(bundle, plugin)

    manifest = {"name": bundle.id, "version": bundle.version,
                "description": bundle.manifest.get("name", bundle.id), "skills": "./skills/"}
    claude = dict(manifest)
    codex = dict(manifest)
    # `agents` is discovered from the conventional directory. Naming it in the manifest fails validation as a
    # string and silently loads nothing as a list, so the key is deliberately absent.
    if bundle.has_tools:
        write_json(plugin / "mcp-claude.json", {"mcpServers": {bundle.server: mcp_entry(
            bundle, f"{CLAUDE_ROOT}/{ADAPTER_DIR}", f"{CLAUDE_ROOT}/{AUDIT_LOG}")}})
        write_json(plugin / "mcp-codex.json", {"mcpServers": {bundle.server: mcp_entry(
            bundle, str(plugin / ADAPTER_DIR), str(plugin / AUDIT_LOG))}})
        claude["mcpServers"] = "./mcp-claude.json"
        codex["mcpServers"] = "./mcp-codex.json"
    write_json(plugin / ".claude-plugin" / "plugin.json", claude)
    write_json(plugin / ".codex-plugin" / "plugin.json", codex)
    _marketplace(bundle, output, plugin)
    (plugin / "README.md").write_text(_readme(bundle, output, skills, roles))

    return {"target": "claude-code+codex", "requested": target, "adapter_id": bundle.id, "version": bundle.version,
            "artifact": str(output), "plugin": str(plugin), "marketplace": f"{bundle.id}-adapters",
            "skills": skills, "roles": roles, "context_as": f"skill:{name}",
            "server_name": bundle.server if bundle.has_tools else None,
            "relocatable": {"claude-code": True, "codex": False},
            "omitted": skipped}


def _readme(bundle: Bundle, output: Path, skills: list, roles: dict) -> str:
    lines = [f"# {bundle.manifest.get('name', bundle.id)}", "",
             f"Generated by `harnessadapter convert` from the `{bundle.id}` adapter, version {bundle.version}.",
             "", "## Install", "", "Claude Code:", "```",
             f"claude plugin marketplace add {output}",
             f"claude plugin install {bundle.id}@{bundle.id}-adapters", "```", "", "Codex:", "```",
             f"codex plugin marketplace add {output}",
             f"codex plugin add {bundle.id}@{bundle.id}-adapters", "```", "",
             "`harnessadapter` must be installed and on PATH: the MCP server is started by the host as",
             f"`harnessadapter-mcp`.", "", "## What is in it", "",
             f"- Skills: {', '.join(skills)}",
             f"- Roles: {', '.join(roles.values()) if roles else 'none'}"
             + (" (Claude Code only; a Codex plugin cannot carry roles)" if roles else ""),
             f"- Context: shipped as the skill `{bundle.id}-context`. A plugin cannot add to CLAUDE.md or",
             "  AGENTS.md, so the context is loaded when the skill's description matches rather than being",
             "  present in every turn.", "", "## Moving this directory", "",
             "Claude Code resolves the adapter through `${CLAUDE_PLUGIN_ROOT}`, so it keeps working.",
             "Codex expands no variables and resolves no relative paths, so its MCP entry holds an absolute",
             "path. If you move this directory, run `harnessadapter convert` again.", ""]
    return "\n".join(lines)
