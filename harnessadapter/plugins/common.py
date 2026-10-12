"""Inputs shared by the plugin emitters: the embedded adapter, the context skill, roles, the MCP entry.

`deploy` prepares a workspace you work in; `convert` emits an artifact you install. The difference drives
everything here: an installed plugin is read from a path the emitter does not choose, so nothing it writes may
assume the interpreter, the package location or the working directory of the machine that produced it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re
import shutil

from harnessadapter.adapter.loader import inside, load_manifest
from harnessadapter.hosts.inputs import render_exports, server_name

# The adapter travels inside the artifact: a plugin that referenced an outside directory would break the moment
# it was installed anywhere else.
ADAPTER_DIR = "adapter"
# A console script, not an interpreter path: the host resolves it on PATH wherever harnessadapter is installed.
SERVER_COMMAND = "harnessadapter-mcp"
AUDIT_LOG = "tool-calls.jsonl"
SKILL = "SKILL.md"


class Unsupported(ValueError):
    """An export the target cannot represent. Raised rather than silently dropped."""


@dataclass(frozen=True)
class Bundle:
    adapter: Path
    manifest: dict
    context: str
    roles: list

    @property
    def id(self) -> str:
        return self.manifest["id"]

    @property
    def version(self) -> str:
        return self.manifest["version"]

    @property
    def has_tools(self) -> bool:
        return bool(self.manifest.get("tools"))

    @property
    def server(self) -> str:
        return server_name(self.manifest)


def load(adapter: Path, python: str) -> Bundle:
    adapter = adapter.resolve()
    manifest = load_manifest(adapter)
    exports = render_exports(adapter, python)
    return Bundle(adapter, manifest, exports["context"], list(exports["agents"]))


def check_output(output: Path, adapter: Path) -> None:
    if output == adapter or adapter in output.parents or output in adapter.parents:
        raise ValueError("The artifact must be written outside the adapter")


def embed_adapter(bundle: Bundle, plugin_root: Path) -> Path:
    """Copy the adapter into the artifact so the installed plugin is self-contained."""
    target = plugin_root / ADAPTER_DIR
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(bundle.adapter, target,
                    ignore=shutil.ignore_patterns("__pycache__", ".git", "*.pyc"))
    return target


def context_skill(bundle: Bundle) -> tuple[str, str]:
    """The adapter's context as a skill.

    A plugin has no way to add to CLAUDE.md or AGENTS.md -- its component types are skills, agents, hooks, MCP
    and LSP servers. The context therefore becomes a skill, which every one of the four hosts understands. This
    is not equivalent: a workspace context is always in the model's context, a skill's body is loaded when its
    description matches. The description below is written to make that match reliable, and `convert` reports
    `context_as` so the difference stays visible.
    """
    name = f"{bundle.id}-context"
    description = (f"The {bundle.manifest.get('name', bundle.id)} harness adapter's working context. "
                   f"Read this before acting on any task this adapter covers.")
    body = f"---\nname: {json.dumps(name)}\ndescription: {json.dumps(description)}\n---\n\n{bundle.context.strip()}\n"
    return name, body


def copy_skills(bundle: Bundle, target: Path) -> list[str]:
    """Copy each skill directory (one holding SKILL.md) of the adapter's `skills` export."""
    declared = bundle.manifest.get("skills")
    if not declared:
        return []
    source = (bundle.adapter / declared).resolve()
    if not source.is_dir():
        return []
    names = []
    for child in sorted(source.iterdir()):
        if (child / SKILL).is_file():
            shutil.copytree(child, target / child.name, dirs_exist_ok=True)
            names.append(child.name)
    return names


def role_body(bundle: Bundle, role: dict) -> str:
    return role.get("body") or inside(bundle.adapter, role["instructions"]).read_text()


def subagent_file(bundle: Bundle, role: dict) -> tuple[str, str]:
    """A role as a Claude Code style subagent file: frontmatter plus instructions."""
    native = role["name"].replace("_", "-")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", native):
        raise ValueError(f"Invalid native role name: {native}")
    text = (f"---\nname: {json.dumps(native)}\ndescription: {json.dumps(role['description'])}\n---\n\n"
            f"{role_body(bundle, role)}")
    return native, text


def mcp_entry(bundle: Bundle, adapter_path: str, audit_path: str | None) -> dict:
    """One stdio MCP server entry. `adapter_path` is whatever the target host can actually resolve."""
    args = ["--adapter", adapter_path]
    if audit_path:
        args += ["--audit-log", audit_path]
    return {"type": "stdio", "command": SERVER_COMMAND, "args": args}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
