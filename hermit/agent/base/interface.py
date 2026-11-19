"""Layer 2's one API (docs/framework/agent-layers.md#layer-2-base-agent).

    agent = make("codex", settings, model)            # a Base Agent: its settings are given here, once
    result = agent.run(prompt, environment=Local(path, Limits(...)), tools=[...], session=None, roles=())
    result.reply, result.trajectory, result.usage, result.session

Layer 3 hands everything for one call through `run`: what to do (`prompt`), where and within what (`environment`:
hermit.environment, its workspace, limits and isolation; a path is a Local environment there), with what
(`tools`), whether to continue an earlier call (`session`), and named sub-agents (`roles`). A Base Agent enforces every
limit with the environment's isolation, or refuses the call (`Unsupported`); it never ignores one. What an agent can be
given of an adapter is its `exports` (`supports`).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Sequence

from hermit.agent.base.tool import Tool
from hermit.agent.base.trajectory import Step
from hermit.environment.base import (Environment, Limits, Mount, Unsupported,  # noqa: F401 (re-exported)
                                           as_environment)
from hermit.agent.model.usage import Usage


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
    def run(self, prompt: str | list[dict], *, environment: Environment | Path | None = None,
            tools: Sequence[Tool] = (), session: Any = None, roles: Sequence[dict] = ()) -> Result:
        """`prompt` is the instruction, or chat messages; `environment` where the agent works (a path: a Local
        there). `roles` are named sub-agents (name, description, instructions) the agent may delegate to, where it
        can."""

    def instructed(self, system: str, prompt: str) -> tuple["BaseAgent", str]:
        """(this agent for a call that takes `system` as its instructions, the prompt to give it): where a caller's
        instructions go is the agent's own (Codex's developer instructions, Claude Code's system prompt)."""
        raise NotImplementedError(f"the {self.name or type(self).__name__} Base Agent takes no instructions apart from "
                                  "its prompt")


def make(name: str, settings: dict | None = None, model: Any = None) -> BaseAgent:
    """A Base Agent by name: react, or a CLI (codex, claude, pi, ...). A single model call is no agent:
    hermit.agent.model.direct.Direct."""
    if name == "react":
        from hermit.agent.base.react import React
        return React(settings, model)
    from hermit.agent.base.cli import CLIS
    if name in CLIS:
        return CLIS[name](settings, model)
    known = ["react", *dict.fromkeys(c.backend for c in CLIS.values())]
    if name == "direct":
        raise ValueError("direct is a single model call, not a Base Agent: hermit.agent.model.direct.Direct")
    raise ValueError(f"no Base Agent {name!r}: {', '.join(known[:-1])} or {known[-1]}")


def configured(agent: BaseAgent, **settings) -> BaseAgent:
    """A copy of `agent` with `settings` over its own (a call's sampling, say); the agent itself is unchanged."""
    import copy
    other = copy.copy(agent)
    other.settings = {**agent.settings, **{k: v for k, v in settings.items() if v is not None}}
    return other


BaseAgent.configured = configured


from hermit.agent.model.direct import as_messages   # noqa: E402,F401 (re-exported)
