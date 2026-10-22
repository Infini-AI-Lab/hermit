#!/usr/bin/env python3
"""The adapter as a standalone service (docs/async-collab-adapter-service-design-2026-09-30.md).

One process per run. Its lifecycle is independent of the agents: it starts before them and serves them while they
connect, are interrupted, resumed or gone.

- Each agent reaches the adapter's tools over MCP (streamable HTTP) on its own unix socket; the socket is the agent's
  identity (no token: a jail binds only its own agent's socket). Tool calls of one agent run one at a time, calls of
  different agents run in parallel threads (a `check` compiles for minutes).
- The run script reaches the control socket: it posts the agents' status and fetches the commands the adapter's
  Reactions decided (interrupt / resume an agent with a text). The run script decides nothing.
- Every `tick` seconds the adapter's reaction entrypoint is called with the latest status; the commands it returns are
  queued with a sequence number.

Config (JSON):
  {"adapter": "<adapter dir>", "control_socket": "<path>", "tick": 5,
   "env": {<settings every agent shares>},
   "agents": {"A1": {"socket": "<path>", "audit_log": "<path>", "env": {<this agent's settings>}}, ...}}
"""
import argparse
import asyncio
import contextlib
import importlib.util
import inspect
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from hermit.adapter.loader import Adapter
from hermit.runtime.agent_context import AGENT_ENV


