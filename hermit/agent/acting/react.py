"""The `react` agent: a chat-completions tool-calling loop over the task's tools and the adapter's tools.

A task names it as its agent and writes no loop:

    agent:
      name: react
      params:
        model: fireworks/accounts/fireworks/models/glm-5p3-flash   # <backend>/<model id>, see hermit.agent.model.backends;
                                             # or {model: vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8, extra_body: {...}},
                                             # or the endpoint itself: {base_url, model, api_key_env, price}
        prompt: environment/prompt.md        # the system prompt; `{context}` marks where the adapter's context goes
        tools: environment/tools.py          # @tool functions (optional)
        user: "{question}"                   # the first user message, from the task's fields (default: see below)
        max_turns: 24                        # model calls per task
        max_output_tokens: 65536             # stop after this many output tokens in total
        answer_tag: FINAL_ANSWER             # optional: ask once more, without tools, if the tag is missing

The system prompt is the file's text with the adapter's context substituted for `{context}`, or appended under a
heading when the file has no placeholder; with no adapter the placeholder becomes empty. The context is ADAPTER.md,
and below it what the adapter's `context_tool` renders for this task's user message when its manifest declares one
(`Session.context(query=..., task=...)`); that tool's result is recorded in the output's `extra["context_tool"]`. The
user message is `user` formatted with the task's fields, or the task's `prompt`, `question`, `instruction` or
`input` field, in that order. Tool calls the model makes are answered in turn: the task's tools by calling the
function, the adapter's by `Session.respond`. The output is the final assistant message; the whole exchange is the
trajectory (`messages`) and is also written as JSON under `logs` (default `.logs/`, beside the config).

This is Layer 3 over the `react` Base Agent (docs/framework/agent-layers.md): it builds the prompt and the tools for
one task, and Layer 2 runs the loop.
"""
from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path
import tempfile
import time
from typing import Callable

from hermit.adapter.session import Session
from hermit.agent.acting.agent import Agent
from hermit.agent.acting.tools import MARK, as_tools, load_tools, tool   # noqa: F401 (tool: for subclasses)
from hermit.agent.base.deadline import Deadline
from hermit.agent.base.react import React
from hermit.agent.model.usage import Ledger
from hermit.agent.base.tool import Tool
from hermit.agent.model.direct import Direct
from hermit.agent.model.llm import ChatAborted, ChatClient
from hermit.common import RunOutput

CONTEXT_HEADING = "# Instructions from the harness adapter"


def Bounded(client, budget_s: float):
    """A chat client whose every call is given up once the task's whole agent budget is spent (a task's own
    environment's `timeout_s`): the run's one Deadline, bound to the client."""
    from hermit.agent.base.deadline import Deadline
    return Deadline.within(budget_s).bound(client)


USER_FIELDS = ("prompt", "question", "instruction", "input")
_TOOLS: dict[tuple, list] = {}


def _text(value: str, base: Path) -> str:
    """A prompt parameter: the text of a file relative to the config when it names one, else the value itself."""
    path = Path(base) / value
    return path.read_text() if value.endswith((".md", ".txt")) and path.is_file() else value


