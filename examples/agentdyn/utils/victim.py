"""OpenAI-compatible victims for AgentDyn, using Hermit's model backend settings.

An api_script is parsed, never imported or executed: only literal OpenAI client
settings (or simple environment-variable lookups) are read. Keys remain in memory.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path
import time


def terminal_protocol_error(content, has_tool_calls=False):
    """Recognize non-terminal protocol output without executing textual calls."""
    if has_tool_calls:
        return None
    if isinstance(content, list):
        content = '\n'.join(part.get('text', part.get('content', '')) for part in content
                            if isinstance(part, dict) and isinstance(part.get('text', part.get('content')), str))
    text = content if isinstance(content, str) else ''
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    if text.startswith('<think>'):
        return 'unfinished_reasoning'
    if not text:
        return 'missing_terminal_answer'
    # A reply that opens with a tool call written as text (in any of the formats models use) is not a final answer.
    if re.match(r'<(?:TOOLCALL|tool_call)>|<function=', text):
        return 'unparsed_textual_tool_call'
    return None


def script_settings(path: Path) -> dict:
    tree = ast.parse(path.expanduser().read_text())
    values = {}

    def read(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name) and node.id in values:
            return values[node.id]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and ast.unparse(node.func) in {"os.getenv", "os.environ.get"}):
            args = [read(arg) for arg in node.args]
            return os.getenv(*args)
        if isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
            return os.environ[read(node.slice)]
        raise ValueError("api_script settings must be literals or environment-variable references")

    for node in tree.body:
        if isinstance(node, ast.Assign):
            try:
                value = read(node.value)
            except ValueError:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = value
    clients = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
               and ast.unparse(n.func) in {"OpenAI", "openai.OpenAI"}]
    if len(clients) != 1:
        raise ValueError("api_script must contain exactly one OpenAI(...) client")
    return {kw.arg: read(kw.value) for kw in clients[0].keywords if kw.arg in {"base_url", "api_key"}}


def model_params(params: dict) -> dict:
    """Adapt a model spec resolved by Config to the victim's existing client settings."""
    if not isinstance(params.get("model"), dict):
        return params
    from hermit.agent.model.backends import Binding
    binding = Binding.from_spec(params["model"])
    return {**params, "model": binding.model, "base_url": binding.base_url,
            "api_key_env": binding.api_key_env, "key_required": binding.backend.key_required,
            "price": params.get("price") if params.get("price") is not None else binding.price,
            "extra_body": {**binding.extra_body, **(params.get("extra_body") or {})}}


