#!/usr/bin/env python3
"""Deploy an adapter into a Pi workspace: the whole adapter under hermit/<id>/, and its exports connected to Pi's
own places: AGENTS.md context, .pi/mcp.json tools, .pi/skills.

Pi reads AGENTS.md from the working directory and its parents without any trust prompt; the project `.pi/`
configuration (mcp.json, skills) loads only after the user trusts the project in Pi. Pi has no subagents and reads
no hook file, so an adapter that exports roles or hooks is refused. Global Pi settings (~/.pi/agent) are never changed.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from hermit.adapter.loader import agent_tools, hook_definitions, load_manifest, not_portable
from hermit.hosts.context import PROJECT_CONTEXT, START, append_context
from hermit.hosts.inputs import (MARKER, PACKAGE_ROOT, adapter_files, adapter_folder, check_workspace, copy_adapter,
                                 copy_skills, folder_note, render_exports, server_name, source_label)


def prepare(adapter: Path, output: Path, python: str, *, learning_material: Path | None = None) -> dict:
    adapter, output = adapter.resolve(), output.resolve()
    refused = not_portable(load_manifest(adapter), "A Pi workspace")   # before anything is written
    if refused:
        raise ValueError(refused)
    previous = check_workspace(adapter, output, "pi")
    manifest = load_manifest(adapter)
    files = adapter_files(adapter, output, manifest, previous)
    if (hook_definitions(adapter, manifest) or {}).get("hooks"):
        raise ValueError(f"Pi reads no hook file, so this adapter's hooks ({manifest['hooks']}) cannot be deployed; "
                         "deploy with --host codex or --host claude-code")
    exports = render_exports(adapter, python)
    if exports["agents"]:
        names = ", ".join(r["name"] for r in exports["agents"])
        raise ValueError(f"Pi has no subagents, so this adapter's roles cannot be deployed ({names}); "
                         "deploy with --host codex or --host claude-code")
    has_tools = bool(agent_tools(manifest))     # the context tool is the harness's, not served
    if has_tools:
        subprocess.run([python, "-c", "import mcp.server"], check=True, capture_output=True)
    output.mkdir(parents=True, exist_ok=True)
    context_path = output / "AGENTS.md"
    existing = context_path.read_text() if context_path.exists() else PROJECT_CONTEXT
    if existing.startswith(START):
        existing = PROJECT_CONTEXT + "\n\n" + existing
    context_text = append_context(existing, folder_note(output, manifest) + "\n\n" + exports["context"],
                                  source=source_label(manifest),
                                  version=manifest["version"], adapter_id=manifest["id"])
    audit = output / "tool-calls.jsonl"
    server = None
    if has_tools:
        server = {"command": python,
                  "args": ["-m", "hermit.runtime.mcp_server", "--adapter", str(adapter), "--audit-log", str(audit)],
                  "env": {"PYTHONPATH": PACKAGE_ROOT}}
        if learning_material is not None:
            server["args"].extend(["--learning-material", str(Path(learning_material).resolve())])
    (output / ".pi").mkdir(exist_ok=True)
    (output / ".pi" / "mcp.json").write_text(json.dumps({"mcpServers": {server_name(manifest): server} if has_tools else {}},
                                                        indent=2) + "\n")
    context_path.write_text(context_text)
    skills = copy_skills(adapter, manifest, output / ".pi" / "skills")
    copy_adapter(adapter, output, manifest, files, previous)
    result = {"host": "pi", "adapter_id": manifest["id"], "version": manifest["version"], "adapter": str(adapter),
              "workspace": str(output), "context_injected_via": "AGENTS.md",
              "server_name": server_name(manifest) if has_tools else None, "server": server, "roles": {},
              "role_name_map": {}, "skills_dir": str(output / ".pi" / "skills") if skills else None,
              "skills": skills, "hooks": None, "audit_log": str(audit) if has_tools else None,
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
