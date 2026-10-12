"""Visual showcase acting agent: GLM 5.3 Flash on Fireworks writes one offline, interactive, animated index.html.

- `run_direct(task, adapter)`: arm A (`direct`), one request without tools; the HTML is taken from the reply.
- `run(task, adapter)`: arm B (`harness`), the tool loop, when `adapter` is None or is the untouched root (its
  adapter.yaml has no `origin`; the Trainer adds one only when it commits a learned change). Otherwise arm C
  (`adapted`): the system prompt gains BRIDGE and the adapter's knowledge package, and the read-only `wm` tool is added.

Each run writes its record under $VISUAL_LOG_ROOT (default .logs/) and returns it as RunOutput.log. The host's final
capture (capture/) is never shown to the model. FIREWORKS_API_KEY is read from the environment and never written.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import threading
import time

import yaml

from harnessadapter.llm import ChatClient
from harnessadapter.task import RunOutput

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import harness  # noqa: E402  (this example's harness.py)

BASE_URL = "https://api.fireworks.ai/inference/v1"
MODEL = "accounts/fireworks/models/glm-5p3-flash"
KEY_ENV = "FIREWORKS_API_KEY"
PRICE = {"input": 0.15, "cached_input": 0.03, "output": 0.50}     # USD per million tokens
SAMPLING = {"temperature": 0, "top_k": 40, "presence_penalty": 0, "frequency_penalty": 0, "reasoning_effort": 4096}
MAX_TOKENS = 32768                    # per response
COMPLETION_BUDGET = 65536             # cumulative completion tokens per actor run
ACTOR_SECONDS = 600
REQUEST_TIMEOUT = 420
MAX_REQUESTS = {"direct": 1, "harness": 12, "adapted": 12}

COMMON = '''You are creating an original polished offline interactive visual scene. Follow the full task below. Author your own HTML, CSS and JavaScript. The supplied local Three.js 0.180.0 module and core are available at /vendor/three.module.min.js and /vendor/three.core.min.js. Plain Canvas or WebGL is also allowed. No external URLs, fonts, assets, packages, source retrieval or network. You have no access to other runs. Produce a real animated scene, not screenshots of a scene. Do not claim physical validation or aesthetic superiority. Never use evaluator-only behavior.'''
DIRECT = '''This is the single-response condition. No tools are available in this condition. Return the COMPLETE index.html only, as raw HTML or one html code fence. The host will save it mechanically and perform final browser capture after your response, without returning feedback or modifying your code. The task's optional inspection paragraph does not grant a tool in this condition.'''
LOOP = '''This is the tool-harness condition. Create index.html using write_file; read_file and replace_text are available for revisions. The task is already included in full. Use inspect_page at least once to inspect actual browser output; then improve observed issues within your budget. Images are original captures, while diagnostics have limited scope. You have at most 600 seconds, 12 model requests, 24 total tool calls, 3 browser inspections, and 65,536 cumulative completion tokens. Only your current index.html is submitted; a final text reply does not save code. After your last change, inspecting again when budget permits establishes coverage of the final file. Finish with a short factual summary of actual checks and unresolved limitations. No shell or Python execution tool is available.'''
BRIDGE = '''A frozen MetaHarnessAdapter knowledge package learned by GLM 5.3 Flash from its previous harness-assisted attempts follows. Its original context mentions MCP; this host exposes the same read-only wm contract as a Fireworks function tool backed by the native Adapter.call API. This is a transport bridge, not a new memory implementation. Earlier episodes are not accessible as full source files or raw evidence. Only this frozen context and its native read-only knowledge operations are available. Tools, delegates and model weights are not evolved in this experiment. Knowledge can be useful or mistaken; use actual current output to verify it.'''

PLACEHOLDER = '<!doctype html><html><head><meta charset="utf-8"><title>No submission</title></head><body><p>No model artifact was submitted.</p></body></html>'
IMAGES_REMOVED = 'Earlier inspection image bytes removed from the current API request to keep image payload bounded. Its recorded text observations remain above.'
IMAGES_INTRO = 'Actual browser observation from your latest inspect_page call. Assess these images along with the tool diagnostics, then continue your current task within the same budget. No external reviewer has edited this feedback.'
FRAMEWORK_ONLY = ("CARD.md",)     # written by the Trainer (spending, eval scores); never part of what the agent reads


def make_client() -> ChatClient:
    """The actor's transport: no retries, 420 s per request (lowered to the time left before each call)."""
    return ChatClient(BASE_URL, MODEL, api_key_env=KEY_ENV, price=PRICE, timeout=REQUEST_TIMEOUT, max_retries=0)


def continuing_message(message: dict) -> dict:
    # Preserve provider reasoning only in the private in-memory conversation.
    return {k: v for k, v in message.items() if k in ("role", "content", "reasoning_content", "tool_calls")}


