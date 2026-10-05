#!/usr/bin/env python3
"""Deploy an adapter's exports into a Claude Code workspace: CLAUDE.md context, .mcp.json tools, roles, skills."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from harnessadapter.adapter.loader import inside, load_manifest
from harnessadapter.hosts.context import PROJECT_CONTEXT, START, append_context
from harnessadapter.hosts.inputs import (MARKER, PACKAGE_ROOT, check_workspace, copy_skills, render_exports, retire_roles,
                                         server_name, source_label)


def prepare(adapter: Path, output: Path, python: str, *, learning_material: Path | None = None) -> dict:
    adapter, output = adapter.resolve(), output.resolve()
    previous = check_workspace(adapter, output, "claude-code")
    output.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(adapter)
    exports = render_exports(adapter, python)
    converted = []
    for role in exports["agents"]:
        native = role["name"].replace("_", "-")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", native):
            raise ValueError(f"Invalid native role name: {native}")
        converted.append((role, native, inside(adapter, role["instructions"]).read_text()))
    if len({name for _, name, _ in converted}) != len(converted):
        raise ValueError("Role names collide after native name conversion")
    context = exports["context"]
    for role, native, _ in converted:
        context = context.replace(f"`{role['name']}`", f"`{native}`")
    context_path = output / "CLAUDE.md"
    existing = context_path.read_text() if context_path.exists() else PROJECT_CONTEXT
    if existing.startswith(START):
        existing = PROJECT_CONTEXT + "\n\n" + existing
    context_text = append_context(existing, context, source=source_label(manifest), version=manifest["version"],
                                  adapter_id=manifest["id"])
    has_tools = bool(manifest.get("tools"))
    if has_tools:
        subprocess.run([python, "-c", "import mcp.server"], check=True, capture_output=True)
    agents_dir = output / ".claude/agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    role_paths, name_map = {}, {}
    for role, native, instructions in converted:
        path = agents_dir / f"{native}.md"
        path.write_text(f"---\nname: {json.dumps(native)}\ndescription: {json.dumps(role['description'])}\n---\n\n{instructions}")
        role_paths[native] = str(path)
        name_map[role["name"]] = native
    audit = output / "tool-calls.jsonl"
    server = None
    if has_tools:
        server = {"type": "stdio", "command": python,
                  "args": ["-m", "harnessadapter.runtime.mcp_server", "--adapter", str(adapter), "--audit-log", str(audit)],
                  "env": {"PYTHONPATH": PACKAGE_ROOT}}
        if learning_material is not None:
            server["args"].extend(["--learning-material", str(Path(learning_material).resolve())])
    (output / ".mcp.json").write_text(json.dumps({"mcpServers": {server_name(manifest): server} if has_tools else {}},
                                                 indent=2) + "\n")
    context_path.write_text(context_text)
    retire_roles(output, previous, role_paths)
    skills = copy_skills(adapter, manifest, output / ".claude" / "skills")
    result = {"host": "claude-code", "adapter_id": manifest["id"], "version": manifest["version"], "adapter": str(adapter),
              "workspace": str(output), "context_injected_via": "CLAUDE.md",
              "server_name": server_name(manifest) if has_tools else None, "server": server, "roles": role_paths,
              "role_name_map": name_map, "skills_dir": str(output / ".claude" / "skills") if skills else None,
              "skills": skills, "audit_log": str(audit) if has_tools else None}
    (output / MARKER).write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    print(json.dumps(prepare(args.adapter, args.output, str(Path(args.python).absolute())), indent=2))
