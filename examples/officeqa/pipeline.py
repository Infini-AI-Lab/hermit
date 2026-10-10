"""OfficeQA acting-agent pipeline: a GLM tool-calling loop over the Treasury Bulletin corpus.

The adapter enters through its context export only (placed in the system prompt); adapter=None runs the bare agent.
Model: GLM 5.3 Flash on Fireworks (FIREWORKS_API_KEY). Logs: one JSON file per run under .logs/.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time

from harnessadapter.llm import ChatClient
from harnessadapter.task import RunOutput

HERE = Path(__file__).resolve().parent
PREPARED = Path((HERE / "data_root.txt").read_text().strip())
MODEL = os.environ.get("OFFICEQA_MODEL", "accounts/fireworks/models/glm-5p3-flash")
PRICE = {"input": 0.15, "cached_input": 0.03, "output": 0.50}     # USD per million tokens, 2026-09-21 public prices
MAX_TURNS = int(os.environ.get("OFFICEQA_MAX_TURNS", "24"))
MAX_OUTPUT_TOKENS = int(os.environ.get("OFFICEQA_MAX_OUTPUT", "65536"))

BASE = """You answer questions about U.S. Treasury Bulletins (1939–2025). The bulletins are text files you can search
and read with the tools. Find the figures in the documents, compute what the question asks, and end with the answer
inside <FINAL_ANSWER>...</FINAL_ANSWER>, in exactly the form the question requests."""

TOOLS = [
    {"type": "function", "function": {"name": "search", "description": "Full-text search (BM25) over the bulletins; "
     "returns filenames, line ranges and excerpts.", "parameters": {"type": "object", "properties": {
         "query": {"type": "string"}, "filename_glob": {"type": "string", "description": "e.g. treasury_bulletin_1995_*"},
         "limit": {"type": "integer"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "read", "description": "Read lines of one bulletin file.",
     "parameters": {"type": "object", "properties": {"filename": {"type": "string"},
         "start_line": {"type": "integer"}, "num_lines": {"type": "integer"}}, "required": ["filename"]}}},
]


def _corpus():
    from corpus import OfficeQACorpus
    return OfficeQACorpus(PREPARED / "agent" / "corpus", PREPARED / "agent" / "corpus.sqlite")


def _context(adapter: Path | None) -> str:
    if adapter is None:
        return ""
    from harnessadapter.adapter.loader import Adapter
    text = Adapter(Path(adapter)).context().strip()
    return f"\n\n# Instructions from the harness adapter\n\n{text}\n" if text else ""


def run(task: dict, adapter: Path | None) -> RunOutput:
    client = ChatClient("https://api.fireworks.ai/inference/v1", MODEL, api_key_env="FIREWORKS_API_KEY", price=PRICE)
    corpus = _corpus()
    messages = [{"role": "system", "content": BASE + _context(adapter)},
                {"role": "user", "content": task["question"]}]
    started, model_s, out_tokens, answer = time.time(), 0.0, 0, ""
    for _ in range(MAX_TURNS):
        t = time.time()
        message, usage = client.chat(messages, tools=TOOLS, tool_choice="auto", max_tokens=16384, temperature=0.6)
        model_s += time.time() - t
        out_tokens += usage.output_tokens
        messages.append({k: v for k, v in message.items() if k in ("role", "content", "tool_calls")})
        calls = message.get("tool_calls") or []
        if not calls:
            answer = message.get("content") or ""
            break
        for call in calls:
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                fn = call["function"]["name"]
                result = corpus.search(args["query"], int(args.get("limit", 8)), args.get("filename_glob", "*.txt")) \
                    if fn == "search" else corpus.read(args["filename"], int(args.get("start_line", 1)),
                                                       int(args.get("num_lines", 80)))
            except Exception as exc:
                result = {"error": f"{type(exc).__name__}: {exc}"}
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result, ensure_ascii=False)[:24000]})
        if out_tokens >= MAX_OUTPUT_TOKENS:
            break
    if "<FINAL_ANSWER>" not in answer.upper():   # no tagged answer (an empty final turn happens): ask once more
        messages.append({"role": "user", "content": "Stop using tools. Give your final answer now inside "
                                                    "<FINAL_ANSWER>...</FINAL_ANSWER>."})
        message, usage = client.chat(messages, max_tokens=4096, temperature=0.6)
        answer = message.get("content") or ""
        messages.append({"role": "assistant", "content": answer})
    logs = HERE / ".logs"
    logs.mkdir(exist_ok=True)
    log = Path(tempfile.mkstemp(prefix=f"{task['id']}-", suffix=".json", dir=logs)[1])
    log.write_text(json.dumps({"task": task["id"], "model": MODEL, "messages": messages}, ensure_ascii=False, indent=1))
    total = client.total
    return RunOutput(output=answer, log=log, cost_usd=total.cost_usd, wall_s=round(time.time() - started, 1),
                     api_speed={"output_tokens_per_s": round(total.output_tokens / model_s, 1) if model_s else None,
                                "model_calls": total.calls},
                     messages=messages, tokens=total.input_tokens + total.output_tokens)
