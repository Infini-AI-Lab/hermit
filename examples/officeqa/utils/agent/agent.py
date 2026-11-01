"""OfficeQA as in the SkillOpt paper, run by SkillOpt's own rollout (skillopt.envs.officeqa.rollout.process_one) in
its offline mode, direct chat: the question's oracle parsed pages in the prompt, glob/read/grep tools limited to the
question's source files, at most 24 turns of up to 16,384 tokens, the answer inside <answer>...</answer>. The Codex
harness is the framework's Codex agent (`agent.base: codex`) with ../task.py's task functions.

The adapter's context is the skill: ADAPTER.md and, below it, what the adapter's context tool renders for the question
when its manifest declares one (ReHarness's retrieved reflections; Session.context(query=question, task=row), whose
whole result is recorded in the output's extra["context_tool"]). adapter=None runs without one. The run's SkillOpt
prediction files (conversation.json, target_*_prompt.txt) are its log.

The target model (`agent.params.model`, a `<backend>/<model>` reference, see hermit.agent.model.backends; default GLM 5.3
Flash on Fireworks, FIREWORKS_API_KEY) answers at medium reasoning effort, as the paper runs its student: SkillOpt's
chat_target_messages is one call of the `direct` Base Agent (docs/framework/agent-layers.md); the loop is SkillOpt's. A rollout the API could not serve raises, so it is recorded as an error
rather than a wrong answer (so does a question whose parsed pages SkillOpt cannot find under data.json's docs:
without them the rollout is not the paper's setting).
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import threading
import time
import types

from hermit.agent.acting import Agent
from hermit.task import RunOutput

HERE = Path(__file__).resolve().parent
DATA = json.loads((HERE.parent / "data.json").read_text())
DEFAULT_MODEL = "fireworks/accounts/fireworks/models/glm-5p3-flash"
EFFORT = "medium"                                                 # paper p.8: teacher and student at medium
MAX_TURNS, MAX_TOKENS = 24, 16384                                 # configs/officeqa/default.yaml; issue #13
UNAVAILABLE = "target API unavailable"

_usage = threading.local()
_lock = threading.Lock()
_agent = None
_binding = None
# The chat client's own report of a request it gave up on after its retries (a rate limit, a server error, no
# connection), not a request the endpoint refused
_UNSERVED = re.compile(r"chat/completions (?:HTTP (?:408|409|429|5\d\d)|failed)")


def _bind(params: dict | None):
    """The target model: agent.params.model (resolved by the framework into a binding), else the default."""
    global _binding
    from hermit.agent.model.backends import Binding, registry, resolve
    model = (params or {}).get("model")
    _binding = Binding.from_spec(model) if isinstance(model, dict) else resolve(model or DEFAULT_MODEL, registry())


class _Call:
    """A tool call as SkillOpt reads it: `.id`, `.function.name`, `.function.arguments`, `model_dump`."""

    def __init__(self, call: dict):
        self._call = call
        self.id = call.get("id")
        fn = call.get("function") or {}
        self.function = types.SimpleNamespace(name=fn.get("name", ""), arguments=fn.get("arguments") or "")

    def model_dump(self, mode: str = "json") -> dict:
        return dict(self._call)


def _base_agent():
    """The Base Agent of every call: `direct` on the target's endpoint at the paper's reasoning effort."""
    global _agent
    with _lock:
        if _agent is None:
            from hermit.agent.base.interface import make
            from hermit.agent.model.llm import ChatClient
            # One request as SkillOpt's own client sent it: its tools as they are, a tool_choice only when it gives
            # one, and its retries of a request the endpoint did not serve: 5 attempts, waiting 4 s, 8 s, 16 s, 32 s
            # (doubling, at most 60 s).
            spec = {**_binding.spec(), "extra_body": {"reasoning_effort": EFFORT}}
            _agent = make("direct", {"tool_choice": None},
                          ChatClient.from_spec(spec, max_retries=4, retry_wait=4.0, retry_wait_max=60.0))
        return _agent


def _chat(messages, max_completion_tokens=MAX_TOKENS, retries=5, stage="rollout", reasoning_effort=None, *,
          tools=None, tool_choice=None, return_message=False, timeout=None):
    """SkillOpt's chat_target_messages as one call of the `direct` Base Agent (Layer 2): the loop is SkillOpt's
    (Layer 3), which runs the tools the model asks for. Counts tokens and replies cut off at max_tokens for the
    current rollout."""
    agent = _base_agent().configured(max_tokens=min(max_completion_tokens, MAX_TOKENS), tool_choice=tool_choice)
    try:
        result = agent.run(list(messages), tools=list(tools or []))
    except RuntimeError as exc:
        if _UNSERVED.match(str(exc)):
            raise RuntimeError(f"{UNAVAILABLE}: {exc}") from None
        raise
    usage = result.usage[0]
    record = {"prompt_tokens": usage.input_tokens, "cached_tokens": usage.cached_input_tokens,
              "completion_tokens": usage.output_tokens}
    totals = getattr(_usage, "totals", None)
    if totals is not None:
        totals["calls"] += 1
        totals["cut_off"] += result.finish_reason == "length"
        for key, value in record.items():
            totals[key] += value
    usage_out = {"prompt_tokens": record["prompt_tokens"], "completion_tokens": record["completion_tokens"],
                 "total_tokens": record["prompt_tokens"] + record["completion_tokens"]}
    if not return_message:
        return result.reply, usage_out
    message = types.SimpleNamespace(content=result.reply or None,
                                    tool_calls=[_Call(c) for c in result.tool_calls] or None)
    return message, usage_out


def _harness(params: dict | None) -> str:
    harness = (params or {}).get("harness") or "direct"
    if harness == "codex":
        raise ValueError("agent.params.harness: codex is gone: Codex is the framework's agent now (agent.name: codex "
                         "with ../utils/task.py's hooks; see the Codex configs, e.g. skillopt/run_codex.yaml)")
    if harness != "direct":
        raise ValueError(f"agent.params.harness is direct, not {harness!r}")
    return harness


def _setup(params: dict | None = None) -> None:
    from skillopt.envs.officeqa import rollout
    if _binding is None:
        _bind(params)
    _harness(params)
    rollout.chat_target_messages = _chat


def _messages(pred_dir: Path) -> list[dict]:
    """The prediction files as chat messages, for Evolvers that read messages."""
    read = lambda name: (pred_dir / name).read_text() if (pred_dir / name).is_file() else ""
    messages = [{"role": "system", "content": read("target_system_prompt.txt")},
                {"role": "user", "content": read("target_user_prompt.txt")}]
    entries = json.loads(read("conversation.json") or "[]")
    for i, e in enumerate(entries[1:]):                  # the first entry is the user prompt
        if e.get("type") == "tool_call":
            call = {"id": f"call_{i}", "type": "function", "function": {"name": e.get("cmd", ""), "arguments": ""}}
            messages += [{"role": "assistant", "content": None, "tool_calls": [call]},
                         {"role": "tool", "tool_call_id": call["id"], "content": e.get("obs", "")}]
        elif e.get("content"):
            messages.append({"role": "assistant", "content": e["content"]})
    return messages


class OfficeQAAgent(Agent):
    """SkillOpt's own rollout with the adapter's context as the skill. `agent.name: ../utils/agent/agent.py:OfficeQAAgent`."""

    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        _setup(self.params)
        from skillopt.envs.officeqa.rollout import process_one

        session = self.session(adapter)
        # The question is the instance's input: what the adapter's context tool, if it declares one, retrieves for.
        skill = session.context(query=task["question"], task=task).strip() if session is not None else ""
        logs = HERE / ".logs"
        logs.mkdir(parents=True, exist_ok=True)
        item = {"id": task["id"], "question": task["question"], "task_type": task.get("category", "officeqa"),
                "source_files": task["source_files"], "source_docs": task["source_docs"], "ground_truth": ""}
        from skillopt.envs.officeqa.tool_runtime import build_oracle_parsed_pages_context, resolve_docs_roots
        if not build_oracle_parsed_pages_context(item["source_files"], item["source_docs"],
                                                 resolve_docs_roots([DATA["docs"]])):   # cached: the rollout rereads them
            raise RuntimeError(f"no parsed pages for {task['id']} under {DATA['docs']}: see utils/prepare.py")
        started = time.time()
        for attempt in range(3):            # the API was unavailable: run the whole rollout again later
            out_root = Path(tempfile.mkdtemp(prefix=f"{task['id']}-", dir=logs))
            _usage.totals = totals = {"calls": 0, "cut_off": 0, "prompt_tokens": 0, "cached_tokens": 0,
                                      "completion_tokens": 0}
            try:
                result = process_one(item, str(out_root), skill, max_tool_turns=MAX_TURNS,
                                     max_completion_tokens=MAX_TOKENS, search_mode="offline", use_local_tools=True,
                                     data_dirs=[DATA["docs"]])
            finally:
                _usage.totals = None
            failure = result["fail_reason"]
            if UNAVAILABLE not in failure:
                break
            if attempt == 2:
                raise RuntimeError(failure[:4000])
            time.sleep(120)
        pred_dir = out_root / "predictions" / task["id"]
        messages = _messages(pred_dir)
        fresh = totals["prompt_tokens"] - totals["cached_tokens"]
        price = _binding.price
        cost = None if not price else round((fresh * price.get("input", 0)
                                             + totals["cached_tokens"] * price.get("cached_input", price.get("input", 0))
                                             + totals["completion_tokens"] * price.get("output", 0)) / 1e6, 6)
        tokens = totals["prompt_tokens"] + totals["completion_tokens"]
        answer = result["predicted_answer"] if result["agent_ok"] else ""
        extra = {"model": _binding.ref, "harness": "direct", "reasoning_effort": EFFORT,
                 "cut_off_replies": totals["cut_off"], "oracle_pages_chars": result["oracle_parsed_pages_chars"],
                 "turns": result["n_turns"]}
        if session is not None and session.last_context is not None:
            extra["context_tool"] = session.last_context      # what the adapter's context tool rendered for this question
        return RunOutput(output={"answer": answer, "fail_reason": "" if result["agent_ok"] else result["fail_reason"]},
                         log=pred_dir, messages=messages, cost_usd=cost, tokens=tokens,
                         wall_s=round(time.time() - started, 1), api_speed={"model_calls": totals["calls"]}, extra=extra)
