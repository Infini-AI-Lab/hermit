"""Deploy an adapter into an OpenCode workspace: AGENTS.md, local MCP tools, subagents and skills."""
import json
from pathlib import Path
import subprocess

from hermit.adapter.loader import load_manifest
from hermit.hosts.context import PROJECT_CONTEXT, append_context
from hermit.hosts.inputs import (MARKER, PACKAGE_ROOT, _adapter_sandbox_env, check_workspace, copy_skills,
                                render_exports, retire_roles, role_body, server_name, source_label, subagent_name)


def prepare(adapter: Path, output: Path, python: str, *, learning_material: Path | None = None) -> dict:
    adapter, output = adapter.resolve(), output.resolve()
    previous = check_workspace(adapter, output, "opencode")
    manifest = load_manifest(adapter)
    exports = render_exports(adapter, python)
    converted = [(role, subagent_name(role["name"]), role_body(adapter, role)) for role in exports["agents"]]
    if len({native for _, native, _ in converted}) != len(converted):
        raise ValueError("Role names collide after native name conversion")
    context = exports["context"]
    for role, native, _ in converted:
        context = context.replace(f"`{role['name']}`", f"`{native}`")
    context_path = output / "AGENTS.md"
    existing = context_path.read_text(encoding="utf-8") if context_path.exists() else PROJECT_CONTEXT
    context_text = append_context(existing, context, source=source_label(manifest), version=manifest["version"],
                                  adapter_id=manifest["id"])
    audit = output / "tool-calls.jsonl"
    server = None
    if manifest.get("tools"):
        subprocess.run([python, "-c", "import mcp.server"], check=True, capture_output=True)
        command = [python, "-m", "hermit.runtime.mcp_server", "--adapter", str(adapter), "--audit-log", str(audit)]
        if learning_material is not None:
            command.extend(["--learning-material", str(Path(learning_material).resolve())])
        server = {"type": "local", "command": command, "enabled": True,
                  "environment": {"PYTHONPATH": PACKAGE_ROOT, **_adapter_sandbox_env(python)}}
    output.mkdir(parents=True, exist_ok=True)
    role_paths, name_map = {}, {}
    for role, native, instructions in converted:
        path = output / ".opencode" / "agents" / f"{native}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ndescription: {json.dumps(role['description'])}\nmode: subagent\n---\n\n{instructions}",
                        encoding="utf-8")
        role_paths[native] = str(path)
        name_map[role["name"]] = native
    config = {"$schema": "https://opencode.ai/config.json", "mcp": {server_name(manifest): server} if server else {}}
    (output / "opencode.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    context_path.write_text(context_text, encoding="utf-8")
    retire_roles(output, previous, role_paths)
    skills_dir = output / ".opencode" / "skills"
    skills = copy_skills(adapter, manifest, skills_dir)
    result = {"host": "opencode", "adapter_id": manifest["id"], "version": manifest["version"], "adapter": str(adapter),
              "workspace": str(output), "context_injected_via": "AGENTS.md",
              "server_name": server_name(manifest) if server else None, "server": server, "roles": role_paths,
              "role_name_map": name_map, "skills_dir": str(skills_dir) if skills else None,
              "skills": skills, "audit_log": str(audit) if server else None}
    (output / MARKER).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result
