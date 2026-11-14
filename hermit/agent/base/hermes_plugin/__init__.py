"""hermit_stream: a Hermes plugin that writes the session's events as JSON lines on its stdout.

Hermes's `chat -Q` prints only its final reply; the `hermes` Base Agent (hermit.agent.base.cli.Hermes) copies this
plugin into the session's home (HERMES_HOME/plugins/hermit_stream) and enables it, so that the session's stdout, the
stream hermit keeps, carries one event per hook Hermes fires, with Hermes's own fields:

  hermes.settings        what the plugin set when Hermes loaded it (hermit.json beside this file): whether Hermes's
                         subdirectory hints are off (`hints_off`) for a session that is to read no context file
  hermes.session_start   a new session (its id)
  hermes.request_tools   the first model request's tools by name, and the MCP servers hermit gave the session
                         (`expected`): a server Hermes could not reach in time leaves its tools out, and Hermes goes
                         on without them; and the context files Hermes's prompt-injection scan refused (`blocked`:
                         the whole file is replaced by a `[BLOCKED: ...]` notice, which the model alone sees)
  hermes.api_request     a model request that answered: its text, reasoning, tool calls, finish reason and usage
  hermes.api_error       a model request that failed: Hermes's classification (`reason`), status, whether it retries
  hermes.tool            a tool call that ran: its name, arguments, result and status
  hermes.reply           the turn's final reply
  hermes.session_end     the turn's end: completed, interrupted; and the session's auxiliary calls (compression, ...)
                         as Hermes recorded them (`aux`: running totals per task, which fire no hook of their own)

It runs inside Hermes's process, which loads it from the home: the standard library only, and nothing it does may
fail the session (Hermes swallows a hook's error; this one never raises). Hermes has no setting for its subdirectory
hints (a folder's AGENTS.md added to the result of a tool that touches it): for a session that is to read no context
file, the plugin turns them off in the process (`agent.subdirectory_hints`, Hermes 0.19), and says whether it could. Events are written to the process's
descriptor 1 directly, whatever Hermes does with sys.stdout, each in one write under a lock (tools may run in threads).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import threading

_LOCK = threading.Lock()
_SESSIONS: list[str] = []          # every session id this process reported (compression continues in a new one)
_TOLD: list[bool] = []             # the first request's tools are reported once
BLOCKED = re.compile(r"\[BLOCKED: (.+?) contained potential prompt injection \(([^)]*)\)\. Content not loaded\.\]")


def _settings() -> dict:
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hermit.json"), encoding="utf-8") as f:
            found = json.load(f)
        return found if isinstance(found, dict) else {}
    except Exception:
        return {}


def _expected() -> list[str]:
    servers = _settings().get("mcp_servers")
    return [str(s) for s in servers] if isinstance(servers, list) else []


def _hints_off() -> bool:
    """Hermes's subdirectory hints off in this process: no folder's AGENTS.md, CLAUDE.md or .cursorrules is added to a
    tool's result. Whether it could (a Hermes without the tracker: no)."""
    try:
        from agent import subdirectory_hints
        subdirectory_hints.SubdirectoryHintTracker.check_tool_call = lambda self, *args, **kwargs: None
        return True
    except Exception:
        return False


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return ""


def _emit(kind: str, **fields) -> None:
    try:
        line = (json.dumps({"type": f"hermes.{kind}", **fields}, ensure_ascii=False, default=str) + "\n").encode()
        with _LOCK:
            view = memoryview(line)
            while view:
                view = view[os.write(1, view):]
    except Exception:              # a closed stdout, an object json cannot render: the session goes on
        pass


def _seen(session_id) -> str:
    sid = str(session_id or "")
    if sid and sid not in _SESSIONS:
        _SESSIONS.append(sid)
    return sid


def _tool_calls(message) -> list[dict]:
    out = []
    for call in getattr(message, "tool_calls", None) or []:
        function = getattr(call, "function", None)
        if isinstance(call, dict):
            function = call.get("function") or {}
            out.append({"id": call.get("id"), "name": function.get("name"), "arguments": function.get("arguments")})
        else:
            out.append({"id": getattr(call, "id", None), "name": getattr(function, "name", None),
                        "arguments": getattr(function, "arguments", None)})
    return out


def _reasoning(message) -> str | None:
    for name in ("reasoning_content", "reasoning"):
        value = message.get(name) if isinstance(message, dict) else getattr(message, name, None)
        if isinstance(value, str) and value:
            return value
    return None


