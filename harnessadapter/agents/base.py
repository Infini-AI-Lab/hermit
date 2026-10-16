"""Agent: the one interface every acting agent has, built-in or the task's own.

    class Agent:
        def __init__(self, params: dict, base: Path)             # agent.params from the config, and its directory
        def run(self, task: dict, adapter: Path | None) -> RunOutput

A config names the agent:

    agent:
      name: react                        # a built-in: react | codex | claude-code
      name: agent/agent.py:CLBenchAgent  # or the task's own class (file.py:Class, module:Class)
      name: agent/pipeline.py:run        # or a plain function run(task, adapter), wrapped as a FunctionAgent
      concurrency: 4
      params: {...}

The Trainer, `eval` and `check` call nothing but `run`; what happens inside (a tool loop, a CLI process, a
benchmark's own loop with the adapter plugged in) is the agent's business. `Session` gives any agent the adapter's
exports; `self.session(adapter)` builds one.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Callable

from harnessadapter.task import RunOutput, load_callable


class Agent:
    def __init__(self, params: dict | None = None, base: Path | str | None = None):
        self.params = dict(params or {})
        self.base = Path(base).resolve() if base is not None else Path.cwd()

    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        raise NotImplementedError(f"{type(self).__name__}.run")

    # --- helpers for subclasses ---------------------------------------------------------------------------------
    def session(self, adapter: Path | None, **kwargs):
        """The adapter's exports as a Session, or None without an adapter (a baseline run)."""
        if adapter is None:
            return None
        from harnessadapter.adapter.session import Session
        return Session(adapter, **kwargs)

    def path(self, value: str | Path) -> Path:
        """A path from the params, relative to the config's directory."""
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.base / p).resolve()

    def log_dir(self, default: str = ".logs") -> Path:
        d = self.path(self.params.get("logs", default))
        d.mkdir(parents=True, exist_ok=True)
        return d


class FunctionAgent(Agent):
    """A plain `run(task, adapter)` function as an Agent; `params` and `base` are passed when it asks for them."""

    def __init__(self, fn: Callable, params: dict | None = None, base: Path | str | None = None):
        super().__init__(params, base)
        self.fn = fn
        wanted = inspect.signature(fn).parameters
        self.extra = {}
        if "params" in wanted:
            self.extra["params"] = self.params
        if "base" in wanted:
            self.extra["base"] = self.base

    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        return self.fn(task, adapter, **self.extra)


def builtin(name: str):
    """The built-in agent classes, imported on demand."""
    if name == "react":
        from harnessadapter.agents.react import ReactAgent
        return ReactAgent
    if name in ("cli", "codex", "claude-code", "claude"):
        from harnessadapter.agents.cli import CliAgent
        backend = {"codex": "codex", "claude-code": "claude", "claude": "claude"}.get(name)
        if backend is None:
            return CliAgent
        return lambda params, base: CliAgent({"backend": backend, **(params or {})}, base)
    return None


BUILTIN_NAMES = ("react", "codex", "claude-code", "cli")


def load_agent(name: str, params: dict | None, base: Path) -> Agent:
    """An Agent from `agent.name`: a built-in name, `file.py:Class` / `module:Class` for an Agent subclass, or
    `file.py:function` / `module:function` for a plain run function."""
    if not name:
        raise ValueError("agent.name is required")
    factory = builtin(str(name))
    if factory is not None:
        return factory(params, base)
    target = load_callable(str(name), base)
    if inspect.isclass(target) and issubclass(target, Agent):
        return target(params, base)
    if callable(target):
        return FunctionAgent(target, params, base)
    raise ValueError(f"{name} is neither an Agent class nor a run function")