def public_message(message: dict) -> dict:
    return {k: v for k, v in message.items() if k in ("role", "content", "tool_calls")}


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def arm_of(adapter: Path | None) -> str:
    """`harness` (B) without an adapter or for the untouched root (no `origin` in adapter.yaml), else `adapted` (C)."""
    if adapter is None:
        return "harness"
    manifest = yaml.safe_load((Path(adapter) / "adapter.yaml").read_text()) or {}
    return "adapted" if "origin" in manifest else "harness"


_bytecode_lock = threading.Lock()
_bytecode_users = 0
_bytecode_saved = False


@contextlib.contextmanager
def no_bytecode():
    """No .pyc files while a run is active. The adapter is loaded in this process, and its package (mwm_core, partly
    imported lazily during wm calls) would otherwise get __pycache__/ inside the adapter, which `wm search` and
    `wm read` would then show to the model. Counted, so overlapping runs agree."""
    global _bytecode_users, _bytecode_saved
    with _bytecode_lock:
        if _bytecode_users == 0:
            _bytecode_saved = sys.dont_write_bytecode
        _bytecode_users += 1
        sys.dont_write_bytecode = True
    try:
        yield
    finally:
        with _bytecode_lock:
            _bytecode_users -= 1
            if _bytecode_users == 0:
                sys.dont_write_bytecode = _bytecode_saved


@contextlib.contextmanager
def agent_view(adapter: Path | None):
    """The adapter as the acting agent may see it. `wm read` and `wm search` reach every file of the adapter folder,
    and the Trainer's CARD.md (in `<run>/final`) holds the run's spending and eval scores. When one is present, a
    temporary copy without it is loaded instead."""
    if adapter is None or not any((Path(adapter) / name).exists() for name in FRAMEWORK_ONLY):
        yield adapter
        return
    top = str(adapter)
    with tempfile.TemporaryDirectory(prefix="visual-adapter-") as tmp:
        view = Path(tmp) / "adapter"
        shutil.copytree(adapter, view, symlinks=True,
                        ignore=lambda d, names: [n for n in names if d == top and n in FRAMEWORK_ONLY])
        yield view


def run(task: dict, adapter: Path | None) -> RunOutput:
    """Arms B and C: the tool loop (see the module docstring for which arm an adapter gets)."""
    arm = arm_of(adapter)
    with no_bytecode(), agent_view(adapter if arm == "adapted" else None) as view:
        return _actor(task, arm, adapter, view)


def run_direct(task: dict, adapter: Path | None) -> RunOutput:
    """Arm A: one request without tools. Any adapter is ignored."""
    return _actor(task, "direct", adapter)


def _log_dir(case: str, arm: str) -> Path:
    root = Path(os.environ.get("VISUAL_LOG_ROOT") or HERE / ".logs")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = root / f"{case}-{arm}-{stamp}-{secrets.token_hex(3)}"
    base.mkdir(parents=True)
    return base


def _final_capture(submission: Path, output: Path, case: str) -> None:
    """The host's final capture; a capturer that crashes or times out leaves a failed summary, not an exception."""
    try:
        harness.capture(submission, output, case)
    except Exception as exc:  # noqa: BLE001
        harness.save(output / "summary.json", {
            "status": "capture_process_failed", "capture_ok": False, "screenshots": [], "page_errors": [],
            "console_errors": [], "capture_errors": [f"{type(exc).__name__}: {exc}"[-2000:]], "model_calls": 0,
            "input": {"index_sha256": harness.sha(submission / "index.html")}})


