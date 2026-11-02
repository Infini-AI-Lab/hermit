#!/usr/bin/env python3
"""The hook runner Codex starts on every lifecycle event (hooks.json); it calls middleware.py. Fixed, not a tool to run.

    python3 -B hook_runner.py <HostEvent> [--strict]

Codex runs the command of hooks.json from the adapter's copy in the workspace (a deployment writes `cd <copy> ||
exit 1` before it), with the event's JSON on stdin; stdout is the wire JSON of hermit_hooks.translate or nothing. The
adapter is this file's directory: middleware.py and hermit_hooks.py are its siblings, and `inp.adapter_dir` names it.
Stdlib only: nothing of the framework is installed where the agent runs.

The exit code is always 0: a learned middleware can never abort a trial (the hosts' exit-2 convention is not used),
so a middleware exception, a return of the wrong shape, a timeout, or anything else that goes wrong produce no output
and an `error` audit line instead; `--strict` (tests) turns them into a non-zero exit with the traceback on stderr.
While the middleware is imported and called, stdout is stderr (the file descriptor too, so a child process it spawns
is covered): the host reads stdout as one JSON document, and a print would corrupt it.

- Logs: under <B> = $HERMIT_HOOK_LOGS when set, else `<cwd>/.hermit-hooks` for the payload's (absolute) cwd, the
  session's workspace. A payload without one gets neither an audit line nor a state that outlives the call.
- State: one dict per session in <B>/hook-state/<session_id>.json, held under fcntl.flock from load to save, as a host
  runs tool hooks concurrently and sub-agents share the session id. A state that cannot be written (a non-string key,
  a cycle) leaves the file as it was and is the call's error; the decision the middleware returned alongside is still
  printed. The prompt of the session's first UserPromptSubmit is kept there (`_task_text`) and is `inp.task_text` of
  every later call; at SessionStart, which Codex fires before the prompt is submitted, `inp.task_text` is "".
- Audit: one line per call, {ts, event, session_id, tool_name, command[:200], decision, reason[:200], context_chars,
  ms, error[:500]}, appended to <B>/hook-calls.jsonl.
- Timeout: one deadline for the call, signal.alarm(TIMEOUT_S - 2) armed before the wait for the session lock and
  covering the middleware (its import included); the host's own `timeout` (hooks.json: TIMEOUT_S) kills a slow hook
  silently and fail-open, so the runner's alarm fires first and leaves the audit line. The alarm is a BaseException,
  so a middleware's `except Exception` does not swallow it.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
import traceback

sys.dont_write_bytecode = True                      # nothing is written into the adapter's copy
ADAPTER = Path(__file__).resolve().parent


def _interface():
    """hermit_hooks.py beside this file, under the name a middleware imports (`from hermit_hooks import ...`)."""
    module = sys.modules.get("hermit_hooks")
    if module is None:
        spec = importlib.util.spec_from_file_location("hermit_hooks", ADAPTER / "hermit_hooks.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["hermit_hooks"] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules["hermit_hooks"]
            raise
    return module


hooks = _interface()
HOST = "codex"                                    # the hook wire is Claude Code's; Codex 0.164 speaks it
TIMEOUT_S = 30                                      # hooks.json's timeout of every event
LOGS_ENV = "HERMIT_HOOK_LOGS"                       # the log base, when set
LOGS_DIR = ".hermit-hooks"                          # else the log base under the session's workspace
AUDIT = "hook-calls.jsonl"
STATE = "hook-state"
TASK = "_task_text"                                 # the runner's key in the session state: the first prompt
ERROR_CHARS = 500                                   # an exception text may embed the tool input


class MiddlewareTimeout(BaseException):
    """The runner's alarm. Not an Exception: a middleware's `except Exception` must not catch it."""


@contextlib.contextmanager
def quiet_stdout():
    """Stdout is stderr inside: sys.stdout for print(), file descriptor 1 for a child process the code spawns.
    Whatever is printed is noise on stderr instead of a corrupt hook wire."""
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        with contextlib.redirect_stdout(sys.stderr):
            yield
    finally:
        sys.stderr.flush()
        os.dup2(saved, 1)
        os.close(saved)


def load_middleware(path: Path, adapter: Path):
    """Import the middleware module as a module of its own with the adapter root on sys.path, so a sibling module
    is imported by its bare name (`import rules`). The template check's dry run imports through here too: what passes
    the check is what the runner can import. No bytecode cache is written into the adapter."""
    sys.dont_write_bytecode = True
    root = str(Path(adapter).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    spec = importlib.util.spec_from_file_location("adapter_middleware", path)
    module = importlib.util.module_from_spec(spec)
    with quiet_stdout():
        spec.loader.exec_module(module)
    return module


def _alarm(signum, frame):
    raise MiddlewareTimeout("middleware timed out")


def call_middleware(path: Path, adapter: Path, function: str, inp: hooks.HookInput, state: dict):
    """Import the module and call `function`, stdout silenced; the caller's alarm bounds it."""
    module = load_middleware(path, adapter)
    fn = getattr(module, function, None)
    if fn is None:
        return None
    with quiet_stdout():
        value = fn(inp, state)
    return hooks.as_result(value)


def log_base(payload: dict) -> Path | None:
    """Where the call's audit line and the session's state go: $HERMIT_HOOK_LOGS, else `.hermit-hooks` under the
    payload's cwd (the session's workspace); None without either."""
    if os.environ.get(LOGS_ENV):
        return Path(os.environ[LOGS_ENV])
    cwd = payload.get("cwd")
    return Path(cwd) / LOGS_DIR if isinstance(cwd, str) and Path(cwd).is_absolute() else None


