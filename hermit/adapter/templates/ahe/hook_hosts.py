"""The hosts' hook wires: the one module of the template that knows the hosts; fixed, not a tool to run.

A host is the agent CLI that runs the adapter's hooks: Codex or Claude Code. The framework's deployment for that host
(hermit.hosts.codex, hermit.hosts.claude_code) installs hooks.json in the host's own place and records the host in
the workspace (`.hermit-deploy.json`, beside the adapter's copy `hermit/<id>/`); hook_runner.py reads the record
(`resolve`) and hands every call to that host's wire: the events the host fires and the middleware function each
one runs (`events`), its payload as the HookInput of hermit_hooks.py (`normalise`), a HookResult as the output the
host reads (`translate`), and whether a model request follows an event (`model_follows`, for before_model on a host
that has no event before a model request). What each host can and cannot do is CAPABILITIES, which EVOLVER.md shows
as its table (`capability_table`; a test keeps the two equal). The middleware sees hermit_hooks.py's HookInput and
HookResult alone, the same on every host.

Each wire was measured on the host's own CLI, Codex 0.164.0 and Claude Code 2.1.317 (2.1.318 alike), run on a
scripted local model: which payload each event carries and where each kind of output reaches the model.
tests/fixtures/ahe_hosts/ holds real payloads of those runs, which tests/test_ahe_hosts.py replays through the wires,
and the same file re-runs both CLIs on a scripted model when they are installed.

Pi reads no hook file: its deployment lists the hook commands in AGENTS.md for the agent to run by hand. Run so, with
no payload (the `pi` wire), session_start still hands over the memory; the other hooks have no tool call, prompt or
stop to act on.

Stdlib only, beside hook_runner.py in the adapter's copy: nothing of the framework is installed where the agent runs.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import sys


def _interface():
    """hermit_hooks, the interface: the one the runner registered, else the file beside this one."""
    module = sys.modules.get("hermit_hooks")
    if module is None:
        spec = importlib.util.spec_from_file_location("hermit_hooks", Path(__file__).with_name("hermit_hooks.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules["hermit_hooks"] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules["hermit_hooks"]
            raise
    return module


_hooks = _interface()
HookInput, HookResult, pending_key = _hooks.HookInput, _hooks.HookResult, _hooks.pending_key

DEPLOYMENT = ".hermit-deploy.json"      # the deployment's record in the workspace (hermit.hosts.inputs.MARKER)
COPY_DIR = "hermit"                     # the copy is <workspace>/hermit/<id>/ (hermit.hosts.inputs.adapter_folder)
BEFORE_MODEL = "PreModelRequest"        # the audit name of before_model, whichever host fires or lacks it
# What before_model keeps of the event the runner calls it after: the session's and the agent's fields, no tool call.
SESSION_FIELDS = ("session_id", "transcript_path", "cwd", "model", "turn_id", "prompt_id", "permission_mode",
                  "agent_id", "agent_type")
EXIT_CODE = re.compile(r"\AExit code:? (-?\d+)")


def payload_cwd(payload: dict) -> str | None:
    cwd = payload.get("cwd")
    return cwd if isinstance(cwd, str) and Path(cwd).is_absolute() else None


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


def _join(*texts) -> str | None:
    parts = [t for t in texts if t]
    return "\n\n".join(parts) if parts else None


class Wire:
    """One host's hook wire. A subclass names its events and differs where the host differs."""
    name = ""
    label = ""
    events: dict[str, str | None] = {}           # host event -> middleware function (None: only hands context over)
    flush: tuple[str, ...] = ()                  # the events whose output hands the queued context over
    tool_kinds: dict[str, str] = {}              # the host's tool name -> HookInput.tool_kind
    specific: dict[str, frozenset] = {}          # host event -> the hookSpecificOutput keys the host reads there
    implied_allow = False                        # an updated_input goes out with permissionDecision allow
    TOP = frozenset({"decision", "reason", "continue", "stopReason", "systemMessage", "hookSpecificOutput"})

    # -- where the session is -------------------------------------------------------------------------------------
    def workspace(self, payload: dict) -> str | None:
        """The session's workspace when no deployment record says it."""
        return payload_cwd(payload)

    # -- payload -> HookInput -------------------------------------------------------------------------------------
    def tool_kind(self, name) -> str | None:
        if name is None:
            return None
        name = str(name)                  # a host's bug (a non-string tool_name) is not the middleware's
        if name in self.tool_kinds:
            return self.tool_kinds[name]
        if name.startswith("mcp__") or (not name.startswith("multi_agent_v1") and "__" in name):
            return "mcp"
        return "other"

    def response(self, event: str, payload: dict, kind: str | None):
        """(tool_output, output_text, exit_code, duration_ms, failed) of an after_tool payload."""
        output = payload.get("tool_response")
        output_text = exit_code = duration_ms = None
        if isinstance(output, str):
            output_text = output
            if kind == "patch":
                match = EXIT_CODE.match(output)
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
        failed = None if exit_code is None else exit_code != 0
        return output, output_text, exit_code, duration_ms, failed

    def turn(self, payload: dict) -> str | None:
        return payload.get("turn_id")

    def agent(self, payload: dict) -> str | None:
        """The sub-agent an event belongs to, when the host names one; None: the main agent."""
        agent = payload.get("agent_id")
        return agent if isinstance(agent, str) and agent else None

    def normalise(self, event: str, payload: dict, adapter_dir: str, workspace: str = "", task_text: str = "",
                  state: dict | None = None, function: str | None = None) -> HookInput:
        """The host's payload for `event` as the HookInput of `function` (the middleware function the caller
        dispatches on; without one, the event's own). Fields the host does not send stay None."""
        if not function:
            function = self.events.get(event) or self.events.get(payload.get("hook_event_name")) or ""
        name = payload.get("tool_name")
        name = str(name) if name is not None else None
        kind = self.tool_kind(name)
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else None
        command = None
        if kind == "shell" and tool_input is not None:
            raw = tool_input.get("command", tool_input.get("cmd"))
            command = shlex.join(str(a) for a in raw) if isinstance(raw, list) else (str(raw) if raw is not None else None)
        output, output_text, exit_code, duration_ms, failed = self.response(event, payload, kind)
        source = payload.get("source")
        return HookInput(
            event=function, host=self.name, session_id=str(payload.get("session_id") or ""),
            cwd=str(payload.get("cwd") or ""), adapter_dir=adapter_dir, model=payload.get("model"),
            turn_id=self.turn(payload), transcript_path=payload.get("transcript_path"), tool_name=name,
            tool_kind=kind, tool_input=tool_input, tool_output=output, command=command, output_text=output_text,
            exit_code=exit_code, duration_ms=duration_ms, failed=failed,
            last_message=payload.get("last_assistant_message"), task_text=task_text or "",
            workspace=workspace or "", source=source if isinstance(source, str) else None,
            agent_id=self.agent(payload), raw=payload, state=state if state is not None else {})

    # -- HookResult -> the host's output --------------------------------------------------------------------------
    def translate(self, event: str, result: HookResult | None, state: dict, *, stop_hook_active: bool = False,
                  agent_id: str | None = None) -> dict | None:
        """The output for `result` at the host event `event`, or None for no output. Hands the queue of the event's
        agent over at the flush events; queues before_model's context."""
        fn = self.events.get(event, "")
        if fn == "":
            return None
        result = result or HookResult()
        key = pending_key(agent_id)
        if fn == "before_model":
            if result.context:
                state.setdefault(key, []).append(result.context)
            return None
        # The decision is validated before the queue is drained: a rejected result must not lose the queue.
        decision = result.decision
        if fn == "before_tool" and decision not in (None, "allow", "deny", "block"):
            raise ValueError(f"before_tool decision must be deny, allow or None, not {decision!r}")
        if fn in ("after_tool", "stop") and decision not in (None, "allow", "block", "deny"):
            raise ValueError(f"{fn} decision must be block or None, not {decision!r}")
        if fn not in ("before_tool", "after_tool", "stop") and decision is not None:
            raise ValueError(f"{fn} takes no decision, got {decision!r}")
        blocks = fn in ("after_tool", "stop") and decision in ("block", "deny") and not (fn == "stop" and stop_hook_active)
        if fn == "stop" and not blocks:
            context = None        # no request follows a stop that goes through: the queue waits, a context is moot
        else:
            pending = "\n\n".join(state.pop(key, []) or []) if event in self.flush else ""
            context = _join(result.context, pending)
        out, specific = {}, {}
        if fn == "before_tool":
            if decision in ("deny", "block"):
                specific["permissionDecision"] = "deny"
                specific["permissionDecisionReason"] = result.reason
            elif decision == "allow" or (result.updated_input is not None and self.implied_allow):
                specific["permissionDecision"] = "allow"
                if result.reason:
                    specific["permissionDecisionReason"] = result.reason
            if result.updated_input is not None and specific.get("permissionDecision") != "deny":
                specific["updatedInput"] = result.updated_input
        elif blocks:
            block, context = self.block(event, result.reason, context)
            out.update(block)
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

    def block(self, event: str, reason: str, context: str | None) -> tuple[dict, str | None]:
        """The output of a block at `event` (an after_tool or a stop), and the context left to send beside it."""
        return {"decision": "block", "reason": reason}, context

    def delivered(self, event: str, out: dict | None, result: HookResult | None) -> str:
        """The context `out` hands the model (the audit's context_chars)."""
        return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""

    def model_follows(self, event: str, result: HookResult | None, stop_hook_active: bool) -> bool:
        """Whether the runner calls before_model after `event`: only on a host without a model-request event."""
        return False

    def model_request(self, event: str, payload: dict) -> dict:
        """The payload before_model gets when the runner calls it after `event`: the session's and the agent's
        fields, and the turn the event named."""
        out = {k: payload[k] for k in SESSION_FIELDS if k in payload}
        turn = self.turn(payload)
        if turn is not None:
            out["turn_id"] = turn
        return {"hook_event_name": BEFORE_MODEL, **out, "after": event}

    # -- what the host reads (the template check holds an output to it) --------------------------------------------
    def output_problems(self, event: str, out) -> list[str]:
        """What in `out`, a hook's output at `event`, this host does not read."""
        if out is None:
            return []
        if not isinstance(out, dict):
            return [f"the output is a {type(out).__name__}, not a JSON object"]
        problems = [f"{self.label} reads no `{k}` in a hook's output" for k in sorted(set(out) - self.TOP)]
        specific = out.get("hookSpecificOutput")
        if specific is not None:
            allowed = self.specific.get(event, frozenset())
            if not isinstance(specific, dict) or specific.get("hookEventName") != event:
                problems.append(f"hookSpecificOutput must name the event {event}")
            else:
                problems += [f"{self.label}'s {event} reads no hookSpecificOutput.{k}"
                             for k in sorted(set(specific) - {"hookEventName"} - allowed)]
        return problems

    # -- synthetic events, as the host sends them (the template check's dry run) ------------------------------------
    def sample(self, event: str, workspace: str, session_id: str, *, tool: str = "Bash", nth: int = 1,
               active: bool = False) -> dict:
        raise NotImplementedError

    def session(self, workspace: str, session_id: str) -> list[tuple[str, dict]]:
        """One session's events in the order the host fires them: a prompt, tool calls of the host's kinds (a shell
        command, a file edit; on Claude Code a failed read too) with their results, a stop and the stop after it (the
        template check runs a middleware through it)."""
        raise NotImplementedError


