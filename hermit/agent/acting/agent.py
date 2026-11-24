"""Agent: the one interface every acting agent has, built-in or the task's own.

    class Agent:
        def __init__(self, params: dict, base: Path)             # agent.params from the config, and its directory
        def run(self, task: dict, adapter: Path | None, environment=None) -> RunOutput

A config names the agent:

    agent:
      name: react                        # a built-in: react | codex | claude-code | pi | mini-swe-agent | opencode | dsh
                                         # | hermes | answer
      name: agent/agent.py:CLBenchAgent  # or the task's own class (file.py:Class, module:Class)
      name: agent/pipeline.py:run        # or a plain function run(task, adapter), wrapped as a FunctionAgent
      concurrency: 4
      params: {...}

The Trainer, `eval` and `check` call nothing but `run`; what happens inside (a tool loop, a
CLI process, a benchmark's own loop with the adapter plugged in) is the agent's business. `Session` gives any agent the
adapter's exports; `self.session(adapter)` builds one. `environment` is the task's own environment when the task set
gives one (a task's Harbor environment: hermit.environment.harbor), where the agent works through its `exec`,
`upload` and `download`; None for a task the agent works on in a workspace of its own. A run hands it only to an
agent whose `run` takes it.

What an agent can take of an adapter is its `supports`: the exports (exports.yaml: tools, context_tool, skills,
agents, hooks; every agent takes the context) it gives the session. A method says what of these it needs (its
evolver.yaml `needs`); a run whose agent does not take one is refused before it starts (hermit.trainer.needs_errors),
never left to drop it silently. An agent that declares nothing (`supports = None`: a function, a task's own class
that says nothing) is not checked.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Callable

from hermit.common import RunOutput, load_callable


EXPORTS = ("context", "context_tool", "tools", "skills", "agents", "hooks")   # an adapter's, as exports.yaml names them
ALL = EXPORTS


class Agent:
    supports: tuple[str, ...] | None = None    # the adapter exports it takes (EXPORTS); None: not declared

    def __init__(self, params: dict | None = None, base: Path | str | None = None):
        self.params = dict(params or {})
        self.base = Path(base).resolve() if base is not None else Path.cwd()

    def run(self, task: dict, adapter: Path | None, environment=None) -> RunOutput:
        raise NotImplementedError(f"{type(self).__name__}.run")

    def installs(self, adapter: Path | None) -> list[dict]:
        """What this agent needs installed in a task's own environment before its run (an environment that installs,
        a task's Harbor one, does it at its setup; nothing installs while the agent runs): items of {agent, version,
        probe} (a CLI by the environment's installer of that name, unless `probe`, a shell line, finds it and no
        version is pinned) or {packages} (best effort). Nothing by default. Read on this machine for a built-in agent
        only (a task's own class is not loaded here): a custom agent brings what it needs in the task's image."""
        return []

    # --- helpers for subclasses ---------------------------------------------------------------------------------
    def session(self, adapter: Path | None, **kwargs):
        """The adapter's exports as a Session, or None without an adapter (a baseline run)."""
        if adapter is None:
            return None
        from hermit.adapter.session import Session
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
        self.supports = getattr(fn, "supports", None)     # a function says what it takes as `fn.supports = (...)`
        wanted = inspect.signature(fn).parameters
        self.extra = {}
        if "params" in wanted:
            self.extra["params"] = self.params
        if "base" in wanted:
            self.extra["base"] = self.base
        self.takes_environment = "environment" in wanted

    def run(self, task: dict, adapter: Path | None, environment=None) -> RunOutput:
        if environment is not None:
            if not self.takes_environment:
                name = getattr(self.fn, "__name__", self.fn)
                raise TypeError(f"{name} takes no environment, and the task gives one: add an `environment` parameter")
            return self.fn(task, adapter, environment=environment, **self.extra)
        return self.fn(task, adapter, **self.extra)


def builtin(name: str):
    """The built-in agent classes, imported on demand."""
    if name == "react":
        from hermit.agent.acting.react import ReactAgent
        return ReactAgent
    if name in ("cli", "codex", "claude-code", "claude", "pi", "mini-swe-agent", "opencode", "dsh", "hermes"):
        from hermit.agent.acting.workspace import CliAgent
        backend = {"codex": "codex", "claude-code": "claude", "claude": "claude", "pi": "pi",
                   "mini-swe-agent": "mini-swe-agent", "opencode": "opencode", "dsh": "dsh",
                   "hermes": "hermes"}.get(name)
        if backend is None:
            return CliAgent
        return lambda params, base: CliAgent({"backend": backend, **(params or {})}, base)
    if name == "answer":
        from hermit.agent.acting.answer import AnswerAgent
        return AnswerAgent
    if name == "harbor":
        raise ValueError("agent.name: harbor is gone: a Harbor task set gives each task its environment "
                         "(environment.harbor), and any acting agent works in it; see docs/framework/task.md")
    return None


BUILTIN_NAMES = ("react", "codex", "claude-code", "pi", "mini-swe-agent", "opencode", "dsh", "hermes", "cli",
                 "answer")


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



def supports_of(name: str, base: Path, params: dict | None = None, *, may_import: bool) -> tuple[str, ...] | None:
    """What the agent `agent.name` names takes of an adapter (its `supports`), without running it: a built-in's from
    its class; a task's own class or function only when `may_import` (it runs on this machine anyway), else None."""
    factory = builtin(str(name))
    if factory is not None:
        if str(name) == "react":
            from hermit.agent.acting.react import ReactAgent
            return ReactAgent.supports
        if str(name) == "answer":
            from hermit.agent.acting.answer import AnswerAgent
            return AnswerAgent.supports
        from hermit.agent.acting.workspace import CliAgent
        return CliAgent.supports
    if not may_import:
        return None
    supports = getattr(load_callable(str(name), base), "supports", None)
    return tuple(supports) if isinstance(supports, (tuple, list)) else None   # a property: known only per instance
