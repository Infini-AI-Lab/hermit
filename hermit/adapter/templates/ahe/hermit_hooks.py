"""The middleware interface hook_runner.py hands to middleware.py (HookInput, HookResult); fixed, not a tool to run.

What a hook function receives and what it returns: the same on every host. How a host's hook payload becomes a
HookInput, and a HookResult the output the host reads, is hook_hosts.py's (one wire per host); this file names no
host. Stdlib only: hook_runner.py imports this file from the adapter's own copy in the agent's workspace, where
nothing of the framework is installed.

A middleware module defines session_start / before_tool / after_tool / stop / before_model, each
`(HookInput, state: dict) -> HookResult | dict | None`. `state` is the session's dict, persisted by the runner between
calls; keys starting with "_" are the runner's. The module imports `hermit_hooks` (this file), which the runner
makes importable before importing it, and otherwise only the stdlib.

What a HookResult does at each function (the host's own limits are hook_hosts.CAPABILITIES, the table of EVOLVER.md):

    function / result                what the model gets
    before_tool decision="deny"      the call is not run; `reason` is what the model reads instead
    before_tool decision="allow"     the call runs (None: the host decides as without the hook)
    before_tool updated_input=       the call runs with this input instead
    after_tool / stop decision="block"   `reason` reaches the model (after_tool: with or instead of the output, by
                                     host); a stop is blocked once per turn, the model continuing with the reason
    context=                         text the model reads with the event (session_start: before its first request;
                                     before_tool: with the call's result; stop: only with a block)
    inp.queue_context(text)          text kept in the state, handed over at the next tool result or prompt, or with
                                     a blocked stop
    before_model context=            queued as by inp.queue_context: no host reads before_model's own output
    stop_session=True                the session ends, where the host lets a hook end it (`reason`: the stop reason)
    system_message=                  a message to the person watching, where the host shows one; never the model
    None                             nothing
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
import sys

# The name a middleware imports (`from hermit_hooks import HookResult`): registered here, so that whatever loads this
# file first, under any module name (the runner, the template check's dry run, a test), makes the name available.
sys.modules.setdefault("hermit_hooks", sys.modules[__name__])

MIDDLEWARE_EVENTS = ("session_start", "before_tool", "after_tool", "stop", "before_model")
PENDING = "_pending"                                            # the runner's key in the session state: queued context
TOOL_KIND_VALUES = ("shell", "patch", "read", "write", "mcp", "other")


def pending_key(agent_id: str | None = None) -> str:
    """Where the state keeps the queue of one agent of the session: the main agent's under "_pending", a sub-agent's
    (a host that names it: Claude Code's agent_id) under "_pending:<agent_id>", so that a note never crosses over."""
    return PENDING if not agent_id else f"{PENDING}:{agent_id}"


@dataclass
class HookInput:
    event: str                            # the middleware function's name (before_tool, ...)
    host: str                             # the host that runs the hook: codex | claude-code (the deployment's record)
    session_id: str
    cwd: str                              # the host's working directory for the event (Claude Code: follows a `cd`)
    adapter_dir: str                      # the adapter's copy in the workspace: MEMORY.md and scripts/ are there
    model: str | None = None
    turn_id: str | None = None
    transcript_path: str | None = None
    tool_name: str | None = None          # the host's own tool name ("Bash" is the shell on both hosts)
    tool_kind: str | None = None          # normalised: "shell" | "patch" | "read" | "write" | "mcp" | "other"
    tool_input: dict | None = None        # the host's tool input, as it sent it
    tool_output: dict | str | None = None  # after_tool: the host's tool response, as it sent it
    command: str | None = None            # shell only: the command line
    output_text: str | None = None        # after_tool: the tool's output as text (a failed call: its error text);
                                          # None where the host reports none (Claude Code's Write, Edit ...)
    exit_code: int | None = None          # after_tool: when the host reports one; otherwise None (parse output_text)
    duration_ms: int | None = None        # after_tool: when the host reports it
    failed: bool | None = None            # after_tool: the call failed (True), did not (False), or the host does not say
    last_message: str | None = None       # stop: the model's last message
    task_text: str = ""                   # the task instruction: task.md in the session's workspace when it has one,
                                          # else the prompt of the session's first UserPromptSubmit ("" before it:
                                          # the hosts fire SessionStart before the prompt is submitted)
    workspace: str = ""                   # the session's workspace: where the deployment put the adapter (task.md,
                                          # .hermit-hooks/); fixed for the session, unlike cwd
    source: str | None = None             # session_start: why the session started (startup, resume ...), when sent
    agent_id: str | None = None           # a sub-agent's id when the host names one (Claude Code); None: the main agent
    raw: dict = field(default_factory=dict)       # the host's payload, as it sent it
    state: dict = field(default_factory=dict, repr=False)   # the session state the runner binds (queue_context's target)

    def queue_context(self, text: str) -> None:
        """Queue text for the model: kept in the state (pending_key) and handed over as context with the next event
        of the same agent whose output reaches the model (a tool result, a prompt, a blocked stop)."""
        if not isinstance(text, str):
            raise TypeError(f"queue_context takes a str, not {type(text).__name__}")
        self.state.setdefault(pending_key(self.agent_id), []).append(text)


@dataclass
class HookResult:
    decision: str | None = None   # before_tool: "deny" (veto; reason shown to the model) | "allow" | None; after_tool/stop: "block" | None
    reason: str = ""
    context: str | None = None    # text for the model
    updated_input: dict | None = None   # before_tool only, best effort (see the host's row)
    stop_session: bool = False    # end the session; reason -> the stop reason
    system_message: str | None = None


RESULT_KEYS = frozenset(f.name for f in fields(HookResult))
RESULT_TYPES = {"decision": (str, type(None)), "reason": (str,), "context": (str, type(None)),
                "updated_input": (dict, type(None)), "stop_session": (bool,), "system_message": (str, type(None))}


def as_result(value) -> HookResult | None:
    """What a middleware function returned, as a HookResult: None, a HookResult, or a dict with only its keys.
    The fields are type-checked (a HookResult built by hand included): a wrong type would otherwise surface as a
    TypeError inside the host's wire or the JSON writer, after the decision was made."""
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
    # A veto or a block without a reason is refused here: Codex ignores a deny whose reason is empty (the call runs as
    # if allowed), and on every host the reason is all the model reads of it.
    if value.decision in ("deny", "block") and not value.reason.strip():
        raise ValueError(f"HookResult(decision={value.decision!r}) needs a non-empty reason: it is what the model reads")
    return value