class Codex(Wire):
    """Codex 0.164: Claude Code's hook schema and output fields, with Claude's tool names ("Bash" is the shell, its
    response the plain string of stdout and stderr, no exit code) and its own `apply_patch` ("Exit code: N" leading
    its response); a PreModelRequest before every model request, whose output it ignores. What it reads differently:
    a Stop's additionalContext is ignored (so a blocked stop's context goes with the reason); a context given with
    `allow` and no `updatedInput` is dropped; `updatedInput` needs `allow`; `continue: false` ends the session only at
    SessionStart and UserPromptSubmit; a systemMessage is shown nowhere. No PostToolUse follows a denied call, a failed
    apply_patch or an apply_patch run through the shell, and a command that outlives its first wait has one, at exit,
    with its last output only (the polls in between fire nothing); the payload does not name a command's `workdir`.
    With a code-mode model (gpt-5.6-luna: one `exec` tool whose script calls the tools) the hooks are the nested
    tools', and a deny or a block reaches the model as an error inside its script. The hook runs under `bash -c` in
    the session's directory, with nothing in its environment that names the host."""
    name, label = "codex", "Codex"
    events = {"SessionStart": "session_start", "UserPromptSubmit": None, "PreToolUse": "before_tool",
              "PostToolUse": "after_tool", "Stop": "stop", "PreModelRequest": "before_model"}
    flush = ("UserPromptSubmit", "PostToolUse", "Stop")
    tool_kinds = {"Bash": "shell", "apply_patch": "patch", "Read": "read", "Write": "write", "Edit": "write",
                  "MultiEdit": "write"}
    specific = {"PreToolUse": frozenset({"permissionDecision", "permissionDecisionReason", "updatedInput",
                                         "additionalContext"}),
                "SessionStart": frozenset({"additionalContext"}), "UserPromptSubmit": frozenset({"additionalContext"}),
                "PostToolUse": frozenset({"additionalContext"})}
    implied_allow = True

    def block(self, event: str, reason: str, context: str | None) -> tuple[dict, str | None]:
        if event == "Stop":                                   # Codex reads a Stop's reason, not its context
            return {"decision": "block", "reason": _join(reason, context) or ""}, None
        return super().block(event, reason, context)

    @staticmethod
    def _dropped(event: str, out) -> bool:
        """A PreToolUse context that Codex drops: given with allow and no updatedInput."""
        specific = (out or {}).get("hookSpecificOutput") or {} if isinstance(out, dict) else {}
        return (event == "PreToolUse" and isinstance(specific, dict) and specific.get("permissionDecision") == "allow"
                and "additionalContext" in specific and "updatedInput" not in specific)

    def delivered(self, event: str, out: dict | None, result: HookResult | None) -> str:
        if self._dropped(event, out):
            return ""
        reason, own = (out or {}).get("reason") or "", (result.reason if result else "") or ""
        if event == "Stop" and reason != own and reason.startswith(own):
            return reason[len(own):].lstrip("\n")         # what block() put after the middleware's reason
        return super().delivered(event, out, result)

    def output_problems(self, event: str, out) -> list[str]:
        problems = super().output_problems(event, out)
        if self._dropped(event, out):
            problems.append("Codex drops a PreToolUse context given with `allow` and no updated_input: return the "
                            "context without a decision")
        return problems

    def sample(self, event: str, workspace: str, session_id: str, *, tool: str = "Bash", nth: int = 1,
               active: bool = False) -> dict:
        function = self.events[event]
        payload = {"session_id": session_id, "transcript_path": str(Path(workspace) / f"rollout-{session_id}.jsonl"),
                   "cwd": workspace, "hook_event_name": event, "model": "check"}
        if event != "SessionStart":
            payload["turn_id"] = "check-turn"
        if event != "PreModelRequest":
            payload["permission_mode"] = "bypassPermissions"
        if event == "SessionStart":
            payload["source"] = "startup"
        if event == "UserPromptSubmit":
            payload["prompt"] = "check"
        if function in ("before_tool", "after_tool"):
            patch = tool == "apply_patch"
            payload.update({"tool_name": tool, "tool_use_id": f"call_check_{nth}",
                            "tool_input": {"command": "*** Begin Patch\n*** End Patch\n" if patch else "true"}})
            if function == "after_tool":
                payload["tool_response"] = ("Exit code: 0\nWall time: 0.1 seconds\nOutput:\nSuccess.\n" if patch
                                            else "")
        if function == "stop":
            payload.update({"last_assistant_message": "check", "stop_hook_active": active})
        return payload

    def session(self, workspace: str, session_id: str) -> list[tuple[str, dict]]:
        s = lambda event, **kw: (event, self.sample(event, workspace, session_id, **kw))   # noqa: E731
        return [s("SessionStart"), s("UserPromptSubmit"), s("PreModelRequest"),
                s("PreToolUse", nth=1), s("PostToolUse", nth=1), s("PreModelRequest"),
                s("PreToolUse", tool="apply_patch", nth=2), s("PostToolUse", tool="apply_patch", nth=2),
                s("PreModelRequest"), s("Stop"), s("PreModelRequest"), s("Stop", active=True)]


