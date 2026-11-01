"""`react`: the framework's tool loop (docs/framework/agent-layers.md#layer-2-base-agent). It calls the model with
the tools, runs the calls the model asks for, gives it the results, and repeats until the model answers without a
tool call.

Settings: `max_turns` (model calls, default 24), `max_output_tokens` (stop after this many output tokens in total),
`max_tokens` (per reply, default 16384), `temperature`, `tool_result_chars` (a tool result's text is cut there,
default 24000). The model is as for `direct`.
"""
from __future__ import annotations

import json
import time
from typing import Any, Sequence

from hermit.agent.base.direct import client_of, message_steps
from hermit.agent.base.interface import BaseAgent, Limits, Result, Unsupported, as_messages
from hermit.agent.base.tool import Tool
from hermit.agent.base.trajectory import Step


class React(BaseAgent):
    name = "react"

    def run(self, prompt, *, workspace=None, tools: Sequence[Tool] = (), limits: Limits | None = None,
            session: Any = None, roles: Sequence[dict] = ()) -> Result:
        if workspace is not None:
            raise Unsupported("react has no workspace: give it tools")
        if roles:
            raise Unsupported("react has no sub-agents")
        if limits is not None and limits.timeout_s:
            raise Unsupported("react cannot stop a model call at a time limit: limit it by max_turns")
        s = self.settings
        client = client_of(self.model)            # a model spec: a fresh client per run
        by_name: dict[str, Tool] = {}
        for t in tools:                           # the first of a name answers, as the task's tools come first
            by_name.setdefault(t.name, t)
        specs = [t.openai_spec() for t in tools]
        max_turns, max_output = int(s.get("max_turns", 24)), int(s.get("max_output_tokens", 0)) or None
        result_chars = int(s.get("tool_result_chars", 24000))
        chat_kwargs = {"max_tokens": int(s.get("max_tokens", 16384)),
                       **({"temperature": s["temperature"]} if s.get("temperature") is not None else {})}
        messages = [*(session or []), *as_messages(prompt)]
        steps: list[Step] = []
        usages = []                               # this run's own calls (a client may be shared)
        model_s, answer, turns, calls_made, finish = 0.0, "", 0, 0, None
        for _ in range(max_turns):
            t = time.time()
            message, usage = client.chat(messages, **({"tools": specs, "tool_choice": "auto"} if specs else {}),
                                          **chat_kwargs)
            model_s += time.time() - t
            usages.append(usage)
            turns += 1
            finish = message.get("finish_reason")
            messages.append({k: v for k, v in message.items() if k in ("role", "content", "tool_calls")})
            steps += message_steps(message, calls=False)
            calls = message.get("tool_calls") or []
            if not calls:
                answer = message.get("content") or ""
                break
            for call in calls:
                calls_made += 1
                fn = call.get("function") or {}
                name, arguments = fn.get("name", ""), fn.get("arguments") or "{}"
                tool = by_name.get(name)
                content = tool.answer(arguments, result_chars) if tool is not None else f"Error: unknown tool {name}"
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": content})
                steps.append(Step("tool_call", json.dumps({"tool": name, "arguments": arguments}, ensure_ascii=False),
                                  output=content))
            if max_output and sum(u.output_tokens for u in usages) >= max_output:
                break
        return Result(reply=answer, trajectory=steps, usage=[_total(client, usages)], session=messages,
                      finish_reason=finish, extra={"turns": turns, "tool_calls": calls_made, "model_s": model_s})


def _total(client, usages: list):
    """The usage of this run: the sum of its calls' usage."""
    from hermit.agent.model.usage import Usage
    costs = [u.cost_usd for u in usages]
    return Usage(model=getattr(client, "model", ""), input_tokens=sum(u.input_tokens for u in usages),
                 cached_input_tokens=sum(u.cached_input_tokens for u in usages),
                 output_tokens=sum(u.output_tokens for u in usages), calls=sum(u.calls for u in usages),
                 cost_usd=None if not costs or any(c is None for c in costs) else sum(costs))
