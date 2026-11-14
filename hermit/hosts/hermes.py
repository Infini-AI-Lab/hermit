#!/usr/bin/env python3
"""Deploy an adapter into a Hermes Agent workspace: the whole adapter under hermit/<id>/, and its exports connected
to Hermes's own places: .hermes.md context, .hermes/skills, and .hermes/hermit.config.yaml, a part of Hermes's config.

Hermes reads one project context file from its working directory, the first it finds of .hermes.md (also searched
up to the repository's root), AGENTS.md, CLAUDE.md and .cursorrules: the context is .hermes.md, so no other file a
folder above holds outranks it. Hermes has no project file of tools or skills: MCP servers and skill folders are
settings of its own config.yaml ($HERMES_HOME), which a deployment never changes. The adapter's tool server and its
skills' folder (by its absolute path) are written instead as a part of that config, .hermes/hermit.config.yaml, for
the user to merge into their own, with the context size Hermes reads whole (its default cuts a context file past a
share of the model's context window). The `hermes` acting agent needs none of it: its Base Agent gives the session
the deployment's skills folder and serves the adapter's tools itself (`serve_tools` false). Hermes runs no hook file
of a project and has no role file: an adapter's roles and hooks stay files of its copy, which the context names with
what to do with them (hermit.hosts.inputs.resources), as it names the other files.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

import yaml

from hermit.adapter.loader import agent_tools, hook_definitions, load_manifest, not_portable
from hermit.hosts.context import PROJECT_CONTEXT, START, append_context
from hermit.hosts.inputs import (MARKER, PACKAGE_ROOT, _adapter_sandbox_env, adapter_files, adapter_folder,
                                 check_skills, check_workspace, copy_adapter, copy_skills, folder_note, render_exports,
                                 resources, retire_skills, server_name, source_label)

NATIVE = {"skills"}
CONTEXT = Path(".hermes.md")                       # Hermes's first project context file
SKILLS = Path(".hermes") / "skills"
CONFIG = Path(".hermes") / "hermit.config.yaml"    # the part of Hermes's config.yaml the deployment needs
TOOL_TIMEOUT_S = 120                               # Codex's tool_timeout_sec, not Hermes's 300 s
CONTEXT_CHARS = 1_000_000                          # context_file_max_chars: the context whole


def prepare(adapter: Path, output: Path, python: str, *, learning_material: Path | None = None,
            context: str | None = None, serve_tools: bool = True) -> dict:
    """`context`: the adapter's context for one task, which the harness computed, in place of the one rendered
    here; an adapter whose context is per task deploys only so. `serve_tools`: false when the caller serves the
    adapter's tools to the session itself (the `hermes` acting agent): the config part then names no server."""
    adapter, output = adapter.resolve(), output.resolve()
    refused = not_portable(load_manifest(adapter), "A Hermes Agent workspace")   # before anything is written
    if refused and context is None:
        raise ValueError(refused)
    previous = check_workspace(adapter, output, "hermes")
    manifest = load_manifest(adapter)
    files = adapter_files(adapter, output, manifest, previous)
    check_skills(adapter, output, manifest, previous, output / SKILLS)
    exports = render_exports(adapter, python)
    listed, placed = resources(adapter, output, manifest, exports, hook_definitions(adapter, manifest), NATIVE)
    has_tools = bool(agent_tools(manifest)) and serve_tools     # the context tool is the harness's, not served
    if has_tools:
        subprocess.run([python, "-c", "import mcp.server"], check=True, capture_output=True)
    output.mkdir(parents=True, exist_ok=True)
    context_path = output / CONTEXT
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
        server = {"command": python,
                  "args": ["-m", "hermit.runtime.mcp_server", "--adapter", str(adapter), "--audit-log", str(audit)],
                  "env": {"PYTHONPATH": PACKAGE_ROOT, **_adapter_sandbox_env(python)}, "timeout": TOOL_TIMEOUT_S}
        if learning_material is not None:
            server["args"].extend(["--learning-material", str(Path(learning_material).resolve())])
    skills = copy_skills(adapter, manifest, output / SKILLS)
    retire_skills(output, previous, skills, output / SKILLS)
    config = {"context_file_max_chars": CONTEXT_CHARS,
              **({"mcp_servers": {server_name(manifest): server}} if has_tools else {}),
              **({"skills": {"external_dirs": [str(output / SKILLS)]}} if skills else {})}
    (output / CONFIG.parent).mkdir(exist_ok=True)
    (output / CONFIG).write_text("# Merge into your Hermes config ($HERMES_HOME/config.yaml, default ~/.hermes): the "
                                 "deployment's tools and skills.\n" + yaml.safe_dump(config, sort_keys=False))
    context_path.write_text(context_text)
    copy_adapter(adapter, output, manifest, files, previous)
    result = {"host": "hermes", "adapter_id": manifest["id"], "version": manifest["version"],
              "adapter": str(adapter), "workspace": str(output), "context_injected_via": str(CONTEXT),
              "server_name": server_name(manifest) if has_tools else None, "server": server,
              "config": str(output / CONFIG), "roles": {}, "role_name_map": {},
              "skills_dir": str(output / SKILLS) if skills else None, "skills": skills, "hooks": None,
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