def _actor(task: dict, arm: str, adapter_dir: Path | None, view: Path | None = None) -> RunOutput:
    """One run. `adapter_dir` is the adapter given (recorded); arm C loads `view`, the agent_view of it."""
    harness.check_vendor()      # before any model call: every capture of this run would need it
    case = task["case"]
    base = _log_dir(case, arm)
    workspace = base / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text(task["prompt"])
    (workspace / "index.html").write_text(PLACEHOLDER)
    sys_text = COMMON + "\n\n" + (DIRECT if arm == "direct" else LOOP)
    adapter = None
    if arm == "adapted":
        from harnessadapter.adapter.loader import Adapter
        adapter = Adapter(Path(view))
        sys_text += "\n\n" + BRIDGE + "\n\n" + asyncio.run(adapter.call("context_package", {}))["text"]
    host = harness.Harness(workspace, base, case, adapter)
    harness.save(base / "request.json", {
        "arm": arm, "case": case, "task_id": task["id"], "adapter": str(adapter_dir) if adapter_dir else None,
        "started_utc": utc(), "model": MODEL, "settings": {**SAMPLING, "max_tokens": MAX_TOKENS},
        "system": sys_text, "task": task["prompt"], "tools": [] if arm == "direct" else host.tools})
    client = make_client()
    messages = [{"role": "system", "content": sys_text}, {"role": "user", "content": task["prompt"]}]
    start = time.monotonic()
    deadline = start + ACTOR_SECONDS
    calls, model_s = 0, 0.0
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
    status, reason = "budget_exhausted", "maximum requests reached"
    image_message_indices, extraction = [], None
    for _ in range(MAX_REQUESTS[arm]):
        remaining = COMPLETION_BUDGET - usage["completion_tokens"]
        if remaining <= 0 or time.monotonic() >= deadline:
            reason = "token or time budget exhausted"
            break
        options = {**SAMPLING, "max_tokens": min(MAX_TOKENS, remaining)}
        if arm != "direct":
            options.update(tools=host.tools, tool_choice="auto")
        client.timeout = max(1, min(REQUEST_TIMEOUT, deadline - time.monotonic()))
        calls += 1
        record = {"call": calls, "request_message_count": len(messages), "request_image_count": sum(
            1 for m in messages if isinstance(m.get("content"), list) for p in m["content"] if p.get("type") == "image_url")}
        call_start = time.monotonic()
        try:
            message, call_usage = client.chat(messages, **options)
        except Exception as exc:  # noqa: BLE001 - recorded; the run ends as api_error
            key = os.environ.get(KEY_ENV)     # an error body may echo the key
            message, record["error"] = None, (str(exc).replace(key, "[REDACTED]") if key else str(exc))[:3000]
        model_s += time.monotonic() - call_start
        record["seconds"] = round(time.monotonic() - call_start, 3)
        if message is None:
            harness.save(base / "calls" / f"{calls:02d}.json", {**record, "request_accepted": False})
            status, reason = "api_error", record["error"]
            break
        usage["prompt_tokens"] += call_usage.input_tokens
        usage["completion_tokens"] += call_usage.output_tokens
        usage["cached_tokens"] += call_usage.cached_input_tokens
        finish = message.get("finish_reason")
        harness.save(base / "calls" / f"{calls:02d}.json", {
            **record, "usage": {"prompt_tokens": call_usage.input_tokens, "completion_tokens": call_usage.output_tokens,
                                "cached_tokens": call_usage.cached_input_tokens},
            "request_accepted": True, "assistant": public_message(message), "finish_reason": finish})
        messages.append(continuing_message(message))
        if arm == "direct":
            (base / "response.txt").write_text(message.get("content") or "")
            try:
                text, extraction = harness.extract_html(message.get("content") or "")
                (workspace / "index.html").write_text(text)
                status = "completed" if finish != "length" else "output_truncated"
                reason = "single response saved mechanically"
            except ValueError as exc:
                status, reason = "invalid_output", str(exc)
            break
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            (base / "final.md").write_text(message.get("content") or "")
            status = "completed" if finish != "length" else "output_truncated"
            reason = "model ended its tool loop"
            break
        new_images = []
        for tool_call in tool_calls:
            name = tool_call.get("function", {}).get("name", "")
            try:
                args = json.loads(tool_call["function"]["arguments"])
            except (ValueError, KeyError, TypeError):
                args = None
            tool_text, images = host.dispatch(name, args, deadline)
            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": tool_text})
            if images:
                new_images = images  # Deterministic: most recent inspection only, even if a batch contains several.
        if new_images:
            for j in image_message_indices:
                messages[j] = {"role": "user", "content": IMAGES_REMOVED}
            image_message_indices = [len(messages)]
            messages.append({"role": "user", "content": [{"type": "text", "text": IMAGES_INTRO}, *new_images]})
        if host.calls >= harness.MAX_TOOL_CALLS:
            reason = "tool-call budget exhausted"
            break
    final = workspace / "index.html"
    valid = final.read_text() != PLACEHOLDER
    if not valid and status == "completed":
        status, reason = "no_artifact", "No index.html was authored with the file tools"
    dest = base / "submission"
    dest.mkdir()
    shutil.copyfile(final, dest / "index.html")
    submission_sha = harness.sha(dest / "index.html")
    cost = client.total.cost_usd
    harness.save(base / "result.json", {
        "arm": arm, "case": case, "status": status, "stop_reason": reason, "finished_utc": utc(),
        "elapsed_seconds": round(time.monotonic() - start, 3), "model_calls": calls, "tool_calls": host.calls,
        "inspection_calls": host.inspections, "wm_calls": host.wm_calls, "usage": usage,
        "submission_sha256": submission_sha, "final_inspected_sha256": host.last_inspected_sha,
        "final_inspection_matches": host.last_inspected_sha == submission_sha, "valid_submission": valid,
        "extraction": extraction, "cost_usd": cost})
    _final_capture(dest, base / "capture", case)
    return RunOutput(output={"case": case, "arm": arm, "status": status, "log_dir": str(base),
                             "submission_sha256": submission_sha},
                     log=base, cost_usd=cost, wall_s=round(time.monotonic() - start, 1),
                     api_speed={"output_tokens_per_s": round(usage["completion_tokens"] / model_s, 1) if model_s else None,
                                "model_calls": calls},
                     tokens=usage["prompt_tokens"] + usage["completion_tokens"])
