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

from adapter import Adapter
from resources import EvidenceStore


async def serve(root: Path, audit_log: Path | None, evidence: Path | None = None):
    resources = {"evidence": EvidenceStore(evidence)} if evidence is not None else {}
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
    parser.add_argument("--evidence", type=Path, help="Optional external evidence directory")
    args = parser.parse_args()
    asyncio.run(serve(args.adapter, args.audit_log, args.evidence))
