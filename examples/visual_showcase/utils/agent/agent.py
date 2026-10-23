"""The acting agent: GLM 5.3 Flash on Fireworks writes one offline interactive index.html with four generic primitives
(write_file, read_file, replace_text, run_command) inside the bubblewrap sandbox of utils/confine.py. It is told nothing
about how a page could be rendered; images a command writes into the workspace are the only pictures it ever sees. An
adapter can add to that: its context (ADAPTER.md) joins the system prompt, and a toolsmith adapter's learned tools (its
`tool_catalog`) become function tools whose results may carry images.

    agent: {name: ../utils/agent/agent.py:VisualAgent, concurrency: 1}

Arms: `harness` without an adapter or for the untouched root (no `origin` in adapter.yaml), else `adapted`. The model,
its settings and the host's final capture come from utils/pipeline.py and utils/harness.py (imported by file). A run has
no time limit (RUN_SECONDS) and a model request is abandoned only when it hangs (REQUEST_GUARD_S); utils/judge.py
scores the record.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import secrets
import sys
import time

from hermit.agents import Agent
from hermit.task import RunOutput

HERE = Path(__file__).resolve().parent
UTILS = HERE.parent


def _load(name: str, path: Path):
    """Import a sibling module by file path once (the Trainer loads this file under a hashed name, not as a package)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


pipeline = _load("visual_pipeline", UTILS / "pipeline.py")
harness = _load("visual_harness", UTILS / "harness.py")
confine = _load("visual_confine", UTILS / "confine.py")
texts = _load("visual_texts", HERE / "texts.py")
gh = _load("visual_generic_harness", HERE / "generic_harness.py")

RUNNER = confine.run                          # the confined runner; tests replace it with a fake
FINAL_CAPTURE = pipeline._final_capture       # the host's final capture (LOCK_EX inside harness.capture); tests fake it
MAX_REQUESTS = pipeline.MAX_REQUESTS
CONTEXT_GUARD_TOKENS = 90_000                 # why: a request above this is one tool result away from the model's window
RUN_SECONDS: float | None = None              # no time limit: a run ends by its requests, tool calls or completion
                                              # tokens, so a slow API or a busy machine cannot cut it short
REQUEST_GUARD_S = 1800                        # why: only a request that hangs is abandoned; the longest reply (32,768
                                              # tokens) takes 18 minutes at 30 tok/s, the lanes' speed floor
FINAL_MESSAGE_CHARS = 1500
RESULT_KEYS = ("arm", "case", "status", "stop_reason", "finished_utc", "elapsed_seconds", "model_calls", "tool_calls",
               "inspection_calls", "wm_calls", "usage", "submission_sha256", "final_inspected_sha256",
               "final_inspection_matches", "valid_submission", "extraction", "cost_usd", "run_commands",
               "adapter_tool_calls", "image_bearing_calls", "looked_calls", "looked_blank_calls", "looked_final",
               "first_image_call", "first_look_call", "tools_exported", "tools_called", "tool_errors",
               "context_exhausted", "images_total", "tool_text_chars", "confined", "seen_final", "seen_pages",
               "finalize")
OUTPUT_KEYS = ("case", "arm", "status", "stop_reason", "model_calls", "tool_calls", "run_commands", "adapter_tool_calls",
               "image_bearing_calls", "looked_calls", "first_image_call", "tools_exported", "tools_called", "tool_errors",
               "seen_final", "finalize")