class ClaudeCode(Wire):
    """Claude Code 2.1.317: its own hook schema and tool names; a call that ran and failed is PostToolUseFailure (the
    error text, "Exit code N" leading it for Bash; no tool response), one that succeeded PostToolUse (Bash: stdout with
    stderr folded in, no exit code; duration_ms beside it), a denied call or one whose input the CLI refused neither;
    PostToolBatch once per model message, after its calls; no event before a model request, so before_model is the
    runner's, after the prompt, each PostToolBatch and a blocked stop (`model_follows`). What it reads differently: a
    Stop's additionalContext alone makes the model go on (so a stop that goes through sends none); a block keeps the
    tool's output; `updatedInput` is applied without `allow` in the session's permission mode; `continue: false` ends
    the session at PreToolUse (once the call has run) and PostToolUse, and is ignored at SessionStart and
    PostToolUseFailure; a systemMessage is shown to the person watching, not to the model. A sub-agent's events carry
    its agent_id (its queue is its own), its first model request follows SubagentStart and its end is SubagentStop,
    neither registered. The payload's cwd and the hook's own directory are the shell's current one, which follows
    the agent's `cd`; $CLAUDE_PROJECT_DIR is the project's. The payload has no model and names the prompt (prompt_id)
    where Codex names the turn."""
    name, label = "claude-code", "Claude Code"
    events = {"SessionStart": "session_start", "UserPromptSubmit": None, "PreToolUse": "before_tool",
              "PostToolUse": "after_tool", "PostToolUseFailure": "after_tool", "PostToolBatch": None, "Stop": "stop"}
    flush = ("UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop")
    tool_kinds = {"Bash": "shell", "Read": "read", "Write": "write", "Edit": "write", "MultiEdit": "write",
                  "NotebookEdit": "write"}
    specific = {"PreToolUse": frozenset({"permissionDecision", "permissionDecisionReason", "updatedInput",
                                         "additionalContext"}),
                "SessionStart": frozenset({"additionalContext"}), "UserPromptSubmit": frozenset({"additionalContext"}),
                "PostToolUse": frozenset({"additionalContext"}), "PostToolUseFailure": frozenset({"additionalContext"}),
                "Stop": frozenset({"additionalContext"})}
    PROJECT = "CLAUDE_PROJECT_DIR"

    def workspace(self, payload: dict) -> str | None:
        project = os.environ.get(self.PROJECT)
        return project if project and Path(project).is_absolute() else payload_cwd(payload)

    def turn(self, payload: dict) -> str | None:
        return payload.get("turn_id") or payload.get("prompt_id")

    def response(self, event: str, payload: dict, kind: str | None):
        duration_ms = _int(payload.get("duration_ms"))
        if event == "PostToolUseFailure":
            error = _text(payload.get("error"))
            match = EXIT_CODE.match(error or "")
            return payload.get("tool_response"), error, int(match.group(1)) if match else None, duration_ms, True
        output, output_text, exit_code, own, failed = super().response(event, payload, kind)
        if isinstance(output, dict) and kind == "shell":           # Bash: {stdout, stderr, interrupted, ...}
            parts = [t for t in (_text(output.get("stdout")), _text(output.get("stderr"))) if t]
            output_text = "\n".join(parts) if parts else output_text
            failed = bool(output.get("interrupted"))
        elif kind == "write":
            output_text = None                                      # Write's `content` is the file, not an output
        elif isinstance(output, dict) and output_text is None and isinstance(output.get("file"), dict):
            output_text = _text(output["file"].get("content"))     # Read: {type, file: {filePath, content, ...}}
        if event == "PostToolUse" and failed is None:
            failed = False                                          # a failed call is PostToolUseFailure
        return output, output_text, exit_code, own if own is not None else duration_ms, failed

    def model_follows(self, event: str, result: HookResult | None, stop_hook_active: bool) -> bool:
        """A model request follows the prompt, every model message's tool calls (PostToolBatch, whose calls may have
        been denied or refused) and a blocked stop: once per model request of the main agent, as Codex's
        PreModelRequest, and per later request of a sub-agent (its first follows SubagentStart, not registered)."""
        if event in ("UserPromptSubmit", "PostToolBatch"):
            return not (result is not None and result.stop_session)
        if event == "Stop":
            return (result is not None and result.decision in ("block", "deny") and not stop_hook_active
                    and not result.stop_session)
        return False

    def sample(self, event: str, workspace: str, session_id: str, *, tool: str = "Bash", nth: int = 1,
               active: bool = False) -> dict:
        function = self.events[event]
        payload = {"session_id": session_id, "transcript_path": str(Path(workspace) / f"{session_id}.jsonl"),
                   "cwd": workspace, "hook_event_name": event}
        if event == "SessionStart":
            payload["source"] = "startup"
        else:
            payload.update({"prompt_id": "check-prompt", "permission_mode": "bypassPermissions"})
            if event != "UserPromptSubmit":
                payload["effort"] = {"level": "medium"}
        if event == "UserPromptSubmit":
            payload["prompt"] = "check"
        bash = tool == "Bash"
        tool_input = ({"command": "true", "description": "check"} if bash else
                      {"file_path": f"{workspace}/check.txt", "content": "check\n"} if tool == "Write" else
                      {"file_path": f"{workspace}/missing.txt"})
        if function in ("before_tool", "after_tool"):
            payload.update({"tool_name": tool, "tool_input": tool_input, "tool_use_id": f"toolu_check_{nth}"})
        if event == "PostToolUse":
            payload.update({"tool_response": {"stdout": "", "stderr": "", "interrupted": False, "isImage": False,
                                              "noOutputExpected": False} if bash else
                            {"type": "create", "filePath": tool_input["file_path"], "content": tool_input["content"],
                             "structuredPatch": [], "originalFile": None, "userModified": False}
                            if tool == "Write" else
                            {"type": "text", "file": {"filePath": tool_input.get("file_path"), "content": "",
                                                      "numLines": 0, "startLine": 1, "totalLines": 0}},
                            "duration_ms": 1})
        if event == "PostToolUseFailure":
            payload.update({"error": "Exit code 1" if bash else "File does not exist.", "is_interrupt": False,
                            "duration_ms": 1})
        if event == "PostToolBatch":
            payload["tool_calls"] = [{"tool_name": tool, "tool_input": tool_input, "tool_use_id": f"toolu_check_{nth}",
                                      "tool_response": ""}]
        if function == "stop":
            payload.update({"stop_hook_active": active, "last_assistant_message": "check", "background_tasks": [],
                            "session_crons": []})
        return payload

    def session(self, workspace: str, session_id: str) -> list[tuple[str, dict]]:
        s = lambda event, **kw: (event, self.sample(event, workspace, session_id, **kw))   # noqa: E731
        return [s("SessionStart"), s("UserPromptSubmit"),
                s("PreToolUse", nth=1), s("PostToolUse", nth=1), s("PostToolBatch", nth=1),
                s("PreToolUse", tool="Read", nth=2), s("PostToolUseFailure", tool="Read", nth=2),
                s("PostToolBatch", tool="Read", nth=2), s("PreToolUse", tool="Write", nth=3),
                s("PostToolUse", tool="Write", nth=3), s("PostToolBatch", tool="Write", nth=3),
                s("Stop"), s("Stop", active=True)]


