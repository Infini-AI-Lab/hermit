"""An adapter's tools in a sandboxed `harnessadapter serve`, reached over MCP from the program that holds the Session.

The program never imports the adapter's code: the tools run in a child process inside the sandbox it is given
(#11), with the adapter read-only, the deployment texts and learning material read-only, the evidence store writable,
and whatever network the sandbox grants. The client runs on its own thread and event loop, so the one connection
serves synchronous and asynchronous callers alike, from any event loop.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading


class ToolError(RuntimeError):
    """A tool call the sandboxed server answered with an error (its text is the message)."""


def _field(result, *names):
    for name in names:
        if name in type(result).model_fields:
            return getattr(result, name)
    raise AttributeError(f"{type(result).__name__} has none of {names}")


class SandboxedServer:
    def __init__(self, sandbox, root: Path, *, deployment: dict | None = None, learning_material: Path | None = None,
                 evidence: Path | None = None, timeout: float = 60):
        from harnessadapter.isolate import _framework_env

        self.work = Path(tempfile.mkdtemp(prefix="ha-session-"))
        command = [sys.executable, "-m", "harnessadapter.runtime.mcp_server", "--adapter", str(root)]
        readable, writable = [root], [self.work]
        if deployment is not None:
            path = self.work / "host.json"
            path.write_text(json.dumps(deployment))
            command += ["--deployment", str(path)]
        if learning_material is not None:
            command += ["--learning-material", str(learning_material)]
            readable.append(learning_material)
        if evidence is not None:
            Path(evidence).mkdir(parents=True, exist_ok=True)
            command += ["--evidence", str(evidence)]
            writable.append(Path(evidence))
        self.sandbox = sandbox
        self.args, self.env = sandbox.wrap(command, readable=readable, writable=writable, home=self.work,
                                           env=_framework_env(), keep=["PYTHONPATH"])
        self.log = self.work / "server.log"
        self._errlog = open(self.log, "w")
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._client = None
        self._failure: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="harnessadapter-session", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout) or self._failure is not None:
            reason = f"{type(self._failure).__name__}: {self._failure}" if self._failure else "it did not start in time"
            self._errlog.flush()
            tail = self.log.read_text(errors="replace")[-1500:]
            self.close()
            raise RuntimeError(f"the sandboxed adapter server did not start ({reason}):\n{tail}")

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        self._stop = asyncio.Event()
        self._task = asyncio.current_task()
        params = StdioServerParameters(command=self.args[0], args=self.args[1:], env=self.env, cwd=str(self.work))
        try:
            async with stdio_client(params, errlog=self._errlog) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    self._client = client
                    self._ready.set()
                    await self._stop.wait()
        except BaseException as exc:                     # a server that dies is reported to the caller waiting on it
            self._failure = exc
        finally:
            self._client = None
            self._ready.set()

    async def _call(self, name: str, arguments: dict):
        if self._client is None:
            raise RuntimeError(f"the sandboxed adapter server is not running; log {self.log}")
        result = await self._client.call_tool(name, arguments)
        # mcp's result fields are snake_case in newer releases, camelCase in older ones.
        if _field(result, "is_error", "isError"):
            raise ToolError("".join(getattr(c, "text", "") for c in result.content))
        return _field(result, "structured_content", "structuredContent")

    async def acall(self, name: str, arguments: dict):
        """Run one tool in the server; await it from any event loop."""
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(self._call(name, arguments), self.loop))

    def _halt(self) -> None:
        if self._client is not None and self._stop is not None:
            self._stop.set()                             # connected: leave the client and stop the server
        elif self._task is not None:
            self._task.cancel()                          # still starting: stop that, and the server with it

    def close(self) -> None:
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self._halt)
        self._thread.join(timeout=15)
        if self._thread.is_alive():                      # it did not stop: leave its files for it, report where
            raise RuntimeError(f"the sandboxed adapter server did not stop; its files are in {self.work}")
        self.loop.close()
        self._errlog.close()
        shutil.rmtree(self.work, ignore_errors=True)
