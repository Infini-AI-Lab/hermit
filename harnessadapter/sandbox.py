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
import subprocess
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


def framework_env(extra: dict | None = None, *, no_bytecode: bool = False) -> dict:
    """The environment for a framework subprocess: this checkout first on PYTHONPATH, so a child imports this
    harnessadapter, not another installed one. `no_bytecode` keeps an acting adapter free of bytecode caches."""
    env = {**os.environ, **(extra or {})}
    env["PYTHONPATH"] = str(FRAMEWORK) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if no_bytecode:
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_in_strict_sandbox(command: list[str], *, role: str, readable, enforce: str, timeout: float | None = None,
                          no_bytecode: bool = False) -> "subprocess.CompletedProcess":
    """Run `command` in a strict sandbox and return the finished process. The adapter's or template's own modules
    (the learner's work) run here, never with the caller's network or keys: an isolated network, only `readable`
    shown read-only, and only PYTHONPATH kept. The caller decides what a non-zero exit means."""
    keep = ["PYTHONPATH", *(["PYTHONDONTWRITEBYTECODE"] if no_bytecode else [])]
    box = Sandbox(role=role, network="isolated", env_allow=keep, readable=[Path(p) for p in readable],
                  enforce=enforce)
    try:
        args, env = box.wrap(command, env=framework_env(no_bytecode=no_bytecode), keep=keep)
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
    finally:
        box.close()
NETWORKS = {"isolated": ("isolated", True), "restricted": ("isolated", True), "none": ("isolated", False),
            "open": ("open", False)}


def network_mode(value: str) -> tuple[str, bool]:
    """sandbox.network -> (isolated | open, whether the allowlist proxy is wanted)."""
    if value not in NETWORKS:
        raise ValueError(f"sandbox.network must be one of {sorted(NETWORKS)}, not {value!r}")
    return NETWORKS[value]


def program_paths(executable: str, depth: int = 2) -> list[Path]:
    """What a program needs to start, and nothing around it: every name on its chain of links, the package its real
    file belongs to (the parent of a bin/ directory, else the file's directory, never the home or above it), the
    interpreter its #! line names (a node CLI runs on the node beside its link), and, for a short wrapper script,
    the programs it names by absolute path (a ~/bin/codex that execs an nvm-installed codex)."""
    found = shutil.which(executable)
    if not found:
        return []
    home = Path.home().resolve()
    out: list[Path] = []
    seen: set[str] = set()

    def system(path: Path) -> bool:
        return str(path).startswith(("/usr/", "/bin/", "/sbin/", "/lib"))

    def add(path: str, depth: int) -> None:
        p = Path(os.path.abspath(path))
        if str(p) in seen or system(p):
            return
        seen.add(str(p))
        hops = [p]
        while hops[-1].is_symlink() and len(hops) < 20:
            hops.append(Path(os.path.normpath(hops[-1].parent / os.readlink(hops[-1]))))
        real = hops[-1].resolve()
        out.extend(hops)
        root = real.parent.parent if real.parent.name == "bin" else real.parent
        out.append(real if home.is_relative_to(root.resolve()) else root)
        try:
            text = real.read_bytes()[:8192]
        except OSError:
            return
        if text.startswith(b"#!"):
            words = text[2:].split(b"\n", 1)[0].decode(errors="replace").split()
            if words:
                name = words[0]
                if name.endswith("/env"):
                    name = next((w for w in words[1:] if not w.startswith("-")), "")
                if name and not name.startswith("/"):
                    beside = [h.parent / name for h in hops if (h.parent / name).exists()]
                    name = str(beside[0]) if beside else (shutil.which(name) or "")
                if name:
                    add(name, depth)
            if depth > 0 and len(text) < 8192:
                for token in re.findall(r"(/[^\s\"'$;|&()]+)", text.decode(errors="replace")):
                    if os.path.isfile(token) and os.access(token, os.X_OK):
                        add(token, depth - 1)
    add(found, depth)
    return list(dict.fromkeys(out))


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


def _names(path) -> list[str]:
    """The names a path is bound under inside: as given (absolute, symlinks kept) and resolved, when they differ.
    A program that was told the given name finds it (a venv under a linked ~/.cache looks for its site-packages by
    the venv's own path), and one that resolved it first finds it too."""
    given = os.path.abspath(os.path.expanduser(str(path)))
    real = os.path.realpath(given)
    return [given] if real == given else [given, real]


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


