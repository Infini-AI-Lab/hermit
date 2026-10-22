#!/usr/bin/env python3
"""MCP stdio server with the learner's tools: the Trainer's tool endpoint, filtered to the names given.

    python learner_tools.py --url http://127.0.0.1:PORT --only sql [--only ...]

The native MWM learner had a shell and the venue's channels (the `sql` tool at the database venue) and nothing else.
The framework's `evolver_tools` server also offers run_agent, llm, budget and check_spec; serving those would give
this learner abilities the method never had (replaying graded tasks, a second model), so only the named tools pass.
"""
from __future__ import annotations

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
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


async def serve(url: str, only: set[str], timeout: float):
    tools = [t for t in _get(url + "/tools") if t["name"] in only]
    names = {t["name"] for t in tools}

    async def list_tools(context, params):
        return ListToolsResult(tools=[Tool(name=t["name"], description=t["description"], inputSchema=t["input_schema"])
                                      for t in tools])

    async def call_tool(context, params):
        if params.name not in names:
            return CallToolResult(content=[TextContent(text=f"unknown tool {params.name!r}")], isError=True)
        reply = await asyncio.to_thread(_post, f"{url}/call/{params.name}", params.arguments or {}, timeout)
        if "error" in reply:
            return CallToolResult(content=[TextContent(text=reply["error"])], isError=True)
        result = reply["result"]
        # A tool that answers with {"text": ...} is shown as that text, as the native broker returned it.
        text = result["text"] if isinstance(result, dict) and set(result) == {"text"} \
            else json.dumps(result, ensure_ascii=False, default=str)
        return CallToolResult(content=[TextContent(text=text)])

    server = Server("evolver_tools", on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    asyncio.run(serve(args.url, set(args.only), args.timeout))
