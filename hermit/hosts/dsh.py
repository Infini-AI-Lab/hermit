#!/usr/bin/env python3
"""Deploy an adapter into a DeepSeek Harness (dsh) workspace: the whole adapter under hermit/<id>/, and its exports
connected to dsh's own places: AGENTS.md context, .dsh/skills, and a patch layer, .dsh/hermit.patch.yml.

dsh reads, without any trust prompt, the instruction files (AGENTS.md, CLAUDE.md and their .local overlays) of every
folder from the project's root down to the working directory, and the skills of the root's .dsh/skills and
.agents/skills; the root is the nearest folder with a `.git`, the working directory included, else the working
directory. It has no project file for MCP servers or for more skill folders: they are rows of its plugin tree, so the
patch layer holds the adapter's tool server (an `insert` row: a server dsh cannot start is reported on its stderr, not
retried in the background, and the `dsh` acting agent then fails the session; dsh itself goes on without the tools)
and names the workspace's .dsh/skills (the `skill-filesystem` row's customSkillDirs, so that a workspace below a
repository's root keeps its skills; the row replaces any `skill-filesystem` settings of the user's own while the layer
is loaded, and dsh's web profile turns the row off). A session loads the layer from the workspace with `dsh --profile
headless --patch .dsh/hermit.patch.yml`, as the `dsh` acting agent does. dsh gives the model 65,536 bytes of a
project's instructions (its agent-instructions maxBytes), its own frame around them included, and cuts the rest, so a
longer AGENTS.md is refused: exactly for a session whose project root is the workspace and which holds no other
instruction file (the acting agent's); a session below a repository's root also counts that repository's AGENTS.md
files. dsh runs no hook file of its own (its experimental bridge to Claude Code's hooks is not used) and has no role
file: an adapter's roles and hooks stay files of its copy, which AGENTS.md names with what to do with them
(hermit.hosts.inputs.resources), as it names the other files. dsh's own settings ($DSH_HOME) are never changed.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from hermit.adapter.loader import agent_tools, hook_definitions, load_manifest, not_portable
from hermit.agent.base.cli import DSH_INSTRUCTION_BYTES, DSH_INSTRUCTION_FRAME
from hermit.hosts.context import PROJECT_CONTEXT, START, append_context
from hermit.hosts.inputs import (MARKER, PACKAGE_ROOT, _adapter_sandbox_env, adapter_files, adapter_folder,
                                 check_workspace, copy_adapter, copy_skills, folder_note, render_exports, resources,
                                 server_name, source_label)

NATIVE = {"skills"}
PATCH = Path(".dsh") / "hermit.patch.yml"     # the workspace's patch layer: the adapter's tool server and skills
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
    # as dsh gives it to the model: in its frame, a `</system-reminder>` of the text escaped by one more character
    size = len(context_text.encode("utf-8"))
    if size + DSH_INSTRUCTION_FRAME + context_text.count("</system-reminder>") > DSH_INSTRUCTION_BYTES:
        raise ValueError(f"AGENTS.md would be {size} bytes: with the {DSH_INSTRUCTION_FRAME} bytes dsh puts around "
                         f"it, past the {DSH_INSTRUCTION_BYTES} bytes dsh gives the model of a project's instructions "
                         "(its agent-instructions maxBytes): dsh would cut the rest, telling only the model. Shorten "
                         "the adapter's context")
    audit = output / "tool-calls.jsonl"
    server = None
    if has_tools:
        server = {"serverName": server_name(manifest), "transport": "stdio", "command": python,
                  "args": ["-m", "hermit.runtime.mcp_server", "--adapter", str(adapter), "--audit-log", str(audit)],
                  "env": {"PYTHONPATH": PACKAGE_ROOT, **_adapter_sandbox_env(python)},
                  "toolCallTimeoutMs": 120_000,               # Codex's tool_timeout_sec, not dsh's 60 s
                  # a server dsh cannot start fails its row, which dsh reports on stderr and the dsh acting agent
                  # reads as a failed session, rather than retrying it in the background while the session runs
                  # without it (dsh itself goes on either way)
                  "failOnStartupError": True}
        if learning_material is not None:
            server["args"].extend(["--learning-material", str(Path(learning_material).resolve())])
    skills = copy_skills(adapter, manifest, output / ".dsh" / "skills")
    rows = [*([{"insert": [{"id": f"mcp-{server_name(manifest)}", "name": "@deepseek-ai/dsh-mcp-client",
                            "config": server}]}] if has_tools else []),
            # the workspace's own skills wherever it lies, beside dsh's default roots (the row's settings replace
            # those of the user's own home layer while the layer is loaded: a row's `config` is replaced whole)
            *([{"id": "skill-filesystem", "config": {"customSkillDirs": [".dsh/skills"]}}] if skills else [])]
    (output / PATCH.parent).mkdir(exist_ok=True)
    # JSON is YAML: dsh reads the layer as YAML, and nothing in it is a `!!js` expression
    (output / PATCH).write_text(json.dumps(rows, indent=2) + "\n")
    context_path.write_text(context_text)
    copy_adapter(adapter, output, manifest, files, previous)
    result = {"host": "dsh", "adapter_id": manifest["id"], "version": manifest["version"], "adapter": str(adapter),
              "workspace": str(output), "context_injected_via": "AGENTS.md",
              "server_name": server_name(manifest) if has_tools else None, "server": server,
              "patch": str(output / PATCH) if rows else None, "roles": {}, "role_name_map": {},
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
