"""The bubblewrap core of every sandbox the framework starts (docs/developer/sandbox-design.md). Every setting is the
run's.

An allowlist: inside exists only what is granted. Read-only: the system (/usr and the /lib, /bin links, a few /etc
files), the interpreter with its standard library, the environment the framework runs in (its site directories as
installed, never what their .pth files add beside them: an editable install's checkout), the distributions `packages`
names besides it, the hermit package itself (not the checkout it lives in) when `framework`, the started program's own
installation and the granted paths. Read-write: the granted writable paths. Under names of their own: the binds (the
evolver's layout). One thing is shown and then covered: what `hide` names (the run's sandbox.hide, a module or package
in the environment that only the judge may read) and, without `framework`, a hermit package installed in the
environment, each replaced by an empty file or directory.

network: isolated gives the process its own network namespace with loopback only. The only ways out are the forwarded
ports: the sandbox's own (`forward`: a model server or meter on the loopback), the ones the caller names (a tool
server), every 127.0.0.1 URL in the environment, and the allowlist proxy when `allow` names hosts (never a URL in the
command's arguments, where a prompt can carry the learner's text). Each is re-created inside by `sandbox_relay.py` and
bridged over a Unix socket, so a program that ignores the proxy variables has no route out. open: the host network. The
run config names these no-network (isolated, no proxy), allowlist (isolated, the proxy) and public (open).

enforce: required refuses to run when bubblewrap is missing; best_effort runs the command unconfined and records
that it did. `enabled: false` turns the sandbox off. env: `env_allow` lists the environment variables passed on
(plus ALWAYS); None passes all of them. limits: memory, file size and CPU time, set on the command by the relay.

Every wrapped command leaves a record in the run's `sandbox/` directory: what was bound, what was forwarded, which
environment variables passed, and the isolation check `sandbox_relay.py` ran inside before the command started. The
proxy writes one line per request to `sandbox/network.jsonl`.
"""
from __future__ import annotations

import base64
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
import importlib.metadata
import fnmatch
import glob
import itertools
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import socketserver
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
from typing import NamedTuple
from urllib.parse import unquote, urlparse
import urllib.request


# Variables the framework itself sets or a program cannot start without; passed whatever the allowlist says.
ALWAYS = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "PYTHONPATH", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "HA_CONTEXT",
          "HA_TOOL_URL", "HA_SANDBOX_HIDE"}
SYSTEM_ETC = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/alternatives", "/etc/ssl", "/etc/pki",
              "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/passwd", "/etc/group", "/etc/localtime",
              "/etc/crypto-policies", "/etc/os-release")
FRAMEWORK = Path(__file__).resolve().parents[1]          # the checkout; only its hermit/ package is shown
PACKAGE = Path(__file__).resolve().parent
DISTRIBUTION = "hermit-harness"                           # the installed distribution (pip name) of this package
RELAY = PACKAGE / "sandbox_relay.py"
ENV_HIDE = "HA_SANDBOX_HIDE"         # the run's sandbox.hide as paths: the sandboxes a child process starts cover them
# What CUDA and EGL read to find an NVIDIA GPU besides its device (measured on the driver's layout: without
# /sys/devices and /sys/module/nvidia EGL falls back to software rendering, without /sys/bus/pci CUDA finds no
# device); shown read-only with `gpu`
GPU_SYS = ("/sys/devices", "/sys/bus/pci", "/sys/module/nvidia", "/sys/module/nvidia_uvm")
# The driver's own devices: control (required), and when present unified memory (CUDA's; a graphics-only host may
# lack it), display modes and MIG capabilities
GPU_DEVICES = ("nvidiactl", "nvidia-uvm", "nvidia-modeset", "nvidia-caps")
DEV = "/dev"                                             # where they are on the host (inside: /dev)
LOCAL_ANY = re.compile(r"(https?)://(?:127\.0\.0\.1|localhost)(?::(\d+))?(?![\w.:-])")   # a loopback URL


def framework_env(extra: dict | None = None, *, no_bytecode: bool = False) -> dict:
    """The environment for a framework subprocess: this checkout first on PYTHONPATH, so a child imports this
    hermit, not another installed one. `no_bytecode` keeps an acting adapter free of bytecode caches."""
    env = {**os.environ, **(extra or {})}
    env["PYTHONPATH"] = str(FRAMEWORK) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if no_bytecode:
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def read_regular(path, limit: int | None = 1 << 20) -> str | None:
    """The text of a file a sandboxed process left (its report, its result), read on the host: only a regular file,
    never through a link (O_NOFOLLOW), never blocking on a pipe (O_NONBLOCK), at most `limit` bytes (None: all of it).
    None when it is missing or anything else."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        if limit is not None:
            return os.read(fd, limit).decode("utf-8", errors="replace")
        with os.fdopen(os.dup(fd), "rb") as f:            # the whole file (limit None)
            return f.read().decode("utf-8", errors="replace")
    finally:
        os.close(fd)


def owner_readable(root: Path) -> None:
    """`root` and what lies under it readable (files) and searchable (directories) by its owner, as a process that
    wrote it may have left them otherwise; links are not followed; one this user does not own is left as it is. Each
    directory is repaired before it is listed."""
    def repair(path):
        try:
            info = os.lstat(path)
            need = stat.S_IRWXU if stat.S_ISDIR(info.st_mode) else stat.S_IRUSR if stat.S_ISREG(info.st_mode) else 0
            if need and stat.S_IMODE(info.st_mode) & need != need:
                os.chmod(path, stat.S_IMODE(info.st_mode) | need)
        except OSError:
            pass
    repair(root)
    for directory, dirs, files in os.walk(root):       # top down: a directory is repaired before it is entered
        for name in [*dirs, *files]:
            repair(os.path.join(directory, name))


def sanitized_copy(source: Path, dest: Path, paths=None, max_bytes: int | None = None, exclude=()) -> list[str]:
    """Copy `paths` (relative to `source`; all of it when None) into `dest` through a tar archive extracted with
    tarfile's `data` filter: setuid and setgid bits cleared, device files, FIFOs and links that leave the archive
    refused. Returns what was skipped. The archive is a temporary file, not memory; `max_bytes` caps it, checked as
    each file is added."""
    skipped = []
    with tempfile.TemporaryFile() as buf:
        with tarfile.open(fileobj=buf, mode="w") as tar:
            def keep(info):
                if exclude and set(Path(info.name).parts) & set(exclude):
                    return None                                  # left out (`exclude`: names such as .git)
                if max_bytes is not None and buf.tell() + info.size > max_bytes:
                    raise ValueError(f"{source} holds more than {max_bytes} bytes")
                return info
            for rel in (paths if paths is not None else ["."]):
                item = Path(source) / rel
                if not os.path.lexists(item):
                    skipped.append(f"{rel}: missing")
                    continue
                # tar keeps a link that is the path's last part, but follows one on the way to it
                parts = Path(rel).parts[:-1]
                if any(os.path.islink(Path(source).joinpath(*parts[:i + 1])) for i in range(len(parts))):
                    skipped.append(f"{rel}: a link on its way")
                    continue
                if not os.path.islink(item):
                    owner_readable(item)                 # what the process that wrote it left unreadable
                tar.add(str(item), arcname=str(Path(rel)), recursive=True, filter=keep)
        buf.seek(0)
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=buf, mode="r") as tar:
            for member in tar.getmembers():
                try:
                    tar.extract(member, dest, filter="data")
                except tarfile.FilterError as exc:
                    skipped.append(f"{member.name}: {exc}")
    return skipped


def drop_unsafe(root) -> list[str]:
    """Remove from a directory a sandboxed session could write what would turn the host's later reads and writes
    there elsewhere: links that leave it, and anything not a regular file, directory or link (a pipe, a socket, a
    device). Nothing is followed. Returns what was removed, relative."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        return []
    real, removed = os.path.realpath(root), []
    for directory, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            path = os.path.join(directory, name)
            try:
                mode = os.lstat(path).st_mode
            except OSError:
                continue
            leaves = stat.S_ISLNK(mode) and not (os.path.realpath(path) + "/").startswith(real.rstrip("/") + "/")
            special = not (stat.S_ISLNK(mode) or stat.S_ISREG(mode) or stat.S_ISDIR(mode))
            if leaves or special:
                if not os.access(directory, os.W_OK | os.X_OK):   # left read-only by the session: the owner's again
                    os.chmod(directory, stat.S_IMODE(os.lstat(directory).st_mode) | stat.S_IRWXU)
                os.unlink(path)
                removed.append(os.path.relpath(path, root))
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(directory, d))]
    return removed


