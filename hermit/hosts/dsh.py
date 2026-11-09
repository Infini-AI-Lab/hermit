#!/usr/bin/env python3
"""Deploy an adapter into a DeepSeek Harness (dsh) workspace: the whole adapter under hermit/<id>/, and its exports
connected to dsh's own places: AGENTS.md context, .dsh/skills, and the tools as a patch layer, .dsh/hermit.patch.yml.

dsh reads AGENTS.md at the project's root (the nearest folder above with a `.git`, else the working directory) and in
the working directory, and the skills of .dsh/skills at the project's root only, without any trust prompt: a
workspace that is not its project's root gets its skills only as the session's own, which the `dsh` acting agent
gives it (hermit.agent.base.cli._dsh_skills). It has no project file for MCP servers: they are rows of its
plugin tree, so the adapter's tool server is an `insert` row in a patch layer of the workspace, which a session
loads with `dsh --patch .dsh/hermit.patch.yml` (what the `dsh` acting agent passes). dsh reads no hook file and has
no role file: an adapter's roles and hooks stay files of its copy, which AGENTS.md names with what to do with them
(hermit.hosts.inputs.resources), as it names the other files. dsh's own settings ($DSH_HOME) are never changed.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from hermit.adapter.loader import agent_tools, hook_definitions, load_manifest, not_portable
from hermit.hosts.context import PROJECT_CONTEXT, START, append_context
from hermit.hosts.inputs import (MARKER, PACKAGE_ROOT, _adapter_sandbox_env, adapter_files, adapter_folder,
                                 check_workspace, copy_adapter, copy_skills, folder_note, render_exports, resources,
                                 server_name, source_label)

NATIVE = {"skills"}
PATCH = Path(".dsh") / "hermit.patch.yml"     # the workspace's patch layer: the adapter's tool server
SERVER_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")   # what dsh takes as an MCP server's name


def prepare(adapter: Path, output: Path, python: str, *, learning_material: Path | None = None,
            context: str | None = None) -> dict:
    """`context`: the adapter's context for one task, which the harness computed, in place of the one rendered
    here; an adapter whose context is per task deploys only so."""
    adapter, output = adapter.resolve(), output.resolve()
    refused = not_portable(load_manifest(adapter), "A DeepSeek Harness workspace")   # before anything is written
    if refused and context is None:
        raise ValueError(refused)
    previous = check_workspace(adapter, output, "dsh")
    manifest = load_manifest(adapter)
    has_tools = bool(agent_tools(manifest))     # the context tool is the harness's, not served
    if has_tools and not SERVER_NAME.fullmatch(server_name(manifest)):
        raise ValueError(f"dsh takes an MCP server name of 32 characters at most ([A-Za-z0-9_-]), not "
                         f"{server_name(manifest)!r} (the adapter's id)")
    files = adapter_files(adapter, output, manifest, previous)
    exports = render_exports(adapter, python)
    listed, placed = resources(adapter, output, manifest, exports, hook_definitions(adapter, manifest), NATIVE)
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
        server = {"serverName": server_name(manifest), "transport": "stdio", "command": python,
                  "args": ["-m", "hermit.runtime.mcp_server", "--adapter", str(adapter), "--audit-log", str(audit)],
                  "env": {"PYTHONPATH": PACKAGE_ROOT, **_adapter_sandbox_env(python)},
                  "toolCallTimeoutMs": 120_000}               # Codex's tool_timeout_sec, not dsh's 60 s
        if learning_material is not None:
            server["args"].extend(["--learning-material", str(Path(learning_material).resolve())])
    rows = [{"insert": [{"id": f"mcp-{server_name(manifest)}", "name": "@deepseek-ai/dsh-mcp-client",
                         "config": server}]}] if has_tools else []
    (output / PATCH.parent).mkdir(exist_ok=True)
    # JSON is YAML: dsh reads the layer as YAML, and nothing in it is a `!!js` expression
    (output / PATCH).write_text(json.dumps(rows, indent=2) + "\n")
    context_path.write_text(context_text)
    skills = copy_skills(adapter, manifest, output / ".dsh" / "skills")
    copy_adapter(adapter, output, manifest, files, previous)
    result = {"host": "dsh", "adapter_id": manifest["id"], "version": manifest["version"], "adapter": str(adapter),
              "workspace": str(output), "context_injected_via": "AGENTS.md",
              "server_name": server_name(manifest) if has_tools else None, "server": server,
              "patch": str(output / PATCH) if has_tools else None, "roles": {}, "role_name_map": {},
              "skills_dir": str(output / ".dsh" / "skills") if skills else None, "skills": skills, "hooks": None,
              "audit_log": str(audit) if has_tools else None,
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
