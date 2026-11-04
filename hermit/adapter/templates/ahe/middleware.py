"""The seed middleware of the AHE harness (arXiv 2604.25850 §3.1): hooks hook_runner.py calls at agent events.

The hook pipeline the host calls back at the lifecycle events of the acting agent: the host (Codex, Claude Code)
runs the adapter's hook_runner.py on every event (hooks.json), and the runner calls the function of this module the
event names, with the same HookInput and HookResult on every host (what each host can do: EVOLVER.md's table).

The seed does one thing: at `session_start` it hands the long-term memory, the body of MEMORY.md below its header,
to the model as context (AHE's "agent reads its memory at startup"). The other four events are defined, so every
event the runner calls exists, and return None: no veto, no note, no stop. A learned middleware grows from
here: a `before_tool` that denies a command with a reason, an `after_tool` that appends an execution note, a
`stop` that blocks an early finish, a `before_model` that queues a reminder (no host reads its own output: it
can only `inp.queue_context(...)`, or return a context, which is queued).

Rules the template check enforces on this file (EVOLVER.md): stdlib imports only, plus `hermit_hooks` (the
interface beside this file, which the runner provides); none of socket, http, urllib, ssl, ftplib, smtplib, xmlrpc,
asyncio, subprocess; every hook `(inp, state)`; helpers private (`_name`); `session_start` still hands MEMORY.md's
entries to the model (the memory channel is never removed: the check runs it on every host). The import below is
guarded so the module also imports where the interface is not importable (a bare `python3 -c "import middleware"`
elsewhere); under the runner `hermit_hooks` is always there.
"""
from __future__ import annotations

from pathlib import Path

try:
    from hermit_hooks import HookInput, HookResult
except ImportError:                                  # pragma: no cover - outside the runner
    class HookInput:                                 # the fields this seed reads; the interface's has more
        def __init__(self, event="", host="", session_id="", cwd="", adapter_dir="", raw=None, state=None,
                     **fields):
            self.event, self.host, self.session_id, self.cwd = event, host, session_id, cwd
            self.adapter_dir, self.raw, self.state = adapter_dir, raw or {}, state if state is not None else {}
            self.__dict__.update(fields)

        def queue_context(self, text: str) -> None:
            self.state.setdefault("_pending", []).append(text)

    class HookResult:
        def __init__(self, decision=None, reason="", context=None, updated_input=None, stop_session=False,
                     system_message=None):
            self.decision, self.reason, self.context = decision, reason, context
            self.updated_input, self.stop_session, self.system_message = updated_input, stop_session, system_message

MEMORY_FILE = "MEMORY.md"
MEMORY_HEADER = "# Long-term memory"


MEMORY_SECTION = "## Agent Added Memories"


def _memory_body(adapter_dir: str) -> str:
    """MEMORY.md below its `# Long-term memory` header, stripped; "" when nothing follows the seed's
    `## Agent Added Memories` line (an entry may be a bullet, prose or a heading of its own) or the file cannot be
    read (fail open: a missing memory is an empty one)."""
    try:
        text = (Path(adapter_dir) / MEMORY_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    lines = text.splitlines()
    if lines and lines[0].strip() == MEMORY_HEADER:
        lines = lines[1:]
    body = "\n".join(lines).strip()
    entries = [line for line in body.splitlines() if line.strip() and line.strip() != MEMORY_SECTION]
    return body if entries else ""


def session_start(inp: HookInput, state: dict) -> HookResult | None:
    """The long-term memory as context for the session, when there is any."""
    body = _memory_body(inp.adapter_dir)
    if not body:
        return None
    state["memory_chars"] = len(body)
    return HookResult(context=body)


def before_tool(inp: HookInput, state: dict) -> HookResult | None:
    return None


def after_tool(inp: HookInput, state: dict) -> HookResult | None:
    return None


def stop(inp: HookInput, state: dict) -> HookResult | None:
    return None


def before_model(inp: HookInput, state: dict) -> HookResult | None:
    return None
