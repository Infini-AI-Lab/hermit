"""OfficeQA as in the SkillOpt paper, run by SkillOpt's own rollout (skillopt.envs.officeqa.rollout.process_one).

Its offline mode: the question's oracle parsed pages in the prompt, glob/read/grep tools limited to the question's
source files, at most 24 turns of up to 16,384 tokens, the answer inside <answer>...</answer>. The adapter's
ADAPTER.md is the skill; adapter=None runs without one. The run's SkillOpt prediction files (conversation.json,
target_*_prompt.txt) are its log.

The target model's calls (SkillOpt's chat_target_messages) go to GLM 5.3 Flash on Fireworks (FIREWORKS_API_KEY) at
medium reasoning effort, as the paper runs its student. A rollout the API could not serve raises, so it is recorded
as an error rather than a wrong answer.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time

from harnessadapter.task import RunOutput

HERE = Path(__file__).resolve().parent
DATA = json.loads((HERE / "data.json").read_text())
MODEL = "accounts/fireworks/models/glm-5p3-flash"
PRICE = {"input": 0.15, "cached_input": 0.03, "output": 0.50}     # USD per million tokens
EFFORT = "medium"                                                 # paper p.8: teacher and student at medium
MAX_TURNS, MAX_TOKENS = 24, 16384                                 # configs/officeqa/default.yaml; issue #13
UNAVAILABLE = "target API unavailable"

_usage = threading.local()
_lock = threading.Lock()
_client = None


def _chat(messages, max_completion_tokens=MAX_TOKENS, retries=5, stage="rollout", reasoning_effort=None, *,
          tools=None, tool_choice=None, return_message=False, timeout=None):
    """SkillOpt's chat_target_messages, sent to Fireworks with the target's reasoning effort. Counts tokens and
    replies cut off at max_tokens for the current rollout."""
    import openai
    global _client
    with _lock:
        if _client is None:
            _client = openai.OpenAI(base_url="https://api.fireworks.ai/inference/v1",
                                    api_key=os.environ["FIREWORKS_API_KEY"], timeout=600, max_retries=0)
    kwargs = {"model": MODEL, "messages": messages, "max_tokens": min(max_completion_tokens, MAX_TOKENS),
              "extra_body": {"reasoning_effort": EFFORT}}
    if tools:
        kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
    for attempt in range(retries):
        try:
            response = _client.chat.completions.create(**kwargs)
            break
        except (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError,
                openai.InternalServerError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"{UNAVAILABLE}: {type(exc).__name__}: {exc}") from None
            time.sleep(min(60, 4 * 2 ** attempt))
    choice = response.choices[0]
    usage = response.usage
    details = getattr(usage, "prompt_tokens_details", None)
    record = {"prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
              "cached_tokens": int(getattr(details, "cached_tokens", 0) or 0),
              "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0)}
    totals = getattr(_usage, "totals", None)
    if totals is not None:
        totals["calls"] += 1
        totals["cut_off"] += choice.finish_reason == "length"
        for key, value in record.items():
            totals[key] += value
    usage_out = {"prompt_tokens": record["prompt_tokens"], "completion_tokens": record["completion_tokens"],
                 "total_tokens": record["prompt_tokens"] + record["completion_tokens"]}
    return (choice.message if return_message else (choice.message.content or "")), usage_out


def _setup() -> None:
    from skillopt.envs.officeqa import rollout
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


def run(task: dict, adapter: Path | None) -> RunOutput:
    _setup()
    from skillopt.envs.officeqa.rollout import process_one

    skill = ""
    if adapter is not None:
        from harnessadapter.adapter.loader import Adapter
        skill = Adapter(Path(adapter)).context().strip()
    (HERE / ".logs").mkdir(exist_ok=True)
    item = {"id": task["id"], "question": task["question"], "task_type": task.get("category", "officeqa"),
            "source_files": task["source_files"], "source_docs": task["source_docs"], "ground_truth": ""}
    started = time.time()
    for attempt in range(3):            # the API was unavailable: run the whole rollout again later
        out_root = Path(tempfile.mkdtemp(prefix=f"{task['id']}-", dir=HERE / ".logs"))
        _usage.totals = totals = {"calls": 0, "cut_off": 0, "prompt_tokens": 0, "cached_tokens": 0,
                                  "completion_tokens": 0}
        try:
            result = process_one(item, str(out_root), skill, max_tool_turns=MAX_TURNS,
                                 max_completion_tokens=MAX_TOKENS, search_mode="offline", use_local_tools=True,
                                 data_dirs=[DATA["docs"]])
        finally:
            _usage.totals = None
        if UNAVAILABLE not in result["fail_reason"]:
            break
        if attempt == 2:
            raise RuntimeError(result["fail_reason"])
        time.sleep(120)
    pred_dir = out_root / "predictions" / task["id"]
    fresh = totals["prompt_tokens"] - totals["cached_tokens"]
    cost = (fresh * PRICE["input"] + totals["cached_tokens"] * PRICE["cached_input"]
            + totals["completion_tokens"] * PRICE["output"]) / 1e6
    answer = result["predicted_answer"] if result["agent_ok"] else ""
    return RunOutput(output={"answer": answer, "fail_reason": "" if result["agent_ok"] else result["fail_reason"]},
                     log=pred_dir, messages=_messages(pred_dir), cost_usd=round(cost, 6),
                     tokens=totals["prompt_tokens"] + totals["completion_tokens"], wall_s=round(time.time() - started, 1),
                     api_speed={"model_calls": totals["calls"]},
                     extra={"model": MODEL, "reasoning_effort": EFFORT, "cut_off_replies": totals["cut_off"],
                            "oracle_pages_chars": result["oracle_parsed_pages_chars"], "turns": result["n_turns"]})
