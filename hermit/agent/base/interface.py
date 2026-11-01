"""Layer 2's one API (docs/framework/agent-layers.md#layer-2-base-agent).

    agent = make("codex", settings, model)            # a Base Agent: its settings are given here, once
    result = agent.run(prompt, workspace=..., tools=[...], limits=Limits(...), session=None, roles=())
    result.reply, result.trajectory, result.usage, result.session

Layer 3 hands everything for one call through `run`: what to do (`prompt`), where (`workspace`), with what
(`tools`), within what (`limits`), whether to continue an earlier call (`session`), and named sub-agents (`roles`).
A Base Agent enforces every limit with its own isolation, or refuses the call (`Unsupported`); it never ignores
one.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from hermit.agent.base.tool import Tool
from hermit.agent.base.trajectory import Step
from hermit.agent.model.usage import Usage


class Unsupported(ValueError):
    """A Base Agent cannot do what the call asks (a limit it cannot enforce, a setting that does not combine)."""


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
class Result:
    reply: str                                         # the agent's last message
    trajectory: list[Step] = field(default_factory=list)
    usage: list[Usage] = field(default_factory=list)
    session: Any = None                                # pass back to continue the same agent
    tool_calls: list[dict] = field(default_factory=list)   # direct: the calls the model asked for, not run
    finish_reason: str | None = None
    reasoning: str = ""
    exit_code: int | None = None                       # a CLI's exit code
    timed_out: bool = False
    attempts: int = 1                                  # runs the provider could not serve were run again
    log: Path | None = None                            # the raw record (a CLI's event stream), kept as an archive
    extra: dict = field(default_factory=dict)


class BaseAgent(ABC):
    name: str = ""

    def __init__(self, settings: dict | None = None, model: Any = None):
        self.settings = dict(settings or {})
        self.model = model

    @abstractmethod
    def run(self, prompt: str | list[dict], *, workspace: Path | None = None, tools: Sequence[Tool] = (),
            limits: Limits | None = None, session: Any = None, roles: Sequence[dict] = ()) -> Result:
        """`prompt` is the instruction, or chat messages. `roles` are named sub-agents (name, description,
        instructions) the agent may delegate to, where it can."""


def make(name: str, settings: dict | None = None, model: Any = None) -> BaseAgent:
    """A Base Agent by name: direct, react, codex or claude (claude-code)."""
    if name == "direct":
        from hermit.agent.base.direct import Direct
        return Direct(settings, model)
    if name == "react":
        from hermit.agent.base.react import React
        return React(settings, model)
    if name == "codex":
        from hermit.agent.base.cli import Codex
        return Codex(settings, model)
    if name in ("claude", "claude-code"):
        from hermit.agent.base.cli import Claude
        return Claude(settings, model)
    raise ValueError(f"no Base Agent {name!r}: direct, react, codex or claude")


def configured(agent: BaseAgent, **settings) -> BaseAgent:
    """A copy of `agent` with `settings` over its own (a call's sampling, say); the agent itself is unchanged."""
    import copy
    other = copy.copy(agent)
    other.settings = {**agent.settings, **{k: v for k, v in settings.items() if v is not None}}
    return other


BaseAgent.configured = configured


def as_messages(prompt: str | list[dict]) -> list[dict]:
    """A prompt as chat messages: a string is one user message."""
    return list(prompt) if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