def _venv_bases(paths) -> list[Path]:
    """The Python installations the virtual environments in `paths` (a granted directory that is one, or holds one
    as .venv) were made from: a venv's interpreter is a link to that installation, which lies elsewhere."""
    out = []
    for path in paths:
        for cfg in (Path(path) / "pyvenv.cfg", Path(path) / ".venv" / "pyvenv.cfg"):
            try:
                lines = cfg.read_text().splitlines()
            except OSError:
                continue
            for line in lines:
                key, _, value = line.partition("=")
                if key.strip() == "home" and value.strip():
                    # The installation's bin/, as written: uv names a version alias (a link to the installation)
                    # and the venv's interpreter links through it, so both names must exist inside.
                    home = Path(value.strip())
                    out.append(home.parent if home.name == "bin" else home)
    return out


def _python_paths(packages: list[str]) -> list[Path]:
    """What the framework's Python needs: the interpreter with its standard library, the virtual environment's own
    files (not its site-packages as a whole), and the needed distributions."""
    out = [Path(sys.base_prefix), Path(sys.executable).resolve().parent.parent, *_venv_paths()]
    if "*" in packages:
        out += _installed_paths()
        packages = [p for p in packages if p != "*"]
    return out + distribution_paths(package_closure(["harnessadapter", *packages]))


def _editable_finder_paths(directory: Path, line: str) -> list[Path]:
    """The source directories of a setuptools editable install that imports a finder (`import __editable___x_finder;
    ...install()`): the finder module's MAPPING names them."""
    import ast
    out = []
    for module in re.findall(r"import\s+(__editable__[\w.]*finder)", line):
        try:
            tree = ast.parse((directory / f"{module}.py").read_text())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "MAPPING" for t in node.targets):
                try:
                    mapping = ast.literal_eval(node.value)
                except ValueError:
                    continue
                for value in mapping.values():
                    p = Path(value)
                    out.append(p.parent if p.is_file() else p)
    return out


