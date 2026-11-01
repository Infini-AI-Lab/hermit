"""Stand-ins for the run's model in Evolver tests."""
from __future__ import annotations

from hermit.agent.base.direct import Direct
from hermit.agent.model.usage import Usage


class FunctionModel:
    """A model whose replies come from `reply(messages=..., **sampling) -> {content, finish_reason}`."""

    def __init__(self, reply, model: str = "scripted"):
        self.reply, self.model = reply, model

    def chat(self, messages, **kwargs):
        out = self.reply(messages=messages, **kwargs) or {}
        message = {"role": "assistant", "content": out.get("content"), "finish_reason": out.get("finish_reason")}
        return message, Usage(self.model, calls=1)


def agent_of(reply) -> Direct:
    """The Evolver's `direct` Base Agent (ctx.agent) over a scripted reply function."""
    return Direct({}, FunctionModel(reply))
