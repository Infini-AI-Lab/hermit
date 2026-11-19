"""Where an agent works: the one interface every environment has (docs/framework/agent-layers.md#environments).

    env = Local(path, Limits(readable=[Mount(data, "docs")], timeout_s=600))
    agent.run(prompt, environment=env, tools=[...])          # a Base Agent works in it
    env.exec(["python", "check.py"])                          # a tool of the agent's runs a command there
    env.upload(local, "inputs/a.json"); env.download("out.txt", local)

An environment is the workspace an agent works in, what it may read, hide and reach (`limits`), and how that is
enforced (`isolation`): none (this machine as it is), sandbox (the run's bubblewrap sandbox) or container (a Docker
container of the session's own). A Base Agent takes its environment from the call, never from its settings; it
enforces every limit with the environment's isolation, or refuses the call. A path given where an environment is
expected is a `Local` environment at that path.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import os
from pathlib import Path
import shutil
import signal
import subprocess
from typing import Sequence


DRAIN_S = 5.0                     # how long a timed-out command's output is still read after the kill


@dataclass
class Mount:
    """Read-only data in the workspace: `source` on the machine, seen at `target` (absolute, or under the
    workspace when relative)."""
    source: Path
    target: Path
    readonly: bool = True


@dataclass
class Limits:
    readable: Sequence[Mount] = ()     # data the agent may read, at its target
    hidden: Sequence[Path] = ()        # paths the agent must not see
    network: Sequence[str] | None = None   # hosts besides the model's it may reach; None: the network is not limited
    timeout_s: float | None = None     # the call's wall clock; None: the Base Agent's setting


class Unsupported(ValueError):
    """An environment or a Base Agent cannot do what the call asks (a limit it cannot enforce, a setting that does not
    combine): refused, never ignored."""


@dataclass
class ExecResult:
    """A command run in an environment: its exit code, its stdout and stderr together, and whether its time ran out."""
    code: int
    output: str = ""
    timed_out: bool = False


def _target(mount: Mount, workspace: Path) -> Path:
    """Where a mount is seen: its target, under the workspace when relative."""
    target = Path(mount.target)
    return target if target.is_absolute() else Path(workspace) / target


class Environment:
    """A workspace and what an agent may reach from it. Subclasses say how that is enforced (`isolation`) and run a
    command there (`exec`), within the environment's limits: its mounts readable, its clock `timeout_s` unless the
    call gives one, and a limit it cannot keep refused (Unsupported)."""
    isolation = "none"
    sandbox = None                         # the run's hermit.sandbox.Sandbox, under isolation sandbox
    container: dict = {}                   # the container's settings (hermit.agent.environment.docker), under container

    def __init__(self, workspace: Path | str, limits: Limits | None = None):
        self.workspace = Path(workspace)
        self.limits = limits or Limits()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self.workspace)!r}, isolation={self.isolation!r})"

    def with_limits(self, limits: Limits) -> "Environment":
        """This environment with other limits (a caller's deadline, say); this one is unchanged."""
        other = copy.copy(self)
        other.limits = limits
        return other

    def path(self, relative: str | Path = "", *, follow: bool = True) -> Path:
        """A path of the workspace, as this machine sees it; one that leaves the workspace is refused. With
        `follow=False` a link at the path itself is kept as the link (only the folders above it are resolved)."""
        root = self.workspace.resolve()
        joined = root / relative
        target = joined.resolve() if follow or joined == root else joined.parent.resolve() / joined.name
        if not target.is_relative_to(root):
            raise ValueError(f"{relative} lies outside the workspace")
        return target

    def upload(self, source: Path | str, target: str | Path) -> Path:
        """Copy a file or a folder of this machine into the workspace, at `target` (relative to it). A link is copied
        as the link it is."""
        source = Path(source)
        if not source.is_dir() or source.is_symlink():
            return self._put(source, target)
        dest = self.path(target, follow=False)
        for folder, dirs, files in os.walk(source):             # links are entries, never followed
            rel = Path(folder).relative_to(source)
            where = self.path(Path(target) / rel, follow=False)
            if where.is_symlink():                              # a link the workspace has there: replaced, not entered
                where.unlink()
            where.mkdir(parents=True, exist_ok=True)
            for name in [*files, *(d for d in dirs if (Path(folder) / d).is_symlink())]:
                self._put(Path(folder) / name, Path(target) / rel / name)
        return dest

    def _put(self, source: Path, target: str | Path) -> Path:
        """One file or link at `target`: a link already there is replaced, never written through."""
        dest = self.path(target, follow=False)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink():
            dest.unlink()
        shutil.copy2(source, dest, follow_symlinks=False)
        return dest

    def download(self, source: str | Path, target: Path | str) -> Path:
        """Copy a file or a folder of the workspace (`source`, relative to it) to `target` on this machine. A link is
        copied as the link it is, never followed out of the workspace."""
        src, target = self.path(source, follow=False), Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(src, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(src, target, follow_symlinks=False)
        return target

    def exec(self, command: list[str], *, cwd: str | Path | None = None, env: dict | None = None,
             timeout: float | None = None, stdin: str | None = None) -> ExecResult:
        """Run `command` in the environment, from `cwd` (relative to the workspace; default its root), with `env`
        added to the environment's own variables, for `timeout` seconds (default: the limits' `timeout_s`)."""
        raise NotImplementedError(f"{type(self).__name__} runs no commands")

    def _run(self, command: list[str], *, cwd: Path, env: dict, timeout: float | None, stdin: str | None
             ) -> ExecResult:
        """The command in a process group of its own: the group and every process it started (one in a session of its
        own too) are killed when its time runs out or the caller is interrupted, so no child outlives the call; what
        they wrote is read for a few seconds more at most. On this machine as it is (Local), a process that left the
        group and whose parent already exited is not found; in the sandbox its PID namespace ends every one."""
        timeout = timeout if timeout is not None else self.limits.timeout_s
        process = subprocess.Popen(command, cwd=cwd, env=env, text=True, start_new_session=True,
                                   stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            output, _ = process.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            try:
                output, _ = process.communicate(timeout=DRAIN_S)
            except subprocess.TimeoutExpired:           # a process still holds the output open: give it up
                process.kill()
                process.wait()
                output = ""
            return ExecResult(code=-1, output=output or "", timed_out=True)
        except BaseException:
            _kill_group(process)
            process.wait()
            raise
        return ExecResult(code=process.returncode, output=output or "")


def _overlap(a: Path, b: Path) -> bool:
    a, b = Path(os.path.realpath(a)), Path(os.path.realpath(b))
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _kill_group(process: subprocess.Popen) -> None:
    descendants = _descendants(process.pid)              # found before the kill re-parents them
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    for pid in descendants:                               # one that left the group (a session of its own)
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _descendants(pid: int) -> list[int]:
    """Every process under `pid`, its children's children too."""
    children: dict[int, list[int]] = {}
    for child, parent in _parents():
        children.setdefault(parent, []).append(child)
    out, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            out.append(child)
            todo.append(child)
    return out


def _parents() -> list[tuple[int, int]]:
    """Every process with its parent: from /proc, else (macOS) from `ps`."""
    if Path("/proc/self/stat").is_file():
        pairs = []
        for entry in Path("/proc").glob("[0-9]*/stat"):
            try:
                pairs.append((int(entry.parent.name), int(entry.read_text().rsplit(")", 1)[1].split()[1])))
            except (OSError, IndexError, ValueError):
                continue
        return pairs
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid="], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [(int(p), int(q)) for p, q in (line.split() for line in out.splitlines() if len(line.split()) == 2)]


class Local(Environment):
    """A directory of this machine, as it is (isolation none): no path can be hidden and the network is not
    limited. A readable mount is linked into the workspace at its target."""

    def exec(self, command, *, cwd=None, env=None, timeout=None, stdin=None):
        if self.limits.hidden:
            raise Unsupported("a Local environment cannot hide paths: use a Sandboxed or Container one")
        if self.limits.network is not None:
            raise Unsupported("a Local environment cannot limit the network: use a Sandboxed or Container one")
        root = self.workspace.resolve()
        for mount in self.limits.readable:
            target = _target(mount, root)
            placed = target.parent.resolve() / target.name
            if placed.resolve() == Path(mount.source).resolve():
                continue
            if not placed.is_relative_to(root):
                raise Unsupported(f"a Local environment links data into its workspace, not at {target}")
            if placed.is_symlink():                   # a link an earlier call left, to other data
                placed.unlink()
            elif os.path.lexists(placed):
                raise Unsupported(f"{target} is to show {mount.source}, but the workspace has its own file there")
            placed.parent.mkdir(parents=True, exist_ok=True)
            placed.symlink_to(Path(mount.source).resolve())
        return self._run(list(command), cwd=self.path(cwd or ""), env={**os.environ, **(env or {})},
                         timeout=timeout, stdin=stdin)


class Sandboxed(Environment):
    """A directory of this machine inside the run's sandbox (isolation sandbox: hermit.sandbox.Sandbox), which
    decides what the agent reads, writes and reaches. A readable mount is shown at its target."""
    isolation = "sandbox"

    def __init__(self, workspace: Path | str, limits: Limits | None = None, *, sandbox):
        super().__init__(workspace, limits)
        if sandbox is None:
            raise ValueError("a Sandboxed environment needs the run's sandbox")
        self.sandbox = sandbox

    def exec(self, command, *, cwd=None, env=None, timeout=None, stdin=None):
        root = self.workspace.resolve()
        box, limits = self.sandbox, self.limits
        if limits.network:
            raise Unsupported("a Sandboxed environment's network is the sandbox's: it cannot be given more hosts")
        if (limits.hidden or limits.network is not None) and not getattr(box, "enabled", True):
            raise Unsupported("the run's sandbox is off: it cannot hide paths or limit the network")
        if limits.network is not None and (getattr(box, "network", None) == "open" or getattr(box, "allow", None) or
                                           getattr(box, "forward", None) or getattr(box, "sockets", None) or
                                           getattr(box, "proxy_url", None)):
            raise Unsupported("the run's sandbox lets the network through (an open network, allowed hosts, a proxy, "
                              "forwarded ports or sockets): it cannot cut the command off from it")
        mounts = [(Path(m.source), _target(m, root), m.readonly) for m in limits.readable]
        if limits.hidden:
            # An allowlist shows what is granted: a path to keep hidden must not lie in anything granted (as the CLI
            # Base Agents check it: hermit.agent.base.cli).
            from hermit.sandbox import SYSTEM_ETC
            shown = [root, *(source for source, _, _ in mounts), *getattr(box, "readable", ()),
                     *getattr(box, "writable", ()), *(b.source for b in getattr(box, "binds", ())),
                     *map(Path, ("/usr", "/bin", "/sbin", "/lib", "/lib64", *SYSTEM_ETC)),
                     *(box.visible([str(command[0])]) if hasattr(box, "visible") and command else [])]
            for path in limits.hidden:
                if any(_overlap(Path(path), Path(p)) for p in shown):
                    raise Unsupported(f"{path} is to be hidden but lies in what the environment shows")
        translate = getattr(box, "translate", None)
        binds = [(source, translate(str(target)) if translate else str(target), not readonly)
                 for source, target, readonly in mounts if target.resolve() != source.resolve()]
        readable = [source for source, target, _ in mounts if target.resolve() == source.resolve()]
        where = self.path(cwd or "")
        wrapped, full_env = box.wrap(list(command), writable=[root], env={**os.environ, **(env or {})},
                                     keep=list(env or {}), chdir=where, readable=readable, binds=binds,
                                     env_ports=limits.network is None)    # a limited network: no port forwarded
        if (limits.hidden or limits.network is not None) and not any(Path(a).name == "bwrap" for a in wrapped):
            if hasattr(box, "abandon"):
                box.abandon(wrapped)
            raise Unsupported("the run's sandbox did not isolate the command (no bubblewrap): it cannot hide paths "
                              "or limit the network")
        try:                                                    # `where` may exist only inside (a mount's target)
            return self._run(wrapped, cwd=root, env=full_env, timeout=timeout, stdin=stdin)
        finally:
            if hasattr(self.sandbox, "abandon"):
                self.sandbox.abandon(wrapped)


class Container(Environment):
    """A Docker container of the session's own (isolation container, hermit.agent.environment.docker): the workspace
    and the session's home bound into it, its network the egress proxy's. `settings` are the container settings
    (image, network, proxy, ...: docker.settings). A CLI's session runs there; other commands are not run there yet."""
    isolation = "container"

    def __init__(self, workspace: Path | str, limits: Limits | None = None, *, settings: dict | None = None):
        super().__init__(workspace, limits)
        self.container = dict(settings or {})


def placed(workspace: Path | str, sandbox=None, limits: Limits | None = None) -> Environment:
    """The workspace in the run's sandbox when there is one (Sandboxed), else on this machine as it is (Local):
    where an Evolver's own sessions work."""
    return Sandboxed(workspace, limits, sandbox=sandbox) if sandbox is not None else Local(workspace, limits)


def as_environment(value) -> Environment | None:
    """An environment from what a caller gave: an Environment as it is, a path as a Local environment there, None
    as None."""
    if value is None or isinstance(value, Environment):
        return value
    if isinstance(value, (str, os.PathLike)):
        return Local(value)
    raise TypeError(f"an environment is an Environment or a path, not {type(value).__name__}")


__all__ = ["Container", "Environment", "ExecResult", "Limits", "Local", "Mount", "Sandboxed", "Unsupported",
           "as_environment", "placed"]
