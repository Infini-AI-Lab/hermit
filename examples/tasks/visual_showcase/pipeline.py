"""Visual showcase acting-agent pipeline: GLM 5.3 Flash writes one offline interactive index.html, ported from the
2026-09-19 experiment's campaign.py (run_actor). Three arms, as before:

- `run_direct(task, adapter)` — arm A (`direct`): one request, no tools; the HTML is extracted mechanically.
- `run(task, adapter)` — arm B (`harness`) when `adapter` is None or is the untouched root (its adapter.yaml has no
  `origin`; the Trainer adds one only when it commits a learned change); otherwise arm C (`adapted`): the system prompt
  gains BRIDGE + the adapter's knowledge package (its `context_package` tool) and the model gets the read-only `wm` tool.
  The adapter is loaded without the Trainer's CARD.md (see agent_view).

System prompts, the user message, tool schemas, budgets, the image-retention rule and the transport settings are the
old ones, byte for byte. Each run writes the old run directory layout under $VISUAL_LOG_ROOT (default .logs/) and
returns it as RunOutput.log; the host-owned final capture (capture/) is never shown to the model.
Model: GLM 5.3 Flash on Fireworks (FIREWORKS_API_KEY); the key is never written and never reaches the browser.

A failure that every later run would repeat raises EnvironmentFailure, as the old campaign stopped on 401/403: a
missing or changed browser runtime before the run starts; no key or HTTP 401/403 after the run is recorded. It is a
SystemExit, which the Trainer's rollout does not catch: training stops before the step is recorded, and `train` with
the same config resumes from the last completed step once the cause is fixed.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import yaml

from harnessadapter.evolver.base import Usage
from harnessadapter.llm import ChatClient
from harnessadapter.task import RunOutput

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import harness  # noqa: E402  (this example's harness.py)
import speedgate  # noqa: E402

BASE_URL = "https://api.fireworks.ai/inference/v1"
MODEL = "accounts/fireworks/models/glm-5p3-flash"
KEY_ENV = "FIREWORKS_API_KEY"
PRICE = {"input": 0.15, "cached_input": 0.03, "output": 0.50}     # USD per million tokens, as in the old config.json
SAMPLING = {"temperature": 0, "top_k": 40, "presence_penalty": 0, "frequency_penalty": 0, "reasoning_effort": 4096}
MAX_TOKENS = 32768                    # per response
COMPLETION_BUDGET = 65536             # cumulative completion tokens per actor run
ACTOR_SECONDS = 600
REQUEST_TIMEOUT = 420
MAX_REQUESTS = {"direct": 1, "harness": 12, "adapted": 12}
SETTINGS = {"model": MODEL, "endpoint": BASE_URL + "/chat/completions", **SAMPLING,
            "max_tokens_per_response": MAX_TOKENS, "max_completion_tokens_per_actor": COMPLETION_BUDGET,
            "max_model_calls": MAX_REQUESTS, "max_tool_calls": harness.MAX_TOOL_CALLS,
            "max_inspections": harness.MAX_INSPECTIONS, "actor_timeout_seconds": ACTOR_SECONDS,
            "request_timeout_seconds": REQUEST_TIMEOUT, "retries": 0, "parallel_actors": 1,
            "price_usd_per_million": PRICE,
            "image_history": "Only newest inspection image batch retained in the next API request, max 8,000,000 "
                             "base64 characters; earlier text observations remain"}

COMMON = '''You are creating an original polished offline interactive visual scene. Follow the full task below. Author your own HTML, CSS and JavaScript. The supplied local Three.js 0.180.0 module and core are available at /vendor/three.module.min.js and /vendor/three.core.min.js. Plain Canvas or WebGL is also allowed. No external URLs, fonts, assets, packages, source retrieval or network. You have no access to other runs. Produce a real animated scene, not screenshots of a scene. Do not claim physical validation or aesthetic superiority. Never use evaluator-only behavior.'''
DIRECT = '''This is the single-response condition. No tools are available in this condition. Return the COMPLETE index.html only, as raw HTML or one html code fence. The host will save it mechanically and perform final browser capture after your response, without returning feedback or modifying your code. The task's optional inspection paragraph does not grant a tool in this condition.'''
LOOP = '''This is the tool-harness condition. Create index.html using write_file; read_file and replace_text are available for revisions. The task is already included in full. Use inspect_page at least once to inspect actual browser output; then improve observed issues within your budget. Images are original captures, while diagnostics have limited scope. You have at most 600 seconds, 12 model requests, 24 total tool calls, 3 browser inspections, and 65,536 cumulative completion tokens. Only your current index.html is submitted; a final text reply does not save code. After your last change, inspecting again when budget permits establishes coverage of the final file. Finish with a short factual summary of actual checks and unresolved limitations. No shell or Python execution tool is available.'''
BRIDGE = '''A frozen MetaHarnessAdapter knowledge package learned by GLM 5.3 Flash from its previous harness-assisted attempts follows. Its original context mentions MCP; this host exposes the same read-only wm contract as a Fireworks function tool backed by the native Adapter.call API. This is a transport bridge, not a new memory implementation. Earlier episodes are not accessible as full source files or raw evidence. Only this frozen context and its native read-only knowledge operations are available. Tools, delegates and model weights are not evolved in this experiment. Knowledge can be useful or mistaken; use actual current output to verify it.'''

PLACEHOLDER = '<!doctype html><html><head><meta charset="utf-8"><title>No submission</title></head><body><p>No model artifact was submitted.</p></body></html>'
IMAGES_REMOVED = 'Earlier inspection image bytes removed from the current API request to keep image payload bounded. Its recorded text observations remain above.'
IMAGES_INTRO = 'Actual browser observation from your latest inspect_page call. Assess these images along with the tool diagnostics, then continue your current task within the same budget. No external reviewer has edited this feedback.'
FRAMEWORK_ONLY = ("CARD.md",)     # written by the Trainer (spending, eval scores); never part of what the agent reads


class EnvironmentFailure(SystemExit):
    """A failure every later run would repeat. A SystemExit, so the Trainer's rollout (which records an ordinary
    exception as a failed task and goes on) lets it through and the run stops, resumable."""


class Client(ChatClient):
    """harnessadapter's ChatClient, same request and accounting, that also keeps the last response's metadata
    (http_status, seconds, the raw usage with reasoning tokens, response model and id) for calls/NN.json, as the old
    client returned it. An HTTP error keeps 3,000 characters of the body with the key redacted, as the old client did.
    The key and the message are never stored."""

    last_meta: dict = {}

    def chat(self, messages: list[dict], **kwargs) -> tuple[dict, Usage]:
        body = {"model": self.model, "messages": messages, **kwargs}
        headers = {"Content-Type": "application/json"}
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise RuntimeError(f"Environment variable {self.api_key_env} is not set")
            headers["Authorization"] = f"Bearer {key}"
        data = json.dumps(body).encode()
        delay, started = 2.0, time.monotonic()
        self.last_meta = {}
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(self.base_url + "/chat/completions", data=data, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read())
                break
            except urllib.error.HTTPError as exc:
                retriable = exc.code in (408, 409, 429, 500, 502, 503, 504)
                detail = _redact(exc.read().decode(errors="replace"))[:3000]
                if not retriable or attempt == self.max_retries:
                    raise RuntimeError(f"chat/completions HTTP {exc.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == self.max_retries:
                    raise RuntimeError(f"chat/completions failed: {exc}") from None
            time.sleep(delay)
            delay *= 2
        u = payload.get("usage") or {}
        prompt = int(u.get("prompt_tokens", 0))
        cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
        completion = int(u.get("completion_tokens", 0))
        usage = Usage(self.model, prompt, cached, completion, 1, self.cost(prompt, cached, completion))
        with self._lock:
            t = self.total
            t.input_tokens += prompt; t.cached_input_tokens += cached; t.output_tokens += completion; t.calls += 1
            if t.cost_usd is not None and usage.cost_usd is not None:
                t.cost_usd += usage.cost_usd
        self.last_meta = {"http_status": 200, "seconds": round(time.monotonic() - started, 3), "usage": u,
                          "response_model": payload.get("model"), "response_id": payload.get("id")}
        choice = (payload.get("choices") or [{}])[0]
        message = dict(choice.get("message") or {})
        message["finish_reason"] = choice.get("finish_reason")
        return message, usage


def make_client() -> ChatClient:
    """The actor's transport: no retries, 420 s per request (lowered to the time left before each call)."""
    return Client(BASE_URL, MODEL, api_key_env=KEY_ENV, price=PRICE, timeout=REQUEST_TIMEOUT,
                  max_retries=0)


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
    `wm read` would then show to the model. The old adapter had no caches. Counted, so overlapping runs agree."""
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
    and the Trainer's CARD.md (in `<run>/final`) holds the run's spending and, after `eval --adapter final`, its mean
    score; the old adapter had no such file. When one is present, a temporary copy without it is loaded instead."""
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
    """Arm A: one request without tools. Any adapter is ignored (recorded in request.json only)."""
    return _actor(task, "direct", adapter)