def on_session_start(session_id=None, model=None, **_):
    _emit("session_start", session_id=_seen(session_id), model=model)


def pre_api_request(session_id=None, request=None, request_messages=None, **_):
    if _TOLD:
        return
    _TOLD.append(True)
    body = request.get("body") if isinstance(request, dict) and isinstance(request.get("body"), dict) else {}
    names = []
    for tool in body.get("tools") or []:
        if isinstance(tool, dict):
            function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
            names.append(function.get("name") or tool.get("name"))
    messages = request_messages if isinstance(request_messages, list) else body.get("messages") or []
    system = "".join(_text(m.get("content")) for m in messages if isinstance(m, dict) and m.get("role") == "system")
    system += _text(body.get("system"))                     # the Messages API's, beside the messages
    blocked = [{"file": f, "findings": why} for f, why in BLOCKED.findall(system)]
    _emit("request_tools", session_id=_seen(session_id), names=[n for n in names if n], expected=_expected(),
          blocked=blocked)


def post_api_request(session_id=None, assistant_message=None, finish_reason=None, usage=None, model=None,
                     response_model=None, api_call_count=None, api_mode=None, **_):
    content = assistant_message.get("content") if isinstance(assistant_message, dict) else \
        getattr(assistant_message, "content", None)
    _emit("api_request", session_id=_seen(session_id), api_call_count=api_call_count, model=model,
          response_model=response_model, api_mode=api_mode, finish_reason=finish_reason,
          content=content if isinstance(content, str) else (None if content is None else str(content)),
          reasoning=_reasoning(assistant_message), tool_calls=_tool_calls(assistant_message),
          usage=usage if isinstance(usage, dict) else None)


def api_request_error(session_id=None, status_code=None, reason=None, retryable=None, retry_count=None,
                      max_retries=None, error=None, api_call_count=None, model=None, **_):
    _emit("api_error", session_id=_seen(session_id), api_call_count=api_call_count, model=model,
          status_code=status_code, reason=reason, retryable=retryable, retry_count=retry_count,
          max_retries=max_retries, error=error if isinstance(error, dict) else {"message": str(error or "")})


def post_tool_call(session_id=None, tool_name=None, args=None, result=None, tool_call_id=None, status=None,
                   error_type=None, error_message=None, duration_ms=None, **_):
    _emit("tool", session_id=_seen(session_id), tool_name=tool_name, tool_call_id=tool_call_id, args=args,
          result=result if isinstance(result, str) or result is None else json.dumps(result, default=str),
          status=status, error_type=error_type, error_message=error_message, duration_ms=duration_ms)


def post_llm_call(session_id=None, assistant_response=None, **_):
    _emit("reply", session_id=_seen(session_id), text=assistant_response)


def _aux() -> list[dict]:
    """The auxiliary calls Hermes recorded for this process's sessions (session_model_usage rows whose `task` is not
    the main loop's ''): running totals, read-only."""
    home = os.environ.get("HERMES_HOME")
    if not home or not _SESSIONS:
        return []
    try:
        with contextlib.closing(sqlite3.connect(f"file:{os.path.join(home, 'state.db')}?mode=ro", uri=True,
                                                timeout=5)) as c:
            marks = ",".join("?" * len(_SESSIONS))
            rows = c.execute("SELECT session_id, task, model, api_call_count, input_tokens, output_tokens, "
                             "cache_read_tokens, cache_write_tokens, reasoning_tokens FROM session_model_usage "
                             f"WHERE session_id IN ({marks}) AND COALESCE(task, '') != ''", _SESSIONS).fetchall()
        names = ("session_id", "task", "model", "api_call_count", "input_tokens", "output_tokens",
                 "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
        return [dict(zip(names, row)) for row in rows]
    except Exception:
        return []


def on_session_end(session_id=None, completed=None, interrupted=None, reason=None, **_):
    _emit("session_end", session_id=_seen(session_id), completed=completed, interrupted=interrupted, reason=reason,
          sessions=list(_SESSIONS), aux=_aux())


def register(ctx) -> None:
    if _settings().get("subdirectory_hints") is False:
        _emit("settings", hints_off=_hints_off())
    for name in ("on_session_start", "pre_api_request", "post_api_request", "api_request_error", "post_tool_call",
                 "post_llm_call", "on_session_end"):
        ctx.register_hook(name, globals()[name])