def _format(template: str, task: dict) -> str:
    class Fields(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    return template.format_map(Fields({k: v for k, v in task.items() if not isinstance(v, (dict, list))}))


def system_prompt(template: str, context: str) -> str:
    context = context.strip()
    if "{context}" in template:
        return template.replace("{context}", context)
    return template.rstrip() + (f"\n\n{CONTEXT_HEADING}\n\n{context}\n" if context else "\n")


def user_message(task: dict, template: str | None) -> str:
    if template:
        return _format(template, task)
    for field in USER_FIELDS:
        if isinstance(task.get(field), str):
            return task[field]
    raise ValueError(f"task {task.get('id')!r} has none of {USER_FIELDS}; set agent.params.user")


def _tools(spec: str | None, base: Path) -> list:
    if not spec:
        return []
    key = (str(base), spec)
    if key not in _TOOLS:
        _TOOLS[key] = load_tools(spec, base)
    return _TOOLS[key]


class ReactAgent(Agent):
    """The `react` acting agent. A benchmark that needs more than these params subclasses it (in its own example
    folder) and overrides only what is its own:

        class DatabaseAgent(ReactAgent):
            @tool("Run one SQL query on the task's database.")
            def run_sql(self, query: str) -> str:          # a tool: a method marked @tool (hermit.agent.acting)
                return self.environment.exec(["sqlite3", "db.sqlite", query]).output

            def system_prompt(self, task, context): ...   # the task-level hooks below
            def before_tool(self, loop, call): ...         # and the loop's (hermit.agent.base.react.React)

    Task-level hooks: `system_prompt`, `user_message`, `tools`, `answer`. Loop hooks: `call_model`, `before_tool`,
    `after_tool`, `stopped` (React's, called with the loop). `environment` is the task's environment when the
    subclass gives one (`environment_for`); the loop reads, writes and runs nothing but its tools."""

    supports = ("context", "context_tool", "tools")   # the adapter exports it takes (hermit.agent.acting.agent)
    environment = None                               # the current task's environment (environment_for), if any

    def run(self, task: dict, adapter: Path | None, environment=None) -> RunOutput:
        return _run(task, adapter, self.params, self.base, agent=self, environment=environment)

    # ---------- task-level hooks ----------
    def system_prompt(self, task: dict, context: str) -> str:
        """The system message: the prompt file with the adapter's context at `{context}` (or appended)."""
        return system_prompt(_text(self.params.get("prompt", ""), self.base), context)

    def user_message(self, task: dict) -> str:
        """The first user message: `user` formatted with the task's fields, else its prompt-like field."""
        return user_message(task, self.params.get("user"))

    def tools(self, task: dict) -> list[Tool]:
        """The task's tools: the `tools` module's @tool functions, then this class's @tool methods."""
        return as_tools(_tools(self.params.get("tools"), self.base), task) + as_tools(method_tools(self), task)

    def answer(self, result, task: dict) -> str:
        """The output: the loop's answer (the answer tag's follow-up, when the params name one, is `_run`'s)."""
        return result.reply

    def environment_for(self, task: dict):
        """The environment the task's tools work in (hermit.environment), or None: the loop needs none. By default the
        call's own (the task's environment, when its task set gives one)."""
        return self.given_environment

    given_environment = None                         # the environment the call was given (run's `environment`)

    # ---------- the loop's hooks (React's), forwarded ----------
    def call_model(self, loop, messages, specs):
        return React.call_model(self._loop_agent, loop, messages, specs)

    def before_tool(self, loop, call):
        return None

    def after_tool(self, loop, call, content):
        return content

    def stopped(self, loop, message):
        return False


class _Loop(React):
    """React with an acting agent's loop hooks: what a ReactAgent subclass overrides reaches the loop."""

    def __init__(self, settings, model, agent: ReactAgent):
        super().__init__(settings, model)
        self.agent = agent

    def call_model(self, loop, messages, specs):
        return self.agent.call_model(loop, messages, specs)

    def before_tool(self, loop, call):
        return self.agent.before_tool(loop, call)

    def after_tool(self, loop, call, content):
        return self.agent.after_tool(loop, call, content)

    def stopped(self, loop, message):
        return self.agent.stopped(loop, message)


def method_tools(agent) -> list[tuple[dict, Callable]]:
    """An agent's @tool methods, bound to it, in definition order (as a tools module's functions)."""
    found = []
    for klass in reversed(type(agent).__mro__):
        for key, fn in vars(klass).items():
            if callable(fn) and hasattr(fn, MARK):
                found = [(s, f) for s, f in found if s["name"] != getattr(fn, MARK)["name"]]   # an override wins
                found.append((getattr(fn, MARK), getattr(agent, key)))
    return found


def run(task: dict, adapter: Path | None, *, params: dict, base: Path) -> RunOutput:
    """The function form (`agent.name: hermit.agent.acting.react:run`); ReactAgent is the class."""
    return ReactAgent(params, base).run(task, adapter)


def _run(task: dict, adapter: Path | None, params: dict, base: Path, agent: ReactAgent,
         environment=None) -> RunOutput:
    model = params.get("model")
    if not isinstance(model, dict) or not model.get("base_url") or not model.get("model"):
        raise ValueError("agent.params.model is required: a `<backend>/<model>` reference, or {base_url, model, "
                         "api_key_env, price} (the run's `model` is the default)")
    client = ChatClient.from_spec(model)          # one client for the loop and the answer-tag call: its total is the run's
    agent = copy.copy(agent)                      # this task's agent: its environment, its tools' self, its loop
    agent.given_environment = environment
    agent.environment = agent.environment_for(task)
    session = Session(adapter) if adapter is not None else None
    tools = agent.tools(task) + (session.as_tools() if session else [])
    settings = {k: params[k] for k in ("max_turns", "max_output_tokens", "max_tokens", "tool_result_chars",
                                       "temperature") if params.get(k) is not None}
    tag = params.get("answer_tag")

    user = agent.user_message(task)
    # The adapter's context for this task: ADAPTER.md, plus what its context tool renders for the task's input (an
    # adapter without one gets the static context).
    context = session.context(query=user, task=task) if session else ""
    messages = [{"role": "system", "content": agent.system_prompt(task, context)},
                {"role": "user", "content": user}]
    started = time.time()
    loop_agent = agent._loop_agent = _Loop(settings, client, agent)
    # The loop cannot stop a model call at a time limit (react refuses one): a task's environment that has its own
    # budget (a Harbor task's) bounds each of its commands, and the whole of the loop's model calls (Bounded: the call
    # in flight given up once it is spent, as Harbor's agent timeout cancels its agents), whatever runs the pipeline.
    place = agent.environment
    bounded = None
    if place is not None and getattr(place, "limits", None) is not None and place.limits.timeout_s:
        at = getattr(place, "deadline_at", None)         # the phase's own absolute deadline, when it has one
        bounded = Deadline(at).bound(client) if at else Bounded(client, place.limits.timeout_s)
        loop_agent.model = bounded
        place = place.with_limits(dataclasses.replace(place.limits, timeout_s=None))
    loop = loop_agent.start(messages, environment=place, tools=tools)
    timed_out = False
    try:
        while not loop.done:
            if bounded is not None and bounded.expired:      # spent between steps (a tool ran it out)
                timed_out = True
                break
            loop_agent.step(loop)
    except ChatAborted:
        if bounded is None or not bounded.expired:
            raise
        timed_out = True                                     # the call in flight given up: what ran before is kept
    result = loop.result()
    if timed_out:
        result = dataclasses.replace(result, timed_out=True)
    messages, turns, usages = result.session, result.extra["turns"], list(result.usage)
    try:
        if tag and not timed_out and f"<{tag}>".upper() not in (result.reply or "").upper():   # the reply's tag
            follow = Direct({"max_tokens": int(params.get("max_tokens", 16384)),
                             "temperature": params.get("temperature")}, bounded or client)
            try:
                reply = follow.run(f"Stop using tools. Give your final answer now inside <{tag}>...</{tag}>.",
                                   session=messages)
                messages = reply.session[:-1] + [{"role": "assistant", "content": reply.reply}]
                result = dataclasses.replace(result, reply=reply.reply, session=messages)
                usages += reply.usage
                turns += 1
            except ChatAborted:
                if bounded is None or not bounded.expired:
                    raise
                result = dataclasses.replace(result, timed_out=True)
    finally:
        if bounded is not None:
            bounded.close()
    ledger = Ledger(getattr(client, "model", "")).add(usages)
    if bounded is not None:           # a request given up in flight may have been billed: its cost is not known
        ledger.unreported(bounded.abandoned)
    answer = agent.answer(result, task)

    logs = Path(base) / params.get("logs", ".logs")
    logs.mkdir(parents=True, exist_ok=True)
    log = Path(tempfile.mkstemp(prefix=f"{task.get('id', 'task')}-", suffix=".json", dir=logs)[1])
    log.write_text(json.dumps({"task": task.get("id"), "model": model["model"], "adapter": str(adapter) if adapter else None,
                               "messages": messages}, ensure_ascii=False, indent=1))
    # What this run's calls used, as call_model reported it (a subclass may call another client) and the follow-up's
    total, model_s = ledger.total(), result.extra["model_s"]
    extra = {"turns": turns, "tool_calls": result.extra["tool_calls"],
             **({"timed_out": True} if result.timed_out else {})}     # the task's agent budget ran out
    if session is not None and session.last_context is not None:
        extra["context_tool"] = session.last_context         # what the adapter's context tool rendered for this task
    return RunOutput(output=answer, log=log, cost_usd=total.cost_usd, wall_s=round(time.time() - started, 1),
                     api_speed={"output_tokens_per_s": round(total.output_tokens / model_s, 1) if model_s else None,
                                "model_calls": total.calls},
                     messages=messages, tokens=total.input_tokens + total.output_tokens, extra=extra)
