"""`react`: the framework's tool loop (docs/framework/agent-layers.md#layer-2-base-agent). It calls the model with
the tools, runs the calls the model asks for, gives it the results, and repeats until the model answers without a
tool call.

    result = make("react", settings, model).run(messages, environment=env, tools=[...])

    loop = agent.start(messages, environment=env, tools=[...], external=["submit"])   # driven step by step:
    while not loop.done:                          # a benchmark's own loop that runs some calls itself
        agent.step(loop)
        for call in list(loop.pending):           # calls of `external` tools wait for their results
            agent.give(loop, call["id"], run_it_myself(call))
    result = loop.result()

Settings: `max_turns` (model calls, default 24), `max_output_tokens` (stop after this many output tokens in total),
`max_tokens` (per reply, default 16384), `temperature`, `tool_result_chars` (a tool result's text is cut there,
default 24000). The model is as for Direct (hermit.agent.model.direct): a model reference, or a chat client.

A subclass changes the loop through its hooks, never by copying it: `call_model` (one request), `before_tool` (an
answer instead of running a call: a cap reached, a call refused), `after_tool` (a call's result as the model reads
it), `stopped` (an end besides an answer). An external call goes through the same hooks: `before_tool` before it
waits, `after_tool` (and `tool_result_chars`) on the result the driver gives; the calls after it in the same message
wait with it, so that every call runs in the order the model asked, and the turn's ends (`stopped`, the caps) are
judged once all of them have results. The environment, when the call gives
one, is the tools' to use (`loop.environment`): the loop itself reads, writes and runs nothing but the tools it is
given.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from typing import Any, Sequence

from hermit.agent.base.interface import BaseAgent, Result, Unsupported, as_environment, as_messages
from hermit.agent.base.tool import Tool
from hermit.agent.base.trajectory import Step
from hermit.agent.model.direct import client_of


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


@dataclass
class Loop:
    """One run of the loop, so far: what `step` advances and `result` reads."""
    messages: list[dict]
    tools: dict[str, Tool]
    specs: list[dict]
    client: Any
    environment: Any = None
    external: frozenset = frozenset()       # tool names the driver runs: their calls wait in `pending`
    steps: list[Step] = field(default_factory=list)
    usages: list = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)
    queued: list[dict] = field(default_factory=list)   # the message's calls after a pending one, run once it is given
    message: dict | None = None             # the model's last message (what `stopped` reads after its calls)
    answer: str = ""
    finish: str | None = None
    turns: int = 0
    calls_made: int = 0
    model_s: float = 0.0
    done: bool = False
    extra: dict = field(default_factory=dict)   # a subclass's own records of the run

    def answered(self, call: dict, content: str) -> None:
        """A call's result, as the model reads it next step (React.step and React.give write it)."""
        fn = call.get("function") or {}
        self.messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": content})
        self.steps.append(Step("tool_call", json.dumps({"tool": fn.get("name", ""), "arguments":
                                                        fn.get("arguments") or "{}"}, ensure_ascii=False),
                               output=content))

    def result(self) -> Result:
        return Result(reply=self.answer, trajectory=self.steps, usage=[_total(self.client, self.usages)],
                      session=self.messages, finish_reason=self.finish,
                      extra={"turns": self.turns, "tool_calls": self.calls_made, "model_s": self.model_s,
                             **self.extra})


