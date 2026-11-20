"""An environment served to an agent that runs elsewhere: the host keeps the environment (a task's Harbor environment,
with its driver and Docker), and the agent's process, in its own sandbox, reaches it only through this server, which
offers the agent's operations and nothing else (never `verify`, never the task's directory or its tests).

    server = EnvironmentServer(env).start()          # on the host: 127.0.0.1, a token in every URL
    spec = server.spec()                             # {url, workspace, timeout_s}: what the agent's process is given
    env = ProxyEnvironment.from_spec(spec)           # in the agent's process: exec / upload / download / install
    server.close()

`upload` and `download` move the files themselves (a tar of them in the request or the reply), so the agent's process
and the host need not see each other's paths. One server is started for one call and stopped after it.
"""
from __future__ import annotations

import inspect
import io
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import tarfile
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hermit.environment.base import Environment, ExecResult, Limits, Unsupported

PAYLOAD = "payload"                       # the one entry of an upload's or a download's tar


def _tar(path: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        tar.add(str(path), arcname=PAYLOAD, recursive=True)
    return buffer.getvalue()


def _untar(data: bytes, into: Path) -> Path:
    """The tar's one entry under `into` (links kept as links, nothing outside `into`: tarfile's data filter)."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        tar.extractall(str(into), filter="data")
    return into / PAYLOAD


class EnvironmentServer:
    """The agent's operations on `environment`, over HTTP on the loopback (module docstring)."""

    def __init__(self, environment: Environment, host: str = "127.0.0.1"):
        limits = environment.limits
        if limits.readable or limits.hidden or limits.network is not None:
            raise Unsupported("the sandbox's proxy carries an environment's time limit only: readable mounts, hidden "
                              "paths or a network limit of Hermit's are refused, never dropped")
        self.environment = environment
        self.token = secrets.token_hex(16)
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):                # quiet: the run's log is not this server's
                pass

            def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path != f"/{server.token}/info":
                    return self._send(404, b"{}")
                self._send(200, json.dumps(server.info()).encode())

            def do_POST(self):
                prefix = f"/{server.token}/"
                if not self.path.startswith(prefix):
                    return self._send(404, b"{}")
                op = self.path[len(prefix):].split("?", 1)[0]
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    if op == "upload":
                        target = self.headers.get("X-Target") or ""
                        payload = server.upload(body, target)
                    elif op == "download":
                        return self._send(200, server.download(json.loads(body)["source"]),
                                          "application/x-tar")
                    elif op == "exec":
                        payload = server.exec(json.loads(body))
                    elif op == "install":
                        request = json.loads(body)
                        payload = {"version": server.environment.install(request["cli"], request.get("version"))}
                    elif op == "secret":
                        server.environment.secret(json.loads(body)["paths"])
                        payload = {}
                    elif op == "ensure":
                        server.environment.ensure(json.loads(body)["packages"])
                        payload = {}
                    else:
                        return self._send(404, b"{}")
                    self._send(200, json.dumps({"ok": True, **payload}).encode())
                except Exception as exc:                 # noqa: BLE001 - the agent's process gets the error
                    kind = "Unsupported" if isinstance(exc, Unsupported) else type(exc).__name__
                    self._send(200, json.dumps({"ok": False, "type": kind,
                                                "error": f"{type(exc).__name__}: {exc}"[-3000:]}).encode())

        self.httpd = ThreadingHTTPServer((host, 0), Handler)
        self.httpd.daemon_threads = True
        self.host, self.port = host, self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/{self.token}"

    def start(self) -> "EnvironmentServer":
        self.thread.start()
        return self

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def info(self) -> dict:
        env = self.environment
        return {"workspace": str(env.workspace), "timeout_s": env.limits.timeout_s, "isolation": env.isolation,
                "installs": hasattr(env, "install"), "instruction": getattr(env, "instruction", None),
                "network_public": getattr(env, "network_public", True),
                "plan_admitted": bool(getattr(env, "plan_admitted", False)),
                "same_clock": bool(getattr(env, "same_clock", True)),
                "bin_served": dict(getattr(env, "bin_served", None) or {})}

    def spec(self) -> dict:
        return {"url": self.url, **self.info()}

    def exec(self, request: dict) -> dict:
        extra = {"by_fd": request["by_fd"]} if request.get("by_fd") else {}
        if request.get("stream") and "stream" in inspect.signature(self.environment.exec).parameters:
            extra["stream"] = request["stream"]
        result = self.environment.exec(request["command"], cwd=request.get("cwd"), env=request.get("env"),
                                       timeout=request.get("timeout"), stdin=request.get("stdin"), **extra)
        return {"code": result.code, "output": result.output, "timed_out": result.timed_out}

    def _as_agent(self) -> dict:
        """A transfer the agent's process asks for: with the environment's agent user's own permissions where the
        environment tells them apart from its provisioning (a task's Harbor environment)."""
        return {"as_agent": True} if "as_agent" in inspect.signature(self.environment.upload).parameters else {}

    def upload(self, body: bytes, target: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="hermit-upload-") as tmp:
            self.environment.upload(_untar(body, Path(tmp)), target, **self._as_agent())
        return {"target": str(self.environment.path(target))}

    def download(self, source: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="hermit-download-") as tmp:
            local = Path(tmp) / PAYLOAD
            self.environment.download(source, local, **self._as_agent())
            return _tar(local)


