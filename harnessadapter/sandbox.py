"""The run's sandbox for what an Evolver starts (agents, external programs). Every setting is the task user's.

A positive allowlist: inside the sandbox exists only what is granted. Read-only: the system (/usr and the /lib, /bin
links, a few /etc files), the Python interpreter, the Python distributions the framework needs (the dependency closure
of `harnessadapter` plus `sandbox.packages`, not every package installed beside it), the `harnessadapter` package
itself (not the checkout it lives in), the started program's own installation, the granted accesses, the Evolver's
directory and the task's `sandbox.read`. Read-write: the workspace areas. Nothing else exists inside.

network: isolated (default) gives the process its own network namespace with loopback only. The only ways out are
the forwarded ports: every 127.0.0.1 URL in the command or its environment (the run's tool endpoint, a tool gate, a
model meter), and the allowlist proxy when `sandbox.allow` names hosts. Each is re-created inside by
`sandbox_relay.py` and bridged over a Unix socket, so a program that ignores the proxy variables has no route out.
open: the host network, unrestricted. The older `restricted` means isolated; `none` means isolated without the proxy.

enforce: required (default) refuses to run when bubblewrap is missing; best_effort runs the command unconfined and
records that it did. `enabled: false` turns the sandbox off. env: `sandbox.env` lists the environment variables
passed on (plus ALWAYS); null passes all of them.

Every wrapped command leaves a record in the run's `sandbox/` directory: what was bound, what was forwarded, which
environment variables passed, and the isolation check `sandbox_relay.py` ran inside before the command started. The
proxy writes one line per request to `sandbox/network.jsonl`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import importlib.metadata
import itertools
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
from urllib.parse import urlparse


# Variables the framework itself sets or a program cannot start without; passed even when sandbox.env is a list.
ALWAYS = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "PYTHONPATH", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "HA_CONTEXT",
          "HA_TOOL_URL"}
SYSTEM_ETC = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/alternatives", "/etc/ssl", "/etc/pki",
              "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/passwd", "/etc/group", "/etc/localtime",
              "/etc/crypto-policies", "/etc/os-release")
FRAMEWORK = Path(__file__).resolve().parents[1]          # the checkout; only its harnessadapter/ package is shown
PACKAGE = Path(__file__).resolve().parent
RELAY = PACKAGE / "sandbox_relay.py"
LOCAL_URL = re.compile(r"https?://(?:127\.0\.0\.1|localhost):(\d+)")
NETWORKS = {"isolated": ("isolated", True), "restricted": ("isolated", True), "none": ("isolated", False),
            "open": ("open", False)}


def network_mode(value: str) -> tuple[str, bool]:
    """sandbox.network -> (isolated | open, whether the allowlist proxy is wanted)."""
    if value not in NETWORKS:
        raise ValueError(f"sandbox.network must be one of {sorted(NETWORKS)}, not {value!r}")
    return NETWORKS[value]


def _program_root(executable: str) -> Path | None:
    """The installation a program lives in: its real file's directory, or the parent of a bin/ directory."""
    found = shutil.which(executable)
    if not found:
        return None
    real = Path(found).resolve()
    return real.parent.parent if real.parent.name == "bin" else real.parent


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def package_closure(roots: list[str]) -> list[importlib.metadata.Distribution]:
    """The installed distributions `roots` need: each root and, recursively, what it requires (extras only where a
    requirement asks for them). A requirement that is not installed is skipped."""
    try:
        from packaging.requirements import Requirement
    except ImportError:                                   # pragma: no cover - packaging ships with pip and setuptools
        Requirement = None
    seen: dict[str, importlib.metadata.Distribution] = {}
    todo = [(name, ()) for name in roots]
    while todo:
        name, extras = todo.pop()
        if _canonical(name) in seen:
            continue
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        seen[_canonical(name)] = dist
        for text in dist.requires or []:
            if Requirement is not None:
                req = Requirement(text)
                if req.marker and not any(req.marker.evaluate({"extra": e}) for e in (extras or ("",))):
                    continue
                todo.append((req.name, tuple(req.extras)))
            elif "extra ==" not in text:
                todo.append((re.split(r"[\s;\[<>=!~@(]", text.strip(), maxsplit=1)[0], ()))
    return list(seen.values())


def distribution_paths(dists) -> list[Path]:
    """The top-level entries (package directories, modules, .dist-info) the given distributions installed."""
    out = set()
    for dist in dists:
        for f in dist.files or []:
            parts = Path(f).parts
            if parts and parts[0] != "..":
                out.add(Path(dist.locate_file(parts[0])))
    return sorted(out)


def _venv_paths() -> list[Path]:
    """The virtual environment's interpreter links and pyvenv.cfg. They are bound at their own path, unresolved: a
    Python started as the link finds pyvenv.cfg beside it and so its environment; started as the file the link leads
    to, it would be the bare base interpreter."""
    if Path(sys.prefix) == Path(sys.base_prefix):
        return []
    venv = Path(sys.prefix)
    out = [venv / "pyvenv.cfg", *sorted(venv.glob("bin/python*"))]
    # pyvenv.cfg names the base interpreter by the path the environment was made with, which may be a link (uv's
    # cpython-3.11-... next to cpython-3.11.15-...). That name has to exist inside too, or Python finds no home.
    for line in (venv / "pyvenv.cfg").read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "home" and value.strip():
            out.append(Path(value.strip()).parent)
    return out


