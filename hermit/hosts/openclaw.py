#!/usr/bin/env python3
"""Deploy an adapter into an OpenClaw workspace: the whole adapter under hermit/<id>/, and its exports connected to
OpenClaw's own places: AGENTS.md context, openclaw.run.json MCP tools, skills/.

Roles and hooks are refused in v1 (OpenClaw workspace runs do not use subagent or hook files).
Global OpenClaw configuration is never modified; everything is written into the workspace.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from hermit.adapter.loader import agent_tools, hook_definitions, load_manifest, not_portable
from hermit.hosts.context import PROJECT_CONTEXT, START, append_context
from hermit.hosts.inputs import (
    MARKER, PACKAGE_ROOT, _adapter_sandbox_env, adapter_files, adapter_folder, check_workspace, copy_adapter,
    copy_skills, folder_note, render_exports, resources, server_name, source_label
)

SKILL_NAME_RE = re.compile(r"^[a-z0-9-]+$")
NATIVE = {"skills"}


def validate_openclaw_skills(adapter: Path, manifest: dict) -> list[str]:
    """Validate skill names and descriptions per OpenClaw AgentSkills layout:
    - Skill name matches directory name
    - Name charset is [a-z0-9-]
    - Description is non-empty single line; warns if > 160 chars
    """
    if not manifest.get("skills"):
        return []
    source = (adapter / manifest["skills"]).resolve()
    names = []
    if source.is_dir():
        for child in sorted(source.iterdir()):
            skill_md = child / "SKILL.md"
            if skill_md.is_file():
                name = child.name
                if not SKILL_NAME_RE.fullmatch(name):
                    raise ValueError(f"Skill name must match [a-z0-9-]+: {name}")
                text = skill_md.read_text(encoding="utf-8")
                desc = None
                frontmatter_name = None
                if text.startswith("---\n"):
                    end = text.find("\n---", 4)
                    if end > 0:
                        for line in text[4:end].splitlines():
                            k, sep, v = line.partition(":")
                            if sep:
                                k = k.strip()
                                v = v.strip().strip("'\"")
                                if k == "name":
                                    frontmatter_name = v
                                elif k == "description":
                                    desc = v
                if frontmatter_name and frontmatter_name != name:
                    raise ValueError(f"Skill directory name '{name}' does not match frontmatter name '{frontmatter_name}'")
                if not desc or "\n" in desc:
                    raise ValueError(f"Skill {name} requires a non-empty single-line description")
                if len(desc) > 160:
                    print(f"hermit deploy: warning: skill '{name}' description exceeds 160 chars ({len(desc)})", file=sys.stderr)
                names.append(name)
    return names


def prepare(adapter: Path, output: Path, python: str, *, learning_material: Path | None = None,
            context: str | None = None) -> dict:
    adapter, output = adapter.resolve(), output.resolve()
    refused = not_portable(load_manifest(adapter), "An OpenClaw workspace")
    if refused and context is None:
        raise ValueError(refused)
    previous = check_workspace(adapter, output, "openclaw")
    manifest = load_manifest(adapter)
    files = adapter_files(adapter, output, manifest, previous)
    hooks = hook_definitions(adapter, manifest)
    if (hooks or {}).get("hooks"):
        raise ValueError(
            f"OpenClaw v1 reads no hook file, so this adapter's hooks ({manifest['hooks']}) cannot be deployed; "
            "deploy with --host codex or --host claude-code"
        )
    exports = render_exports(adapter, python)

    # Refuse roles in v1
    if exports.get("agents"):
        names = ", ".join(r["name"] for r in exports["agents"])
        raise ValueError(
            f"OpenClaw v1 workspace integration does not support subagent roles ({names}); "
            "deploy with --host codex or --host claude-code"
        )

    validate_openclaw_skills(adapter, manifest)
    listed, placed = resources(adapter, output, manifest, exports, hooks, NATIVE)

    has_tools = bool(agent_tools(manifest))
    if has_tools:
        subprocess.run([python, "-c", "import mcp.server"], check=True, capture_output=True)
    output.mkdir(parents=True, exist_ok=True)

    # Context in AGENTS.md
    context_path = output / "AGENTS.md"
    existing = context_path.read_text(encoding="utf-8") if context_path.exists() else PROJECT_CONTEXT
    if existing.startswith(START):
        existing = PROJECT_CONTEXT + "\n\n" + existing
    body_ctx = exports["context"] if context is None else context
    context_text = append_context(
        existing, "\n\n".join(t for t in (folder_note(output, manifest), body_ctx, listed) if t),
        source=source_label(manifest),
        version=manifest["version"], adapter_id=manifest["id"]
    )
    if len(context_text) > 20000:
        print(
            f"hermit deploy: warning: AGENTS.md length ({len(context_text)} chars) exceeds OpenClaw bootstrap limit (20,000 chars)",
            file=sys.stderr
        )
    context_path.write_text(context_text, encoding="utf-8")

    # Skills in skills/
    skills = copy_skills(adapter, manifest, output / "skills")
    copy_adapter(adapter, output, manifest, files, previous)

    # Tool server configuration
    audit = output / "tool-calls.jsonl"
    server = None
    srv_name = server_name(manifest) if has_tools else None
    run_config = {
        "skills": {
            "workshop": {
                "autonomous": {
                    "mode": "off"
                }
            }
        },
        "tools": {
            "deny": ["skill_workshop"]
        }
    }
    if has_tools:
        server = {
            "command": python,
            "args": ["-m", "hermit.runtime.mcp_server", "--adapter", str(adapter), "--audit-log", str(audit)],
            "env": {"PYTHONPATH": PACKAGE_ROOT, **_adapter_sandbox_env(python)}
        }
        if learning_material is not None:
            server["args"].extend(["--learning-material", str(Path(learning_material).resolve())])
        run_config["mcp"] = {
            "servers": {
                srv_name: server
            }
        }

    # Write openclaw.run.json in workspace
    config_path = output / "openclaw.run.json"
    config_path.write_text(json.dumps(run_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    result = {
        "host": "openclaw",
        "adapter_id": manifest["id"],
        "version": manifest["version"],
        "adapter": str(adapter),
        "workspace": str(output),
        "context_injected_via": "AGENTS.md",
        "server_name": srv_name,
        "server": server,
        "roles": {},
        "role_name_map": {},
        "skills_dir": str(output / "skills") if skills else None,
        "skills": skills,
        "hooks": None,
        "audit_log": str(audit) if has_tools else None,
        "adapter_dir": str(adapter_folder(output, manifest)),
        "files": files,
        "placed": placed,
        "openclaw": {
            "config_file": "openclaw.run.json",
            "skills_workshop_disabled": True
        }
    }
    (output / MARKER).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--workspace", "--output", dest="workspace", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    print(json.dumps(prepare(args.adapter, args.workspace, str(Path(args.python).absolute())), indent=2))
