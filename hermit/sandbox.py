"""The bubblewrap core of every sandbox the framework starts (docs/developer/sandbox-design.md). Every setting is the
run's.

An allowlist: inside exists only what is granted. Read-only: the system (/usr and the /lib, /bin links, a few /etc
files), the interpreter with its standard library, the Python distributions asked for (the dependency closure of
`hermit` when `framework`, plus `packages`; every other installed package is absent), the hermit package itself (not
the checkout it lives in) when `framework`, the started program's own installation and the granted paths.
Read-write: the granted writable paths. Under names of their own: the binds (the evolver's layout). Nothing is shown
and then covered.

network: isolated gives the process its own network namespace with loopback only. The only ways out are the
forwarded ports: every 127.0.0.1 URL in the command or its environment (the tool endpoint, the model proxy), and the
allowlist proxy when `allow` names hosts. Each is re-created inside by `sandbox_relay.py` and bridged over a Unix
socket, so a program that ignores the proxy variables has no route out. open: the host network. The run config
names these no-network (isolated, no proxy), allowlist (isolated, the proxy) and public (open).

enforce: required refuses to run when bubblewrap is missing; best_effort runs the command unconfined and records
that it did. `enabled: false` turns the sandbox off. env: `env_allow` lists the environment variables passed on
(plus ALWAYS); None passes all of them. limits: memory, file size and CPU time, set on the command by the relay.

Every wrapped command leaves a record in the run's `sandbox/` directory: what was bound, what was forwarded, which
environment variables passed, and the isolation check `sandbox_relay.py` ran inside before the command started. The
proxy writes one line per request to `sandbox/network.jsonl`.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
import importlib.metadata
import fnmatch
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
from typing import NamedTuple
from urllib.parse import urlparse


# Variables the framework itself sets or a program cannot start without; passed whatever the allowlist says.
ALWAYS = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "PYTHONPATH", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "HA_CONTEXT",
          "HA_TOOL_URL"}
SYSTEM_ETC = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/alternatives", "/etc/ssl", "/etc/pki",
              "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/passwd", "/etc/group", "/etc/localtime",
              "/etc/crypto-policies", "/etc/os-release")
FRAMEWORK = Path(__file__).resolve().parents[1]          # the checkout; only its hermit/ package is shown
PACKAGE = Path(__file__).resolve().parent
DISTRIBUTION = "hermit-harness"                           # the installed distribution (pip name) of this package
RELAY = PACKAGE / "sandbox_relay.py"
LOCAL_URL = re.compile(r"https?://(?:127\.0\.0\.1|localhost):(\d+)")


def framework_env(extra: dict | None = None, *, no_bytecode: bool = False) -> dict:
    """The environment for a framework subprocess: this checkout first on PYTHONPATH, so a child imports this
    hermit, not another installed one. `no_bytecode` keeps an acting adapter free of bytecode caches."""
    env = {**os.environ, **(extra or {})}
    env["PYTHONPATH"] = str(FRAMEWORK) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if no_bytecode:
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_in_strict_sandbox(command: list[str], *, role: str, readable, enforce: str | None,
                          timeout: float | None = None, no_bytecode: bool = False) -> "subprocess.CompletedProcess":
    """Run `command` in a strict sandbox and return the finished process. The adapter's or template's own modules
    (the learner's work) run here, never with the caller's network or keys: an isolated network, only `readable`
    shown read-only, and only PYTHONPATH kept. `enforce` is the run's sandbox.enforce, or None when the run's
    sandbox is off (sandbox.enabled: false), and the command then runs unconfined like everything else the run
    starts. The caller decides what a non-zero exit means."""
    keep = ["PYTHONPATH", *(["PYTHONDONTWRITEBYTECODE"] if no_bytecode else [])]
    box = Sandbox(enabled=enforce is not None, role=role, network="isolated", env_allow=keep,
                  readable=[Path(p) for p in readable], enforce=enforce or "required")
    try:
        args, env = box.wrap(command, env=framework_env(no_bytecode=no_bytecode), keep=keep)
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
    finally:
        box.close()


NETWORK_MODES = ("no-network", "allowlist", "public")            # what a run config may name
NETWORKS = {"no-network": ("isolated", False), "allowlist": ("isolated", True), "public": ("open", False),
            "isolated": ("isolated", True), "open": ("open", False)}       # the last two: the framework's own callers


def network_mode(value: str) -> tuple[str, bool]:
    """A network mode -> (isolated | open, whether the allowlist proxy is wanted)."""
    if value not in NETWORKS:
        raise ValueError(f"network must be one of {list(NETWORK_MODES)}, not {value!r}")
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
    from packaging.requirements import Requirement    # a dependency of hermit: pip's own copy is not importable
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
            req = Requirement(text)
            if req.marker and not any(req.marker.evaluate({"extra": e}) for e in (extras or ("",))):
                continue
            todo.append((req.name, tuple(req.extras)))
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


def _venv_bases(paths, writable=()) -> list[Path]:
    """The Python installations the virtual environments in `paths` (a granted directory that is one, or holds one
    as .venv) were made from: a venv's interpreter is a link to that installation, which lies elsewhere. Only places
    shown read-only are read, and no `pyvenv.cfg` in a `writable` one (a place granted both ways, or one below):
    a session writes it there, and its `home` would be shown in the next call (a venv made inside the sandbox is
    made from an installation the sandbox shows already)."""
    places = {n for w in writable for n in _names(w)}
    out = []
    for path in paths:
        for cfg in (Path(path) / "pyvenv.cfg", Path(path) / ".venv" / "pyvenv.cfg"):
            if any(n == w or n.startswith(w.rstrip("/") + "/") for n in _names(cfg) for w in places):
                continue
            try:
                lines = cfg.read_text().splitlines()
            except (OSError, ValueError):               # ValueError: not UTF-8
                continue
            for line in lines:
                key, _, value = line.partition("=")
                if key.strip() == "home" and value.strip():
                    # The installation's bin/, as written: uv names a version alias (a link to the installation)
                    # and the venv's interpreter links through it, so both names must exist inside.
                    home = Path(value.strip())
                    out.append(home.parent if home.name == "bin" else home)
    return out


def _interpreter() -> tuple[list[Path], list[Path], list[Path]]:
    """What the interpreter needs, without the packages installed beside it: (read-only paths, site directories to
    replace with an empty directory, directories to keep visible inside them). The installation's lib/ (the standard
    library and the shared libraries C extensions load), its ssl/ (a conda installation's certificates), the
    executable and the virtual environment's own files. Every site-packages directory under them is replaced by an
    empty one; the distributions a sandbox shows are bound back into it."""
    ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    shown, sites = [Path(sys.executable), Path(sys.executable).resolve(), *_venv_paths()], []
    prefixes = [Path(sys.base_prefix), Path(sys.prefix), Path(sys.executable).resolve().parent.parent]
    for prefix in dict.fromkeys(prefixes):
        for name in ("lib", "lib64", "ssl"):
            if (prefix / name).is_dir() and not (prefix / name).is_symlink():
                shown.append(prefix / name)
        for name in ("site-packages", "dist-packages"):
            if (prefix / "lib" / ver / name).is_dir():
                sites.append(prefix / "lib" / ver / name)
    return shown, list(dict.fromkeys(sites))


def _dist_sources(dist) -> list[Path]:
    """Where an editable install's code lives: the source directories its finder module maps, or the plain paths its
    .pth file adds."""
    out = []
    for f in dist.files or []:
        name = Path(f).name
        if name.endswith(".pth"):
            located = Path(dist.locate_file(f))
            try:
                lines = located.read_text().splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if line.startswith(("import ", "import\t")):
                    out += _editable_finder_paths(located.parent, line)
                elif line and not line.startswith("#"):
                    p = Path(line) if os.path.isabs(line) else (located.parent / line)
                    if p.is_dir():
                        out.append(p.resolve())
    return out


def _python_paths(packages: list[str], framework: bool = True) -> tuple[list[Path], list[Path]]:
    """(read-only paths, site directories shown empty): the interpreter, and the distributions to show: the
    framework's dependency closure when `framework`, plus `packages` and theirs ("*": every installed package)."""
    shown, sites = _interpreter()
    if "*" in packages:
        return shown + _installed_paths(), []
    roots = [*([DISTRIBUTION] if framework else []), *packages]
    dists = package_closure(roots) if roots else []
    home = Path.home().resolve()
    sources = [s for d in dists for s in _dist_sources(d)
               if s.resolve() != FRAMEWORK and not home.is_relative_to(s.resolve())]   # the checkout: PACKAGE alone
    return shown + distribution_paths(dists) + sources, sites


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
            from hermit.sandbox_relay import pipe
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


_COUNTERS: dict[tuple[str, str], "itertools.count"] = {}
_COUNTERS_LOCK = threading.Lock()


def _record_counter(record_dir: Path | None, role: str):
    """The numbering of a role's records in a directory, one per process: every Sandbox writing there (a view, a
    resumed run's new one) takes the next number, after the records already there."""
    key = (os.path.realpath(record_dir) if record_dir else "", role)
    with _COUNTERS_LOCK:
        if key not in _COUNTERS:
            taken = [int(m.group(1)) for f in (Path(record_dir).glob(f"{role}-*.json") if record_dir else [])
                     if (m := re.fullmatch(rf"{re.escape(role)}-(\d+)\.json", f.name))]
            _COUNTERS[key] = itertools.count(max(taken, default=0) + 1)
        return _COUNTERS[key]


class Bind(NamedTuple):
    """A path shown inside under a name of its own: `source` (outside) at `target` (inside)."""
    source: Path
    target: str
    writable: bool = False


LIMITS = {"memory_mb", "file_mb", "cpu_s"}
END = r"(?=$|/|[\s\"'`:;,)\]}>]|\.(?:\s|$))"      # where a path ends in running text


@dataclass
class Sandbox:
    enabled: bool = True
    network: str = "isolated"                            # isolated | open (config: no-network | allowlist | public)
    allow: list[str] = field(default_factory=list)       # hosts the proxy lets through
    writable: list[Path] = field(default_factory=list)   # read-write, at their own paths
    readable: list[Path] = field(default_factory=list)   # read-only, at their own paths
    binds: list[Bind] = field(default_factory=list)      # shown under names of their own (the evolver's layout)
    layout: tuple[Path, str] | None = None               # (outside, inside): the working directory's root, not bound
    proxy_url: str | None = None
    env_allow: list[str] | None = None                   # None: every variable passes; else only these (+ ALWAYS);
                                                         # a name may be a pattern (MWM_*)
    enforce: str = "required"                            # required | best_effort
    packages: list[str] = field(default_factory=list)    # Python distributions to show ("*": all)
    framework: bool = True                               # show the hermit package and its dependencies
    record_dir: Path | None = None                       # where each wrapped command leaves its record
    probe_absent: list[Path] = field(default_factory=list)   # paths the isolation check must find missing
    limits: dict = field(default_factory=dict)           # memory_mb, file_mb, cpu_s: per process, set before exec
    role: str = "learner"

    def __post_init__(self):
        self.network, _ = network_mode(self.network)
        if self.enforce not in ("required", "best_effort"):
            raise ValueError("sandbox.enforce must be required or best_effort")
        unknown = set(self.limits) - LIMITS
        if unknown:
            raise ValueError(f"sandbox limits: unknown keys {sorted(unknown)}; known: {sorted(LIMITS)}")
        self.binds = [Bind(Path(b[0]), str(b[1]), bool(b[2]) if len(b) > 2 else False) for b in self.binds]
        self._bridges: _Bridges | None = None
        self.last_record: Path | None = None             # the record of the latest wrapped command
        self._setup = threading.Lock()                   # concurrent first calls (an agent's concurrency) share one
        self._count = _record_counter(self.record_dir, self.role)

    def visible(self, command: list[str], writable=()) -> list[Path]:
        """Every read-only path inside the sandbox for this command at its own path (besides the system); `writable`:
        the call's own writable paths, besides the sandbox's (a venv in one names no installation to show)."""
        shown, _ = _python_paths(self.packages, self.framework)
        out = [*shown, *([PACKAGE] if self.framework else [RELAY]), *self.readable]
        out += _venv_bases(self.readable, [*self.writable, *writable])
        found = shutil.which(command[0]) if command else None
        if found and Path(found).resolve() != Path(sys.executable).resolve():
            out += program_paths(command[0])          # this Python is shown above, by its packages, not its venv
        bwrap = shutil.which("bwrap")
        if bwrap and not Path(bwrap).resolve().is_relative_to("/usr"):
            out.append(Path(bwrap).resolve())            # a nested sandbox needs the same binary
        return out

    def forwarded_ports(self, command: list[str], env: dict, forward: list[int] = ()) -> list[int]:
        """The ports to re-create inside: the explicit ones, every 127.0.0.1 URL in the command or the environment
        (the tool endpoint, the model proxy), and the allowlist proxy's."""
        ports = set(forward)
        for text in [*command, *env.values()]:
            ports.update(int(p) for p in LOCAL_URL.findall(str(text)))
        if self.proxy_url:
            ports.add(urlparse(self.proxy_url).port)
        return sorted(p for p in ports if p)

    def _all_binds(self, binds=()) -> list[Bind]:
        return [*self.binds, *[Bind(Path(b[0]), str(b[1]), bool(b[2]) if len(b) > 2 else False) for b in binds]]

    def _pairs(self, binds=()) -> list[tuple[str, str]]:
        """(outside, inside) spellings of every bound source and of the layout root, longest outside first. A source
        shown under several names (MWM's adapter at /w and, read-only, at /world-model) is spelled by the layout's
        root, else by its writable name, else by the first one given."""
        root = self.layout[1].rstrip("/") if self.layout else None
        chosen: dict[str, tuple] = {}
        for i, b in enumerate(self._all_binds(binds)):
            inside = b.target.rstrip("/") or "/"
            rank = (inside != root, not b.writable, i)
            for n in _names(b.source):
                if n not in chosen or rank < chosen[n][0]:
                    chosen[n] = (rank, inside)
        if self.layout:
            for n in _names(self.layout[0]):
                chosen.setdefault(n, ((False, False, -1), self.layout[1]))
        return sorted(((n, v[1]) for n, v in chosen.items()), key=lambda p: -len(p[0]))

    def _in_layout(self, path) -> bool:
        return any(n == q or n.startswith(q.rstrip("/") + "/") for n in _names(path) for q in _names(self.layout[0]))

    def translate(self, text: str, binds=()) -> str:
        """`text` with every path under a bound source (or under the layout root) spelled by its name inside, under
        each name the source has outside: as given and resolved. A path not bound under another name is the same
        inside."""
        if not self.enabled:
            return text
        for outside, inside in self._pairs(binds):
            text = re.sub(re.escape(outside) + END, lambda _m, i=inside: i, text)
        return text

    def relative(self, text: str, binds=()) -> str:
        """`text` with every host path under a bound source written as a learner is told it: relative to the layout's
        root (its working directory inside) when it lies there, else by its name inside. Text that names an inside
        path already is left as it is."""
        if not (self.enabled and self.layout):
            return self.translate(text, binds)
        root = self.layout[1].rstrip("/")

        def told(inside: str) -> str:
            if inside == root:
                return "."
            return inside[len(root) + 1:] if inside.startswith(root + "/") else inside
        for outside, inside in self._pairs(binds):
            if inside.rstrip("/") == root:                 # the root itself: what lies below it is relative to it
                text = re.sub(re.escape(outside) + "/", "", text)
            text = re.sub(re.escape(outside) + END, lambda _m, i=told(inside): i, text)
        return text

    def outside(self, path: str | Path, binds=()) -> Path:
        """A path a sandboxed session named (inside, or relative to the layout root) as it is outside."""
        text = str(path)
        if self.enabled and self.layout and not os.path.isabs(text):
            text = self.layout[1].rstrip("/") + "/" + text
        if self.enabled:
            for out, inside in sorted(((n, i) for n, i in self._pairs(binds)), key=lambda p: -len(p[1])):
                if text == inside or text.startswith(inside.rstrip("/") + "/"):
                    return Path(out + text[len(inside.rstrip("/")):])
        return Path(text)

    def wrap(self, command: list[str], *, writable: list[Path] = (), env: dict | None = None,
             home: Path | None = None, keep=(), forward: list[int] = (), readable: list[Path] = (),
             binds=(), chdir: Path | str | None = None) -> tuple[list[str], dict]:
        """The command to run and its environment. `home` becomes HOME (it must be among the writable paths).
        `keep` names variables the caller set for this command itself (an agent's provider settings): they pass
        whatever the allowlist says. `forward` names more ports to re-create inside an isolated network. `readable`
        and `writable` add paths at their own location for this command only; `binds` add paths under names of their
        own. Paths under a bound source in the command, the environment and `chdir` are translated to their names
        inside. Order inside: what is shown read-only, the writable paths, then the named binds."""
        env = dict(env if env is not None else os.environ)
        if not self.enabled:                             # unconfined: nothing is held back, the environment either
            return command, env
        if self.env_allow is not None:
            allowed = set(self.env_allow) | ALWAYS | set(keep)
            patterns = [a for a in allowed if any(c in a for c in "*?[")]
            env = {k: v for k, v in env.items() if k in allowed or any(fnmatch.fnmatchcase(k, p) for p in patterns)}
        if self.proxy_url:
            for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
                env[key] = self.proxy_url
            env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
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
        # A path given for this call at its own location but lying under the layout's root is shown where the
        # layout puts it, so its spelling inside (translated) is where it is.
        if self.layout:
            lay = [Bind(Path(p), self.translate(str(p), binds), w) for p, w in
                   [*((p, False) for p in readable), *((p, True) for p in [*writable, *([home] if home else [])])]
                   if self._in_layout(p) and self.translate(str(p), binds) != str(p)]
            moved = {str(b.source) for b in lay}
            readable = [p for p in readable if str(Path(p)) not in moved]
            writable = [p for p in writable if str(Path(p)) not in moved]
            binds = [*binds, *lay]
        all_binds = self._all_binds(binds)
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
        seen, ro, rw = set(), [], []

        def bind(paths, flag, out):
            for path in paths:
                if flag == "--bind":
                    Path(path).mkdir(parents=True, exist_ok=True)
                for name in _names(path):
                    if not os.path.exists(name) or name.startswith("/usr"):
                        continue
                    # A link under a directory bound already (uv's version alias of an interpreter kept inside a
                    # granted checkout) is there through that directory; binding it again fails on a link.
                    if os.path.islink(name) and any(name.startswith(b + "/") for b in (*ro, *rw)):
                        continue
                    if (flag, name) in seen:
                        continue
                    seen.add((flag, name))
                    out.append(name)
                    args.extend([flag, name, name])
        _, sites = _python_paths(self.packages, self.framework)
        visible = self.visible(command, writable)
        bind([p for p in visible if not any(_under(p, s) for s in sites)], "--ro-bind", ro)
        # The interpreter's site directories hold every installed package: each is replaced by an empty directory,
        # and the distributions this sandbox shows are bound back into it.
        for site in sites:
            for name in _names(site):
                if any(name == r or name.startswith(r.rstrip("/") + "/") for r in ro) and os.path.isdir(name):
                    args.extend(["--tmpfs", name])
        bind([p for p in visible if any(_under(p, s) for s in sites)], "--ro-bind", ro)
        for site in sites:
            for name in _names(site):
                if any(name == r or name.startswith(r.rstrip("/") + "/") for r in ro) and os.path.isdir(name):
                    args.extend(["--remount-ro", name])
        bind(_venv_bases(readable, [*self.writable, *writable]), "--ro-bind", ro)
        bind(readable, "--ro-bind", ro)
        bind(self.writable, "--bind", rw)
        bind([*writable, *([home] if home and str(Path(home)) not in {str(b.source) for b in all_binds} else [])],
             "--bind", rw)
        named = []
        for b in all_binds:
            if b.writable:
                b.source.mkdir(parents=True, exist_ok=True)
            if not os.path.exists(b.source):
                continue
            args.extend(["--bind" if b.writable else "--ro-bind", os.path.realpath(b.source), b.target])
            named.append({"source": str(b.source), "target": b.target, "writable": b.writable})
        ro = [p for p in ro if p not in rw]               # a path bound both ways is writable: the later bind wins
        command = [command[0], *[self.translate(str(a), binds) for a in command[1:]]]
        env = {k: self.translate(str(v), binds) for k, v in env.items()}
        if home:
            env["HOME"] = self.translate(_names(home)[0], binds)
        elif self.writable:
            env["HOME"] = _names(self.writable[0])[0]
        else:
            env["HOME"] = "/tmp"
        if chdir is not None:
            args += ["--chdir", self.translate(str(chdir), binds)]
        policy = {"enforced": True, "network": self.network, "read_only": ro, "read_write": rw, "binds": named,
                  "proxy": self.proxy_url, "allow": list(self.allow), "limits": dict(self.limits)}
        name = f"{self.role}-{next(self._count)}"
        deny = self._absent(ro + rw + [b["source"] for b in named])
        if self.network == "isolated" or deny or self.limits:
            # The check inside: on an isolated network the network and the paths, on an open one the paths only;
            # the limits are set on the command before it starts.
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
                       *[a for k, v in sorted(self.limits.items()) if v for a in ("--limit", f"{k}={int(v)}")],
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
            if any(p == b or str(b).startswith(p + "/") for b in bound):
                continue
            out.append(p)
        return out

    def _record(self, command: list[str], policy: dict, env: dict, name: str | None = None) -> Path | None:
        if self.record_dir is None:
            return None
        self.record_dir.mkdir(parents=True, exist_ok=True)
        name = name or f"{self.role}-{next(self._count)}"
        path = self.record_dir / f"{name}.json"
        self.last_record = path
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

    def view(self, *, binds=(), layout: tuple[Path, str] | None = None, readable=(), writable=(),
             framework: bool | None = None, role: str | None = None) -> "Sandbox":
        """This sandbox (its network, proxies, environment, enforcement, records, probe and limits) with a method's
        own view in place of the run's: `binds` and `layout` instead of its binds and layout, `readable` and
        `writable` instead of its own-path grants. Close it after use."""
        box = dataclasses.replace(self, binds=list(binds), layout=layout, readable=list(readable),
                                  writable=list(writable),
                                  framework=self.framework if framework is None else framework,
                                  role=role or self.role)
        box.model_proxy = getattr(self, "model_proxy", None)
        box.model_bindings = list(getattr(self, "model_bindings", ()))
        return box

    def close(self):
        if self._bridges is not None:
            self._bridges.close()
            self._bridges = None


def _under(path, parent) -> bool:
    """Whether `path` lies strictly below `parent`, under any of their names."""
    return any(n.startswith(q.rstrip("/") + "/") for n in _names(path) for q in _names(parent))


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