def _log_dir(root: Path, case: str, arm: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = root / f"{case}-{arm}-{stamp}-{secrets.token_hex(3)}"
    base.mkdir(parents=True)
    return base


def _replayable(message: dict) -> dict:
    """The assistant message as it is sent back in later requests. A tool call whose arguments are not a JSON object
    (a response cut off at the token limit mid-call) makes the provider refuse the whole conversation (HTTP 400), so
    such arguments are replaced by "{}" in the replayed copy; the original stays in calls/NN.json."""
    calls = message.get("tool_calls")
    if not calls:
        return message
    fixed = []
    for call in calls:
        function = dict(call.get("function") or {})
        try:
            valid = isinstance(json.loads(function.get("arguments") or ""), dict)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            function["arguments"] = "{}"
        fixed.append({**call, "function": function})
    return {**message, "tool_calls": fixed}


class VisualAgent(Agent):
    """`agent.name: ../utils/agent/agent.py:VisualAgent`; params: logs (run records; default $VISUAL_LOG_ROOT, else
    utils/.logs)."""

    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        return self._actor(task, pipeline.arm_of(adapter), adapter)

    def _log_root(self) -> Path:
        if self.params.get("logs"):
            return self.path(self.params["logs"])
        return Path(os.environ.get("VISUAL_LOG_ROOT") or UTILS / ".logs")

    def _system_text(self, session) -> tuple[str, dict]:
        """The system prompt and what the adapter contributed: exposed function tools, skipped and rejected entries."""
        sys_text = texts.ACTING_SYSTEM_PROMPT
        contributed = {"tools": [], "skipped": [], "rejected": [], "catalog_error": None}
        if session is None:
            return sys_text, contributed
        sys_text += "\n\n" + texts.ADAPTED + "\n\n" + session.context()
        if session.has_tool("tool_catalog"):
            try:
                catalog = session.call("tool_catalog", {})
            except Exception as exc:  # noqa: BLE001 - a broken tools/ dir costs the tools, not the run
                catalog, contributed["catalog_error"] = {}, f"{type(exc).__name__}: {str(exc)[:1000]}"
            contributed["tools"], contributed["skipped"] = gh.expose_catalog(catalog)
            contributed["rejected"] = list((catalog or {}).get("rejected") or [])
            if contributed["tools"]:
                sys_text += "\n\n" + texts.ADAPTER_TOOLS_NOTE
        return sys_text, contributed

    @staticmethod
    def _finalize(session, base: Path, final: bytes, seen_pages: list[dict]) -> dict | None:
        """Ask the adapter's `finalize` export (toolsmith) which version to submit; None when it has none. A replaced
        final version is kept under unseen_final/ so that it can be judged later."""
        if session is None or not session.has_tool("finalize"):
            return None
        final_sha = hashlib.sha256(final).hexdigest()
        try:
            decision = session.call("finalize", {"final_sha": final_sha,
                                                 "seen": [{"sha": s["sha"], "call": s["call"]} for s in seen_pages]})
        except Exception as exc:  # noqa: BLE001 - a broken export costs the choice, not the run
            decision = {"submit": final_sha, "replaced": False, "reason": f"finalize failed: {type(exc).__name__}: {exc}"}
        record = {"replaced": False, "reason": str(decision.get("reason") or "")[:500], "final_sha": final_sha,
                  "submitted_call": None}
        chosen = next((s for s in reversed(seen_pages) if s["sha"] == decision.get("submit")), None)
        if decision.get("replaced") and decision.get("submit") != final_sha and chosen is not None:
            (base / "unseen_final").mkdir(exist_ok=True)
            (base / "unseen_final" / "index.html").write_bytes(final)
            record.update(replaced=True, submitted_call=chosen["call"])
        elif decision.get("replaced"):
            record["reason"] += " (no snapshot of the chosen version: the final version is submitted)"
        return record

    def _actor(self, task: dict, arm: str, adapter_dir: Path | None) -> RunOutput:
        harness.check_vendor()      # before any model call: the final capture of this run would need it
        case = task["case"]
        base = _log_dir(self._log_root(), case, arm)
        workspace = base / "workspace"
        workspace.mkdir()
        (workspace / "README.md").write_text(task["prompt"])
        (workspace / "index.html").write_text(pipeline.PLACEHOLDER)
        runner = gh.ConfinedRunner(RUNNER)
        session = None
        if arm == "adapted":
            session = self.session(adapter_dir, resources={"tool_runner": runner, "workspace": str(workspace)})
        sys_text, contributed = self._system_text(session)
        host = gh.Harness(workspace, base, runner=runner, session=session, adapter_tools=contributed["tools"])
        tools_exported = [t["function"]["name"] for t in contributed["tools"]]
        # request.json and result.json keep the format experiment 4's learners read, constant fields included
        # (facts_file, inspection_calls, wm_calls, final_inspected_sha256, final_inspection_matches, extraction).
        gh.save(base / "request.json", {
            "arm": arm, "case": case, "task_id": task["id"], "adapter": str(adapter_dir) if adapter_dir else None,
            "started_utc": pipeline.utc(), "model": pipeline.MODEL,
            "settings": {**pipeline.SAMPLING, "max_tokens": pipeline.MAX_TOKENS},
            "facts_file": None, "system": sys_text, "task": task["prompt"],
            "tools": host.tools, "tools_exported": tools_exported, "tools_skipped": contributed["skipped"],
            "tools_rejected": contributed["rejected"], "catalog_error": contributed["catalog_error"]})
        client = pipeline.make_client()
        messages = [{"role": "system", "content": sys_text}, {"role": "user", "content": task["prompt"]}]
        start = time.monotonic()
        deadline = math.inf if RUN_SECONDS is None else start + RUN_SECONDS
        calls, model_s = 0, 0.0
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
        status, reason = "budget_exhausted", "maximum requests reached"
        image_message_indices: list[int] = []
        pending_seen: dict | None = None    # the look whose images are attached to the next request
        seen_pages: list[dict] = []         # looks whose images an accepted request carried to the model
        last_prompt_tokens, context_exhausted = 0, False
        for _ in range(MAX_REQUESTS):
            remaining = pipeline.COMPLETION_BUDGET - usage["completion_tokens"]
            if remaining <= 0 or time.monotonic() >= deadline:
                reason = "token or time budget exhausted"
                break
            if last_prompt_tokens > CONTEXT_GUARD_TOKENS:
                status = "context_exhausted"
                reason = f"the previous request held {last_prompt_tokens} prompt tokens (more than {CONTEXT_GUARD_TOKENS})"
                context_exhausted = True
                break
            options = {**pipeline.SAMPLING, "max_tokens": min(pipeline.MAX_TOKENS, remaining), "tools": host.tools,
                       "tool_choice": "auto"}
            client.timeout = max(1, min(REQUEST_GUARD_S, deadline - time.monotonic()))
            calls += 1
            record = {"call": calls, "request_message_count": len(messages), "request_image_count": sum(
                1 for m in messages if isinstance(m.get("content"), list)
                for p in m["content"] if p.get("type") == "image_url")}
            call_start = time.monotonic()
            try:
                message, call_usage = client.chat(messages, **options)
            except Exception as exc:  # noqa: BLE001 - recorded; the run ends as api_error
                key = os.environ.get(pipeline.KEY_ENV)     # an error body may echo the key
                message, record["error"] = None, (str(exc).replace(key, "[REDACTED]") if key else str(exc))[:3000]
            model_s += time.monotonic() - call_start
            record["seconds"] = round(time.monotonic() - call_start, 3)
            if message is None:
                gh.save(base / "calls" / f"{calls:02d}.json", {**record, "request_accepted": False})
                status, reason = "api_error", record["error"]
                break
            if pending_seen is not None:     # this accepted request carried the look's images: the model saw them
                seen_pages.append(pending_seen)
                pending_seen = None
            usage["prompt_tokens"] += call_usage.input_tokens
            usage["completion_tokens"] += call_usage.output_tokens
            usage["cached_tokens"] += call_usage.cached_input_tokens
            last_prompt_tokens = call_usage.input_tokens
            finish = message.get("finish_reason")
            gh.save(base / "calls" / f"{calls:02d}.json", {
                **record, "usage": {"prompt_tokens": call_usage.input_tokens, "completion_tokens": call_usage.output_tokens,
                                    "cached_tokens": call_usage.cached_input_tokens},
                "request_accepted": True, "assistant": pipeline.public_message(message), "finish_reason": finish})
            messages.append(_replayable(pipeline.continuing_message(message)))
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                (base / "final.md").write_text(message.get("content") or "")
                status = "completed" if finish != "length" else "output_truncated"
                reason = "model ended its tool loop"
                break
            new_images: list = []
            new_seen: dict | None = None
            for tool_call in tool_calls:
                name = tool_call.get("function", {}).get("name", "")
                try:
                    args = json.loads(tool_call["function"]["arguments"])
                except (ValueError, KeyError, TypeError):
                    args = None
                tool_text, images = host.dispatch(name, args, deadline)
                if not isinstance(args, dict):     # tell the model why, in words it can act on
                    tool_text = json.dumps({"ok": False, "error": "The arguments of this call were not a valid JSON object, so "
                                            "nothing was done." + (" Your response was cut off at the output token limit: "
                                            "write less per call." if finish == "length" else "")})
                messages.append({"role": "tool", "tool_call_id": tool_call.get("id"), "content": tool_text})
                if images:
                    new_images = images  # Deterministic: the most recent image-bearing call only.
                    new_seen = host.last_dispatch_look
            if new_images:
                for j in image_message_indices:
                    messages[j] = {"role": "user", "content": pipeline.IMAGES_REMOVED}
                image_message_indices = [len(messages)]
                messages.append({"role": "user", "content": [{"type": "text", "text": texts.IMAGES_INTRO}, *new_images]})
                pending_seen = new_seen
            if host.calls >= gh.MAX_TOOL_CALLS:
                reason = "tool-call budget exhausted"
                break
        final = workspace / "index.html"
        placeholder = pipeline.PLACEHOLDER.encode()
        submitted = final.read_bytes() if final.is_file() and not final.is_symlink() else placeholder
        seen_pages = [s for s in seen_pages if Path(s["snapshot"]).is_file()
                      and Path(s["snapshot"]).read_bytes() != placeholder]
        finalize = self._finalize(session, base, submitted, seen_pages)
        if finalize and finalize["replaced"]:
            submitted = Path(next(s["snapshot"] for s in seen_pages if s["call"] == finalize["submitted_call"])).read_bytes()
        valid = submitted != placeholder
        if not valid and status == "completed":
            status, reason = "no_artifact", "No index.html was authored with the file tools"
        dest = base / "submission"
        dest.mkdir()
        (dest / "index.html").write_bytes(submitted)
        submission_sha = gh.sha(dest / "index.html")
        cost = client.total.cost_usd
        final_message = (base / "final.md").read_text()[:FINAL_MESSAGE_CHARS] if (base / "final.md").is_file() else ""
        result = {
            "arm": arm, "case": case, "status": status, "stop_reason": reason, "finished_utc": pipeline.utc(),
            "elapsed_seconds": round(time.monotonic() - start, 3), "model_calls": calls, "tool_calls": host.calls,
            "inspection_calls": 0, "wm_calls": 0, "usage": usage, "submission_sha256": submission_sha,
            "final_inspected_sha256": None, "final_inspection_matches": False, "valid_submission": valid,
            "extraction": None, "cost_usd": cost,
            "run_commands": host.run_commands, "adapter_tool_calls": host.adapter_calls,
            "image_bearing_calls": host.image_bearing_calls, "looked_calls": host.looked_calls,
            "looked_blank_calls": host.looked_blank_calls,
            "looked_final": valid and host.last_look_index_sha == submission_sha,
            "seen_final": valid and any(s["sha"] == submission_sha for s in seen_pages),
            "seen_pages": [{"call": s["call"], "sha": s["sha"]} for s in seen_pages], "finalize": finalize,
            "first_image_call": host.first_image_call, "first_look_call": host.first_look_call,
            "tools_exported": tools_exported, "tools_called": host.tools_called, "tool_errors": host.tool_errors,
            "context_exhausted": context_exhausted, "images_total": host.images_total,
            "tool_text_chars": host.text_chars, "confined": True}
        gh.save(base / "result.json", result)
        FINAL_CAPTURE(dest, base / "capture", case)
        output = {k: result[k] for k in OUTPUT_KEYS}
        output["final_message"] = final_message
        speed = round(usage["completion_tokens"] / model_s, 1) if model_s else None
        return RunOutput(output=output, log=base, cost_usd=cost, wall_s=round(time.monotonic() - start, 1),
                         api_speed={"output_tokens_per_s": speed, "model_calls": calls},
                         tokens=usage["prompt_tokens"] + usage["completion_tokens"],
                         extra={**result, "calls": host.summaries})