def _python_paths(packages: list[str]) -> list[Path]:
    """What the framework's Python needs: the interpreter with its standard library, the virtual environment's own
    files (not its site-packages as a whole), and the needed distributions."""
    out = [Path(sys.base_prefix), Path(sys.executable).resolve().parent.parent, *_venv_paths()]
    return out + distribution_paths(package_closure(["harnessadapter", *packages]))


class _Bridges:
    """The host side of the forwarded ports: one Unix socket per port, bridged to 127.0.0.1:port, and one listener
    per wrapped command that takes the relay's isolation report. The directory is short-named under /tmp because a
    Unix socket path may not exceed about 100 bytes."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ha-sbx-"))
        self._ports: dict[int, socket.socket] = {}
        self._lock = threading.Lock()

    def socket_for(self, port: int) -> Path:
        path = self.dir / f"fwd-{port}.sock"
        with self._lock:
            if port in self._ports:
                return path
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(64)
            self._ports[port] = server

        def serve():
            from harnessadapter.sandbox_relay import pipe
            while True:
                try:
                    client, _ = server.accept()
                except OSError:
                    return
                try:
                    upstream = socket.create_connection(("127.0.0.1", port), timeout=30)
                    upstream.settimeout(None)
                except OSError:
                    client.close()
                    continue
                threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
                threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()
        threading.Thread(target=serve, daemon=True).start()
        return path

    def report_listener(self, name: str, on_report) -> Path:
        """A socket that accepts one report, hands it to on_report, and disappears."""
        path = self.dir / f"report-{name}.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)

        def wait():
            try:
                conn, _ = server.accept()
                chunks = []
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
                conn.close()
                on_report(json.loads(b"".join(chunks) or b"null"))
            except (OSError, ValueError):
                pass
            finally:
                server.close()
                path.unlink(missing_ok=True)
        threading.Thread(target=wait, daemon=True).start()
        return path

    def close(self):
        with self._lock:
            for server in self._ports.values():
                server.close()
            self._ports.clear()
        shutil.rmtree(self.dir, ignore_errors=True)


@dataclass
class Sandbox:
    enabled: bool = True
    network: str = "isolated"                            # isolated | open (or the older restricted | none)
    allow: list[str] = field(default_factory=list)       # hosts the proxy lets through
    read: list[Path] = field(default_factory=list)       # the task's sandbox.read
    writable: list[Path] = field(default_factory=list)   # the workspace areas
    readable: list[Path] = field(default_factory=list)   # granted accesses, the Evolver's directory, ...
    proxy_url: str | None = None
    env_allow: list[str] | None = None                   # None: every variable passes; else only these (+ ALWAYS)
    enforce: str = "required"                            # required | best_effort
    packages: list[str] = field(default_factory=list)    # Python distributions to show beside the framework's
    record_dir: Path | None = None                       # where each wrapped command leaves its record
    probe_absent: list[Path] = field(default_factory=list)   # paths the isolation check must find missing
    role: str = "learner"

    def __post_init__(self):
        self.network, _ = network_mode(self.network)
        if self.enforce not in ("required", "best_effort"):
            raise ValueError("sandbox.enforce must be required or best_effort")
        self._bridges: _Bridges | None = None
        # Number records after the ones already there: a resumed run builds a new Sandbox in the same run directory.
        taken = [int(m.group(1)) for f in (self.record_dir.glob(f"{self.role}-*.json") if self.record_dir else [])
                 if (m := re.fullmatch(rf"{re.escape(self.role)}-(\d+)\.json", f.name))]
        self._count = itertools.count(max(taken, default=0) + 1)

    def visible(self, command: list[str]) -> list[Path]:
        """Every read-only path inside the sandbox for this command (besides the system)."""
        out = [*_python_paths(self.packages), PACKAGE, *self.readable, *self.read]
        root = _program_root(command[0]) if command else None
        if root is not None:
            out.append(root)
        bwrap = shutil.which("bwrap")
        if bwrap and not Path(bwrap).resolve().is_relative_to("/usr"):
            out.append(Path(bwrap).resolve())            # a nested sandbox (the mwm Evolver) needs the same binary
        return out

    def forwarded_ports(self, command: list[str], env: dict, forward: list[int] = ()) -> list[int]:
        """The ports to re-create inside: the explicit ones, every 127.0.0.1 URL in the command or the environment
        (the tool endpoint, a gate, a meter), and the proxy's."""
        ports = set(forward)
        for text in [*command, *env.values()]:
            ports.update(int(p) for p in LOCAL_URL.findall(str(text)))
        if self.proxy_url:
            ports.add(urlparse(self.proxy_url).port)
        return sorted(p for p in ports if p)

    def wrap(self, command: list[str], *, writable: list[Path] = (), env: dict | None = None,
             home: Path | None = None, keep=(), forward: list[int] = ()) -> tuple[list[str], dict]:
        """The command to run and its environment. `home` becomes HOME (it must be among the writable paths).
        `keep` names variables the caller set for this command itself (an agent's provider settings): they pass
        whatever sandbox.env lists. `forward` names more ports to re-create inside an isolated network."""
        env = dict(env if env is not None else os.environ)
        if self.env_allow is not None:
            allowed = set(self.env_allow) | ALWAYS | set(keep)
            env = {k: v for k, v in env.items() if k in allowed}
        if self.proxy_url:
            for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
                env[key] = self.proxy_url
            env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
        if not self.enabled:
            return command, env
        if not shutil.which("bwrap"):
            if self.enforce == "required":
                raise RuntimeError("sandbox.enforce is required but bubblewrap (bwrap) is not installed; install it, "
                                   "or set sandbox.enforce: best_effort to run unconfined and record that")
            self._record(command, {"enforced": False, "reason": "bubblewrap is not installed"}, env)
            return command, env
        found = shutil.which(command[0]) if command else None
        if found:      # run the program by its real path: PATH entries outside the sandbox do not exist inside
            real = Path(found).resolve()     # ... except this Python, which must keep its environment's path
            command = [sys.executable if real == Path(sys.executable).resolve() else str(real), *command[1:]]
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
        if self.network == "isolated":
            args.append("--unshare-net")
        seen, ro, rw = set(), [], []
        literal = {str(p) for p in _venv_paths()}
        for path in self.visible(command):
            path = Path(path).expanduser()
            path = path if str(path) in literal else path.resolve()
            if path.exists() and str(path) not in seen and not str(path).startswith("/usr"):
                seen.add(str(path))
                ro.append(str(path))
                args += ["--ro-bind", str(path), str(path)]
        for path in [*self.writable, *writable]:
            Path(path).mkdir(parents=True, exist_ok=True)
            rw.append(str(Path(path).resolve()))
            args += ["--bind", rw[-1], rw[-1]]
        env["HOME"] = str(Path(home).resolve()) if home else str(Path(self.writable[0]).resolve()) \
            if self.writable else "/tmp"
        policy = {"enforced": True, "network": self.network, "read_only": ro, "read_write": rw,
                  "proxy": self.proxy_url, "allow": list(self.allow)}
        if self.network == "isolated":
            if self._bridges is None:
                self._bridges = _Bridges()
            ports = self.forwarded_ports(command, env, forward)
            name = f"{self.role}-{next(self._count)}"
            record = self._record(command, {**policy, "forwarded": ports, "probe": None}, env, name=name)
            report = self._bridges.report_listener(name, lambda result: self._probe_result(record, result))
            args += ["--bind", str(self._bridges.dir), str(self._bridges.dir)]
            command = [sys.executable, str(RELAY),
                       *[a for p in ports for a in ("--forward", f"{p}={self._bridges.socket_for(p)}")],
                       *[a for p in self._absent(ro + rw) for a in ("--deny", p)],
                       "--report", str(report), "--", *command]
        else:
            self._record(command, policy, env)
        return args + ["--", *command], env

    def _absent(self, bound: list[str]) -> list[str]:
        """The probe paths that exist outside and are neither bound nor above or below a bound path."""
        out = []
        for path in self.probe_absent:
            p = Path(path).expanduser()
            if not p.exists():
                continue
            p = str(p.resolve())
            if any(p == b or p.startswith(b + "/") or b.startswith(p + "/") for b in bound):
                continue
            out.append(p)
        return out

    def _record(self, command: list[str], policy: dict, env: dict, name: str | None = None) -> Path | None:
        if self.record_dir is None:
            return None
        self.record_dir.mkdir(parents=True, exist_ok=True)
        name = name or f"{self.role}-{next(self._count)}"
        path = self.record_dir / f"{name}.json"
        path.write_text(json.dumps({"role": self.role, "time": datetime.now(timezone.utc).isoformat(),
                                    "program": command[0] if command else None, **policy,
                                    "environment": sorted(env)}, indent=1))
        return path

    @staticmethod
    def _probe_result(record: Path | None, result) -> None:
        if record is None or not record.exists():
            return
        data = json.loads(record.read_text())
        data["probe"] = result
        record.write_text(json.dumps(data, indent=1))

    def close(self):
        if self._bridges is not None:
            self._bridges.close()
            self._bridges = None


class AllowlistProxy:
    """HTTP CONNECT (and plain HTTP) proxy on 127.0.0.1 that forwards only to allowed hosts."""

    def __init__(self, allow: list[str], log: Path | None = None):
        self.hosts = {urlparse(a).hostname or a for a in allow}
        self.log = Path(log) if log else None
        self._log_lock = threading.Lock()
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
                    outer._write(method, host, port, False)
                    self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                    return
                outer._write(method, host, port, True)
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

    def _write(self, method: str, host: str, port: int, allowed: bool) -> None:
        if self.log is None:
            return
        line = json.dumps({"time": datetime.now(timezone.utc).isoformat(), "method": method, "host": host,
                           "port": port, "allowed": allowed})
        with self._log_lock:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log, "a") as f:
                f.write(line + "\n")

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
