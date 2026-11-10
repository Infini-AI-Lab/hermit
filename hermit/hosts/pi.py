#!/usr/bin/env python3
"""Deploy an adapter into a Pi workspace: the whole adapter under hermit/<id>/, and its exports connected to Pi's
own places: AGENTS.md context, .pi/mcp.json tools, .pi/skills.

Pi reads AGENTS.md from the working directory and its parents without any trust prompt; the project `.pi/`
configuration (mcp.json, skills) loads only after the user trusts the project in Pi, or for one run with `pi
--approve` (what the `pi` acting agent passes). The tools are declared with `exposure: direct`, so the model is
offered them as tools, not only through Pi's code mode (its default). Pi has no subagents and reads no hook file:
an adapter's roles and hooks stay files of its copy, which AGENTS.md names with what to do with them
(hermit.hosts.inputs.resources), as it names the other files. Global Pi settings (~/.pi/agent) are never changed.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from hermit.adapter.loader import agent_tools, hook_definitions, load_manifest, not_portable
from hermit.hosts.context import PROJECT_CONTEXT, START, append_context
from hermit.hosts.inputs import (MARKER, PACKAGE_ROOT, adapter_files, adapter_folder, check_workspace, copy_adapter,
                                 copy_skills, folder_note, render_exports, resources, retire_skills, server_name,
                                 source_label)

NATIVE = {"skills"}


def prepare(adapter: Path, output: Path, python: str, *, learning_material: Path | None = None,
            context: str | None = None) -> dict:
    """`context`: the adapter's context for one task, which the harness computed, in place of the one rendered
    here; an adapter whose context is per task deploys only so."""
    adapter, output = adapter.resolve(), output.resolve()
    refused = not_portable(load_manifest(adapter), "A Pi workspace")   # before anything is written
    if refused and context is None:
        raise ValueError(refused)
    previous = check_workspace(adapter, output, "pi")
    manifest = load_manifest(adapter)
    files = adapter_files(adapter, output, manifest, previous)
    exports = render_exports(adapter, python)
    listed, placed = resources(adapter, output, manifest, exports, hook_definitions(adapter, manifest), NATIVE)
    has_tools = bool(agent_tools(manifest))     # the context tool is the harness's, not served
    if has_tools:
        subprocess.run([python, "-c", "import mcp.server"], check=True, capture_output=True)
    output.mkdir(parents=True, exist_ok=True)
    context_path = output / "AGENTS.md"
    existing = context_path.read_text() if context_path.exists() else PROJECT_CONTEXT
    if existing.startswith(START):
        existing = PROJECT_CONTEXT + "\n\n" + existing
    context_text = append_context(existing, "\n\n".join(t for t in (
                                      folder_note(output, manifest),
                                      exports["context"] if context is None else context, listed) if t),
                                  source=source_label(manifest),
                                  version=manifest["version"], adapter_id=manifest["id"])
    audit = output / "tool-calls.jsonl"
    server = None
    if has_tools:
        server = {"command": python, "exposure": "direct", "timeout": 120,     # Codex's tool_timeout_sec, not 60 s
                  "args": ["-m", "hermit.runtime.mcp_server", "--adapter", str(adapter), "--audit-log", str(audit)],
                  "env": {"PYTHONPATH": PACKAGE_ROOT}}
        if learning_material is not None:
            server["args"].extend(["--learning-material", str(Path(learning_material).resolve())])
    (output / ".pi").mkdir(exist_ok=True)
    (output / ".pi" / "mcp.json").write_text(json.dumps({"mcpServers": {server_name(manifest): server} if has_tools else {}},
                                                        indent=2) + "\n")
    context_path.write_text(context_text)
    skills = copy_skills(adapter, manifest, output / ".pi" / "skills")
    retire_skills(output, previous, skills, output / ".pi" / "skills")
    copy_adapter(adapter, output, manifest, files, previous)
    result = {"host": "pi", "adapter_id": manifest["id"], "version": manifest["version"], "adapter": str(adapter),
              "workspace": str(output), "context_injected_via": "AGENTS.md",
              "server_name": server_name(manifest) if has_tools else None, "server": server, "roles": {},
              "role_name_map": {}, "skills_dir": str(output / ".pi" / "skills") if skills else None,
              "skills": skills, "hooks": None, "audit_log": str(audit) if has_tools else None,
              "adapter_dir": str(adapter_folder(output, manifest)), "files": files, "placed": placed}
    (output / MARKER).write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--workspace", "--output", dest="workspace", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    print(json.dumps(prepare(args.adapter, args.workspace, str(Path(args.python).absolute())), indent=2))
