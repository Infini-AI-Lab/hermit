#!/usr/bin/env python3
"""Framework-owned MCP wrapper for declared Python callables in an adapter repo."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

from harnessadapter.adapter.loader import Adapter
from harnessadapter.adapter.resources import LearningMaterial, resolve_learning_material


async def serve(root: Path, audit_log: Path | None, evidence: Path | None = None, *, learning_material: Path | None = None,
                deployment: Path | None = None, python_sandbox: bool = False):
    evidence = resolve_learning_material(learning_material, evidence)
    store = LearningMaterial(evidence) if evidence is not None else None
    resources = {"learning_material": store, "evidence": store} if store is not None else {}
    if deployment is not None:
        # The deployment's host texts (a host.json), the same resource the
        # in-process hosts bind.
        resources["deployment"] = json.loads(Path(deployment).read_text())
    if python_sandbox:
        from harnessadapter.runtime.python_sandbox import PythonSandbox
        resources["python_sandbox"] = PythonSandbox(root)
    adapter = Adapter(root, resources=resources)
    if audit_log is not None and audit_log.resolve().is_relative_to(adapter.root):
        raise ValueError("Framework audit logs must live outside the adapter core")
    lock = asyncio.Lock()

    async def list_tools(context, params):
        return ListToolsResult(tools=[Tool(
            name=tool["name"], description=tool["description"],
            inputSchema=tool["input_schema"], outputSchema=tool["output_schema"],
            annotations=ToolAnnotations(**tool["annotations"]) if "annotations" in tool else None,
        ) for tool in adapter.tools.values()])

    async def call_tool(context, params):
        async with lock:
            arguments = params.arguments if params.arguments is not None else {}
            record = {"time": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
                      "adapter": adapter.manifest["name"], "version": adapter.manifest["version"],
                      "tool": params.name, "arguments": arguments}
            try:
                result = await adapter.call(params.name, arguments)
                record.update(status="ok", result=result)
                return CallToolResult(content=[TextContent(text=json.dumps(result, ensure_ascii=False))],
                                      structuredContent=result)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                record.update(status="error", error=message)
                return CallToolResult(content=[TextContent(text=message)], isError=True)
            finally:
                if audit_log is not None:
                    audit_log.parent.mkdir(parents=True, exist_ok=True)
                    with audit_log.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    server = Server(adapter.manifest["name"], version=adapter.manifest["version"],
                    on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--audit-log", type=Path)
    material = parser.add_mutually_exclusive_group(required=False)
    material.add_argument("--learning-material", dest="evidence", type=Path, help="Read-only LearningMaterial directory with README.md")
    material.add_argument("--evidence", dest="evidence", type=Path, help="Compatibility alias for --learning-material")
    parser.add_argument("--deployment", type=Path, help="A host.json of deployment texts, bound as the `deployment` resource")
    parser.add_argument("--python-sandbox", action="store_true", help="Bind the bubblewrap Python sandbox so admitted learned tools can run")
    args = parser.parse_args()
    asyncio.run(serve(args.adapter, args.audit_log, args.evidence, deployment=args.deployment, python_sandbox=args.python_sandbox))
