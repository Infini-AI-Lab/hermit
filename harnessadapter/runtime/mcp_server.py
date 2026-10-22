#!/usr/bin/env python3
"""Framework-owned MCP wrapper for declared Python callables in an adapter repo."""
import argparse
import asyncio
import json
from pathlib import Path

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

from harnessadapter.adapter.session import Session


async def serve(root: Path, audit_log: Path | None, learning_material: Path | None = None, *,
                deployment: Path | None = None, python_sandbox: bool = False, evidence: Path | None = None):
    resources = {}
    if python_sandbox:
        from harnessadapter.runtime.python_sandbox import PythonSandbox
        resources["python_sandbox"] = PythonSandbox(root)
    # The same Session a Python program uses; the audit record is its record.
    session = Session(root, resources=resources, deployment=deployment, learning_material=learning_material,
                      evidence=evidence, audit_log=audit_log)
    adapter = session.adapter
    lock = asyncio.Lock()

    async def list_tools(context, params):
        return ListToolsResult(tools=[Tool(
            name=tool["name"], description=tool["description"],
            inputSchema=tool["input_schema"], outputSchema=tool["output_schema"],
            annotations=ToolAnnotations(**tool["annotations"]) if "annotations" in tool else None,
        ) for tool in session.tools])

    async def call_tool(context, params):
        async with lock:
            arguments = params.arguments if params.arguments is not None else {}
            try:
                result = await session.acall(params.name, arguments)
                return CallToolResult(content=[TextContent(text=json.dumps(result, ensure_ascii=False))],
                                      structuredContent=result)
            except Exception as exc:
                return CallToolResult(content=[TextContent(text=f"{type(exc).__name__}: {exc}")], isError=True)

    server = Server(adapter.manifest["name"], version=adapter.manifest["version"],
                    on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main(argv=None) -> None:
    """Run the server; `harnessadapter serve` calls this."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--audit-log", type=Path)
    parser.add_argument("--learning-material", type=Path, help="Read-only LearningMaterial directory bound to the tools")
    parser.add_argument("--deployment", type=Path, help="A host.json of deployment texts, bound as the `deployment` resource")
    parser.add_argument("--evidence", type=Path, help="An evidence store directory bound as the `evidence` resource")
    parser.add_argument("--python-sandbox", action="store_true", help="Bind the bubblewrap Python sandbox so admitted learned tools can run")
    args = parser.parse_args(argv)
    asyncio.run(serve(args.adapter, args.audit_log, args.learning_material, deployment=args.deployment, python_sandbox=args.python_sandbox,
                      evidence=args.evidence))


if __name__ == "__main__":
    main()