def model_access(specs) -> tuple[list[str], list[str], list[int]]:
    """What a sandbox needs to reach the models `specs` name (bindings or model specs, their plain form: base_url,
    messages_url, api_key_env), as Harbor gives an agent its model: (the key variables to pass, the provider hosts
    to allow, the loopback ports to forward: a local server or a meter in front of a provider)."""
    keys, hosts, ports = [], [], []
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        if spec.get("api_key_env"):
            keys.append(spec["api_key_env"])
        for field_name in ("base_url", "messages_url"):
            url = urlparse(spec.get(field_name) or "")
            if not url.hostname:
                continue
            if url.hostname in ("127.0.0.1", "localhost"):
                ports.append(url.port or (443 if url.scheme == "https" else 80))
            else:
                hosts.append(url.hostname)
    return list(dict.fromkeys(keys)), list(dict.fromkeys(hosts)), sorted(set(ports))


def translate_values(sandbox, value, binds=()):
    """`value` (a tool's result, a context) with every string in it translated as the sandbox shows paths: each string
    on its own, so nothing a serialization escapes (a non-ASCII directory name) is missed."""
    if isinstance(value, str):
        return sandbox.translate(value, binds)
    if isinstance(value, dict):
        return {(sandbox.translate(k, binds) if isinstance(k, str) else k): translate_values(sandbox, v, binds)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [translate_values(sandbox, v, binds) for v in value]
    if isinstance(value, Path):
        return sandbox.translate(str(value), binds)
    return value


def outside_values(sandbox, value, binds=()):
    """`value` (a tool's arguments) with every absolute path that lies under a name inside the sandbox (the layout
    root, a bind's target) spelled as it is outside: what a tool's result was translated to comes back as the host
    path the tool reads. Other strings are unchanged."""
    if isinstance(value, str):
        names = sandbox.inside_names(binds) if hasattr(sandbox, "inside_names") else []
        if any(value == n or value.startswith(n + "/") for n in names):
            return str(sandbox.outside(value, binds))
        return value                                   # anything else (a pattern such as /api//v1) as given
    if isinstance(value, dict):
        return {outside_values(sandbox, k, binds): outside_values(sandbox, v, binds) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [outside_values(sandbox, v, binds) for v in value]
    return value


def inside_tools(tools, sandbox, binds=()):
    """The tools as a sandboxed session calls them through a server: each result with the host paths it names spelled
    as the sandbox shows them (a run's log copied into the run's agent_logs/ is /w/agent-logs/...), and each argument
    that names a path inside spelled back as the host's, so a result handed on to another tool still resolves. The
    tools themselves are unchanged: called in this process, they keep answering with host paths. A dict or a
    sequence, as given."""
    import copy
    if sandbox is None or not getattr(sandbox, "enabled", False) or not hasattr(sandbox, "translate"):
        return tools

    def wrapped(tool):
        out = copy.copy(tool)
        fn = tool.fn
        out.fn = lambda **arguments: translate_values(sandbox, fn(**outside_values(sandbox, arguments, binds)), binds)
        return out
    if isinstance(tools, dict):
        return {name: wrapped(tool) for name, tool in tools.items()}
    return [wrapped(tool) for tool in tools]


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


def _runs_on_python(found: str) -> bool:
    """Whether a program is a script whose #! line names a Python interpreter."""
    try:
        with open(os.path.realpath(found), "rb") as f:
            line = f.readline(512)
    except OSError:
        return False
    words = line[2:].decode(errors="replace").split() if line.startswith(b"#!") else []
    if words and words[0].endswith("/env"):
        words = [w for w in words[1:] if not w.startswith("-")]
    return bool(words) and os.path.basename(words[0]).startswith("python")


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
    asked: dict[str, set] = {}                         # the extras each distribution was asked for so far
    todo = [(name, ()) for name in roots]
    while todo:
        name, extras = todo.pop()
        key = _canonical(name)
        first = key not in seen
        if first:
            try:
                seen[key] = importlib.metadata.distribution(name)
            except importlib.metadata.PackageNotFoundError:
                continue
            asked[key] = set()
        wanted = set(extras) - asked[key]
        if not first and not wanted:
            continue                                   # visited, and asked for nothing more
        asked[key] |= wanted
        for text in seen[key].requires or []:
            req = Requirement(text)
            if req.marker is None:
                if not first:
                    continue                           # followed on the first visit
            elif not any(req.marker.evaluate({"extra": e}) for e in ({"", *wanted} if first else wanted)) or \
                    (not first and req.marker.evaluate({"extra": ""})):
                continue
            todo.append((req.name, tuple(req.extras)))
    return list(seen.values())


def _namespace(directory: Path) -> bool:
    """Whether a package directory is a namespace other distributions share: no __init__.py (PEP 420), or one that
    only extends the path (pkgutil.extend_path, pkg_resources.declare_namespace)."""
    init = directory / "__init__.py"
    if not init.is_file():
        return True
    try:
        text = init.read_text(errors="replace")[:4096]
    except OSError:
        return False
    return "extend_path" in text or "declare_namespace" in text


def distribution_paths(dists) -> list[Path]:
    """The entries the given distributions installed: their top-level packages, modules and .dist-info; below a
    namespace shared with others (google/, opencensus/) the first regular package on each file's path (or the file),
    with the namespaces' own __init__.py, so a namespace sibling of another distribution is not shown."""
    out = set()
    for dist in dists:
        for f in dist.files or []:
            parts = Path(f).parts
            if not parts or parts[0] == "..":
                continue
            root = Path(dist.locate_file(""))
            top = root / parts[0]
            if len(parts) == 1 or parts[0].endswith((".dist-info", ".egg-info")) or not top.is_dir() \
                    or not _namespace(top):
                out.add(top)
                continue
            for i in range(1, len(parts)):
                candidate = root.joinpath(*parts[:i + 1])
                if (candidate.parent / "__init__.py").is_file():
                    out.add(candidate.parent / "__init__.py")          # each namespace's own initializer
                if i == len(parts) - 1 or (candidate.is_dir() and not _namespace(candidate)):
                    out.add(candidate)
                    break
    return sorted(out)


def _names(path) -> list[str]:
    """The names a path is bound under inside: as given (absolute, symlinks kept) and resolved, when they differ.
    A program that was told the given name finds it (a venv under a linked ~/.cache looks for its site-packages by
    the venv's own path), and one that resolved it first finds it too."""
    given = os.path.abspath(os.path.expanduser(str(path)))
    real = os.path.realpath(given)
    return [given] if real == given else [given, real]


def _granted_venv(found: str, granted) -> bool:
    """Whether `found` is a virtual environment's interpreter (a pyvenv.cfg beside it or one directory up) and the
    environment lies in a place `granted` shows at its own path, so that it exists inside under the name it is run
    by. Its pyvenv.cfg is not read: the installation it was made from is shown, as for any program, by following the
    interpreter's links (program_paths), never by a `home` a granted place could name."""
    where = os.path.dirname(os.path.abspath(found))
    venv = next((d for d in (where, os.path.dirname(where)) if os.path.isfile(os.path.join(d, "pyvenv.cfg"))), None)
    if venv is None:
        return False
    places = {n.rstrip("/") for g in granted for n in _names(g)}
    return any(n == w or n.startswith(w + "/") for n in _names(venv) for w in places)


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


def _interpreter() -> tuple[list[Path], list[Path]]:
    """What the interpreter needs, without the packages installed beside it: (read-only paths, site directories to
    replace with an empty directory). The installation's lib/ (the standard library and the shared libraries C
    extensions load), its ssl/ (a conda installation's certificates), the executable and the virtual environment's
    own files. Every site-packages directory under them, and every system Python's under /usr, is replaced by an empty
    one; the distributions a sandbox shows are bound back into it."""
    real = Path(sys.executable).resolve()
    # Its other names beside it (python3 next to python): PATH inside leads with this directory.
    siblings = [p for p in Path(sys.executable).parent.glob("python*") if p.resolve() == real]
    shown, sites = [Path(sys.executable), real, *siblings, *_venv_paths()], []
    prefixes = [Path(sys.base_prefix), Path(sys.prefix), Path(sys.executable).resolve().parent.parent]
    for prefix in dict.fromkeys(prefixes):
        for name in ("lib", "lib64", "ssl"):
            if (prefix / name).is_dir() and not (prefix / name).is_symlink():
                shown.append(prefix / name)
        # every Python version's under it (lib/ is shown whole: a conda prefix may hold python3.11/ beside 3.12/)
        sites += [Path(p) for p in sorted(glob.glob(str(prefix / "lib*" / "python3*" / "*-packages")))
                  if p.endswith(("/site-packages", "/dist-packages")) and os.path.isdir(p)]
    # And the interpreter's own list (a Debian system Python's /usr/lib/python3/dist-packages, /usr/local/...), and
    # every system Python's under /usr, which is shown whole whatever interpreter runs (a conda one, say).
    import site
    sites += [Path(p) for p in site.getsitepackages() if os.path.isdir(p)]
    sites += [Path(p) for pattern in ("/usr/lib*/python3*/*-packages", "/usr/local/lib*/python3*/*-packages")
              for p in sorted(glob.glob(pattern)) if p.endswith(("/site-packages", "/dist-packages"))
              and os.path.isdir(p)]
    return shown, list(dict.fromkeys(sites))


def _python_code(directory: Path, top: bool = False) -> list[Path]:
    """The Python code in `directory`, never the data beside it: a package (with __init__.py) whole, a module file,
    and below a directory without __init__.py (a namespace package, PEP 420) its own packages and modules only; at
    the top (`top`), setup.py left out."""
    import importlib.machinery as m
    out = []
    for c in sorted(directory.iterdir()):
        if c.name.startswith(".") or c.is_symlink():
            continue
        if c.is_dir():
            out += [c] if (c / "__init__.py").is_file() else _python_code(c)
        elif (c.suffix == ".py" and not (top and c.name == "setup.py")) or \
                c.name.endswith(tuple(m.EXTENSION_SUFFIXES)):      # a native module too
            out.append(c)
    return out


def _dist_sources(dist) -> list[Path]:
    """Where an editable install's code lives: the source directories its finder module maps, or, below a plain
    path its .pth file adds (often the whole checkout, with data and runs beside the code), the distribution's own
    top-level packages and modules (top_level.txt, else its name; without either there, as a backend that writes no
    top_level.txt leaves it (Hatchling), the Python code in that path: packages, modules, a namespace package's
    own code, never data beside it) and nothing else."""
    try:
        listed = [n.strip() for n in (dist.read_text("top_level.txt") or "").splitlines() if n.strip()]
    except OSError:
        listed = []
    tops = listed or [re.sub(r"[-.]+", "_", dist.metadata["Name"]).lower()]
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
                        for name in tops:
                            out += [c.resolve() for c in _entries(p, name)]   # a native module too
                        if not listed and not any(_entries(p, name) for name in tops):
                            out += [c.resolve() for c in _python_code(p, top=True)]
    return out


def _environment_sites() -> list[Path]:
    """The site directories of the environment the framework runs in: the ones on its sys.path (a virtual
    environment's own; its base installation's only when it includes them; the user's when enabled)."""
    import site
    on_path = {os.path.realpath(p) for p in sys.path if p}
    found = [*site.getsitepackages(), site.getusersitepackages()]
    return [Path(p) for p in dict.fromkeys(found) if os.path.isdir(p) and os.path.realpath(p) in on_path]


def _python_paths(packages: list[str]) -> tuple[list[Path], list[Path]]:
    """(read-only paths, site directories shown empty): the interpreter, the environment's site directories as
    installed, and the distributions `packages` names (with their dependencies) that lie outside them: an editable
    install's package, one on PYTHONPATH ("*": also every path the environment's .pth files add). Every other
    interpreter's site directory (a venv's base installation, a system Python under /usr) is shown empty."""
    shown, sites = _interpreter()
    environment = _environment_sites()
    own = {os.path.realpath(p) for p in environment}
    sites = [p for p in sites if os.path.realpath(p) not in own]
    dists = package_closure([p for p in packages if p != "*"])
    home = Path.home().resolve()
    sources = [s for d in dists for s in _dist_sources(d)
               if s.resolve() != FRAMEWORK and not home.is_relative_to(s.resolve())]   # the checkout: PACKAGE alone
    extra = [*distribution_paths(dists), *sources, *(_installed_paths() if "*" in packages else [])]
    extra = [p for p in extra if not any(p == e or _under(p, e) for e in environment)]
    return shown + environment + extra, sites


def _copies(name: str) -> list[Path]:
    """Every copy of a top-level module or package on this interpreter's path: a package's directory, a module's
    file, an extension module."""
    out = []
    for entry in sys.path:
        if entry and os.path.isdir(entry):
            out += _entries(Path(entry), name)
    return out


def _in_archives(name: str) -> bool:
    """Whether a zip or wheel on this interpreter's path holds the top-level module or package `name`."""
    import zipfile
    for entry in sys.path:
        if entry and os.path.isfile(entry) and zipfile.is_zipfile(entry):
            try:
                with zipfile.ZipFile(entry) as z:
                    if any(n == f"{name}.py" or n.startswith(f"{name}/") for n in z.namelist()):
                        return True
            except (OSError, zipfile.BadZipFile):
                continue
    return False


def _entries(directory: Path, name: str) -> list[Path]:
    """`name` as a package directory, a source file or an extension module (every suffix this interpreter loads)
    in `directory`."""
    import importlib.machinery as m
    return [c for c in [directory / name, *(directory / f"{name}{s}" for s in
                                            [*m.SOURCE_SUFFIXES, *m.BYTECODE_SUFFIXES, *m.EXTENSION_SUFFIXES])]
            if c.exists()]


def hidden_paths(names) -> list[Path]:
    """sandbox.hide: each module or package name (`pkg.scoring`) -> every installed copy of it on the framework's
    path (a package's directory, a module's file), found without importing anything: no package's __init__ runs. One
    inside another hidden directory is left out (that directory covers it)."""
    import importlib.util
    out = []
    for name in names:
        if not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", str(name)):
            raise ValueError(f"sandbox.hide names {name!r}: not a module or package name (pkg.module)")
        parts = str(name).split(".")
        try:
            spec = importlib.util.find_spec(parts[0])
        except (ImportError, ValueError):
            spec = None
        found = []
        if spec is not None:          # an editable install's finder maps it to where no sys.path entry leads
            found = [Path(p) for p in spec.submodule_search_locations or []] or \
                ([Path(spec.origin)] if spec.origin and os.path.isfile(spec.origin) else [])
        found += _copies(parts[0])
        if any(not os.path.exists(p) for p in found) or _in_archives(parts[0]):   # a zip or wheel: nothing to cover
            raise ValueError(f"sandbox.hide names {name}, which is imported from an archive; it cannot be covered")
        for part in parts[1:]:
            found = [c for d in found if d.is_dir() for c in _entries(d, part)]
        if not found:
            raise ValueError(f"sandbox.hide names {name}, which is not installed in the framework's environment")
        out += [p.resolve() for p in found]
    out = list(dict.fromkeys(out))
    return [p for p in out if not any(q != p and q.is_dir() and p.is_relative_to(q) for q in out)]


def _covered(paths) -> list[Path]:
    """What covering `paths` covers: each, and beside a module file its bytecode (the package's whole __pycache__,
    where a cache the judge's import writes later lands too, and a legacy .pyc beside it)."""
    out = []
    for p in map(Path, paths):
        out.append(p)
        if p.suffix == ".py":
            out += [c for c in (p.parent / "__pycache__", p.with_suffix(".pyc")) if c.exists()]
    return list(dict.fromkeys(out))


def set_hidden(paths) -> None:
    """The run's sandbox.hide, for every sandbox this process and the processes it starts make (ENV_HIDE). A hidden
    module's __pycache__ is made now, so that a cache the judge's import writes later lands in a covered directory."""
    for p in map(Path, paths):
        if p.suffix == ".py":
            try:
                (p.parent / "__pycache__").mkdir(exist_ok=True)
            except OSError:
                pass                                   # an installation not writable: no cache is written there
    if paths:
        os.environ[ENV_HIDE] = os.pathsep.join(str(p) for p in paths)
    else:
        os.environ.pop(ENV_HIDE, None)


def _env_hidden() -> list[Path]:
    return [Path(p) for p in os.environ.get(ENV_HIDE, "").split(os.pathsep) if p]


_EMPTY: list[str] = []


def _empty_file() -> str:
    """One empty read-only file of this process, bound over each hidden file (/dev/null cannot be opened where the
    mount is nodev)."""
    if not _EMPTY:
        fd, path = tempfile.mkstemp(prefix="ha-empty-")
        os.close(fd)
        os.chmod(path, 0o444)
        import atexit
        atexit.register(lambda: os.path.exists(path) and os.unlink(path))
        _EMPTY.append(path)
    return _EMPTY[0]


def _editable_finder_paths(directory: Path, line: str) -> list[Path]:
    """The sources of a setuptools editable install that imports a finder (`import __editable___x_finder;
    ...install()`): the package directories and module files its MAPPING names."""
    import ast
    out = []
    for module in re.findall(r"import\s+(__editable__[\w.]*finder)", line):
        try:
            tree = ast.parse((directory / f"{module}.py").read_text())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            # MAPPING = {...} or, as current setuptools writes it, MAPPING: dict[str, str] = {...}
            targets = node.targets if isinstance(node, ast.Assign) else \
                [node.target] if isinstance(node, ast.AnnAssign) and node.value is not None else []
            if not any(getattr(t, "id", None) == "MAPPING" for t in targets):
                continue
            try:
                mapping = ast.literal_eval(node.value)
            except ValueError:
                continue
            for value in mapping.values():
                p = Path(value)
                # a package directory, or a module file itself (never the directory around it: the checkout)
                if not p.exists():                     # a module mapped without its suffix: .py or native
                    p = next(iter(_entries(p.parent, p.name)), p)
                if p.exists():
                    out.append(p)
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
        self._reports: dict[str, socket.socket] = {}         # report listeners still waiting
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
        with self._lock:
            self._reports[str(path)] = server

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
                with self._lock:
                    self._reports.pop(str(path), None)
        threading.Thread(target=wait, daemon=True).start()
        return path

    def cancel(self, path) -> None:
        """A report that will not come (the command was never started): its listener closed."""
        with self._lock:
            server = self._reports.pop(str(path), None)
        if server is not None:
            try:
                server.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            server.close()

    def close(self):
        with self._lock:
            for server in [*self._ports.values(), *self._reports.values()]:
                try:
                    server.shutdown(socket.SHUT_RDWR)     # a thread blocked in accept() returns
                except OSError:
                    pass
                server.close()
            self._ports.clear()
            self._reports.clear()
        shutil.rmtree(self.dir, ignore_errors=True)


_COUNTERS: dict[tuple[str, str], "itertools.count"] = {}
_COUNTERS_LOCK = threading.Lock()


def _scrubbed(value):
    """`value` with every model key's value replaced (hermit.redact), for the records the sandbox writes; as given
    where that module is not shown (a learner's view that grants this file alone writes no record)."""
    try:
        from hermit.redact import redact_value
    except ImportError:
        return value
    return redact_value(value)


def gpu_list(value) -> list[int]:
    """`gpu` as a list of GPU numbers (the N of /dev/nvidia<N>), refused when it is anything else."""
    gpus = [] if value is None else value
    if not isinstance(gpus, (list, tuple)) or \
            not all(isinstance(g, int) and not isinstance(g, bool) and g >= 0 for g in gpus):
        raise ValueError(f"gpu: a list of NVIDIA GPU numbers (the N of /dev/nvidia<N>), not {value!r}")
    return sorted(set(gpus))


def gpu_args(gpus: list[int]) -> list[str]:
    """bwrap arguments that show these NVIDIA GPUs and nothing of the others: their /dev/nvidia<N>, the driver's
    devices, and read-only what CUDA and EGL read to find them (GPU_SYS)."""
    if not gpus:
        return []
    names = [*GPU_DEVICES, *(f"nvidia{g}" for g in gpus)]
    missing = [f"/dev/{n}" for n in [names[0], *names[len(GPU_DEVICES):]] if not os.path.exists(os.path.join(DEV, n))]
    if missing:
        raise RuntimeError(f"gpu {gpus}: {', '.join(missing)} not on this machine (an NVIDIA GPU and its driver are "
                           "needed; `nvidia-smi -q` shows each GPU's Minor Number, the N of /dev/nvidia<N>)")
    args = []
    for name in names:
        if os.path.exists(os.path.join(DEV, name)):
            args += ["--dev-bind", os.path.join(DEV, name), f"/dev/{name}"]
    for path in GPU_SYS:
        if os.path.exists(path):
            args += ["--ro-bind", path, path]
    return args


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
PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
START = r"(?<![\w.~/-])"                          # where a path starts: not inside a longer one (/mirror/data)
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
    packages: list[str] = field(default_factory=list)    # distributions to show besides the environment's
                                                         # ("*": what its .pth files add too)
    framework: bool = True                               # show the hermit package
    hide: list[Path] = field(default_factory=_env_hidden)    # covered: what only the judge may read (sandbox.hide)
    record_dir: Path | None = None                       # where each wrapped command leaves its record
    probe_absent: list[Path] = field(default_factory=list)   # paths the isolation check must find missing
    limits: dict = field(default_factory=dict)           # memory_mb, file_mb, cpu_s: per process, set before exec
    role: str = "learner"
    forward: list[int] = field(default_factory=list)     # loopback ports every command reaches (a model meter)
    sockets: dict = field(default_factory=dict)          # isolated network: {port inside: Unix socket outside}
    gpu: list[int] = field(default_factory=list)         # NVIDIA GPUs shown, by /dev/nvidia<N>; inside they count
                                                         # from 0 (CUDA, EGL); none: no GPU (EGL: Mesa's software)

    def __post_init__(self):
        self.network, _ = network_mode(self.network)
        if self.enforce not in ("required", "best_effort"):
            raise ValueError("sandbox.enforce must be required or best_effort")
        unknown = set(self.limits) - LIMITS
        if unknown:
            raise ValueError(f"sandbox limits: unknown keys {sorted(unknown)}; known: {sorted(LIMITS)}")
        self.gpu = gpu_list(self.gpu)
        self.binds = [Bind(Path(b[0]), str(b[1]), bool(b[2]) if len(b) > 2 else False) for b in self.binds]
        self._bridges: _Bridges | None = None
        self.last_record: Path | None = None             # the record of the latest wrapped command
        self._setup = threading.Lock()                   # concurrent first calls (an agent's concurrency) share one
        self._count = _record_counter(self.record_dir, self.role)

    def visible(self, command: list[str], writable=()) -> list[Path]:
        """Every read-only path inside the sandbox for this command at its own path (besides the system); `writable`:
        the call's own writable paths, besides the sandbox's (a venv in one names no installation to show). The
        installation a virtual environment was made from is read only from the sandbox's own grants (the run's
        config), never from a path given for one call (an adapter could name any path as its venv's home)."""
        shown, _ = _python_paths(self.packages)
        out = [*shown, *([PACKAGE] if self.framework else [RELAY]), *self.readable]
        out += _venv_bases(self.readable, [*self.writable, *writable])
        found = shutil.which(command[0]) if command else None
        if found and Path(found).resolve() != Path(sys.executable).resolve():
            out += program_paths(command[0])          # this Python is shown above, by its packages, not its venv
        bwrap = shutil.which("bwrap")
        if bwrap and not Path(bwrap).resolve().is_relative_to("/usr"):
            out.append(Path(bwrap).resolve())            # a nested sandbox needs the same binary
        return out

    def forwarded_ports(self, command: list[str], env: dict, forward: list[int] = (),
                        from_env: bool = True) -> list[int]:
        """The ports to re-create inside: the sandbox's own (`forward`: a model server or meter on the loopback), the
        explicit ones (a tool server), every 127.0.0.1 URL in the environment (which the caller sets) and the
        allowlist proxy's. Never one read from the command's arguments: a prompt passed there can carry text the
        learner wrote, and a URL in it would open a port."""
        ports = {*self.forward, *forward}
        for text in env.values() if from_env else ():   # a URL without a port: its scheme's (80, 443)
            ports.update(int(port) if port else (443 if scheme == "https" else 80)
                         for scheme, port in LOCAL_ANY.findall(str(text)))
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

    def confines(self) -> bool:
        """Whether a wrapped command runs confined: the sandbox is on and bubblewrap is there (best_effort without it
        runs the command unconfined, at the host's paths)."""
        return self.enabled and bool(shutil.which("bwrap"))

    def translate(self, text: str, binds=()) -> str:
        """`text` with every path under a bound source (or under the layout root) spelled by its name inside, under
        each name the source has outside: as given and resolved. A path not bound under another name is the same
        inside. Unchanged when nothing is confined."""
        if not self.confines():
            return text
        pairs = dict(self._pairs(binds))
        if not pairs:
            return text
        # One pass, the longest source first: a name inside that is another source's path is not translated again.
        pattern = START + "(" + "|".join(map(re.escape, pairs)) + ")" + END
        return re.sub(pattern, lambda m: pairs[m.group(1)], text)

    def relative(self, text: str, binds=()) -> str:
        """`text` with every host path under a bound source written as a learner is told it: relative to the layout's
        root (its working directory inside) when it lies there, else by its name inside. Text that names an inside
        path already is left as it is."""
        if not (self.confines() and self.layout):
            return self.translate(text, binds)
        root = self.layout[1].rstrip("/")

        def told(inside: str) -> str:
            if inside == root:
                return "."
            return inside[len(root) + 1:] if inside.startswith(root + "/") else inside
        pairs = dict(self._pairs(binds))
        if not pairs:
            return text

        def spelled(m) -> str:
            inside = pairs[m.group(1)]
            if m.group(2):                                 # a path below it
                return "" if inside.rstrip("/") == root else told(inside) + "/"
            return told(inside)
        # One pass, as translate: the root itself drops out of what lies below it (relative to it).
        pattern = START + "(" + "|".join(map(re.escape, pairs)) + ")(?:(/)|" + END + ")"
        return re.sub(pattern, spelled, text)

    def inside_names(self, binds=()) -> list[str]:
        """The names a confined command sees bound places under (the layout root, the binds' targets) that differ
        from their places outside; none when nothing is confined."""
        if not self.confines():
            return []
        names = [(b.target.rstrip("/") or "/") for b in self._all_binds(binds) if b.target != str(b.source)]
        if self.layout:
            names.append(self.layout[1].rstrip("/") or "/")
        return sorted(set(names), key=len, reverse=True)

    def outside(self, path: str | Path, binds=()) -> Path:
        """A path a sandboxed session named (inside, or relative to the layout root) as it is outside: normalized
        first (no `..` crosses a bind), then under whichever name it was shown (a source bound under two names is
        found under both)."""
        text = str(path)
        if not self.confines():                       # the session ran at the host's paths
            return Path(text) if os.path.isabs(text) or not self.layout else Path(self.layout[0]) / text
        if self.layout and not os.path.isabs(text):
            text = self.layout[1].rstrip("/") + "/" + text
        text = os.path.normpath(text)
        names = [(str(b.source), b.target.rstrip("/") or "/") for b in self._all_binds(binds)]
        if self.layout:
            names.append((str(self.layout[0]), self.layout[1].rstrip("/")))
        for out, inside in sorted(names, key=lambda p: -len(p[1])):
            if text == inside or text.startswith(inside.rstrip("/") + "/"):
                return Path(out + text[len(inside.rstrip("/")):])
        return Path(text)

    def wrap(self, command: list[str], *, writable: list[Path] = (), env: dict | None = None,
             home: Path | None = None, keep=(), forward: list[int] = (), readable: list[Path] = (),
             binds=(), chdir: Path | str | None = None, interpreters=(), env_ports: bool = True
             ) -> tuple[list[str], dict]:
        """The command to run and its environment. `home` becomes HOME (it must be among the writable paths).
        `keep` names variables the caller set for this command itself (an agent's provider settings): they pass
        whatever the allowlist says. `forward` names more ports to re-create inside an isolated network. `readable`
        and `writable` add paths at their own location for this command only; `binds` add paths under names of their
        own; `interpreters` are virtual environments the caller trusts (the framework's own), whose base
        installations are shown too (never one read from `readable`, which may be the learner's
        work). Paths under a bound source in the command, the environment and `chdir` are translated to their names
        inside. Order inside: what is shown read-only, the writable paths, then the named binds."""
        env = dict(env if env is not None else os.environ)
        if not self.enabled:                             # unconfined: nothing is held back, the environment either
            return command, env
        if self.env_allow is not None:
            allowed = set(self.env_allow) | ALWAYS | set(keep)
            patterns = [a for a in allowed if any(c in a for c in "*?[")]
            env = {k: v for k, v in env.items() if k in allowed or any(fnmatch.fnmatchcase(k, p) for p in patterns)}
        if self.proxy_url:
            for key in PROXY_VARS:
                env[key] = self.proxy_url
            env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
        elif self.network == "isolated":
            # No way out but the forwarded ports: an outer sandbox's proxy variables (a nested sandbox inherits them)
            # would send even a loopback request to a proxy this one cannot use.
            for key in (*PROXY_VARS, "ALL_PROXY", "all_proxy"):
                env.pop(key, None)
        if not shutil.which("bwrap"):
            if self.enforce == "required":
                raise RuntimeError("sandbox.enforce is required but bubblewrap (bwrap) is not installed; install it, "
                                   "or set sandbox.enforce: best_effort to run unconfined and record that")
            self._record(command, {"enforced": False, "reason": "bubblewrap is not installed"}, env)
            return command, env
        if "PATH" in env:
            # `python3` inside is this environment's: when another interpreter comes first on PATH (an unactivated
            # venv), whose packages are emptied below, this one's directory leads it; otherwise its order is kept
            # (a /usr/local/bin before /usr/bin keeps its tools).
            own, real = os.path.dirname(sys.executable), os.path.realpath(sys.executable)

            def ours(first: str) -> bool:      # a venv's python3 is its own bin/'s, not the base it links to
                where = os.path.dirname(first)
                if sys.prefix != sys.base_prefix:
                    return os.path.realpath(where) == os.path.realpath(own)
                in_venv = any(os.path.exists(os.path.join(d, "pyvenv.cfg")) for d in (where, os.path.dirname(where)))
                return not in_venv and os.path.realpath(first) == real
            if any(first and not ours(first)
                   for first in (shutil.which(name, path=env["PATH"]) for name in ("python3", "python"))):
                env["PATH"] = os.pathsep.join([own, *(p for p in env["PATH"].split(os.pathsep) if p and p != own)])
        found = shutil.which(command[0]) if command else None
        if found and _granted_venv(found, [*self.readable, *readable, *self.writable, *writable, *interpreters]):
            # a granted virtual environment's interpreter, run by its own name: its real file is the base
            # installation's, which, run as itself, finds no pyvenv.cfg and so not the environment's packages
            command = [os.path.abspath(found), *command[1:]]
        elif found:    # run the program by its real path: PATH entries outside the sandbox do not exist inside
            real = Path(found).resolve()     # ... except this Python, which must keep its environment's path
            command = [sys.executable if real == Path(sys.executable).resolve() else str(real), *command[1:]]
        # A path given for this call at its own location but lying under the layout's root is shown where the
        # layout puts it, so its spelling inside (translated) is where it is.
        if self.layout:
            lay = [Bind(Path(p), self.translate(str(p), binds), w) for p, w in
                   [*((p, False) for p in readable), *((p, True) for p in [*writable, *([home] if home else [])])]
                   if self._in_layout(p) and self.translate(str(p), binds) != str(p)]
            lay = list({(str(b.source), b.target): b for b in lay}.values())    # the home is often writable too
            moved = {str(b.source) for b in lay}
            readable = [p for p in readable if str(Path(p)) not in moved]
            writable = [p for p in writable if str(Path(p)) not in moved]
            binds = [*binds, *lay]
        all_binds = self._all_binds(binds)
        # A process namespace of its own: the host's processes (their environment, their open files, their root
        # through /proc/<pid>/root) are not there to read or signal.
        # bwrap by its path: a caller's PATH may lack it
        args = [shutil.which("bwrap") or "bwrap", "--die-with-parent", "--new-session", "--unshare-pid",
                "--dev", "/dev", "--proc", "/proc",
                "--tmpfs", "/tmp",
                "--ro-bind", "/usr", "/usr", *gpu_args(self.gpu)]
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

        def bind(paths, flag, out, system=False):
            for path in paths:
                if flag == "--bind" and not os.path.lexists(path):     # a writable file stays a file
                    Path(path).mkdir(parents=True, exist_ok=True)
                for name in _names(path):
                    if not os.path.exists(name) or (name.startswith("/usr") and not system):
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
        _, sites = _python_paths(self.packages)
        # The installations a declared virtual environment was made from are shown for their interpreter, not for
        # what is installed in them: their site directories are emptied too (the environment's own stay).
        own = {os.path.realpath(p) for p in _environment_sites()}      # a venv made from this installation: kept
        trusted = _venv_bases(interpreters, [*self.writable, *writable])   # shown before the masking below
        found = shutil.which(command[0]) if command else None
        # A CLI's own installation (~/.local): its Python packages masked too, unless the CLI runs on Python and
        # they are its own (a pipx or venv install).
        program = program_paths(command[0]) if found and Path(found).resolve() != Path(sys.executable).resolve() \
            and not _runs_on_python(found) else []
        mine = [os.path.realpath(i) for i in interpreters]      # a trusted interpreter's own packages stay
        for base in [*_venv_bases(self.readable, [*self.writable, *writable]), *trusted,
                     *(p for p in program if Path(p).is_dir())]:
            sites += [Path(p) for p in sorted(glob.glob(str(base / "lib*" / "python3*" / "*-packages")))
                      if p.endswith(("/site-packages", "/dist-packages")) and os.path.realpath(p) not in own
                      and not any(os.path.realpath(p).startswith(m.rstrip("/") + "/") for m in mine)]
        sites = list(dict.fromkeys(sites))
        visible = self.visible(command, writable)
        bind([p for p in [*visible, *interpreters, *trusted] if not any(_under(p, s) for s in sites)], "--ro-bind", ro)
        # The interpreter's site directories hold every installed package: each is replaced by an empty directory,
        # and the distributions this sandbox shows are bound back into it.
        # A site directory is shown when it lies in a bound path or under /usr (bound whole above: a system Python).
        def shown(name):
            return name.startswith("/usr/") or any(name == r or name.startswith(r.rstrip("/") + "/") for r in ro)
        for site in sites:
            for name in _names(site):
                if shown(name) and os.path.isdir(name):
                    args.extend(["--tmpfs", name])
        bind([p for p in visible if any(_under(p, s) for s in sites)], "--ro-bind", ro, system=True)
        for site in sites:
            for name in _names(site):
                if shown(name) and os.path.isdir(name):
                    args.extend(["--remount-ro", name])
        bind(readable, "--ro-bind", ro)
        bind(self.writable, "--bind", rw)
        bind([*writable, *([home] if home and str(Path(home)) not in {str(b.source) for b in all_binds} else [])],
             "--bind", rw)
        named = []
        for b in all_binds:
            if b.writable and not os.path.lexists(b.source):     # a file bound writable stays a file
                b.source.mkdir(parents=True, exist_ok=True)
            if not os.path.exists(b.source):
                continue
            args.extend(["--bind" if b.writable else "--ro-bind", os.path.realpath(b.source), b.target])
            named.append({"source": str(b.source), "target": b.target, "writable": b.writable})
        # What only the judge may read, and without the framework every hermit package the environment shows: covered
        # under every name it is shown by (its own; below a bound place, a linked path or a bind's target; and for
        # sandbox.hide a bound place itself when it is, or lies in, what is hidden: a grant inside it; the framework's
        # own grants, the relay and a method's helpers, stay), a directory by an empty one, a file by an empty file.
        places = [(n, os.path.realpath(n)) for n in [*ro, *rw]] + \
            [(b["target"], os.path.realpath(b["source"])) for b in named]
        hidden, covers = [], []
        framework = [] if self.framework else [PACKAGE, *_copies("hermit")]
        framework_names = {n for p in framework for n in _names(p)}
        for path in _covered([*self.hide, *framework]):
            real = os.path.realpath(path)
            if not os.path.lexists(real):
                continue
            covers += [(name, os.path.isdir(real)) for name in _names(path)]
            for inside, source in places:
                if real.startswith(source.rstrip("/") + "/"):
                    covers.append((inside + real[len(source):], os.path.isdir(real)))
                elif (source == real or source.startswith(real + "/")) and path not in framework:
                    covers.append((inside, os.path.isdir(source)))      # a grant of what sandbox.hide names
        covers = [(n, d) for n, d in dict.fromkeys(covers)
                  if shown(n) or any(n == w or n.startswith(w.rstrip("/") + "/") for w in rw)
                  or any(n == b["target"] or n.startswith(b["target"].rstrip("/") + "/") for b in named)]
        covers.sort(key=lambda c: c[0] not in framework_names)   # the framework package first, sandbox.hide after
        dirs, shown_again = [n for n, d in covers if d], []
        for name, is_dir in covers:
            if any(name.startswith(d.rstrip("/") + "/") for d in dirs) and \
                    not any(name == r or name.startswith(r + "/") for r in shown_again):
                continue                                   # inside a covered directory (bwrap cannot mount there)
            if is_dir:
                # Under a covered framework package, the relay and what was granted in it (a method's helpers) are
                # bound again before it is made read-only; what sandbox.hide names in them is covered after.
                again = [] if self.framework or name not in framework_names else \
                    [p for p in [str(RELAY), *(n for g in [*self.readable, *readable] for n in _names(g))]
                     if p.startswith(name + "/") and os.path.exists(p)]
                shown_again += again
                args.extend(["--tmpfs", name, *[a for r in dict.fromkeys(again) for a in ("--ro-bind", r, r)],
                             "--remount-ro", name])
            else:
                args.extend(["--ro-bind", _empty_file(), name])
            hidden.append(name)
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
        policy = {"enforced": True, "network": self.network, "read_only": ro, "read_write": rw, "hidden": hidden,
                  "binds": named,
                  "proxy": self.proxy_url, "allow": list(self.allow), "limits": dict(self.limits),
                  "gpu": list(self.gpu)}
        name = f"{self.role}-{next(self._count)}"
        deny = [p for p in self._absent(ro + rw + [b["source"] for b in named], binds)    # covered: empty inside
                if not any(p == h or p.startswith(h.rstrip("/") + "/") for h in hidden)]
        if self.network == "isolated" or deny or self.limits:
            # The check inside: on an isolated network the network and the paths, on an open one the paths only;
            # the limits are set on the command before it starts.
            with self._setup:
                if self._bridges is None:
                    self._bridges = _Bridges()
            ports = self.forwarded_ports(command, env, forward, env_ports) if self.network == "isolated" else []
            sockets = {int(p): str(s) for p, s in self.sockets.items()} if self.network == "isolated" else {}
            record = self._record(command, {**policy, "forwarded": sorted({*ports, *sockets}), "sockets": sockets,
                                            "probe": None}, env, name=name)
            report = self._bridges.report_listener(name, lambda result: self._probe_result(record, result))
            args += ["--bind", str(self._bridges.dir), str(self._bridges.dir)]
            for sock in sockets.values():                 # a service of its own per sandbox, at one port inside
                args += ["--bind", sock, sock]
            command = [sys.executable, str(RELAY),
                       *[a for p in ports if p not in sockets
                         for a in ("--forward", f"{p}={self._bridges.socket_for(p)}")],
                       *[a for p, s in sorted(sockets.items()) for a in ("--forward", f"{p}={s}")],
                       *[a for p in deny for a in ("--deny", p)],
                       *[a for k, v in sorted(self.limits.items()) if v for a in ("--limit", f"{k}={int(v)}")],
                       *(["--open-network"] if self.network == "open" else []),
                       "--report", str(report), "--", *command]
        else:
            self._record(command, policy, env, name=name)
        return args + ["--", *command], env

    def _absent(self, bound: list[str], binds=()) -> list[str]:
        """The probe paths that exist outside and are not above a bound path, each under its own name and, beneath a
        source bound under another name, under that name too. One beneath a bound directory
        stays: it is shown by accident (a home bound whole, a secret in a directory shown at /w/method), and the
        check must catch that."""
        out = []
        for path in self.probe_absent:
            p = Path(path).expanduser()
            if not p.exists():
                continue
            p = str(p.resolve())
            # A probe path that holds what is bound (the run's directory holding the workspace) is not checked; one
            # that is bound itself is (a grant that is, or was retargeted to, a secret fails the check).
            if any(str(b).startswith(p + "/") for b in bound):
                continue
            out.append(p)
            inside = self.translate(p, binds)
            if inside != p:
                out.append(inside)
        return list(dict.fromkeys(out))

    def _record(self, command: list[str], policy: dict, env: dict, name: str | None = None) -> Path | None:
        if self.record_dir is None:
            return None
        self.record_dir.mkdir(parents=True, exist_ok=True)
        name = name or f"{self.role}-{next(self._count)}"
        path = self.record_dir / f"{name}.json"
        self.last_record = path
        path.write_text(json.dumps(_scrubbed({"role": self.role, "time": datetime.now(timezone.utc).isoformat(),
                                              "program": command[0] if command else None, **policy,
                                              "environment": sorted(env)}), indent=1))
        return path

    @staticmethod
    def _probe_result(record: Path | None, result) -> None:
        if record is None or not record.exists():
            return
        data = json.loads(record.read_text())
        data["probe"] = _scrubbed(result)
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
        return box

    def abandon(self, args: list[str]) -> None:
        """A wrapped command that will not be run: the listener waiting for its isolation report is closed."""
        if getattr(self, "_bridges", None) is not None and "--report" in args:
            self._bridges.cancel(args[args.index("--report") + 1])

    def close(self):
        if self._bridges is not None:
            self._bridges.close()
            self._bridges = None


def _under(path, parent) -> bool:
    """Whether `path` lies strictly below `parent`, under any of their names."""
    return any(n.startswith(q.rstrip("/") + "/") for n in _names(path) for q in _names(parent))


class AllowlistProxy:
    """HTTP CONNECT (and plain HTTP) proxy on 127.0.0.1 that forwards only to allowed hosts. When this process has
    a proxy of its own (HTTPS_PROXY / HTTP_PROXY, an offline machine's egress proxy), it connects through it, except
    to the hosts NO_PROXY names."""

    def __init__(self, allow: list[str], log: Path | None = None):
        self.hosts = {urlparse(a).hostname or a for a in allow}
        self.log = Path(log) if log else None
        upstream = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("HTTP_PROXY") \
            or os.environ.get("http_proxy")
        self.upstream = urlparse(upstream) if upstream else None
        self._env_proxies = urllib.request.getproxies_environment()       # NO_PROXY, read once like the upstream
        self._log_lock = threading.Lock()
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            rbufsize = 0                 # the head is read as it comes: a request body is left for the pipe

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
                if method == "CONNECT":                # host:port, an IPv6 literal bracketed ([::1]:8443)
                    u = urlparse(f"//{target}")
                    host, port = u.hostname or "", u.port or 443
                else:
                    u = urlparse(target)
                    host, port = u.hostname or "", u.port or 80
                if host not in outer.hosts:
                    outer._write(method, host, port, False)
                    self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                    return
                outer._write(method, host, port, True)
                try:
                    upstream = outer._connect(host, port)
                except OSError:
                    self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                    return
                if method == "CONNECT":
                    self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                else:                                # a plain request: to the server, in origin form, and the
                    # connection closed after it (a kept-alive one would carry the next request, for any host,
                    # past this check)
                    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                    line = f"{method} {path} {parts[2]}\r\n"
                    hop = (b"connection:", b"proxy-connection:", b"keep-alive:")
                    kept = [h for h in headers if not h.lower().startswith(hop)]
                    upstream.sendall(line.encode("latin-1") + b"".join(kept) + b"Connection: close\r\n\r\n")
                self._pipe(self.connection, upstream)

            @staticmethod
            def _pipe(a, b):
                sockets = [a, b]
                try:
                    while True:
                        # A TLS connection to the upstream proxy may hold decrypted bytes select does not see.
                        ready = [s for s in sockets if isinstance(s, ssl.SSLSocket) and s.pending()] \
                            or select.select(sockets, [], [], 600)[0]
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

    def _connect(self, host: str, port: int) -> socket.socket:
        """A connection to host:port, directly or through this process's own proxy (a CONNECT tunnel)."""
        if self.upstream is None or not self.upstream.hostname \
                or urllib.request.proxy_bypass_environment(f"[{host}]:{port}" if ":" in host else f"{host}:{port}",
                                                           self._env_proxies):
            return socket.create_connection((host, port), timeout=30)
        tls = self.upstream.scheme == "https"           # https://proxy: TLS to the proxy, then CONNECT inside it
        sock = socket.create_connection((self.upstream.hostname, self.upstream.port or (443 if tls else 80)),
                                        timeout=30)
        if tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.upstream.hostname)
        auth = ""
        if self.upstream.username:                       # http://user:pass@proxy:3128
            pair = f"{unquote(self.upstream.username)}:{unquote(self.upstream.password or '')}"
            auth = f"Proxy-Authorization: Basic {base64.b64encode(pair.encode()).decode()}\r\n"
        name = f"[{host}]" if ":" in host else host
        sock.sendall(f"CONNECT {name}:{port} HTTP/1.1\r\nHost: {name}:{port}\r\n{auth}\r\n".encode())
        reply = b""
        while b"\r\n\r\n" not in reply and len(reply) < 65536:
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply += chunk
        if not reply.split(b"\r\n", 1)[0].split(b" ")[1:2] == [b"200"]:
            sock.close()
            raise OSError(f"the upstream proxy refused {host}:{port}: {reply[:100]!r}")
        return sock

    def _write(self, method: str, host: str, port: int, allowed: bool) -> None:
        if self.log is None:
            return
        line = json.dumps(_scrubbed({"time": datetime.now(timezone.utc).isoformat(), "method": method,
                                     "host": host, "port": port, "allowed": allowed}))
        with self._log_lock:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log, "a") as f:
                f.write(line + "\n")

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _main(argv: list[str]) -> int:
    """`python -m hermit.sandbox SPEC -- COMMAND...`: COMMAND in a sandbox made from SPEC, a JSON object of Sandbox
    fields (readable, writable, hide, network, env_allow, sockets, enforce, role, ...) and of the call's `env` (its
    HOME, when it names one, kept: /tmp inside) and `chdir`; its exit status. A launcher that hands another program a
    command prefix (an adapter's compile) uses it."""
    spec, command = json.loads(argv[0]), argv[argv.index("--") + 1:]
    env, chdir = spec.pop("env", None), spec.pop("chdir", None)
    for key in ("readable", "writable", "hide"):
        spec[key] = [Path(p) for p in spec.get(key) or []]
    spec["hide"] = [*_env_hidden(), *spec["hide"]]      # the run's sandbox.hide holds here too
    if spec.get("record_dir"):
        spec["record_dir"] = Path(spec["record_dir"])
    box = Sandbox(**spec)
    try:
        args, full = box.wrap(command, env=env, keep=list(env or {}), chdir=chdir)
        if env and env.get("HOME"):
            full["HOME"] = box.translate(env["HOME"])
        confined = bool(args) and Path(args[0]).name == "bwrap"     # else unconfined: chdir is this process's own
        return subprocess.run(args, env=full, cwd=None if confined or chdir is None else chdir).returncode
    finally:
        box.close()


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
