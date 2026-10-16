"""The run's sandbox for what an Evolver starts (agents, external programs). Every setting is the task user's.

enabled: bubblewrap with an allowlist. Visible, read-only: the system (/usr and the /lib, /bin links, a few /etc
files), the Python installation and the framework, the started program's own installation, the granted accesses, the
Evolver's directory, and the task's `sandbox.read`. Read-write: the workspace areas. Nothing else exists inside.
network: none (own network namespace), open (host network), restricted (host network plus HTTP(S)_PROXY pointing at
an allowlist CONNECT proxy run by the Trainer; best effort: a program that ignores proxy variables is not stopped).
env: `sandbox.env` lists the environment variables passed on (plus ALWAYS); null passes all of them.
disabled: commands run unconfined.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import select
import shutil
import socket
import socketserver
import sys
import threading
from urllib.parse import urlparse


# Variables the framework itself sets or a program cannot start without; passed even when sandbox.env is a list.
ALWAYS = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "PYTHONPATH", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "HA_CONTEXT",
          "HA_TOOL_URL"}
SYSTEM_ETC = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/alternatives", "/etc/ssl", "/etc/pki",
              "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/passwd", "/etc/group", "/etc/localtime",
              "/etc/crypto-policies", "/etc/os-release")
FRAMEWORK = Path(__file__).resolve().parents[1]


def _program_root(executable: str) -> Path | None:
    """The installation a program lives in: its real file's directory, or the parent of a bin/ directory."""
    found = shutil.which(executable)
    if not found:
        return None
    real = Path(found).resolve()
    return real.parent.parent if real.parent.name == "bin" else real.parent


@dataclass
class Sandbox:
    enabled: bool = True
    network: str = "restricted"                 # none | open | restricted
    allow: list[str] = field(default_factory=list)       # reachable hosts when restricted
    read: list[Path] = field(default_factory=list)       # the task's sandbox.read
    writable: list[Path] = field(default_factory=list)   # the workspace areas
    readable: list[Path] = field(default_factory=list)   # granted accesses, the Evolver's directory, ...
    proxy_url: str | None = None
    env_allow: list[str] | None = None                   # None: every variable passes; else only these (+ ALWAYS)

    def visible(self, command: list[str]) -> list[Path]:
        """Every read-only path inside the sandbox for this command (besides the system)."""
        out = [Path(sys.prefix), Path(sys.base_prefix), Path(sys.executable).resolve().parent.parent, FRAMEWORK,
               *self.readable, *self.read]
        root = _program_root(command[0]) if command else None
        if root is not None:
            out.append(root)
        return out

    def wrap(self, command: list[str], *, writable: list[Path] = (), env: dict | None = None,
             home: Path | None = None) -> tuple[list[str], dict]:
        """The command to run and its environment. `home` becomes HOME (it must be among the writable paths)."""
        env = dict(env if env is not None else os.environ)
        if self.env_allow is not None:
            keep = set(self.env_allow) | ALWAYS
            env = {k: v for k, v in env.items() if k in keep}
        if self.network == "restricted" and self.proxy_url:
            for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
                env[key] = self.proxy_url
            env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
        if not self.enabled:
            return command, env
        if not shutil.which("bwrap"):
            raise RuntimeError("sandbox.enabled is true but bubblewrap (bwrap) is not installed")
        found = shutil.which(command[0]) if command else None
        if found:      # run the program by its real path: PATH entries outside the sandbox do not exist inside
            command = [str(Path(found).resolve()), *command[1:]]
        args = ["bwrap", "--die-with-parent", "--new-session", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
                "--ro-bind", "/usr", "/usr"]
        for link in ("/lib", "/lib64", "/bin", "/sbin"):
            if os.path.islink(link):
                args += ["--symlink", os.readlink(link), link]
            elif os.path.isdir(link):
                args += ["--ro-bind", link, link]
        for path in SYSTEM_ETC:
            if os.path.exists(path):
                args += ["--ro-bind", path, path]
        if self.network == "none":
            args.append("--unshare-net")
        seen = set()
        for path in self.visible(command):
            path = Path(path).expanduser().resolve()
            if path.exists() and str(path) not in seen and not str(path).startswith("/usr"):
                seen.add(str(path))
                args += ["--ro-bind", str(path), str(path)]
        for path in [*self.writable, *writable]:
            Path(path).mkdir(parents=True, exist_ok=True)
            args += ["--bind", str(Path(path).resolve()), str(Path(path).resolve())]
        env["HOME"] = str(Path(home).resolve()) if home else str(Path(self.writable[0]).resolve()) \
            if self.writable else "/tmp"
        return args + ["--", *command], env


