"""An adapter's tools in a sandboxed `hermit serve`, reached over MCP from the program that holds the Session.

The program never imports the adapter's code: the tools run in a child process inside the sandbox it is given
(#11), with the adapter read-only, the deployment texts and learning material read-only, the evidence store writable,
and whatever network the sandbox grants. The client runs on its own thread and event loop, so the one connection
serves synchronous and asynchronous callers alike, from any event loop. It speaks MCP over stdio by itself and needs
no package beyond the standard library, so a program on any interpreter can hold a sandboxed Session.
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


class SandboxedServer:
    def __init__(self, sandbox, root: Path, *, deployment: dict | None = None, learning_material: Path | None = None,
                 evidence: Path | None = None, timeout: float = 60, call_timeout: float | None = 600):
        import os
        from hermit.adapter.session import ENV_PYTHON, ENV_SANDBOX
        from hermit.sandbox import framework_env
        from hermit.sandbox import program_paths

        self.work = Path(tempfile.mkdtemp(prefix="ha-session-"))
        readable, writable = [root], [self.work]
        # The server runs on the framework's Python. A program on another interpreter (a benchmark's own venv) that
        # holds a Session names it through the environment; that installation is shown for the server.
        python = os.environ.get(ENV_PYTHON) or sys.executable
        if Path(python).resolve() != Path(sys.executable).resolve():
            readable += [Path(python).parent.parent, *program_paths(python)]
        command = [python, "-m", "hermit.runtime.mcp_server", "--adapter", str(root)]
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
        self.call_timeout = call_timeout
        env = framework_env({ENV_SANDBOX: "0"})          # the server is the sandbox: its Session serves in place
        self.args, self.env = sandbox.wrap(command, readable=readable, writable=writable, home=self.work,
                                           env=env, keep=["PYTHONPATH", ENV_SANDBOX])
        self.log = self.work / "server.log"
        self._errlog = open(self.log, "w")
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._client = None
        self._failure: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="hermit-session", daemon=True)
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
        # The client side is a few lines of MCP over stdio (newline-delimited JSON-RPC), written here rather than
        # taken from the `mcp` package: the program that holds the Session may run on another interpreter (a
        # benchmark's own virtual environment) where that package is not installed. The server, on the framework's
        # Python, is the real thing.
        self._stop = asyncio.Event()
        self._task = asyncio.current_task()
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        proc = None
        reader = None
        try:
            proc = await asyncio.create_subprocess_exec(*self.args, stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.PIPE, stderr=self._errlog,
                                                        env=self.env, cwd=str(self.work))
            self._proc = proc
            reader = asyncio.ensure_future(self._read(proc.stdout))
            from hermit import __version__
            await self._request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "hermit", "version": __version__}})
            await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._client = proc
            self._ready.set()
            await self._stop.wait()
        except BaseException as exc:                     # a server that dies is reported to the caller waiting on it
            self._failure = exc
        finally:
            self._client = None
            if reader is not None:
                reader.cancel()
            if proc is not None and proc.returncode is None:
                try:
                    proc.stdin.close()
                    await asyncio.wait_for(proc.wait(), 5)
                except (asyncio.TimeoutError, OSError):
                    proc.kill()
            self._ready.set()

    async def _send(self, message: dict) -> None:
        self._proc.stdin.write((json.dumps(message) + "\n").encode())
        await self._proc.stdin.drain()

    async def _request(self, method: str, params: dict):
        self._next_id += 1
        rid = self._next_id
        future = self.loop.create_future()
        self._pending[rid] = future
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return await future

    async def _read(self, stdout) -> None:
        """Answers to our requests, by id; the server's own requests and notifications are left unanswered."""
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                future = self._pending.pop(message.get("id"), None) if "id" in message and "method" not in message else None
                if future is None or future.done():
                    continue
                if "error" in message:
                    err = message["error"]
                    future.set_exception(RuntimeError(f"{err.get('message')} ({err.get('code')})"))
                else:
                    future.set_result(message.get("result"))
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("the sandboxed adapter server closed its channel"))
            self._pending.clear()

    async def _call(self, name: str, arguments: dict):
        if self._client is None:
            raise RuntimeError(f"the sandboxed adapter server is not running; log {self.log}")
        # A tool that breaks its own channel (writes to the server's stdout) leaves the answer unreadable: the call
        # ends with an error after `call_timeout`, never waits forever.
        try:
            result = await asyncio.wait_for(self._request("tools/call", {"name": name, "arguments": arguments}),
                                            self.call_timeout)
        except asyncio.TimeoutError:
            raise ToolError(f"{name}: no answer from the sandboxed adapter server in {self.call_timeout:.0f} s; "
                            f"log {self.log}") from None
        if result.get("isError"):
            raise ToolError("".join(c.get("text", "") for c in result.get("content", [])))
        return result.get("structuredContent")

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
