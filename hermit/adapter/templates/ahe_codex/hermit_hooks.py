"""The middleware interface hook_runner.py hands to middleware.py (HookInput, HookResult); fixed, not a tool to run.

What a hook function receives, what it returns, and how that return becomes the host's hook wire. Stdlib only:
hook_runner.py imports this file from the adapter's own copy in the agent's workspace, where nothing of the framework
is installed.

A middleware module defines any subset of session_start / before_tool / after_tool / stop / before_model, each
`(HookInput, state: dict) -> HookResult | dict | None`. `state` is the session's dict, persisted by the runner between
calls; keys starting with "_" are the runner's. The module imports `hermit_hooks` (this file), which the runner
makes importable before importing it, and otherwise only the stdlib.

Both hosts speak the Claude Code hook wire (Codex 0.164 included, with Claude's tool names: "Bash" for the shell,
"apply_patch", "spawn_agent" ...). `normalise` turns a payload into a HookInput, `translate` a HookResult into the
JSON the host reads on stdout:

    event / result                   stdout
    before_tool deny                 hookSpecificOutput{PreToolUse, permissionDecision: deny, permissionDecisionReason, additionalContext?}
    before_tool allow / None         hookSpecificOutput{PreToolUse, permissionDecision: allow?, additionalContext?, updatedInput?}
    after_tool block / context       {"decision": "block", "reason"} and/or hookSpecificOutput{PostToolUse, additionalContext}
    session_start context            hookSpecificOutput{SessionStart, additionalContext}
    stop block                       {"decision": "block", "reason"}; ignored when the host reports stop_hook_active
                                     (a Stop block keeps the model in the same turn and Stop fires again)
    before_model context             no stdout: appended to state["_pending"] (Codex ignores PreModelRequest output)
    stop_session / system_message    "continue": false + "stopReason" / "systemMessage" (top level, at every event:
                                     Codex 0.164's embedded output schemas declare both for each hook)
    None                             no output

The pending queue (`HookInput.queue_context`, and before_model's context) is flushed as additionalContext of the
next UserPromptSubmit, PostToolUse or Stop. UserPromptSubmit has no middleware function: the runner handles it as
that flush point only.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
import re
import shlex
import sys

# The name a middleware imports (`from hermit_hooks import HookResult`): registered here, so that whatever loads this
# file first, under any module name (the runner, the template check's dry run, a test), makes the name available.
sys.modules.setdefault("hermit_hooks", sys.modules[__name__])

# Host event -> middleware function. None: a flush point without a function.
EVENTS = {"SessionStart": "session_start", "UserPromptSubmit": None, "PreToolUse": "before_tool",
          "PostToolUse": "after_tool", "Stop": "stop", "PreModelRequest": "before_model"}
MIDDLEWARE_EVENTS = tuple(fn for fn in EVENTS.values() if fn)
FLUSH_EVENTS = ("UserPromptSubmit", "PostToolUse", "Stop")     # where the pending queue reaches the model
PENDING = "_pending"                                            # the runner's key in the session state

# The host's tool name -> tool_kind. Codex 0.164 uses Claude's names on the wire; "mcp__<server>__<tool>" and
# "<server>__<tool>" are MCP tools, "spawn_agent" / "multi_agent_v1*" and anything unknown are "other".
TOOL_KINDS = {"Bash": "shell", "apply_patch": "patch", "Read": "read", "Write": "write", "Edit": "write",
              "MultiEdit": "write"}
EXIT_CODE_LINE = re.compile(r"\AExit code: (-?\d+)")


@dataclass
class HookInput:
    event: str                            # the middleware function's name (before_tool, ...)
    host: str                             # codex | claude-code
    session_id: str
    cwd: str
    adapter_dir: str
    model: str | None = None
    turn_id: str | None = None
    transcript_path: str | None = None
    tool_name: str | None = None          # the host's wire name: "Bash" (the shell, both hosts), "apply_patch", "Read" ...
    tool_kind: str | None = None          # normalised: "shell" | "patch" | "read" | "write" | "mcp" | "other"
    tool_input: dict | None = None        # raw host payload
    tool_output: dict | str | None = None  # after_tool: raw tool_response (Codex: a plain string of aggregated output)
    command: str | None = None            # shell only: tool_input.command (a list joined) or .cmd
    output_text: str | None = None        # after_tool: tool_response itself when str; else .output|stdout|content
    exit_code: int | None = None          # not on the Codex hook wire; apply_patch: parsed from the leading
    duration_ms: int | None = None        # "Exit code: N" line; otherwise None
    last_message: str | None = None       # stop: last_assistant_message
    task_text: str = ""                   # the task instruction: task.md in the session's workspace (cwd) when it has
                                          # one, else the prompt of the session's first UserPromptSubmit ("" before
                                          # it: Codex fires SessionStart before the prompt is submitted)
    raw: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict, repr=False)   # the session state the runner binds (queue_context's target)

    def queue_context(self, text: str) -> None:
        """Queue text for the model: stored in state["_pending"], flushed as additionalContext at the next
        UserPromptSubmit / PostToolUse / Stop (PreModelRequest output is ignored by Codex)."""
        self.state.setdefault(PENDING, []).append(text)


@dataclass
class HookResult:
    decision: str | None = None   # before_tool: "deny" (veto; reason shown to the model) | "allow" | None; after_tool/stop: "block" | None
    reason: str = ""
    context: str | None = None    # additionalContext for the model
    updated_input: dict | None = None   # before_tool only, best effort (Codex rejects it for some tools)
    stop_session: bool = False    # continue=false, reason -> stopReason
    system_message: str | None = None


RESULT_KEYS = frozenset(f.name for f in fields(HookResult))
RESULT_TYPES = {"decision": (str, type(None)), "reason": (str,), "context": (str, type(None)),
                "updated_input": (dict, type(None)), "stop_session": (bool,), "system_message": (str, type(None))}


def as_result(value) -> HookResult | None:
    """What a middleware function returned, as a HookResult: None, a HookResult, or a dict with only its keys.
    The fields are type-checked (a HookResult built by hand included): a wrong type would otherwise surface as a
    TypeError inside translate or the JSON writer, after the decision was made."""
    if value is None:
        return None
    if isinstance(value, dict):
        unknown = set(value) - RESULT_KEYS
        if unknown:
            raise TypeError(f"middleware returned unknown keys {sorted(unknown)}; HookResult takes {sorted(RESULT_KEYS)}")
        value = HookResult(**value)
    elif not isinstance(value, HookResult):
        raise TypeError(f"middleware must return None, HookResult or a dict, not {type(value).__name__}")
    for name, types in RESULT_TYPES.items():
        if not isinstance(getattr(value, name), types):
            expected = " or ".join(t.__name__ for t in types)
            raise TypeError(f"HookResult.{name} must be {expected}, not {type(getattr(value, name)).__name__}")
    return value


def tool_kind(name) -> str | None:
    if name is None:
        return None
    name = str(name)                  # a host's bug (a non-string tool_name) is not the middleware's
    if name in TOOL_KINDS:
        return TOOL_KINDS[name]
    if name.startswith("mcp__") or (not name.startswith("multi_agent_v1") and "__" in name):
        return "mcp"
    return "other"


def _text(value) -> str | None:
    """A string out of a tool output field: the string, or the text parts of a content list."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [p.get("text", "") if isinstance(p, dict) else str(p) for p in value]
        return "".join(parts)
    return None


