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
import subprocess
from typing import Sequence


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


@dataclass
class ExecResult:
    """A command run in an environment: its exit code, its stdout and stderr together, and whether its time ran out."""
    code: int
    output: str = ""
    timed_out: bool = False


class Environment:
    """A workspace and what an agent may reach from it. Subclasses say how that is enforced (`isolation`) and run a
    command there (`exec`)."""
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

    def path(self, relative: str | Path = "") -> Path:
        """A path of the workspace, as this machine sees it; one that leaves the workspace is refused."""
        root = self.workspace.resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"{relative} lies outside the workspace")
        return target

    def upload(self, source: Path | str, target: str | Path) -> Path:
        """Copy a file or a folder of this machine into the workspace, at `target` (relative to it)."""
        dest, source = self.path(target), Path(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(source, dest, follow_symlinks=False)
        return dest

    def download(self, source: str | Path, target: Path | str) -> Path:
        """Copy a file or a folder of the workspace (`source`, relative to it) to `target` on this machine. A link is
        copied as the link it is, never followed out of the workspace."""
        src, target = self.path(source), Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(src, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(src, target, follow_symlinks=False)
        return target

    def exec(self, command: list[str], *, cwd: str | Path | None = None, env: dict | None = None,
             timeout: float | None = None, stdin: str | None = None) -> ExecResult:
        """Run `command` in the environment, from `cwd` (relative to the workspace; default its root), with `env`
        added to the environment's own variables."""
        raise NotImplementedError(f"{type(self).__name__} runs no commands")

    def _run(self, command: list[str], *, cwd: Path, env: dict, timeout: float | None, stdin: str | None
             ) -> ExecResult:
        try:
            done = subprocess.run(command, cwd=cwd, env=env, input=stdin, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            return ExecResult(code=-1, output=out if isinstance(out, str) else out.decode(errors="replace"),
                              timed_out=True)
        return ExecResult(code=done.returncode, output=(done.stdout or "") + (done.stderr or ""))


class Local(Environment):
    """A directory of this machine, as it is (isolation none): no path can be hidden and the network is not
    limited."""

    def exec(self, command, *, cwd=None, env=None, timeout=None, stdin=None):
        return self._run(list(command), cwd=self.path(cwd or ""), env={**os.environ, **(env or {})},
                         timeout=timeout, stdin=stdin)


class Sandboxed(Environment):
    """A directory of this machine inside the run's sandbox (isolation sandbox: hermit.sandbox.Sandbox), which
    decides what the agent reads, writes and reaches."""
    isolation = "sandbox"

    def __init__(self, workspace: Path | str, limits: Limits | None = None, *, sandbox):
        super().__init__(workspace, limits)
        if sandbox is None:
            raise ValueError("a Sandboxed environment needs the run's sandbox")
        self.sandbox = sandbox

    def exec(self, command, *, cwd=None, env=None, timeout=None, stdin=None):
        where = self.path(cwd or "")
        wrapped, full_env = self.sandbox.wrap(list(command), writable=[self.workspace.resolve()],
                                              env={**os.environ, **(env or {})}, keep=list(env or {}), chdir=where)
        try:
            return self._run(wrapped, cwd=where, env=full_env, timeout=timeout, stdin=stdin)
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


__all__ = ["Container", "Environment", "ExecResult", "Limits", "Mount", "Sandboxed", "Local", "as_environment",
           "placed"]
