#!/usr/bin/env python3
"""Optional, demo-scoped bootstrap. Does not edit global Codex settings."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib

from adapter_context import PROJECT_CONTEXT, append_context
from host_inputs import adapter_id, generic_task, render_exports, retire_roles, server_name, source_label

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from framework.adapter import load_manifest
MARKER = ".ha-demo-generated.json"


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Adapter entry escapes its repo: {relative}")
    if not path.is_file():
        raise ValueError(f"Missing adapter entry: {relative}")
    return path


def prepare(adapter: Path, output: Path, python: str, evidence: Path | None = None) -> dict:
    adapter = adapter.resolve()
    output = output.resolve()
    previous = {}
    if output == adapter or adapter in output.parents or output in adapter.parents:
        raise ValueError("Generated host workspace must be separate from adapter source")
    if output.exists() and any(output.iterdir()) and not (output / MARKER).exists():
        raise ValueError("Refusing to overwrite a nonempty, non-demo workspace")
    if (output / MARKER).is_file():
        previous = json.loads((output / MARKER).read_text())
        if previous.get("host", "codex") != "codex":
            raise ValueError("This workspace was generated for another host")
    manifest = load_manifest(adapter)
    exports = render_exports(adapter, manifest, python)
    context = exports['context']
    roles = [{**r, 'name': r['name'].replace('-', '_')} for r in exports['agents']]
    role_name_map = {r['name']: r['name'].replace('-', '_') for r in exports['agents']}
    if any(k != v for k, v in role_name_map.items()):
        context += '\n\nNative role mapping: ' + ', '.join(f'{k} → {v}' for k, v in role_name_map.items())
    context_path = output / "AGENTS.md"
    existing = context_path.read_text() if context_path.exists() else PROJECT_CONTEXT
    legacy = (
        "# Adapter demo host workspace\n\n"
        "The selected adapter's Context document follows. Tools use MCP; roles load through native Codex settings.\n"
        "There is no need to edit the adapter or generate additional configuration.\n\n" + context
    )
    # Migrate only the exact old generated file, never guess which user text to remove.
    if existing == legacy:
        existing = PROJECT_CONTEXT
    context_text = append_context(existing, context, source=source_label(manifest), version=manifest["version"], adapter_id=adapter_id(manifest))
    entry = ROOT / "framework/mcp_server.py"
    names = [r["name"] for r in roles]
    if len(set(names)) != len(names) or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", n) for n in names):
        raise ValueError("Agent names must be unique, lowercase Codex identifiers")
    has_tools = bool(manifest.get("tools"))
    if has_tools:
        subprocess.run([python, "-c", "from mcp.server import MCPServer"], check=True, capture_output=True)
    agents_dir = output / ".codex" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    audit = output / "tool-calls.jsonl"
    server = {"command": python, "args": [str(entry), "--adapter", str(adapter), "--audit-log", str(audit)],
              "startup_timeout_sec": 20, "tool_timeout_sec": 20, "required": True}
    if evidence is not None:
        server["args"].extend(["--evidence", str(evidence.resolve())])
    mcp_name = server_name(manifest) if has_tools else None
    if not has_tools:
        server = None
    config = ["# Generated configuration. No global settings are changed."]
    if has_tools:
        config.append(f"[mcp_servers.{mcp_name}]")
        for key, value in server.items():
            config.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    role_paths = {}
    for role in roles:
        name = role["name"]
        instructions = inside(adapter, role["instructions"]).read_text()
        role_text = "\n".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in {
            "name": name, "description": role["description"], "developer_instructions": instructions,
        }.items()) + "\n"
        tomllib.loads(role_text)
        target = agents_dir / f"{name}.toml"
        target.write_text(role_text)
        role_paths[name] = str(target)
        config.extend(["", f"[agents.{name}]", f"description = {json.dumps(role['description'], ensure_ascii=False)}",
                       f"config_file = {json.dumps('agents/' + name + '.toml')}"])
    config_text = "\n".join(config) + "\n"
    tomllib.loads(config_text)
    (output / ".codex" / "config.toml").write_text(config_text)
    context_path.write_text(context_text)
    retire_roles(output, previous, role_paths)
    (output / "PROMPT.md").write_text(
        "This is a Harness Adapter integration test. Use the registered native ha_memory_scout role "
        "to investigate the policies below through ha_demo MCP read_memory. Launch only this one child and wait for its result.\n\n"
        "Case: an ordinary item from the fictional shop, unopened, returned for a non-defect reason, "
        "delivered seven days ago. Item subtotal: CNY 200.00. Original shipping: CNY 10.00. "
        "Is the return eligible, and how much is refundable?\n\n"
        "After the child responds, the parent must actually call ha_demo calculate_refund. "
        "Briefly report policy sources, the calculated result, and the first-line marker returned by the child. "
        "Do not replace MCP with direct memory or source reads, run shell calculations, or edit files. "
        "If tools or the named role are unavailable, report that directly. Do not present an ordinary child as the registered role.\n")
    generic_task(output, manifest)
    result = {"host": "codex", "adapter_id": adapter_id(manifest), "server_name": mcp_name, "adapter": str(adapter), "workspace": str(output), "context_injected_via": "AGENTS.md",
              "server": server, "roles": role_paths, "role_name_map": role_name_map, "audit_log": str(audit) if has_tools else None, "version": manifest["version"]}
    (output / MARKER).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, default=ROOT / "examples/minimal_shop")
    parser.add_argument("--output", type=Path, default=ROOT / ".demo/codex")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    # Preserve a venv interpreter's path; resolve() would follow its symlink out of the venv.
    print(json.dumps(prepare(args.adapter, args.output, str(Path(args.python).absolute())),
                     ensure_ascii=False, indent=2))