def _int(value) -> int | None:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def normalise(host: str, payload: dict, adapter_dir: str, task_text: str = "", state: dict | None = None,
              event: str | None = None) -> HookInput:
    """The host's hook payload as a HookInput. Fields the wire does not carry stay None. `event` is the middleware
    function the caller dispatches on (the runner's, from the event it was started for); without one it is read from
    the payload's hook_event_name."""
    if not event:
        event = EVENTS.get(payload.get("hook_event_name")) or ""
    name = payload.get("tool_name")
    name = str(name) if name is not None else None
    kind = tool_kind(name)
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else None
    output = payload.get("tool_response")
    command = None
    if kind == "shell" and tool_input is not None:
        raw = tool_input.get("command", tool_input.get("cmd"))
        command = shlex.join(str(a) for a in raw) if isinstance(raw, list) else (str(raw) if raw is not None else None)
    output_text = exit_code = duration_ms = None
    if isinstance(output, str):
        output_text = output
        if kind == "patch":
            match = EXIT_CODE_LINE.match(output)
            exit_code = int(match.group(1)) if match else None
    elif isinstance(output, dict):
        for key in ("output", "stdout", "content"):
            output_text = _text(output.get(key))
            if output_text is not None:
                break
        exit_code = _int(next((output[k] for k in ("exit_code", "exitCode", "returncode") if k in output), None))
        duration_ms = _int(next((output[k] for k in ("duration_ms", "durationMs") if k in output), None))
    else:
        output = None if output is None else {"value": output}
    return HookInput(
        event=event, host=host, session_id=str(payload.get("session_id") or ""), cwd=str(payload.get("cwd") or ""),
        adapter_dir=adapter_dir, model=payload.get("model"), turn_id=payload.get("turn_id"),
        transcript_path=payload.get("transcript_path"), tool_name=name, tool_kind=kind, tool_input=tool_input,
        tool_output=output, command=command, output_text=output_text, exit_code=exit_code, duration_ms=duration_ms,
        last_message=payload.get("last_assistant_message"), task_text=task_text or "", raw=payload,
        state=state if state is not None else {})


