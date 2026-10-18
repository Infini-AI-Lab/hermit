"""The generic acting agent (experiment 2): GLM 5.3 Flash on Fireworks writes one offline interactive index.html with
four generic primitives (write_file, read_file, replace_text, run_command) inside the bubblewrap sandbox of
utils/confine.py. It is told nothing about how a page could be rendered; images a command writes into the workspace are
the only pictures it ever sees. An adapter can add to that: a toolsmith adapter's learned tools (its `tool_catalog`)
become function tools whose results may carry images, a prompt-only adapter contributes ADAPTER.md, an MWM adapter its
context package and read-only `wm` tool.

    agent: {name: ../utils/agent/agent.py:VisualAgent, concurrency: 1, params: {facts_file: null}}

Arms: `harness` (B') without an adapter or for the untouched root (no `origin` in adapter.yaml), else `adapted` (C).
`facts_file` (the hinted baseline) names a text file appended to the system prompt after the tool instructions.
Limits, model, prices, the request loop's shape, the records and the host's final capture are experiment 1's
(utils/pipeline.py, utils/harness.py, imported by file); utils/judge.py scores the record unchanged.
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
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
MAX_REQUESTS = pipeline.MAX_REQUESTS["harness"]
CONTEXT_GUARD_TOKENS = 90_000                 # why: a request above this is one tool result away from the model's window
FINAL_MESSAGE_CHARS = 1500
RESULT_KEYS = ("arm", "case", "status", "stop_reason", "finished_utc", "elapsed_seconds", "model_calls", "tool_calls",
               "inspection_calls", "wm_calls", "usage", "submission_sha256", "final_inspected_sha256",
               "final_inspection_matches", "valid_submission", "extraction", "cost_usd", "run_commands",
               "adapter_tool_calls", "image_bearing_calls", "looked_calls", "looked_blank_calls", "looked_final",
               "first_image_call", "first_look_call", "tools_exported", "tools_called", "tool_errors",
               "context_exhausted", "images_total", "tool_text_chars", "confined")
OUTPUT_KEYS = ("case", "arm", "status", "stop_reason", "model_calls", "tool_calls", "run_commands", "adapter_tool_calls",
               "image_bearing_calls", "looked_calls", "first_image_call", "tools_exported", "tools_called", "tool_errors")


def _log_dir(root: Path, case: str, arm: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = root / f"{case}-{arm}-{stamp}-{secrets.token_hex(3)}"
    base.mkdir(parents=True)
    return base


class VisualAgent(Agent):
    """`agent.name: ../utils/agent/agent.py:VisualAgent`; params: facts_file (text appended to the system prompt),
    logs (run records; default $VISUAL_LOG_ROOT, else utils/.logs)."""

    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        arm = pipeline.arm_of(adapter)
        with pipeline.no_bytecode(), pipeline.agent_view(adapter if arm == "adapted" else None) as view:
            return self._actor(task, arm, adapter, view)

    def _log_root(self) -> Path:
        if self.params.get("logs"):
            return self.path(self.params["logs"])
        return Path(os.environ.get("VISUAL_LOG_ROOT") or UTILS / ".logs")

    def _system_text(self, session) -> tuple[str, dict]:
        """The system prompt and what the adapter contributed: exposed function tools, skipped and rejected entries,
        the wm spec."""
        sys_text = texts.ACTING_SYSTEM_PROMPT
        facts_file = self.params.get("facts_file")
        if facts_file:
            sys_text += "\n\n" + self.path(facts_file).read_text().strip()
        contributed = {"tools": [], "skipped": [], "rejected": [], "wm_spec": None, "catalog_error": None}
        if session is None:
            return sys_text, contributed
        if session.has_tool("context_package"):             # the MWM template: its live five-part package
            package = session.call("context_package", {})["text"]
        else:                                               # any other template: its context export
            package = session.context()
        sys_text += "\n\n" + (texts.BRIDGE if session.has_tool("wm") else texts.ADAPTED) + "\n\n" + package
        if session.has_tool("tool_catalog"):
            try:
                catalog = session.call("tool_catalog", {})
            except Exception as exc:  # noqa: BLE001 - a broken tools/ dir costs the tools, not the run
                catalog, contributed["catalog_error"] = {}, f"{type(exc).__name__}: {str(exc)[:1000]}"
            contributed["tools"], contributed["skipped"] = gh.expose_catalog(catalog)
            contributed["rejected"] = list((catalog or {}).get("rejected") or [])
            if contributed["tools"]:
                sys_text += "\n\n" + texts.ADAPTER_TOOLS_NOTE
        if session.has_tool("wm"):
            tool = next(t for t in session.tools if t["name"] == "wm")
            contributed["wm_spec"] = {"type": "function", "function": {
                "name": "wm", "description": tool["description"] + texts.WM_SUFFIX, "parameters": tool["input_schema"]}}
        return sys_text, contributed

    def _actor(self, task: dict, arm: str, adapter_dir: Path | None, view: Path | None) -> RunOutput:
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
            session = self.session(view, resources={"tool_runner": runner, "workspace": str(workspace)})
        sys_text, contributed = self._system_text(session)
        host = gh.Harness(workspace, base, case, runner=runner, session=session, adapter_tools=contributed["tools"],
                          wm_spec=contributed["wm_spec"])
        tools_exported = [t["function"]["name"] for t in contributed["tools"]]
        facts_file = self.params.get("facts_file")
        gh.save(base / "request.json", {
            "arm": arm, "case": case, "task_id": task["id"], "adapter": str(adapter_dir) if adapter_dir else None,
            "started_utc": pipeline.utc(), "model": pipeline.MODEL,
            "settings": {**pipeline.SAMPLING, "max_tokens": pipeline.MAX_TOKENS},
            "facts_file": str(self.path(facts_file)) if facts_file else None, "system": sys_text, "task": task["prompt"],
            "tools": host.tools, "tools_exported": tools_exported, "tools_skipped": contributed["skipped"],
            "tools_rejected": contributed["rejected"], "catalog_error": contributed["catalog_error"]})
        client = pipeline.make_client()
        messages = [{"role": "system", "content": sys_text}, {"role": "user", "content": task["prompt"]}]
        start = time.monotonic()
        deadline = start + pipeline.ACTOR_SECONDS
        calls, model_s = 0, 0.0
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
        status, reason = "budget_exhausted", "maximum requests reached"
        image_message_indices: list[int] = []
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
            client.timeout = max(1, min(pipeline.REQUEST_TIMEOUT, deadline - time.monotonic()))
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
            usage["prompt_tokens"] += call_usage.input_tokens
            usage["completion_tokens"] += call_usage.output_tokens
            usage["cached_tokens"] += call_usage.cached_input_tokens
            last_prompt_tokens = call_usage.input_tokens
            finish = message.get("finish_reason")
            gh.save(base / "calls" / f"{calls:02d}.json", {
                **record, "usage": {"prompt_tokens": call_usage.input_tokens, "completion_tokens": call_usage.output_tokens,
                                    "cached_tokens": call_usage.cached_input_tokens},
                "request_accepted": True, "assistant": pipeline.public_message(message), "finish_reason": finish})
            messages.append(pipeline.continuing_message(message))
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                (base / "final.md").write_text(message.get("content") or "")
                status = "completed" if finish != "length" else "output_truncated"
                reason = "model ended its tool loop"
                break
            new_images: list = []
            for tool_call in tool_calls:
                name = tool_call.get("function", {}).get("name", "")
                try:
                    args = json.loads(tool_call["function"]["arguments"])
                except (ValueError, KeyError, TypeError):
                    args = None
                tool_text, images = host.dispatch(name, args, deadline)
                messages.append({"role": "tool", "tool_call_id": tool_call.get("id"), "content": tool_text})
                if images:
                    new_images = images  # Deterministic: the most recent image-bearing call only, as experiment 1.
            if new_images:
                for j in image_message_indices:
                    messages[j] = {"role": "user", "content": pipeline.IMAGES_REMOVED}
                image_message_indices = [len(messages)]
                messages.append({"role": "user", "content": [{"type": "text", "text": texts.IMAGES_INTRO}, *new_images]})
            if host.calls >= gh.MAX_TOOL_CALLS:
                reason = "tool-call budget exhausted"
                break
        final = workspace / "index.html"
        placeholder = pipeline.PLACEHOLDER.encode()
        submitted = final.read_bytes() if final.is_file() and not final.is_symlink() else placeholder
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
            "inspection_calls": 0, "wm_calls": host.wm_calls, "usage": usage, "submission_sha256": submission_sha,
            "final_inspected_sha256": None, "final_inspection_matches": False, "valid_submission": valid,
            "extraction": None, "cost_usd": cost,
            "run_commands": host.run_commands, "adapter_tool_calls": host.adapter_calls,
            "image_bearing_calls": host.image_bearing_calls, "looked_calls": host.looked_calls,
            "looked_blank_calls": host.looked_blank_calls,
            "looked_final": valid and host.last_look_index_sha == submission_sha,
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
