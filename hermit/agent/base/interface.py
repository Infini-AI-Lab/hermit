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
import json
from pathlib import Path
from typing import Any, Sequence

from hermit.agent.base.tool import Tool
from hermit.agent.base.trajectory import Step
from hermit.agent.model.usage import Usage


class Unsupported(ValueError):
    """A Base Agent cannot do what the call asks (a limit it cannot enforce, a setting that does not combine)."""


LOGIN_FAILED = "hermit_login_failed"     # in a failure's text: it crosses a sandbox's worker and an endpoint as text


def login_failure(exc: BaseException) -> str | None:
    """What a failure says of a lost sign-in, or None: an endpoint error's body names it (`code`; the client's text
    keeps only the start of the body), a sandbox's worker sends it as text."""
    body = getattr(exc, "body", None)
    if isinstance(body, (bytes, bytearray)):
        try:
            error = json.loads(body).get("error")
        except (ValueError, AttributeError):
            error = None
        if isinstance(error, dict) and error.get("code") == LOGIN_FAILED:
            return str(error.get("message") or "the sign-in cannot serve")
    text = str(exc)
    return text.split(f" [{LOGIN_FAILED}]")[0] if LOGIN_FAILED in text else None


class LoginFailed(SystemExit):
    """A subscription's sign-in cannot serve (no token, an expired or revoked one, a held account): no session will run
    until the user restores it, and the tasks still to come would each fail the same way. The run ends here, unfinished
    (`hermit train` resumes after the last step). A SystemExit: a method's or a benchmark's `except Exception` around a
    model call never swallows it, and the command line ends with its message."""

    def __init__(self, why: str):
        super().__init__(f"{why} [{LOGIN_FAILED}]")


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

    def instructed(self, system: str, prompt: str) -> tuple["BaseAgent", str]:
        """(this agent for a call that takes `system` as its instructions, the prompt to give it): where a caller's
        instructions go is the agent's own (Codex's developer instructions, Claude Code's system prompt)."""
        raise NotImplementedError(f"the {self.name or type(self).__name__} Base Agent takes no instructions apart from "
                                  "its prompt")


def make(name: str, settings: dict | None = None, model: Any = None) -> BaseAgent:
    """A Base Agent by name: direct, react, codex or claude (claude-code)."""
    if name == "direct":
        from hermit.agent.base.direct import Direct
        return Direct(settings, model)
    if name == "react":
        from hermit.agent.base.react import React
        return React(settings, model)
    from hermit.agent.base.cli import CLIS
    if name in CLIS:
        return CLIS[name](settings, model)
    known = ["direct", "react", *dict.fromkeys(c.backend for c in CLIS.values())]
    raise ValueError(f"no Base Agent {name!r}: {', '.join(known[:-1])} or {known[-1]}")


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
