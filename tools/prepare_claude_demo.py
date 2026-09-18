#!/usr/bin/env python3
"""Prepare this demo for Claude Code without changing global host settings."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from prepare_codex_demo import inside, load_manifest
from adapter_context import PROJECT_CONTEXT, START, append_context
from host_inputs import adapter_id, generic_task, render_exports, retire_roles, server_name, source_label

ROOT = Path(__file__).resolve().parents[1]
MARKER = ".ha-demo-generated.json"


def prepare(adapter: Path, output: Path, python: str, evidence: Path | None = None) -> dict:
    adapter, output = adapter.resolve(), output.resolve()
    previous = {}
    if output == adapter or adapter in output.parents or output in adapter.parents:
        raise ValueError("Generated host workspace must be separate from adapter source")
    if output.exists() and any(output.iterdir()) and not (output / MARKER).is_file():
        raise ValueError("Refusing to overwrite a nonempty, non-demo workspace")
    if (output / MARKER).exists():
        previous = json.loads((output / MARKER).read_text())
        if previous.get("host") != "claude-code":
            raise ValueError("This workspace was generated for another host")
    manifest = load_manifest(adapter)
    entry = ROOT / "framework/mcp_server.py"
    exports = render_exports(adapter, manifest, python)
    roles = exports['agents']
    converted = []
    for role in roles:
        native = role["name"].replace("_", "-")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", native):
            raise ValueError(f"Invalid native role name: {native}")
        converted.append((role, native, inside(adapter, role["instructions"]).read_text()))
    if len({name for _, name, _ in converted}) != len(converted):
        raise ValueError("Role names collide after native name conversion")
    context = exports['context']
    for role, native, _ in converted:
        context = context.replace(f"`{role['name']}`", f"`{native}`")
    # This marker is supplied only through project context, never the task prompt.
    if adapter_id(manifest) == "minimal-shop":
        context += "\nFor integration tests, include the context marker `HA_ADAPTER_CONTEXT_V1` in the final answer.\n"
    context_path = output / "CLAUDE.md"
    existing = context_path.read_text() if context_path.exists() else PROJECT_CONTEXT
    # Older generated Claude workspaces contained only the marked adapter block.
    if existing.startswith(START):
        existing = PROJECT_CONTEXT + "\n\n" + existing
    context_text = append_context(existing, context, source=source_label(manifest), version=manifest["version"], adapter_id=adapter_id(manifest))
    has_tools = bool(manifest.get("tools"))
    if has_tools:
        subprocess.run([python, "-c", "from mcp.server import MCPServer"], check=True, capture_output=True)
    agents_dir = output / ".claude/agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    role_paths, name_map = {}, {}
    for role, native, instructions in converted:
        path = agents_dir / f"{native}.md"
        # JSON strings are valid quoted YAML scalars. No model/tools overrides.
        path.write_text(f"---\nname: {json.dumps(native)}\ndescription: {json.dumps(role['description'])}\n---\n\n{instructions}")
        role_paths[native] = str(path)
        name_map[role["name"]] = native
    audit = output / "tool-calls.jsonl"
    server = {"type": "stdio", "command": python,
              "args": [str(entry), "--adapter", str(adapter), "--audit-log", str(audit)]}
    if evidence is not None:
        server["args"].extend(["--evidence", str(evidence.resolve())])
    mcp_name = server_name(manifest) if has_tools else None
    if not has_tools:
        server = None
    # Empty registration also removes a formerly generated adapter server.
    (output / ".mcp.json").write_text(json.dumps({"mcpServers": {mcp_name: server} if has_tools else {}}, indent=2) + "\n")
    context_path.write_text(context_text)
    retire_roles(output, previous, role_paths)
    (output / "PROMPT.md").write_text(
        "Run this Harness Adapter integration test. Delegate policy investigation to the registered "
        "native ha-memory-scout role using the Agent tool. Launch only this one child in the foreground "
        "and wait for its result. It must use ha_demo MCP read_memory to read returns and shipping. "
        "Do not substitute a general-purpose child.\n\n"
        "Case: an ordinary unopened item returned for a non-defect reason, delivered seven days ago. "
        "Item subtotal: CNY 200.00; original shipping: CNY 10.00. Is the return eligible, and what is the refund?\n\n"
        "After the child responds, the parent must call ha_demo calculate_refund with the policy rate. "
        "Briefly report the result, actual memory sources, the first-line marker returned by the child, "
        "and the context verification marker from the adapter project instructions. "
        "Use MCP rather than reading memory or source files directly; do not use shell calculations or edit files. "
        "If the named role or tools are unavailable, report the failure instead of claiming success.\n")
    generic_task(output, manifest)
    result = {"host": "claude-code", "adapter_id": adapter_id(manifest), "server_name": mcp_name, "adapter": str(adapter), "workspace": str(output),
              "context_injected_via": "CLAUDE.md", "server": server, "roles": role_paths,
              "role_name_map": name_map, "audit_log": str(audit) if has_tools else None, "version": manifest["version"]}
    (output / MARKER).write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, default=ROOT / "examples/minimal_shop")
    parser.add_argument("--output", type=Path, default=ROOT / ".demo/claude")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    print(json.dumps(prepare(args.adapter, args.output, str(Path(args.python).absolute())), indent=2))