def _session_file(state_dir: Path, session_id: str) -> Path:
    name = re.sub(r"[^\w.-]", "_", session_id or "unknown")
    if len(name) > 100:                              # a file name has a limit; keep the id recognisable
        name = name[:100] + "-" + hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:8]
    return state_dir / (name + ".json")


class SessionState:
    """The session's state dict, loaded under an exclusive flock and written back on exit. `error` names a state
    that could not be serialised: the file is then left as it was."""

    def __init__(self, path: Path | None):
        self.path = path
        self.state = {}
        self.error = None
        self._fd = None
        self._locked = False

    def __enter__(self) -> dict:
        if self.path is None:
            return self.state
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX)     # the alarm may fire here: the lock wait is part of the call
            self._locked = True
            text = os.read(self._fd, os.fstat(self._fd).st_size).decode("utf-8", "replace")
        except BaseException:
            self.close()
            raise
        try:
            self.state = json.loads(text) if text.strip() else {}
        except ValueError:
            self.state = {}
        if not isinstance(self.state, dict):
            self.state = {}
        return self.state

    def __exit__(self, *exc):
        if self._fd is None:
            return
        try:
            if self._locked:
                try:
                    data = json.dumps(self.state, ensure_ascii=False, default=str).encode("utf-8")
                except (TypeError, ValueError) as error:
                    self.error = f"state not saved: {type(error).__name__}: {error}"
                    return
                os.lseek(self._fd, 0, os.SEEK_SET)
                os.ftruncate(self._fd, 0)
                os.write(self._fd, data)
        finally:
            self.close()

    def close(self):
        if self._fd is not None:
            if self._locked:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd, self._locked = None, False


def audit(path: Path | None, record: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:ERROR_CHARS]


def run(event: str, stdin_text: str, *, adapter: Path = ADAPTER, strict: bool = False) -> int:
    """One hook call for the host event `event`: returns the exit code, prints the wire JSON (if any) on stdout."""
    started = time.monotonic()
    adapter = Path(adapter)
    function = hooks.EVENTS.get(event, "")
    record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, "session_id": "", "tool_name": None,
              "command": "", "decision": None, "reason": "", "context_chars": 0, "ms": 0, "error": None}
    error, result, out, text, stop_hook_active, base = None, None, None, None, False, None
    # One deadline for the whole call, the wait for the session lock included; disarmed before the output and
    # the audit line are written, so that neither is cut.
    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(max(1, TIMEOUT_S - 2))
    try:
        try:
            payload = json.loads(stdin_text) if stdin_text.strip() else {}
            if not isinstance(payload, dict):
                raise TypeError("hook payload must be a JSON object")
        except (ValueError, TypeError) as exc:
            payload, error = {}, _describe(exc)
        base = log_base(payload)
        session_id = record["session_id"] = str(payload.get("session_id") or "")
        record["tool_name"] = payload.get("tool_name")
        named = payload.get("hook_event_name")
        if named is not None and named != event and error is None:
            error = f"hook_event_name {named!r} does not match the runner's event {event!r}"
        state_file = _session_file(base / STATE, session_id) if base is not None else None
        session = SessionState(state_file)
        with session as state:
            prompt = payload.get("prompt")
            if event == "UserPromptSubmit" and not state.get(TASK) and isinstance(prompt, str) and prompt.strip():
                state[TASK] = prompt                  # the task instruction, for every later call of the session
            task_text = state.get(TASK) if isinstance(state.get(TASK), str) else ""
            inp = hooks.normalise(HOST, payload, str(adapter), task_text, state, event=function or None)
            record["command"] = (inp.command or "")[:200]
            if function == "":
                error = error or f"unknown event {event!r}"
            elif function is not None and error is None:
                try:
                    result = call_middleware(adapter / "middleware.py", adapter, function, inp, state)
                except (Exception, SystemExit, MiddlewareTimeout) as exc:   # a sys.exit inside is its error, not ours
                    error = _describe(exc)
                    if strict:
                        traceback.print_exc()
            stop_hook_active = bool(payload.get("stop_hook_active")) if event == "Stop" else False
            if function != "" and error is None:
                try:
                    out = hooks.translate(HOST, event, result, state, stop_hook_active=stop_hook_active)
                except ValueError as exc:
                    error = _describe(exc)
                    if strict:
                        traceback.print_exc()
        error = error or session.error
        if out is not None:
            try:
                text = json.dumps(out, ensure_ascii=False)
            except (TypeError, ValueError) as exc:        # a value the middleware put in updated_input
                out, error = None, _describe(exc)
    except BaseException as exc:                      # whatever else: no output, the error on the audit line
        out, text, error = None, None, error or _describe(exc)
        if strict:
            traceback.print_exc()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    if text is not None:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    specific = (out or {}).get("hookSpecificOutput", {})
    context = specific.get("additionalContext") or ""
    if function == "before_model" and result is not None and result.context:
        context = result.context                 # queued, not printed
    reason = (result.reason if result else "") or ""
    if result is not None and result.decision and event == "Stop" and stop_hook_active:
        reason = "stop_hook_active"
    record.update({
        "decision": (out or {}).get("decision") or specific.get("permissionDecision") or (result.decision if result else None),
        "reason": reason[:200], "context_chars": len(context), "ms": int((time.monotonic() - started) * 1000),
        "error": error})
    audit(base / AUDIT if base is not None else None, record)
    if error is not None and strict:
        sys.stderr.write(f"hook_runner: {error}\n")
        return 1
    return 0


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    strict = "--strict" in argv
    words = [a for a in argv if a != "--strict"]
    sys.exit(run(words[0] if len(words) == 1 else " ".join(words), sys.stdin.read(), strict=strict))


if __name__ == "__main__":
    main()