class AllowlistProxy:
    """HTTP CONNECT (and plain HTTP) proxy on 127.0.0.1 that forwards only to allowed hosts."""

    def __init__(self, allow: list[str]):
        self.hosts = {urlparse(a).hostname or a for a in allow}
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    self._handle()
                except OSError:          # a client or server dropping the connection is normal for a proxy
                    pass

            def _handle(self):
                line = self.rfile.readline(65536).decode("latin-1")
                parts = line.split()
                if len(parts) < 3:
                    return
                method, target = parts[0], parts[1]
                headers = []
                while True:
                    h = self.rfile.readline(65536)
                    if h in (b"\r\n", b"\n", b""):
                        break
                    headers.append(h)
                if method == "CONNECT":
                    host, _, port = target.partition(":")
                    port = int(port or 443)
                else:
                    u = urlparse(target)
                    host, port = u.hostname or "", u.port or 80
                if host not in outer.hosts:
                    self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                    return
                try:
                    upstream = socket.create_connection((host, port), timeout=30)
                except OSError:
                    self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                    return
                if method == "CONNECT":
                    self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                else:
                    upstream.sendall(line.encode("latin-1") + b"".join(headers) + b"\r\n")
                self._pipe(self.connection, upstream)

            @staticmethod
            def _pipe(a, b):
                sockets = [a, b]
                try:
                    while True:
                        ready, _, _ = select.select(sockets, [], [], 600)
                        if not ready:
                            return
                        for s in ready:
                            data = s.recv(65536)
                            if not data:
                                return
                            (b if s is a else a).sendall(data)
                finally:
                    b.close()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def hidden_paths(hide: list | None) -> list[Path]:
    """What a learner must not read although the sandbox shows the directories holding it: the framework checkout's
    examples (task code and data), tests, docs and git objects, plus `hide`: more paths, or "module:<name>" for an
    installed Python package (a benchmark whose evaluator sits beside the framework)."""
    import importlib.util
    out = [FRAMEWORK / "examples", FRAMEWORK / "tests", FRAMEWORK / "docs", FRAMEWORK / ".git"]
    for item in hide or []:
        item = str(item)
        if item.startswith("module:"):
            spec = importlib.util.find_spec(item[len("module:"):])
            if spec is None or not spec.submodule_search_locations:
                raise RuntimeError(f"hide: no installed package named {item[len('module:'):]!r}")
            out += [Path(loc) for loc in spec.submodule_search_locations]
        else:
            out.append(Path(item).expanduser())
    return out


class HidingSandbox:
    """A sandbox with paths covered: a directory by an empty tmpfs, a file by /dev/null, and `keep` bound back on
    top (the task description lives under a hidden tree). Subtractive, until the sandbox is a positive allowlist."""

    def __init__(self, inner, hidden: list[Path], keep: list[Path] = ()):
        self.inner, self.hidden, self.keep = inner, list(hidden), list(keep)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def wrap(self, command, **kw):
        args, env = self.inner.wrap(command, **kw)
        if args and args[0] == "bwrap" and "--" in args:
            cut = args.index("--")
            cover = []
            for p in self.hidden:
                p = p.resolve()
                if p.is_dir():
                    cover += ["--tmpfs", str(p)]
                elif p.is_file():
                    cover += ["--ro-bind", "/dev/null", str(p)]
            for p in self.keep:
                p = p.resolve()
                if p.exists():
                    cover += ["--ro-bind", str(p), str(p)]
            args = args[:cut] + cover + args[cut:]
        return args, env