class ProxyEnvironment(Environment):
    """The environment an EnvironmentServer serves, from the agent's process (module docstring)."""
    isolation = "remote"

    def __init__(self, url: str, workspace: str, limits: Limits | None = None, *, installs: bool = False,
                 instruction: str | None = None, network_public: bool = True, plan_admitted: bool = False,
                 same_clock: bool = True, bin_served: dict | None = None):
        super().__init__(PurePosixPath("/"), limits)
        self.workspace = PurePosixPath(workspace)
        self.url = url.rstrip("/")
        self.installs = installs
        self.instruction = instruction                   # the task's instruction as its environment gives it
        self.network_public = network_public             # whether the agent's phase reaches the public network
        self.plan_admitted = plan_admitted               # the run holds the login's plan admission for this task
        self.same_clock = same_clock                     # whether the environment shares this machine's clock
        self.bin_served = dict(bin_served or {})         # cli -> whether its mounted binary serves there

    @classmethod
    def from_spec(cls, spec: dict) -> "ProxyEnvironment":
        timeout = spec.get("timeout_s")
        return cls(spec["url"], spec.get("workspace") or "/", Limits(timeout_s=float(timeout) if timeout else None),
                   installs=bool(spec.get("installs")), instruction=spec.get("instruction"),
                   network_public=spec.get("network_public", True) is not False,
                   plan_admitted=bool(spec.get("plan_admitted")), same_clock=spec.get("same_clock", True) is not False,
                   bin_served=spec.get("bin_served"))

    def __repr__(self) -> str:
        return f"ProxyEnvironment(workspace={str(self.workspace)!r})"

    def _post(self, op: str, body: bytes, *, headers: dict | None = None, timeout: float | None = None,
              raw: bool = False):
        request = urllib.request.Request(f"{self.url}/{op}", data=body, method="POST",
                                         headers={"Content-Type": "application/octet-stream", **(headers or {})})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # the loopback, never a proxy
        with opener.open(request, timeout=timeout) as reply:
            data = reply.read()
            kind = reply.headers.get("Content-Type", "")
        if raw and kind == "application/x-tar":
            return data
        payload = json.loads(data)
        if not payload.get("ok"):
            error = payload.get("error") or f"{op} failed"
            raise Unsupported(error) if payload.get("type") == "Unsupported" else RuntimeError(error)
        return payload

    def path(self, relative: str | Path = "", *, follow: bool = True) -> PurePosixPath:
        rel = PurePosixPath(str(relative))
        if rel.is_absolute():
            return rel
        parts: list[str] = []
        for part in rel.parts:
            if part == "..":
                if not parts:
                    raise ValueError(f"{relative} lies outside the workspace")
                parts.pop()
            elif part not in (".", ""):
                parts.append(part)
        return PurePosixPath(self.workspace, *parts)

    def exec(self, command, *, cwd=None, env=None, timeout=None, stdin=None, by_fd=None,
             stream: str | None = None) -> ExecResult:
        if self.limits.readable or self.limits.hidden or self.limits.network is not None:
            raise Unsupported("a task's own environment takes no readable mounts, hidden paths or network limits of "
                              "Hermit's (its task.toml says what it reaches): refused, never ignored")
        timeout = timeout if timeout is not None else self.limits.timeout_s
        body = json.dumps({"command": command if isinstance(command, str) else [str(c) for c in command],
                           "cwd": str(cwd) if cwd is not None else None, "env": env, "timeout": timeout,
                           "stdin": stdin, "by_fd": by_fd or None, "stream": stream}).encode()
        reply = self._post("exec", body, timeout=None if timeout is None else float(timeout) + 600)
        return ExecResult(code=int(reply["code"]), output=str(reply.get("output") or ""),
                          timed_out=bool(reply.get("timed_out")))

    def secret(self, paths) -> None:
        self._post("secret", json.dumps({"paths": [str(p) for p in paths]}).encode(), timeout=120)

    def upload(self, source, target) -> PurePosixPath:
        reply = self._post("upload", _tar(Path(source)), headers={"X-Target": str(target)}, timeout=3600)
        return PurePosixPath(reply["target"])

    def download(self, source, target) -> Path:
        data = self._post("download", json.dumps({"source": str(source)}).encode(), timeout=3600, raw=True)
        if isinstance(data, dict):
            raise RuntimeError(f"download of {source} returned no files")
        target = Path(target)
        with tempfile.TemporaryDirectory(prefix="hermit-download-") as tmp:
            got = _untar(data, Path(tmp))
            target.parent.mkdir(parents=True, exist_ok=True)
            if got.is_dir() and not got.is_symlink():
                shutil.copytree(got, target, symlinks=True, dirs_exist_ok=True)
            else:
                if os.path.lexists(target) and not target.is_dir():
                    target.unlink()
                shutil.move(str(got), str(target))
        return target

    def ensure(self, packages) -> None:
        if not self.installs:
            raise Unsupported("the environment installs nothing")
        self._post("ensure", json.dumps({"packages": list(packages)}).encode(), timeout=3600)

    def install(self, cli: str, version: str | None = None) -> str | None:
        if not self.installs:
            raise Unsupported("the environment installs no CLI")
        return self._post("install", json.dumps({"cli": cli, "version": version}).encode(), timeout=3600).get("version")


__all__ = ["EnvironmentServer", "ProxyEnvironment"]
