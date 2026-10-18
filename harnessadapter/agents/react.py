"""The `react` agent: a chat-completions tool-calling loop over the task's tools and the adapter's tools.

A task names it as its agent and writes no loop:

    agent:
      name: react
      params:
        model: fireworks/accounts/fireworks/models/glm-5p3-flash   # <backend>/<model id>, see harnessadapter.backends;
                                             # or {model: vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8, extra_body: {...}},
                                             # or the endpoint itself: {base_url, model, api_key_env, price}
        prompt: environment/prompt.md        # the system prompt; `{context}` marks where the adapter's context goes
        tools: environment/tools.py          # @tool functions (optional)
        user: "{question}"                   # the first user message, from the task's fields (default: see below)
        max_turns: 24                        # model calls per task
        max_output_tokens: 65536             # stop after this many output tokens in total
        answer_tag: FINAL_ANSWER             # optional: ask once more, without tools, if the tag is missing

The system prompt is the file's text with the adapter's context (ADAPTER.md) substituted for `{context}`, or
appended under a heading when the file has no placeholder; with no adapter the placeholder becomes empty. The
user message is `user` formatted with the task's fields, or the task's `prompt`, `question`, `instruction` or
`input` field, in that order. Tool calls the model makes are answered in turn: the task's tools by calling the
function, the adapter's by `Session.respond`. The output is the final assistant message; the whole exchange is the
trajectory (`messages`) and is also written as JSON under `logs` (default `.logs/`, beside the config).
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time

from harnessadapter.adapter.session import Session
from harnessadapter.agents.base import Agent
from harnessadapter.agents.tools import invoke, load_tools, openai_specs
from harnessadapter.llm import ChatClient
from harnessadapter.task import RunOutput

CONTEXT_HEADING = "# Instructions from the harness adapter"
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
    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        return _run(task, adapter, self.params, self.base)


def run(task: dict, adapter: Path | None, *, params: dict, base: Path) -> RunOutput:
    """The function form (`agent.name: harnessadapter.agents.react:run`); ReactAgent is the class."""
    return _run(task, adapter, params, base)


def _run(task: dict, adapter: Path | None, params: dict, base: Path) -> RunOutput:
    model = params.get("model")
    if not isinstance(model, dict) or not model.get("base_url") or not model.get("model"):
        raise ValueError("agent.params.model is required: a `<backend>/<model>` reference, or {base_url, model, "
                         "api_key_env, price} (the run's `model` is the default)")
    client = ChatClient.from_spec(model)
    session = Session(adapter) if adapter is not None else None
    task_tools = _tools(params.get("tools"), base)
    functions = {spec["name"]: fn for spec, fn in task_tools}
    tools = openai_specs(task_tools) + (session.tool_specs("openai") if session else [])
    max_turns = int(params.get("max_turns", 24))
    max_output = int(params.get("max_output_tokens", 0)) or None
    max_tokens = int(params.get("max_tokens", 16384))
    result_chars = int(params.get("tool_result_chars", 24000))
    temperature = params.get("temperature")
    chat_kwargs = {"max_tokens": max_tokens, **({"temperature": temperature} if temperature is not None else {})}
    tag = params.get("answer_tag")

    messages = [{"role": "system", "content": system_prompt(_text(params.get("prompt", ""), base),
                                                            session.context() if session else "")},
                {"role": "user", "content": user_message(task, params.get("user"))}]
    started, model_s, answer, turns, tool_calls = time.time(), 0.0, "", 0, 0
    for _ in range(max_turns):
        t = time.time()
        message, usage = client.chat(messages, **({"tools": tools, "tool_choice": "auto"} if tools else {}), **chat_kwargs)
        model_s += time.time() - t
        turns += 1
        messages.append({k: v for k, v in message.items() if k in ("role", "content", "tool_calls")})
        calls = message.get("tool_calls") or []
        if not calls:
            answer = message.get("content") or ""
            break
        for call in calls:
            tool_calls += 1
            name = (call.get("function") or {}).get("name", "")
            if name in functions:
                try:
                    arguments = json.loads((call["function"].get("arguments") or "{}"))
                    content = json.dumps(invoke(functions[name], arguments, task), ensure_ascii=False)
                except Exception as exc:
                    content = f"Error: {type(exc).__name__}: {exc}"
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": content[:result_chars]})
            elif session is not None and session.has_tool(name):
                messages.append(session.respond(call, max_chars=result_chars))
            else:
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": f"Error: unknown tool {name}"})
        if max_output and client.total.output_tokens >= max_output:
            break
    if tag and f"<{tag}>".upper() not in answer.upper():
        messages.append({"role": "user", "content": f"Stop using tools. Give your final answer now inside <{tag}>...</{tag}>."})
        message, _ = client.chat(messages, **chat_kwargs)
        answer = message.get("content") or ""
        messages.append({"role": "assistant", "content": answer})
        turns += 1

    logs = Path(base) / params.get("logs", ".logs")
    logs.mkdir(parents=True, exist_ok=True)
    log = Path(tempfile.mkstemp(prefix=f"{task.get('id', 'task')}-", suffix=".json", dir=logs)[1])
    log.write_text(json.dumps({"task": task.get("id"), "model": model["model"], "adapter": str(adapter) if adapter else None,
                               "messages": messages}, ensure_ascii=False, indent=1))
    total = client.total
    return RunOutput(output=answer, log=log, cost_usd=total.cost_usd, wall_s=round(time.time() - started, 1),
                     api_speed={"output_tokens_per_s": round(total.output_tokens / model_s, 1) if model_s else None,
                                "model_calls": total.calls},
                     messages=messages, tokens=total.input_tokens + total.output_tokens,
                     extra={"turns": turns, "tool_calls": tool_calls})