def load_reactions(root: Path, manifest: dict):
    decl = manifest.get("reactions")
    if not decl:
        return None
    filename, _, function = decl["entrypoint"].partition(":")
    path = (root / filename).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"reactions entrypoint outside the adapter: {filename}")
    spec = importlib.util.spec_from_file_location(f"adapter_reactions_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(root))   # the reactions module imports the adapter's own modules
    spec.loader.exec_module(module)
    return getattr(module, function)


class Service:
    def __init__(self, config: dict):
        self.config = config
        self.root = Path(config["adapter"]).resolve()
        self.adapter = Adapter(self.root, resources={})
        self.shared_env = {k: str(v) for k, v in (config.get("env") or {}).items()}
        self.agents = config["agents"]
        # one lock per agent serializes its tool calls; an adapter that locks its own resources declares
        # "concurrent_tool_calls": true and its calls then run concurrently (a long compile blocks nothing)
        self.concurrent = bool(self.adapter.manifest.get("concurrent_tool_calls"))
        self.locks = {a: asyncio.Lock() for a in self.agents}
        self.react = load_reactions(self.root, self.adapter.manifest)
        self.status: dict = {}           # the run script's latest report
        self.status_t = 0.0
        self.commands: list = []         # queued commands, seq = index + 1
        self.cmd_lock = threading.Lock()
        self.results: list = []          # the run script's reports on executed commands
        self.log = Path(config.get("log") or Path(config["control_socket"]).with_suffix(".log"))

    def env_of(self, agent: str) -> dict:
        env = dict(self.shared_env)
        env.update({k: str(v) for k, v in (self.agents[agent].get("env") or {}).items()})
        env["COLLAB_AGENT"] = agent
        return env

    def note(self, text: str):
        with self.log.open("a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {text}\n")

    # ---------- tools ----------
    def _call_blocking(self, agent: str, name: str, arguments: dict):
        token = AGENT_ENV.set(self.env_of(agent))
        try:
            return asyncio.run(self.adapter.call(name, arguments))
        finally:
            AGENT_ENV.reset(token)

    def mcp_server(self, agent: str) -> Server:
        adapter = self.adapter
        audit = Path(self.agents[agent]["audit_log"]) if self.agents[agent].get("audit_log") else None

        async def list_tools(context, params):
            return ListToolsResult(tools=[Tool(
                name=t["name"], description=t["description"], inputSchema=t["input_schema"], outputSchema=t["output_schema"],
                annotations=ToolAnnotations(**t["annotations"]) if "annotations" in t else None) for t in adapter.tools.values()])

        async def call_tool(context, params):
            async with (contextlib.nullcontext() if self.concurrent else self.locks[agent]):
                arguments = params.arguments if params.arguments is not None else {}
                record = {"time": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), "agent": agent,
                          "adapter": adapter.manifest["name"], "version": adapter.manifest["version"],
                          "tool": params.name, "arguments": arguments}
                try:
                    result = await asyncio.to_thread(self._call_blocking, agent, params.name, arguments)
                    record.update(status="ok", result=result)
                    return CallToolResult(content=[TextContent(text=json.dumps(result, ensure_ascii=False))], structuredContent=result)
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    record.update(status="error", error=message)
                    return CallToolResult(content=[TextContent(text=message)], isError=True)
                finally:
                    if audit is not None:
                        audit.parent.mkdir(parents=True, exist_ok=True)
                        with audit.open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

        return Server(adapter.manifest["name"], version=adapter.manifest["version"], on_list_tools=list_tools, on_call_tool=call_tool)

    def agent_app(self, agent: str):
        manager = StreamableHTTPSessionManager(app=self.mcp_server(agent), json_response=True, stateless=True)

        @contextlib.asynccontextmanager
        async def lifespan(app):
            async with manager.run():
                yield

        async def handle(scope, receive, send):
            await manager.handle_request(scope, receive, send)

        return Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)

    # ---------- control ----------
    def control_app(self):
        async def post_status(request: Request):
            self.status = await request.json(); self.status_t = time.time()
            return JSONResponse({"ok": True})

        async def get_commands(request: Request):
            after = int(request.query_params.get("after", "0"))
            with self.cmd_lock:
                out = [dict(c, seq=i + 1) for i, c in enumerate(self.commands) if i + 1 > after]
            return JSONResponse({"commands": out})

        async def post_result(request: Request):
            r = await request.json(); self.results.append(r)
            self.note(f"result {json.dumps(r, ensure_ascii=False)[:300]}")
            return JSONResponse({"ok": True})

        async def health(request: Request):
            return JSONResponse({"ok": True, "adapter": self.adapter.manifest["version"], "agents": list(self.agents), "commands": len(self.commands)})

        return Starlette(routes=[Route("/status", post_status, methods=["POST"]), Route("/commands", get_commands),
                                 Route("/result", post_result, methods=["POST"]), Route("/health", health)])

    # ---------- reactions ----------
    def _react_blocking(self, now: float):
        token = AGENT_ENV.set(dict(self.shared_env))
        try:
            return self.react(dict(now=now, status=self.status, status_t=self.status_t, results=list(self.results),
                                   agents={a: self.env_of(a) for a in self.agents})) or []
        finally:
            AGENT_ENV.reset(token)

    async def tick_loop(self):
        if self.react is None:
            return
        period = float(self.config.get("tick", 5))
        while True:
            await asyncio.sleep(period)
            if not self.status:
                continue   # nothing to react to before the run script's first report
            try:
                cmds = await asyncio.to_thread(self._react_blocking, time.time())
            except Exception as exc:
                self.note(f"reaction error: {type(exc).__name__}: {exc}")
                continue
            for c in cmds:
                if c.get("kind") not in ("interrupt", "resume") or c.get("agent") not in self.agents or not isinstance(c.get("text"), str):
                    self.note(f"invalid command dropped: {c}")
                    continue
                with self.cmd_lock:
                    self.commands.append(dict(kind=c["kind"], agent=c["agent"], text=c["text"], id=c.get("id"), t=time.time()))
                self.note(f"command {c['kind']} {c['agent']} {c.get('id')}")

    async def run(self):
        servers = []
        for agent, spec in self.agents.items():
            with contextlib.suppress(FileNotFoundError):
                os.unlink(spec["socket"])
            servers.append(uvicorn.Server(uvicorn.Config(self.agent_app(agent), uds=spec["socket"], log_level="warning", timeout_keep_alive=3600)))
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.config["control_socket"])
        servers.append(uvicorn.Server(uvicorn.Config(self.control_app(), uds=self.config["control_socket"], log_level="warning")))
        for s in servers:
            s.install_signal_handlers = lambda: None
        self.note(f"service up: adapter {self.adapter.manifest['version']}, agents {', '.join(self.agents)}")
        await asyncio.gather(*(s.serve() for s in servers), self.tick_loop())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(Service(json.loads(args.config.read_text())).run())
