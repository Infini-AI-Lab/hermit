"""The framework's tool server: a list of Tools over HTTP, for coding-agent CLIs and external programs.

    with ToolServer(tools, host="127.0.0.1") as server:
        server.mcp_url    # MCP (streamable HTTP), for Codex and Claude Code
        server.url        # plain HTTP and JSON: GET <url>/tools, POST <url>/call/<name> with the arguments

Both entrances sit under a random token in the path, so that only whoever was handed the URL can call the tools.
The tools run here, in this process, on the framework's side. `host` is the address to listen on: loopback for a
session on this machine, the container network's gateway for a session in a container.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import socket
import threading
from typing import Iterable

from hermit.agent.base.tool import Tool


class ToolServer:
    def __init__(self, tools: Iterable[Tool] | dict[str, Tool], host: str = "127.0.0.1",
                 mcp_only: Iterable[str] | None = None):
        self.tools = dict(tools) if isinstance(tools, dict) else {t.name: t for t in tools}
        # The MCP entrance may offer fewer tools than the JSON one (a learner whose shell client reaches them all)
        self.mcp_tools = {n: t for n, t in self.tools.items() if mcp_only is None or n in set(mcp_only)}
        self.host = host
        self.token = secrets.token_hex(16)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, 0))
        self.port = port = self._sock.getsockname()[1]
        self.url = f"http://{host}:{port}/{self.token}"
        self.mcp_url = f"{self.url}/mcp"
        self._server = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

    # ---------- the app ----------
    def _app(self):
        from mcp.server.lowlevel import Server
        from mcp.server.transport_security import TransportSecuritySettings
        from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool as McpTool
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        tools, mcp_tools = self.tools, self.mcp_tools

        async def list_tools(context, params):
            return ListToolsResult(tools=[McpTool(name=t.name, description=t.description, inputSchema=t.input_schema)
                                          for t in mcp_tools.values()])

        async def call_tool(context, params):
            tool = mcp_tools.get(params.name)
            if tool is None:
                return CallToolResult(content=[TextContent(text=f"unknown tool {params.name}")], isError=True)
            try:
                result = await asyncio.to_thread(lambda: tool(**(params.arguments or {})))
            except Exception as exc:
                return CallToolResult(content=[TextContent(text=f"{type(exc).__name__}: {exc}")], isError=True)
            # The rendered text only: a client that prefers structured content would show a model the raw object
            # where the tool means its text (a result {"text": ...} reads as that text).
            return CallToolResult(content=[TextContent(text=tool.render(result))])

        async def describe(request: Request):
            return JSONResponse([t.describe() for t in tools.values()])

        async def call(request: Request):
            tool = tools.get(request.path_params["name"])
            if tool is None:
                return JSONResponse({"error": f"unknown tool {request.path_params['name']}"}, status_code=404)
            try:
                arguments = json.loads(await request.body() or b"{}")
                result = await asyncio.to_thread(lambda: tool(**arguments))
                return JSONResponse(json.loads(json.dumps({"result": result}, ensure_ascii=False, default=str)))
            except Exception as exc:
                return JSONResponse({"error": f"{type(exc).__name__}: {exc}"})

        server = Server("hermit_tools", on_list_tools=list_tools, on_call_tool=call_tool)
        return server.streamable_http_app(
            streamable_http_path=f"/{self.token}/mcp", host=self.host,
            # The token in the path is the guard; a gateway address would fail the loopback-only host check.
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
            custom_starlette_routes=[Route(f"/{self.token}/tools", describe, methods=["GET"]),
                                     Route(f"/{self.token}/call/{{name}}", call, methods=["POST"])])

    # ---------- lifetime ----------
    def start(self) -> "ToolServer":
        import uvicorn
        config = uvicorn.Config(self._app(), log_level="error", lifespan="on", access_log=False)
        self._server = uvicorn.Server(config)

        def serve():
            async def main():
                task = asyncio.create_task(self._server.serve(sockets=[self._sock]))
                while not self._server.started and not task.done():
                    await asyncio.sleep(0.01)
                if not self._server.started:            # it ended before serving: say so, not a dead URL
                    self._failure = repr(task.exception()) if not task.cancelled() and task.exception() else \
                        "it stopped before serving"
                self._started.set()
                await task
            try:
                asyncio.run(main())
            except BaseException as exc:                # noqa: BLE001 - reported by start()
                self._failure = self._failure or repr(exc)
                self._started.set()
        self._failure = ""
        self._thread = threading.Thread(target=serve, daemon=True, name="hermit-tool-server")
        self._thread.start()
        if not self._started.wait(30):
            self.close()
            raise RuntimeError("the tool server did not start")
        if self._failure:
            self._sock.close()
            raise RuntimeError(f"the tool server did not start: {self._failure}")
        return self

    def close(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._sock.close()

    def __enter__(self) -> "ToolServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()
