#!/usr/bin/env python3
"""MCP stdio server that exposes a run's Evolver tools by forwarding to the Trainer's tool endpoint.

    python -m hermit.tools_mcp --url http://127.0.0.1:PORT
"""
import argparse
import asyncio
import json
import urllib.request

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool


def _get(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read())


def _post(url, payload, timeout):
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


async def serve(url: str, timeout: float):
    tools = _get(url + "/tools")

    async def list_tools(context, params):
        return ListToolsResult(tools=[Tool(name=t["name"], description=t["description"], inputSchema=t["input_schema"])
                                      for t in tools])

    async def call_tool(context, params):
        reply = await asyncio.to_thread(_post, f"{url}/call/{params.name}", params.arguments or {}, timeout)
        if "error" in reply:
            return CallToolResult(content=[TextContent(text=reply["error"])], isError=True)
        text = json.dumps(reply["result"], ensure_ascii=False, default=str)
        return CallToolResult(content=[TextContent(text=text)], structuredContent=reply["result"])

    server = Server("evolver_tools", on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    asyncio.run(serve(args.url, args.timeout))