def empty_usage():
    """Additive episode totals; cost_usd is the known subtotal, not a price estimate.

    Unknown counters keep incomplete totals from being reported as exact episode usage/cost.
    """
    return {"calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "cost_usd": 0.0, "unknown_usage_calls": 0, "unknown_cost_calls": 0}


def record_usage(total, result=None):
    """Accumulate the shared Base Agent Usage, before parsing the task's response.

    ChatClient defaults missing provider usage to zeros. Inspect only its presence/completeness in the raw
    response so those defaults never become a claim of known zero usage; all amounts come from Result.usage.
    """
    usages = result.usage if result is not None else []
    calls = max(1, sum(u.calls for u in usages))
    raw = result.extra.get("message", {}).get("_response", {}) if result is not None else {}
    provider_usage = raw.get("usage") if isinstance(raw, dict) else None
    known = bool(usages) and isinstance(provider_usage, dict) and all(
        type(provider_usage.get(k)) is int and provider_usage[k] >= 0
        for k in ("prompt_tokens", "completion_tokens"))
    total["calls"] += calls
    if not known:
        total["unknown_usage_calls"] += calls
    for usage in usages:
        for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
            total[key] += getattr(usage, key)
        if known and usage.cost_usd is not None:
            total["cost_usd"] += usage.cost_usd
    if not known:
        total["unknown_cost_calls"] += calls
    else:
        total["unknown_cost_calls"] += sum(max(u.calls, 1) for u in usages if u.cost_usd is None)


def make_victim(params: dict, base: Path, on_call=None):
    from openai.types.chat import ChatCompletion
    from hermit.agent.base.interface import make
    from hermit.agent.model.llm import ChatClient
    from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
    from agentdojo.agent_pipeline.llms.openai_llm import (
        _function_to_openai, _message_to_openai, _openai_to_assistant_message,
    )

    if params.get("base", "direct") != "direct":
        raise ValueError("AgentDyn uses agent.base: direct for each native model turn")
    params = model_params(params)
    settings = script_settings(base / Path(params["api_script"]).expanduser()) if params.get("api_script") else {}
    if params.get("base_url"):
        settings["base_url"] = params["base_url"]
    if params.get("api_key_env"):
        settings["api_key"] = os.environ.get(params["api_key_env"])
    if not settings.get("base_url") or (params.get("key_required", True) and not settings.get("api_key")):
        raise ValueError("victim needs a configured model backend, or base_url and api_key_env (or api_script)")
    client = ChatClient(settings["base_url"], params["model"], price=params.get("price"),
                        timeout=float(params.get("request_timeout_s", 120)),
                        max_retries=int(params.get("max_retries", 1)), extra_body=params.get("extra_body"))
    agent = make("direct", {}, client)
    # A key that is optional and not set sends no Authorization header, as the shared client does.
    headers = {"Authorization": "Bearer " + settings["api_key"]} if settings.get("api_key") else {}

    class CompatibleVictim(BasePipelineElement):
        name = params["model"]

        def __init__(self):
            self.usage = empty_usage()

        def close(self):
            pass  # ChatClient closes the connection after every call.

        def query(self, query, runtime, env, messages=(), extra_args=None):
            request = {"model": self.name, "messages": [_message_to_openai(m, self.name) for m in messages],
                       "temperature": float(params.get("temperature", 0.0)),
                       "max_tokens": int(params.get("max_tokens", 4096))}
            tools = [_function_to_openai(tool) for tool in runtime.functions.values()]
            if tools:
                request.update(tools=tools, tool_choice="auto")
            if params.get("top_p") is not None:
                request["top_p"] = float(params["top_p"])
            started = time.monotonic()
            recorded = False
            try:
                history = request.pop("messages")
                offered = request.pop("tools", ())
                choice = request.pop("tool_choice", None)
                result = agent.configured(tool_choice=choice, request={**request, "extra_headers": headers,
                                          "return_response": True}).run(history, tools=offered)
                record_usage(self.usage, result)
                recorded = True
                completion = ChatCompletion.model_validate(result.extra["message"]["_response"])
            except Exception as exc:
                # Transport failures have unknown usage; a response parsing failure keeps
                # the shared usage already returned, without counting the call twice.
                if not recorded:
                    record_usage(self.usage)
                if on_call is not None:
                    on_call({"status": "error", "type": type(exc).__name__,
                             "http_status": getattr(exc, "code", None),
                             "usage": dict(self.usage),
                             "wall_s": time.monotonic() - started})
                # Provider errors can include request bodies. Keep credentials and
                # prompts out of exception reports forwarded to the learner.
                raise RuntimeError(f"victim API failed: {type(exc).__name__}, HTTP {getattr(exc, 'code', None)}") from None
            message = completion.choices[0].message if completion.choices else None
            finish = completion.choices[0].finish_reason if completion.choices else None
            protocol_error = terminal_protocol_error(message.content, bool(message.tool_calls)) if message else 'empty_response'
            if not protocol_error and finish == "tool_calls" and not message.tool_calls:
                protocol_error = 'missing_structured_tool_calls'
            output = None
            if not protocol_error:
                output = _openai_to_assistant_message(message)
                # Upstream silently drops malformed tool calls. Reject those episodes
                # instead of mistaking a parser failure for a secure final response.
                if len(output.get("tool_calls") or []) != len(message.tool_calls or []):
                    protocol_error = 'malformed_tool_call_arguments'
            # The call's event is written once its response has been validated.
            if on_call is not None:
                rejected = protocol_error or not completion.choices or finish in {"length", "content_filter"}
                on_call({"status": "error" if rejected else "ok", "call": self.usage["calls"],
                         "protocol_error": protocol_error,
                         "wall_s": time.monotonic() - started, "usage": dict(self.usage),
                         "finish_reason": finish})
            if not completion.choices or finish in {"length", "content_filter"}:
                raise RuntimeError("victim response was empty, truncated or filtered")
            if protocol_error == 'malformed_tool_call_arguments':
                raise RuntimeError("victim returned malformed tool-call arguments")
            if protocol_error:
                raise RuntimeError(f"victim terminal protocol error: {protocol_error}")
            return query, runtime, env, [*messages, output], extra_args or {}

    return CompatibleVictim()