def _log_dir(case: str, arm: str) -> Path:
    root = Path(os.environ.get("VISUAL_LOG_ROOT") or HERE / ".logs")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = root / f"{case}-{arm}-{stamp}-{secrets.token_hex(3)}"
    base.mkdir(parents=True)
    return base


def _tree_sha256(root: Path) -> str:
    """One digest of every file of an adapter directory (paths and bytes), to identify the adapter a run used; the
    Trainer's CARD.md is not part of it."""
    digest = hashlib.sha256()
    for path in sorted(p for p in Path(root).rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        relative = path.relative_to(root).as_posix()
        if relative not in FRAMEWORK_ONLY:
            digest.update(relative.encode() + b"\0" + harness.sha(path).encode() + b"\n")
    return digest.hexdigest()


def _redact(text: str) -> str:
    key = os.environ.get(KEY_ENV)
    return text.replace(key, "[REDACTED]") if key else text


def _final_capture(submission: Path, output: Path, case: str) -> dict:
    """The host-owned final capture; a capturer that crashes or times out leaves a failed summary, not an exception."""
    try:
        return harness.capture(submission, output, case)
    except Exception as exc:
        output.mkdir(parents=True, exist_ok=True)
        summary = {"status": "capture_process_failed", "capture_ok": False, "screenshots": [], "page_errors": [],
                   "console_errors": [], "capture_errors": [_redact(f"{type(exc).__name__}: {exc}")[-2000:]],
                   "model_calls": 0, "input": {"index_sha256": harness.sha(submission / "index.html")}}
        harness.save(output / "summary.json", summary)
        return summary


def _actor(task: dict, arm: str, adapter_dir: Path | None, view: Path | None = None) -> RunOutput:
    """One run. `adapter_dir` is the adapter given (recorded); arm C loads `view` (agent_view of it) when given."""
    try:
        harness.verify_runtime()
        capture_runtime = harness.check_browser()     # node, playwright, pngjs and Chrome versions, for request.json
    except RuntimeError as exc:
        raise EnvironmentFailure(f"visual showcase stopped before the run: {exc}. Every later run would fail the "
                                 "same way; fix it, then rerun the same command (train resumes from its last "
                                 "completed step).") from None
    gate = speedgate.wait_until_fast(f"{task['case']}-{arm}")   # optional (VISUAL_SPEED_MIN); before any clock starts
    t0 = time.monotonic()
    case = task["case"]
    base = _log_dir(case, arm)
    workspace = base / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "README.md").write_text(task["prompt"])
    (workspace / "index.html").write_text(PLACEHOLDER)
    adapter, context, adapter_record = None, None, None
    if adapter_dir is not None:
        manifest = yaml.safe_load((Path(adapter_dir) / "adapter.yaml").read_text()) or {}
        adapter_record = {"path": str(adapter_dir), "origin": manifest.get("origin"),
                          "tree_sha256": _tree_sha256(Path(adapter_dir)), "used": arm == "adapted"}
    if arm == "adapted":
        from harnessadapter.adapter.loader import Adapter
        adapter = Adapter(Path(view or adapter_dir))
        if view is not None and Path(view) != Path(adapter_dir):
            adapter_record["not_shown_to_agent"] = [n for n in FRAMEWORK_ONLY if (Path(adapter_dir) / n).exists()]
        context = asyncio.run(adapter.call("context_package", {}))["text"]
        adapter_record["context_sha256"] = hashlib.sha256(context.encode()).hexdigest()
    host = harness.Harness(workspace, base, case, adapter)
    sys_text = COMMON + "\n\n" + (DIRECT if arm == "direct" else LOOP)
    if adapter:
        sys_text += "\n\n" + BRIDGE + "\n\n" + context
    harness.save(base / "request.json", {"arm": arm, "case": case, "task_id": task["id"], "model": MODEL,
                                         "started_utc": utc(), "system": sys_text, "task": task["prompt"],
                                         "tools": [] if arm == "direct" else host.tools, "config": SETTINGS,
                                         "adapter": adapter_record, "speed_gate": gate,
                                         "capture_runtime": capture_runtime})
    harness.save(base / "case.json", task)
    client = make_client()
    messages = [{"role": "system", "content": sys_text}, {"role": "user", "content": task["prompt"]}]
    start = time.monotonic()
    deadline = start + ACTOR_SECONDS
    calls, model_s = 0, 0.0
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0}
    status, reason = "budget_exhausted", "maximum requests reached"
    image_message_indices, response_models, extraction, fatal = [], [], None, None
    for _ in range(MAX_REQUESTS[arm]):
        remaining = COMPLETION_BUDGET - usage["completion_tokens"]
        if remaining <= 0 or time.monotonic() >= deadline:
            reason = "token or time budget exhausted"
            break
        options = {**SAMPLING, "max_tokens": min(MAX_TOKENS, remaining)}
        if arm != "direct":
            options.update(tools=host.tools, tool_choice="auto")
        client.timeout = max(1, min(REQUEST_TIMEOUT, deadline - time.monotonic()))
        request_images = []
        for item in messages:
            if isinstance(item.get("content"), list):
                for part in item["content"]:
                    if part.get("type") == "image_url":
                        encoded = part["image_url"]["url"].split(",", 1)[1]
                        request_images.append({"sha256": hashlib.sha256(base64.b64decode(encoded)).hexdigest(),
                                               "base64_characters": len(encoded)})
        request_count = len(messages)
        call_start = time.monotonic()
        try:
            message, call_usage = client.chat(messages, **options)
            error = None
        except Exception as exc:
            message, call_usage, error = None, None, _redact(f"{exc}")[:3000]
        model_s += time.monotonic() - call_start
        calls += 1
        if error is not None:
            found = re.match(r"chat/completions HTTP (\d+): (.*)", error, re.S)
            http_status = int(found.group(1)) if found else None
            record = {"call": calls, "http_status": http_status, "seconds": round(time.monotonic() - call_start, 3),
                      "error": found.group(2) if found else error, "request_message_count": request_count,
                      "request_image_count": len(request_images), "request_images": request_images,
                      "request_accepted": False}
            harness.save(base / "calls" / f"{calls:02d}.json", record)
            status, reason = "api_error", f"HTTP {http_status}: {record['error']}"
            if http_status in (401, 403) or error.startswith(f"Environment variable {KEY_ENV} "):
                fatal = reason          # the old campaign: authentication failure, stop further runs
            break
        meta = dict(getattr(client, "last_meta", None) or {})
        raw = meta.get("usage") or {"prompt_tokens": call_usage.input_tokens,
                                    "completion_tokens": call_usage.output_tokens,
                                    "prompt_tokens_details": {"cached_tokens": call_usage.cached_input_tokens}}
        meta.setdefault("http_status", 200)
        meta.setdefault("seconds", round(time.monotonic() - call_start, 3))
        meta["usage"] = raw
        usage["prompt_tokens"] += call_usage.input_tokens
        usage["completion_tokens"] += call_usage.output_tokens
        usage["cached_tokens"] += call_usage.cached_input_tokens
        usage["reasoning_tokens"] += (raw.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
        if meta.get("response_model"):
            response_models.append(meta["response_model"])
        finish = message.get("finish_reason")
        record = {"call": calls, **meta, "request_message_count": request_count,
                  "request_image_count": len(request_images), "request_images": request_images,
                  "request_accepted": True, "assistant": public_message(message), "finish_reason": finish}
        harness.save(base / "calls" / f"{calls:02d}.json", record)
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
    total = client.total
    result = {"arm": arm, "case": case, "status": status, "stop_reason": reason, "finished_utc": utc(),
              "elapsed_seconds": round(time.monotonic() - start, 6), "model_calls": calls, "tool_calls": host.calls,
              "within_budget_tool_requests": min(host.calls, harness.MAX_TOOL_CALLS),
              "over_budget_tool_requests": max(0, host.calls - harness.MAX_TOOL_CALLS),
              "inspection_calls": host.inspections, "wm_calls": host.wm_calls, "usage": usage,
              "response_models": sorted(set(response_models)),
              "adapter_context_sha256": adapter_record["context_sha256"] if adapter else None,
              "submission_sha256": submission_sha, "final_inspected_sha256": host.last_inspected_sha,
              "final_inspection_matches": host.last_inspected_sha == submission_sha, "valid_submission": valid,
              "extraction": extraction, "reasoning_saved": False, "last_capture_not_model_feedback": True,
              "cost_usd": total.cost_usd}
    harness.save(base / "result.json", result)
    _final_capture(dest, base / "capture", case)
    if fatal:
        raise EnvironmentFailure(f"visual showcase stopped: the model request failed ({fatal}; run record {base}). "
                                 "Every later request would fail the same way; fix the key, then rerun the same "
                                 "command (train resumes from its last completed step).")
    return RunOutput(output={"case": case, "arm": arm, "status": status, "log_dir": str(base),
                             "submission_sha256": submission_sha},
                     log=base, cost_usd=total.cost_usd, wall_s=round(time.monotonic() - t0, 1),
                     api_speed={"output_tokens_per_s": round(usage["completion_tokens"] / model_s, 1) if model_s else None,
                                "model_calls": calls},
                     messages=None, tokens=usage["prompt_tokens"] + usage["completion_tokens"])
