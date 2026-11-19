"""`Direct`: one model call, passed through (docs/framework/agent-layers.md#layer-1-model).

The model call for code that decides every step itself: an Evolver whose method is its own code (GEPA, SkillOpt, the
run's model `ctx.agent`), a benchmark's own loop, the framework's ReAct agent. It is not an agent: it works in no
environment and runs nothing. Tools in a call are offered to the model, and the calls it asks for come back in
`Reply.tool_calls`, not run: the caller runs them and calls again with the results and the session. A tool is anything
with `openai_spec()` (hermit.agent.base.tool.Tool), or a tool spec as the chat-completions API takes it (a
benchmark's own, offered as it is).

    reply = Direct({"max_tokens": 1024}, model).run(messages, tools=[...], session=None)
    reply.reply, reply.tool_calls, reply.usage, reply.session, reply.finish_reason

Settings: the request's sampling (`max_tokens`, `temperature`, `top_p`, `seed`, `stop`, `response_format`),
`tool_choice` (default auto; None sends none), and `request`: more request fields sent as they are (a relay's
whole request; with the model's client, also `extra_headers` and `return_response`). The model is a resolved model
reference (hermit.agent.model.backends), a ChatClient, or anything with the ChatClient's
`chat(messages, **kwargs) -> (message, usage)`.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Sequence

from hermit.agent.model.llm import ChatClient
from hermit.agent.model.usage import Usage

SAMPLING = ("max_tokens", "temperature", "top_p", "seed", "stop", "response_format")


def client_of(model: Any):
    """The model's chat client: a ChatClient (or anything with `chat`) as it is, a model spec as a new ChatClient."""
    if hasattr(model, "chat"):
        return model
    if isinstance(model, dict):
        return ChatClient.from_spec(model)
    raise ValueError("a model is a resolved model reference (`<backend>/<model>`) or a chat client")


def as_messages(prompt: str | list[dict]) -> list[dict]:
    """A prompt as chat messages: a string is one user message."""
    return list(prompt) if isinstance(prompt, list) else [{"role": "user", "content": prompt}]


@dataclass
class Reply:
    reply: str                                         # the message's text
    usage: list[Usage] = field(default_factory=list)   # the call's
    session: list[dict] = field(default_factory=list)  # the messages so far, the reply last: pass back to continue
    tool_calls: list[dict] = field(default_factory=list)   # the calls the model asked for, not run
    finish_reason: str | None = None
    reasoning: str = ""
    extra: dict = field(default_factory=dict)          # {"message": the message as the client returned it}


class Direct:
    name = "direct"

    def __init__(self, settings: dict | None = None, model: Any = None):
        self.settings = dict(settings or {})
        self.model = model
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = client_of(self.model)
        return self._client

    def configured(self, **settings) -> "Direct":
        """A copy with `settings` over its own (a call's sampling, say); this one is unchanged. None leaves a setting
        as it is."""
        other = copy.copy(self)
        other.settings = {**self.settings, **{k: v for k, v in settings.items() if v is not None}}
        return other

    def run(self, prompt: str | list[dict], *, tools: Sequence = (), session: list[dict] | None = None) -> Reply:
        messages = [*(session or []), *as_messages(prompt)]
        kwargs = {k: self.settings[k] for k in SAMPLING if self.settings.get(k) is not None}
        kwargs.update(self.settings.get("request") or {})
        if tools:
            kwargs["tools"] = [t.openai_spec() if hasattr(t, "openai_spec") else t for t in tools]
            choice = self.settings.get("tool_choice", "auto")
            if choice is not None:
                kwargs["tool_choice"] = choice
        message, usage = self.client.chat(messages, **kwargs)
        history = messages + [{k: v for k, v in message.items() if k in ("role", "content", "tool_calls")}
                              | {"role": message.get("role") or "assistant"}]
        return Reply(reply=message.get("content") or "", usage=[usage], session=history,
                     tool_calls=list(message.get("tool_calls") or []), finish_reason=message.get("finish_reason"),
                     reasoning=message.get("reasoning_content") or "", extra={"message": message})
