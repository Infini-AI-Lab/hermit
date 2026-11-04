#!/usr/bin/env python3
"""The hook runner a host starts on each lifecycle event (hooks.json); it calls middleware.py. Fixed, not a tool to run.

    python3 -B hook_runner.py <HostEvent> [--host codex|claude-code] [--strict]

A deployment (hermit.hosts.codex or hermit.hosts.claude_code) installs hooks.json in the host's own place and runs
each command from the adapter's copy in the workspace (`cd <copy> || exit 1` before it); the host passes the event's
JSON on stdin and reads stdout as its hook output, or nothing. The adapter is this file's directory: middleware.py,
hermit_hooks.py (the interface) and hook_hosts.py (the hosts' wires) are its siblings, and `inp.adapter_dir` names it.
Stdlib only: nothing of the framework is installed where the agent runs.

- Host: the deployment's to say. The runner reads `host` from the deployment's record (`.hermit-deploy.json` in the
  workspace the copy was deployed to, which hermit.hosts.<host>.prepare writes), or takes `--host` (the tests' runs of
  a copy that was not deployed). Without either the call is an error: no output. Everything that differs between
  hosts (events, payloads, outputs, what each can do) is hook_hosts.py's.
- The exit code is always 0: a learned middleware can never abort a trial (the hosts' exit-2 convention is not used),
  so a middleware exception, a return of the wrong shape, a timeout, or anything else that goes wrong produce no
  output and an `error` audit line instead; `--strict` (tests) turns them into a non-zero exit with the traceback on
  stderr. While the middleware is imported and called, stdout is stderr (the file descriptor too, so a child process
  it spawns is covered): the host reads stdout as one JSON document, and a print would corrupt it.
- Workspace: the directory of the deployment's record; for a copy that was not deployed, the host's own notion
  (Codex: the payload's cwd; Claude Code: $CLAUDE_PROJECT_DIR, else the payload's cwd). `inp.workspace` names it.
- Logs: under <B> = $HERMIT_HOOK_LOGS when set, else `<workspace>/.hermit-hooks`. A call with neither gets no audit
  line and no state that outlives it.
- State: one dict per session in <B>/hook-state/<session_id>.json, held under fcntl.flock from load to save, as a host
  runs tool hooks concurrently and sub-agents share the session id. A state that cannot be written (a non-string key,
  a cycle) leaves the file as it was and is the call's error; the decision the middleware returned alongside is still
  printed. The prompt of the session's first UserPromptSubmit is kept there (`_prompt`).
- Task text: `inp.task_text` is `task.md` in the workspace when it has one, read at every call: the task's own file
  (OfficeQA's prepare writes the question there and the prompt only names it). Else it is the session's first prompt,
  so "" before that prompt (at SessionStart, which the hosts fire before the prompt is submitted).
- before_model on a host without a model-request event (Claude Code): the runner calls it where a model request
  follows, after the event's own function and output (hook_hosts: `model_follows`), so that its queued context goes
  out with the next event, as Codex's PreModelRequest's does. That call has an audit line of its own (event
  PreModelRequest, `at` the host event), and its error costs only its own context: a state it leaves unsaveable is
  put back as it was before it.
- Queue: per agent of the session (hermit_hooks.pending_key): a sub-agent the host names (agent_id) has its own.
- Audit: one line per call, {ts, event, session_id, tool_name, command[:200], decision, reason[:200], context_chars,
  ms, error[:500]} (+ `at` for a before_model the runner called), appended to <B>/hook-calls.jsonl. The output and the
  audit line are written as UTF-8 with any unencodable character escaped, so no text a middleware returns can make
  the call fail.
- Timeout: one deadline for the whole call, signal.alarm(TIMEOUT_S - 2) armed before the wait for the session lock and
  covering the middleware (its import, and a before_model the runner calls, included); the host's own `timeout`
  (hooks.json: TIMEOUT_S) kills a slow hook silently and fail-open, so the runner's alarm fires first and leaves the
  audit line. The alarm is a BaseException, so a middleware's `except Exception` does not swallow it. When it fires
  in a before_model the runner called, the event's own output is still printed.
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


def _sibling(name: str):
    """A fixed module beside this file, under the name the others import it by (`from hermit_hooks import ...`)."""
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, ADAPTER / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[name]
            raise
    return module


hooks = _sibling("hermit_hooks")
hosts = _sibling("hook_hosts")
TIMEOUT_S = 30                                      # hooks.json's timeout of every event
LOGS_ENV = "HERMIT_HOOK_LOGS"                       # the log base, when set
LOGS_DIR = ".hermit-hooks"                          # else the log base under the session's workspace
AUDIT = "hook-calls.jsonl"
STATE = "hook-state"
TASK_FILE = "task.md"                               # the task's own file in the session's workspace, when it has one
PROMPT = "_prompt"                                  # the runner's key in the session state: the session's first prompt
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
    is imported by its bare name (`import rules`). No bytecode cache is written into the adapter."""
    sys.dont_write_bytecode = True
    root = str(Path(adapter).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    spec = importlib.util.spec_from_file_location("adapter_middleware", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module                  # as an import would: a dataclass resolves its module by name
    try:
        with quiet_stdout():
            spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _alarm(signum, frame):
    raise MiddlewareTimeout("middleware timed out")


def call_middleware(module, function: str, inp: hooks.HookInput, state: dict):
    """Call `function` of the imported module, stdout silenced; the caller's alarm bounds it."""
    fn = getattr(module, function, None)
    if fn is None:
        return None
    with quiet_stdout():
        value = fn(inp, state)
    return hooks.as_result(value)


def log_base(workspace: str | None) -> Path | None:
    """Where the call's audit line and the session's state go: $HERMIT_HOOK_LOGS, else `.hermit-hooks` under the
    session's workspace; None without either."""
    if os.environ.get(LOGS_ENV):
        return Path(os.environ[LOGS_ENV])
    return Path(workspace) / LOGS_DIR if workspace and Path(workspace).is_absolute() else None


def task_text(workspace: str | None, state: dict) -> str:
    """`inp.task_text`: `task.md` in the workspace when it is a file there (read now: the task's own text), else the
    session's first prompt as the state keeps it, else ""."""
    if workspace and Path(workspace).is_absolute():
        path = Path(workspace) / TASK_FILE
        try:
            if path.is_file():                       # a regular file only: a pipe would block the call
                return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    prompt = state.get(PROMPT)
    return prompt if isinstance(prompt, str) else ""


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
                    data = _state_bytes(self.state)
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


def _state_bytes(state: dict) -> bytes:
    """The state as it is saved: JSON, UTF-8, an unencodable character (a lone surrogate) escaped, which json.loads
    reads back as it was."""
    return json.dumps(state, ensure_ascii=False, default=str).encode("utf-8", "backslashreplace")


def audit(path: Path | None, record: dict) -> None:
    """One line on the audit log. A log that cannot be written loses the line, never the call (fail open)."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", errors="backslashreplace") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"hook_runner: no audit line: {_describe(exc)}\n")


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:ERROR_CHARS]


def _record(event: str, payload: dict) -> dict:
    return {"ts": datetime.now(timezone.utc).isoformat(), "event": event,
            "session_id": str(payload.get("session_id") or ""), "tool_name": payload.get("tool_name"), "command": "",
            "decision": None, "reason": "", "context_chars": 0, "ms": 0, "error": None}


def run(event: str, stdin_text: str, *, adapter: Path = ADAPTER, strict: bool = False, host: str | None = None) -> int:
    """One hook call for the host event `event`: returns the exit code, prints the host's output (if any) on stdout.
    `host`: the host's name when the copy has no deployment record to say it (hook_hosts.resolve)."""
    started = time.monotonic()
    adapter = Path(adapter)
    record = _record(event, {})
    error, result, out, text, stop_hook_active, base = None, None, None, None, False, None
    wire, function, module, ready, agent = None, "", None, False, None
    follow = None                                   # the audit line of a before_model the runner calls, if any
    # One deadline for the middleware, the wait for the session lock included; disarmed once the middleware has
    # returned, so that neither the state, the output nor the audit lines are cut.
    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(max(1, TIMEOUT_S - 2))
    try:
        try:
            payload = json.loads(stdin_text) if stdin_text.strip() else {}
            if not isinstance(payload, dict):
                raise TypeError("hook payload must be a JSON object")
        except (ValueError, TypeError) as exc:
            payload, error = {}, _describe(exc)
        record = _record(event, payload)
        try:
            wire, workspace = hosts.resolve(host, adapter, payload)
        except LookupError as exc:
            workspace = hosts.payload_cwd(payload)    # the audit line still finds the session's directory
            error = error or _describe(exc)
        base = log_base(workspace)
        function = wire.events.get(event, "") if wire is not None else ""
        named = payload.get("hook_event_name")
        if named is not None and named != event and error is None:
            error = f"hook_event_name {named!r} does not match the runner's event {event!r}"
        state_file = _session_file(base / STATE, record["session_id"]) if base is not None else None
        session = SessionState(state_file)
        with session as state:
            prompt = payload.get("prompt")
            if event == "UserPromptSubmit" and not state.get(PROMPT) and isinstance(prompt, str) and prompt.strip():
                state[PROMPT] = prompt                # the task text of a workspace without task.md, from now on
            task = task_text(workspace, state)
            if wire is not None:
                inp = wire.normalise(event, payload, str(adapter), workspace or "", task, state,
                                     function=function or None)
                record["command"] = (inp.command or "")[:200]
                agent = inp.agent_id
                if function == "":
                    error = error or f"unknown event {event!r}"     # not an event of this host's wire
            ready = error is None                     # the call itself is sound: a model request follows as usual
            if function and ready:
                try:
                    module = load_middleware(adapter / "middleware.py", adapter)
                    result = call_middleware(module, function, inp, state)
                except (Exception, SystemExit, MiddlewareTimeout) as exc:   # a sys.exit inside is its error, not ours
                    error = _describe(exc)
                    if strict:
                        traceback.print_exc()
            stop_hook_active = bool(payload.get("stop_hook_active")) if function == "stop" else False
            if function != "" and error is None:
                try:
                    out = wire.translate(event, result, state, stop_hook_active=stop_hook_active, agent_id=agent)
                except ValueError as exc:
                    error = _describe(exc)
                    if strict:
                        traceback.print_exc()
            if ready and wire.model_follows(event, result, stop_hook_active):
                follow = _before_model(wire, module, event, payload, adapter, workspace, task, state, strict)
            signal.alarm(0)
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
        sys.stdout.flush()
        sys.stdout.buffer.write((text + "\n").encode("utf-8", "backslashreplace"))
        sys.stdout.buffer.flush()
    specific = (out or {}).get("hookSpecificOutput", {})
    context = wire.delivered(event, out, result) if wire is not None else ""
    if function == "before_model" and result is not None and result.context:
        context = result.context                 # queued, not printed
    reason = (result.reason if result else "") or ""
    if result is not None and result.decision and function == "stop" and stop_hook_active:
        reason = "stop_hook_active"
    record.update({
        "decision": (out or {}).get("decision") or specific.get("permissionDecision") or (result.decision if result else None),
        "reason": reason[:200], "context_chars": len(context), "ms": int((time.monotonic() - started) * 1000),
        "error": error})
    log = base / AUDIT if base is not None else None
    audit(log, record)
    if follow is not None:
        audit(log, follow)
    failed = error or (follow or {}).get("error")
    if failed is not None and strict:
        sys.stderr.write(f"hook_runner: {failed}\n")
        return 1
    return 0


def _before_model(wire, module, event: str, payload: dict, adapter: Path, workspace: str | None, task: str,
                  state: dict, strict: bool) -> dict:
    """before_model where the host has no event for it and a model request follows `event`: called after the
    event's own function and output, its context queued for the next event (as the host's would be). Its own audit
    line; a failure loses only its own context. The call's deadline still runs: an alarm here leaves the event's
    output as it is."""
    started = time.monotonic()
    line = _record(hosts.BEFORE_MODEL, payload)
    line.update({"tool_name": None, "at": event})
    before = None
    try:
        try:
            before = _state_bytes(state)                    # what to put back if it leaves the state unsaveable
        except (TypeError, ValueError):
            pass
        request = wire.model_request(event, payload)        # the session's fields: no tool call is under way
        inp = wire.normalise(hosts.BEFORE_MODEL, request, str(adapter), workspace or "", task, state,
                             function="before_model")
        if module is None:
            module = load_middleware(adapter / "middleware.py", adapter)
        result = call_middleware(module, "before_model", inp, state)
        if result is not None and result.context:
            state.setdefault(hooks.pending_key(wire.agent(payload)), []).append(result.context)
            line["context_chars"] = len(result.context)
    except (Exception, SystemExit, MiddlewareTimeout) as exc:
        line["error"] = _describe(exc)
        if strict:
            traceback.print_exc()
    finally:
        signal.alarm(0)                                     # the middleware is done: nothing after it may be cut
    if before is not None:
        try:
            _state_bytes(state)
        except (TypeError, ValueError) as exc:
            state.clear()
            state.update(json.loads(before))
            line["error"] = line["error"] or f"state not saved: {type(exc).__name__}: {exc}"[:ERROR_CHARS]
    line["ms"] = int((time.monotonic() - started) * 1000)
    return line


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    strict = "--strict" in argv
    words = [a for a in argv if a != "--strict"]
    host = None
    if "--host" in words:
        at = words.index("--host")
        host = words[at + 1] if at + 1 < len(words) else ""
        del words[at:at + 2]
    sys.exit(run(words[0] if len(words) == 1 else " ".join(words), sys.stdin.read(), strict=strict, host=host))


if __name__ == "__main__":
    main()
