"""`direct`: one model call, passed through (docs/framework/agent-layers.md#layer-2-base-agent).

The Base Agent for code that decides every step itself: an Evolver whose method is its own code (GEPA, SkillOpt), a
benchmark's own loop. Tools in a call are offered to the model, and the calls it asks for come back in
`Result.tool_calls`, not run: the caller runs them and calls `direct` again with the results and the session. A tool
is a Tool, or a tool spec as the chat-completions API takes it (a benchmark's own, offered as it is).

Settings: the request's sampling (`max_tokens`, `temperature`, `top_p`, `seed`, `stop`, `response_format`),
`tool_choice` (default auto; None sends none), and `request`: more request fields sent as they are (a relay's
whole request; with the model's client, also `extra_headers` and `return_response`). The model is a resolved model reference (hermit.agent.model.backends), a ChatClient, or anything with
the ChatClient's `chat(messages, **kwargs) -> (message, usage)`.
"""
from __future__ import annotations

import json
from typing import Any, Sequence

from hermit.agent.base.interface import BaseAgent, Limits, Result, Unsupported, as_messages
from hermit.agent.base.tool import Tool
from hermit.agent.base.trajectory import Step
from hermit.agent.model.llm import ChatClient

SAMPLING = ("max_tokens", "temperature", "top_p", "seed", "stop", "response_format")


def client_of(model: Any):
    """The model's chat client: a ChatClient (or anything with `chat`) as it is, a model spec as a new ChatClient."""
    if hasattr(model, "chat"):
        return model
    if isinstance(model, dict):
        return ChatClient.from_spec(model)
    raise ValueError("a model is a resolved model reference (`<backend>/<model>`) or a chat client")


def message_steps(message: dict, calls: bool = True) -> list[Step]:
    """One assistant message as steps: its reasoning, its text and (with `calls`) its tool calls."""
    steps = []
    if str(message.get("reasoning_content") or "").strip():
        steps.append(Step("reasoning", str(message["reasoning_content"])))
    if str(message.get("content") or "").strip():
        steps.append(Step("message", str(message["content"])))
    for call in (message.get("tool_calls") or []) if calls else []:
        fn = call.get("function") or {}
        steps.append(Step("tool_call", json.dumps({"tool": fn.get("name"), "arguments": fn.get("arguments")},
                                                  ensure_ascii=False)))
    return steps


class Direct(BaseAgent):
    name = "direct"

    def __init__(self, settings: dict | None = None, model: Any = None):
        super().__init__(settings, model)
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = client_of(self.model)
        return self._client

    def run(self, prompt, *, workspace=None, tools: Sequence[Tool] = (), limits: Limits | None = None,
            session: Any = None, roles: Sequence[dict] = ()) -> Result:
        if workspace is not None:
            raise Unsupported("direct makes one model call: it has no workspace")
        if roles:
            raise Unsupported("direct makes one model call: it has no sub-agents")
        if limits is not None and limits.timeout_s:
            raise Unsupported("direct cannot stop a model call at a time limit: the client's own timeout applies")
        # direct reads, writes and connects to nothing but the model, so every other limit holds.
        messages = [*(session or []), *as_messages(prompt)]
        kwargs = {k: self.settings[k] for k in SAMPLING if self.settings.get(k) is not None}
        kwargs.update(self.settings.get("request") or {})
        if tools:
            kwargs["tools"] = [t.openai_spec() if isinstance(t, Tool) else t for t in tools]
            choice = self.settings.get("tool_choice", "auto")
            if choice is not None:
                kwargs["tool_choice"] = choice
        message, usage = self.client.chat(messages, **kwargs)
        history = messages + [{k: v for k, v in message.items() if k in ("role", "content", "tool_calls")}
                              | {"role": message.get("role") or "assistant"}]
        return Result(reply=message.get("content") or "", trajectory=message_steps(message), usage=[usage],
                      session=history, tool_calls=list(message.get("tool_calls") or []),
                      finish_reason=message.get("finish_reason"), reasoning=message.get("reasoning_content") or "",
                      extra={"message": message})