def _join(*texts) -> str | None:
    parts = [t for t in texts if t]
    return "\n\n".join(parts) if parts else None


def translate(host: str, event: str, result: HookResult | None, state: dict, *, stop_hook_active: bool = False) -> dict | None:
    """The wire JSON for `result` at the host event `event`, or None for no output. Flushes the pending queue at
    the flush events; queues before_model's context. The same wire serves both hosts; `host` is kept for a
    difference that may come."""
    fn = EVENTS.get(event, "")
    if fn == "":
        return None
    result = result or HookResult()
    if fn == "before_model":
        if result.context:
            state.setdefault(PENDING, []).append(result.context)
        return None
    # The decision is validated before the pending queue is drained: a rejected result must not lose the queue.
    decision = result.decision
    if fn == "before_tool" and decision not in (None, "allow", "deny", "block"):
        raise ValueError(f"before_tool decision must be deny, allow or None, not {decision!r}")
    if fn in ("after_tool", "stop") and decision not in (None, "allow", "block", "deny"):
        raise ValueError(f"{fn} decision must be block or None, not {decision!r}")
    if fn not in ("before_tool", "after_tool", "stop") and decision is not None:
        raise ValueError(f"{fn} takes no decision, got {decision!r}")
    pending = "\n\n".join(state.pop(PENDING, []) or []) if event in FLUSH_EVENTS else ""
    context = _join(result.context, pending)
    out, specific = {}, {}
    if fn == "before_tool":
        if decision in ("deny", "block"):
            specific["permissionDecision"] = "deny"
            specific["permissionDecisionReason"] = result.reason
        elif decision == "allow":
            specific["permissionDecision"] = "allow"
            if result.reason:
                specific["permissionDecisionReason"] = result.reason
        if result.updated_input is not None and specific.get("permissionDecision") != "deny":
            specific["updatedInput"] = result.updated_input
    elif fn in ("after_tool", "stop") and decision in ("block", "deny"):
        if not (fn == "stop" and stop_hook_active):
            out["decision"] = "block"
            out["reason"] = result.reason
    if context:
        specific["additionalContext"] = context
    if specific:
        out["hookSpecificOutput"] = {"hookEventName": event, **specific}
    if result.stop_session:
        out["continue"] = False
        out["stopReason"] = result.reason
    if result.system_message:
        out["systemMessage"] = result.system_message
    return out or None