class React(BaseAgent):
    name = "react"

    def run(self, prompt, *, environment=None, tools: Sequence[Tool] = (), session: Any = None,
            roles: Sequence[dict] = ()) -> Result:
        loop = self.start(prompt, environment=environment, tools=tools, session=session, roles=roles)
        while not loop.done:
            self.step(loop)
        return loop.result()

    def start(self, prompt, *, environment=None, tools: Sequence[Tool] = (), session: Any = None,
              roles: Sequence[dict] = (), external: Sequence[str] = ()) -> Loop:
        """A run of the loop, not yet stepped. Calls of `external` tools are offered to the model but not run: they
        wait in `loop.pending` for the driver's `give`."""
        if roles:
            raise Unsupported("react has no sub-agents")
        env = as_environment(environment)
        if env is not None and env.limits.timeout_s:
            raise Unsupported("react cannot stop a model call at a time limit: limit it by max_turns")
        by_name: dict[str, Tool] = {}
        for t in tools:                           # the first of a name answers, as the task's tools come first
            by_name.setdefault(t.name, t)
        return Loop(messages=[*(session or []), *as_messages(prompt)], tools=by_name,
                    specs=[t.openai_spec() for t in tools], client=client_of(self.model),   # a spec: a fresh client
                    environment=env, external=frozenset(external))

    def step(self, loop: Loop) -> Loop:
        """One model call and the calls it asks for. Ends the loop on an answer without a call, at `max_turns`, at
        `max_output_tokens`, or when `stopped` says so; waits (returns with `loop.pending`) on a call of an external
        tool."""
        if loop.done or loop.pending:
            return loop
        s = self.settings
        if loop.turns >= int(s.get("max_turns", 24)):
            loop.done = True
            return loop
        t = time.time()
        message, usage = self.call_model(loop, loop.messages, loop.specs)
        loop.model_s += time.time() - t
        loop.usages.append(usage)
        loop.turns += 1
        loop.finish = message.get("finish_reason")
        loop.messages.append({k: v for k, v in message.items() if k in ("role", "content", "tool_calls")})
        loop.steps += message_steps(message, calls=False)
        calls = message.get("tool_calls") or []
        if not calls:
            loop.answer = message.get("content") or ""
            loop.done = True
            return loop
        loop.message, loop.queued = message, list(calls)
        self._calls(loop)
        return loop

    def _calls(self, loop: Loop) -> None:
        """Run the message's calls in their order: up to a call of an external tool, which waits for `give` (the
        calls after it wait with it), else to the end of the turn."""
        result_chars = int(self.settings.get("tool_result_chars", 24000))
        while loop.queued:
            call = loop.queued.pop(0)
            loop.calls_made += 1
            fn = call.get("function") or {}
            name, arguments = fn.get("name", ""), fn.get("arguments") or "{}"
            content = self.before_tool(loop, call)
            if content is None and name in loop.external:
                loop.pending.append(call)               # the driver runs it: `give` brings its result back
                return
            if content is None:
                tool = loop.tools.get(name)
                content = tool.answer(arguments, result_chars) if tool is not None else f"Error: unknown tool {name}"
                content = self.after_tool(loop, call, content)
            loop.answered(call, content)
        self._end_turn(loop)

    def _end_turn(self, loop: Loop) -> None:
        """After every call of the message has its result: the ends besides an answer."""
        s = self.settings
        max_output = int(s.get("max_output_tokens", 0)) or None
        if max_output and sum(u.output_tokens for u in loop.usages) >= max_output:
            loop.done = True
        elif self.stopped(loop, loop.message):
            loop.done = True
        elif loop.turns >= int(s.get("max_turns", 24)):
            loop.done = True

    def give(self, loop: Loop, call_id: str, content) -> None:
        """The result of a pending call (an `external` tool's), which the driver ran: cut at `tool_result_chars` and
        passed through `after_tool`, as a call the loop ran itself; the message's later calls then run, and the
        turn ends as one without external calls would (`stopped`, the caps). The model reads it next step."""
        for i, call in enumerate(loop.pending):
            if call.get("id") == call_id:
                del loop.pending[i]
                text = str(content)[:int(self.settings.get("tool_result_chars", 24000))]
                loop.answered(call, self.after_tool(loop, call, text))
                if not loop.pending:
                    self._calls(loop)                   # the calls after it, then the end of the turn
                return
        raise ValueError(f"no pending call {call_id!r}")

    # ---------- the hooks a subclass overrides ----------
    def call_model(self, loop: Loop, messages: list[dict], specs: list[dict]) -> tuple[dict, Any]:
        """One request: (the model's message, its usage)."""
        s = self.settings
        kwargs = {"max_tokens": int(s.get("max_tokens", 16384)),
                  **({"temperature": s["temperature"]} if s.get("temperature") is not None else {})}
        return loop.client.chat(messages, **({"tools": specs, "tool_choice": "auto"} if specs else {}), **kwargs)

    def before_tool(self, loop: Loop, call: dict) -> str | None:
        """What the model reads for `call` instead of running it (a cap reached, a call refused), or None: run it."""
        return None

    def after_tool(self, loop: Loop, call: dict, content: str) -> str:
        """A call's result as the model reads it."""
        return content

    def stopped(self, loop: Loop, message: dict) -> bool:
        """Whether the run ends after this message's calls, besides an answer, max_turns and max_output_tokens."""
        return False


def _total(client, usages: list):
    """The usage of this run: the sum of its calls' usage."""
    from hermit.agent.model.usage import Usage
    costs = [u.cost_usd for u in usages]
    return Usage(model=getattr(client, "model", ""), input_tokens=sum(u.input_tokens for u in usages),
                 cached_input_tokens=sum(u.cached_input_tokens for u in usages),
                 output_tokens=sum(u.output_tokens for u in usages), calls=sum(u.calls for u in usages),
                 cost_usd=None if not costs or any(c is None for c in costs) else sum(costs))