def _installed_paths() -> list[Path]:
    """Every installed package: the environment's site directories and what their .pth files add (an editable
    install's source). Not the rest of sys.path: the working directory a program was started from (`python -m`
    puts it first) or PYTHONPATH can be the home or a directory of runs. The framework's checkout is excluded too
    (its package is bound by itself), and so is any directory that holds the home."""
    import site
    sites = [Path(p) for p in [*site.getsitepackages(), site.getusersitepackages()] if os.path.isdir(p)]
    out = list(sites)
    for directory in sites:
        for pth in sorted(directory.glob("*.pth")):
            try:
                lines = pth.read_text().splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if line.startswith(("import ", "import\t")):
                    out += _editable_finder_paths(directory, line)
                    continue
                if not line or line.startswith("#"):
                    continue
                p = (directory / line).resolve() if not os.path.isabs(line) else Path(line).resolve()
                if p.is_dir():
                    out.append(p)
    home = Path.home().resolve()
    return [p for p in out if p.resolve() != FRAMEWORK and not home.is_relative_to(p.resolve())]


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
    packages: list[str] = field(default_factory=list)    # Python distributions to show beside the framework's ("*": all)
    record_dir: Path | None = None                       # where each wrapped command leaves its record
    probe_absent: list[Path] = field(default_factory=list)   # paths the isolation check must find missing
    masked: list[Path] = field(default_factory=list)     # paths inside a bound directory to cover (a run's secrets)
    audit: str | None = None                             # files: strace every file access from outside, beside the record
    role: str = "learner"

    def __post_init__(self):
        self.network, _ = network_mode(self.network)
        if self.enforce not in ("required", "best_effort"):
            raise ValueError("sandbox.enforce must be required or best_effort")
        if self.audit not in (None, "files"):
            raise ValueError("sandbox.audit must be null or files")
        self._bridges: _Bridges | None = None
        self._empty: str | None = None
        self._setup = threading.Lock()                   # concurrent first calls (an agent's concurrency) share one
        # Number records after the ones already there: a resumed run builds a new Sandbox in the same run directory.
        taken = [int(m.group(1)) for f in (self.record_dir.glob(f"{self.role}-*.json") if self.record_dir else [])
                 if (m := re.fullmatch(rf"{re.escape(self.role)}-(\d+)\.json", f.name))]
        self._count = itertools.count(max(taken, default=0) + 1)

    def visible(self, command: list[str]) -> list[Path]:
        """Every read-only path inside the sandbox for this command (besides the system)."""
        out = [*_python_paths(self.packages), PACKAGE, *self.readable, *self.read]
        out += _venv_bases([*self.readable, *self.read, *self.writable])
        found = shutil.which(command[0]) if command else None
        if found and Path(found).resolve() != Path(sys.executable).resolve():
            out += program_paths(command[0])          # this Python is shown above, by its packages, not its venv
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
             home: Path | None = None, keep=(), forward: list[int] = (),
             readable: list[Path] = (), masked: list[Path] = ()) -> tuple[list[str], dict]:
        """The command to run and its environment. `home` becomes HOME (it must be among the writable paths).
        `keep` names variables the caller set for this command itself (an agent's provider settings): they pass
        whatever sandbox.env lists. `forward` names more ports to re-create inside an isolated network.
        `readable` adds read-only paths for this command only (the adapter a rollout runs with).

        Order inside: the visible paths, then the masks over them (the run's secrets in a bound directory), then
        `readable` and the writable paths, which may lie under a mask and are bound back on top of it."""
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
        # A process namespace of its own: the host's processes (their environment, their open files, their root
        # through /proc/<pid>/root) are not there to read or signal.
        args = ["bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--dev", "/dev", "--proc", "/proc",
                "--tmpfs", "/tmp",
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
        # Order: what is shown read-only, the role's own writable paths, the masks over both (read-only, so a
        # writable directory cannot uncover a secret inside it), then this call's readable and writable paths, which
        # may lie under a mask and are bound back on top of it.
        to_mask = [*self.masked, *masked]                  # the sandbox's own, and this call's (computed now)
        seen, ro, rw, covered, aliases = set(), [], [], [], []

        def bind(paths, flag, out, fresh=False):
            for path in paths:
                if flag == "--bind":
                    Path(path).mkdir(parents=True, exist_ok=True)
                names = _names(path)
                if len(names) == 2:
                    aliases.append(tuple(names))           # the same directory under two names: masks need both
                for name in names:
                    if not os.path.exists(name) or name.startswith("/usr"):
                        continue
                    key = (flag, name)
                    if key in seen and not fresh:          # a call's own paths go on top of the masks, always
                        continue
                    seen.add(key)
                    out.append(name)
                    args.extend([flag, name, name])
        # A call's own path that holds a covered path goes in before the masks, or binding it after would uncover
        # them (a tool's task directory holding the runs); one beneath a mask goes on top, after them.
        cover = [n for path in to_mask for n in _names(path) if os.path.exists(n)]

        def holds_mask(path) -> bool:
            return any(c == n or c.startswith(n + "/") for n in _names(path) for c in cover)

        def beneath_mask(path) -> bool:
            """Strictly under a covered path. A role's grant kept inside the run's records (a launcher's task
            directory under the runs directory, which the agent writes) is bound on top of the mask, after it, like
            a call's own path; bound before, the mask would cover it."""
            return any(n.startswith(c + "/") for n in _names(path) for c in cover)
        deferred_ro = [p for p in [*self.readable, *self.read] if beneath_mask(p)]
        deferred_rw = [p for p in self.writable if beneath_mask(p)]
        skip = {os.path.abspath(str(p)) for p in deferred_ro}
        bind([p for p in self.visible(command) if os.path.abspath(str(p)) not in skip], "--ro-bind", ro)
        bind([p for p in readable if holds_mask(p)], "--ro-bind", ro)
        bind([p for p in self.writable if p not in deferred_rw], "--bind", rw)
        bind([p for p in writable if holds_mask(p)], "--bind", rw)
        readable, writable = [*readable, *deferred_ro], [*writable, *deferred_rw]
        readable = [p for p in readable if not holds_mask(p)]
        writable = [p for p in writable if not holds_mask(p)]
        shown = ro + rw
        # Shortest first, so a covered directory comes before anything beneath it; a path under one is already
        # hidden, and covering it again would leave a mount point behind in the empty directory.
        # A secret is covered under every name it is shown by: through a bound directory reached by a link, the
        # same file has a second path inside, and covering one name leaves the other.
        names = {n for path in to_mask for n in _names(path)}
        for n in list(names):
            for given, real in aliases:
                for a, b in ((real, given), (given, real)):
                    if n == a or n.startswith(a + "/"):
                        names.add(b + n[len(a):])
        for name in sorted(names, key=len):
            if not os.path.exists(name) or not any(name == b or name.startswith(b + "/") for b in shown) or any(
                    name == m or name.startswith(m + "/") for m in covered):
                continue                                  # nothing there, not shown anyway, or covered already
            covered.append(name)
            # A file is covered by an empty regular file: /dev/null cannot be opened on a mount without devices.
            args.extend(["--tmpfs", name] if os.path.isdir(name) else ["--ro-bind", self._empty_file(), name])
        n_ro, n_rw = len(ro), len(rw)
        bind(readable, "--ro-bind", ro, fresh=True)
        bind(writable, "--bind", rw, fresh=True)
        own = ro[n_ro:] + rw[n_rw:]                       # this call's paths, which may lie under a mask
        # A covered directory becomes read-only last: a call's own paths beneath it needed their mount points made
        # first, and the remount leaves what is mounted inside it as it is.
        for name in covered:
            if os.path.isdir(name):
                args.extend(["--remount-ro", name])
        ro = [p for p in ro if p not in rw]               # a path bound both ways is writable: the later bind wins
        env["HOME"] = _names(home)[0] if home else _names(self.writable[0])[0] if self.writable else "/tmp"
        policy = {"enforced": True, "network": self.network, "read_only": ro, "read_write": rw, "masked": covered,
                  "proxy": self.proxy_url, "allow": list(self.allow)}
        name = f"{self.role}-{next(self._count)}"
        if self.audit == "files":
            # strace runs outside, as bwrap's parent: what the sandbox does cannot reach the trace it leaves.
            if self.record_dir is None or not shutil.which("strace"):
                raise RuntimeError("sandbox.audit: files needs strace installed and a directory for the records")
            self.record_dir.mkdir(parents=True, exist_ok=True)
            trace = self.record_dir / f"{name}.strace"
            args = ["strace", "-f", "-qq", "-e", "trace=%file", "-o", str(trace), *args]
            policy["audit"] = str(trace)
        deny = [*self._absent(ro + rw), *covered]
        if self.network == "isolated" or deny:
            # The check inside: on an isolated network the network and the paths, on an open one the paths only
            # (what must be missing, and the covered secrets).
            with self._setup:
                if self._bridges is None:
                    self._bridges = _Bridges()
            ports = self.forwarded_ports(command, env, forward) if self.network == "isolated" else []
            record = self._record(command, {**policy, "forwarded": ports, "probe": None}, env, name=name)
            report = self._bridges.report_listener(name, lambda result: self._probe_result(record, result))
            args += ["--bind", str(self._bridges.dir), str(self._bridges.dir)]
            command = [sys.executable, str(RELAY),
                       *[a for p in ports for a in ("--forward", f"{p}={self._bridges.socket_for(p)}")],
                       *[a for p in deny for a in ("--deny", p)],
                       *[a for p in own for a in ("--keep", p)],
                       *(["--open-network"] if self.network == "open" else []),
                       "--report", str(report), "--", *command]
        else:
            self._record(command, policy, env, name=name)
        return args + ["--", *command], env

    def _absent(self, bound: list[str]) -> list[str]:
        """The probe paths that exist outside and are neither bound themselves nor above a bound path. One beneath a
        bound directory stays: it is shown by accident (a home bound whole), and the check must catch that."""
        out = []
        for path in self.probe_absent:
            p = Path(path).expanduser()
            if not p.exists():
                continue
            p = str(p.resolve())
            if any(p == b or b.startswith(p + "/") for b in bound):
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

    def _empty_file(self) -> str:
        with self._setup:
            if self._empty is None:
                fd, name = tempfile.mkstemp(prefix="ha-sbx-empty-")
                os.close(fd)
                os.chmod(name, 0o444)
                self._empty = name
            return self._empty

    def close(self):
        if self._bridges is not None:
            self._bridges.close()
            self._bridges = None
        if self._empty is not None:
            Path(self._empty).unlink(missing_ok=True)
            self._empty = None


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