class Pi(Wire):
    """Pi runs no hooks: its deployment lists each hook command in AGENTS.md for the agent to run by hand (hermit.hosts
    .inputs.resources), with no payload. Every event name of hooks.json runs its function on what little there is;
    session_start hands the memory over (printed for the agent to read); a tool call, a prompt or a stop never
    reaches a hook, so the other functions have nothing to act on."""
    name, label = "pi", "Pi"
    events = {**Codex.events, **ClaudeCode.events}
    flush = ClaudeCode.flush
    tool_kinds = ClaudeCode.tool_kinds


CODEX, CLAUDE_CODE, PI = Codex(), ClaudeCode(), Pi()
HOSTS = {CODEX.name: CODEX, CLAUDE_CODE.name: CLAUDE_CODE, PI.name: PI}
HOOK_HOSTS = (CODEX.name, CLAUDE_CODE.name)               # the hosts that run the hooks themselves


def deployment(adapter_dir) -> tuple[dict, str] | None:
    """The record of the deployment that put this copy where it is, and that deployment's workspace:
    `.hermit-deploy.json` two levels above the copy (<workspace>/hermit/<id>/), when it names a copy of this name.
    None for a copy no deployment made (the template itself, a test's copy)."""
    copy = Path(adapter_dir).resolve()
    if copy.parent.name != COPY_DIR:
        return None
    root = copy.parent.parent
    try:
        record = json.loads((root / DEPLOYMENT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    named = record.get("adapter_dir")
    if isinstance(named, str) and Path(named).name != copy.name:
        return None                                         # another adapter's deployment
    return record, str(root)


def resolve(host: str | None, adapter_dir, payload: dict) -> tuple[Wire, str | None]:
    """The wire of the host that runs this call and the session's workspace: `host` when given, else the
    deployment's record's; the workspace is the record's directory, else the host's own notion of it."""
    found = deployment(adapter_dir)
    name = host or (found[0].get("host") if found else None)
    if not name:
        raise LookupError("no host: this copy of the adapter has no deployment record (.hermit-deploy.json in the "
                          "workspace it was deployed to) and no --host was given")
    wire = HOSTS.get(name)
    if wire is None:
        raise LookupError(f"the host {name!r} runs none of this template's hooks (the hosts are "
                          f"{', '.join(HOSTS)})")
    return wire, found[1] if found else wire.workspace(payload)


# -- what each host can and cannot do: EVOLVER.md's table ------------------------------------------------------------
CAPABILITIES = [
    ("`session_start`",
     "SessionStart, before the prompt (again on a resumed session: `inp.source`); `context` is read before the first "
     "model request",
     "SessionStart, before the prompt (`inp.source`: startup, resume ...); `context` is read before the first model "
     "request"),
    ("`before_tool`",
     "PreToolUse (none for a `write_stdin` poll): `deny` (not run; the model reads `reason`), `allow`, `updated_input` "
     "(run instead); `context` is read with the call's result, but dropped when given with `allow` and no "
     "`updated_input`",
     "PreToolUse (none for a call whose input the CLI refused): `deny` (not run; the model reads `reason`), `allow`, "
     "`updated_input` (run instead; without `allow` the session's permission mode decides); `context` is read with "
     "the call's result"),
    ("`after_tool`",
     "PostToolUse after a call that ran, a failed command too (no exit code: `failed` is None); `output_text` may be "
     "the command's last output chunk only (always for one that outlives its first wait: its polls fire nothing); "
     "none after a denied call, a failed `apply_patch` or one run through the shell; `block`: the model reads `reason` "
     "instead of the output; `context`: beside it. With a code-mode model (`gpt-5.6-luna`) a block or a deny reaches "
     "the model as an error in its script",
     "PostToolUse, or PostToolUseFailure after a call that failed (`failed`; `exit_code` and the error as "
     "`output_text`); `output_text` is None after a Write, Edit, MultiEdit or NotebookEdit (no output text); a command "
     "moved to the background at its timeout is a PostToolUse with the output so far; none after a denied call or one "
     "whose input the CLI refused; `block`: the output stays and the model also reads `reason`; `context`: beside it"),
    ("`stop`",
     "Stop: `block` once per turn, the model going on with `reason`; a blocked stop's `context` and the queue go with "
     "the reason",
     "Stop: `block` once per turn, the model going on with `reason`; a stop's `context` goes out only with a block "
     "(alone, it would make the model go on); a sub-agent's end is no stop"),
    ("`before_model`",
     "PreModelRequest, before every model request; its output is ignored: only its `context` is queued",
     "no such event: the runner calls it after the prompt, after every model message's tool calls (PostToolBatch) "
     "and after a blocked stop, once per model request (none before a sub-agent's first); only its `context` is "
     "queued"),
    ("queued context",
     "handed over with the next prompt or tool result, or a blocked stop; a stop that goes through leaves it for the "
     "session's next prompt (a resumed session)",
     "handed over with the agent's next prompt or tool result (a failed one too), or a blocked stop; a stop that goes "
     "through leaves it for the next prompt; a sub-agent's queue is its own"),
    ("`stop_session`",
     "ends the session at `session_start` only; elsewhere the session goes on (with a classic, not code-mode, model a "
     "stop at `after_tool` replaces the output with `reason`)",
     "ends the session at `before_tool` (once the call has run) and at `after_tool` of a call that succeeded; ignored "
     "at `session_start` and at `after_tool` of a failed call"),
    ("`system_message`",
     "shown nowhere",
     "shown to the person watching, not to the model"),
    ("`inp.tool_kind`",
     "`shell` (Bash: a code-mode script's `exec_command` too), `patch` (`apply_patch`), `mcp`, `other`",
     "`shell` (Bash), `read` (Read), `write` (Write, Edit, MultiEdit, NotebookEdit), `mcp`, `other` (Grep, Glob, "
     "Agent ...)"),
    ("`inp.cwd`, `inp.model`, `inp.turn_id`",
     "the session's directory (`inp.workspace`), even for a command run in another `workdir`; the model; the turn",
     "the shell's current directory, which follows the agent's `cd` (`inp.workspace` is the project's); no model; the "
     "prompt's id"),
]


def capability_table() -> str:
    """CAPABILITIES as the Markdown table EVOLVER.md carries."""
    wires = [HOSTS[name] for name in HOOK_HOSTS]
    rows = ["| | " + " | ".join(w.label for w in wires) + " |", "|---|" + "---|" * len(wires)]
    rows += [f"| {what} | " + " | ".join(cells) + " |" for what, *cells in CAPABILITIES]
    return "\n".join(rows)


if __name__ == "__main__":                                   # python3 hook_hosts.py: print the table
    sys.stdout.write(capability_table() + "\n")
