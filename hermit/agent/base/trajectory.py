"""A run's trajectory as host-neutral steps, read from the host's own log.

An acting agent keeps the log its host writes, unchanged (`RunOutput.log`, copied into the learning material as a
record's `trajectory`); what a learner sees of it is the Evolver's choice. This module is the one reader of those
logs, so that every Evolver reads the same steps:

    from hermit.agent.base.trajectory import read, text
    steps = read(record, step_dir)          # [] when the record's log holds no host stream
    text(steps, message=1200, output=600)   # one line per step, each field clipped from its start

The streams read: Codex's `codex exec --json` events (`agent-codex.jsonl`), Claude Code's stream-json
(`agent-claude.jsonl`), Pi's `--mode json` events (`agent-pi.jsonl`) and OpenCode's `run --format json` events
(`agent-opencode.jsonl`), also under another name below a log directory (Harbor's `agent/codex.txt`). A normalised
message list (`RunOutput.messages`) is the pipeline's own rendering and is left to each Evolver.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

CODEX_STREAM = "agent-codex.jsonl"
CLAUDE_STREAM = "agent-claude.jsonl"
PI_STREAM = "agent-pi.jsonl"
OPENCODE_STREAM = "agent-opencode.jsonl"
STREAM_HOSTS = {CODEX_STREAM: "codex", CLAUDE_STREAM: "claude", PI_STREAM: "pi",      # the file a host's log keeps
                OPENCODE_STREAM: "opencode"}
STREAMS = tuple(STREAM_HOSTS)
OTHER_STREAMS = ("agent/codex.txt", "codex.txt")   # a Harbor trial's Codex events: from the trial or its agent/


@dataclass
class Step:
    """One thing the agent did or was told, in order. `kind` is message, reasoning, command, file_change, tool_call
    or error; `text` is a message's or reasoning's text, an error's message, or a file change's or tool call's
    summary (JSON); a command has `command`, `exit_code` and `output`."""
    kind: str
    text: str = ""
    role: str = "assistant"
    command: str = ""
    exit_code: object = None
    output: str = ""


def events(path: Path) -> list[dict]:
    """The JSON objects of a stream, one per line; other lines (a CLI's own notices) are skipped."""
    path = Path(path)
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []:
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def codex_steps(stream: list[dict]) -> list[Step]:
    """Codex's events: completed items (reasoning, messages, commands, file changes, MCP tool calls with their
    arguments, and their result as the step's `output`, which `text` does not print, web searches, to-do lists,
    sub-agent calls, errors), top-level errors and a failed turn (once, when Codex also reported it as an error);
    turn and item starts and usage are left out."""
    steps = []
    for event in stream:
        kind, item = event.get("type"), event.get("item") or {}
        if kind == "error":
            steps.append(Step("error", str(event.get("message") or "")))
            continue
        if kind == "turn.failed":
            error = event.get("error")
            message = str(error.get("message") if isinstance(error, dict) else error or "")
            if not (steps and steps[-1].kind == "error" and steps[-1].text == message):   # Codex's error, again
                steps.append(Step("error", message))
            continue
        if kind != "item.completed":
            continue
        t = item.get("type")
        if t == "reasoning":
            steps.append(Step("reasoning", str(item.get("text") or "")))
        elif t == "agent_message":
            steps.append(Step("message", str(item.get("text") or "")))
        elif t == "command_execution":
            steps.append(Step("command", command=str(item.get("command") or ""), exit_code=item.get("exit_code"),
                              output=str(item.get("aggregated_output") or "")))
        elif t == "file_change":
            steps.append(Step("file_change", json.dumps(item.get("changes") or item, default=str)))
        elif t == "mcp_tool_call":                     # the arguments first: a line keeps a field's end (text())
            steps.append(Step("tool_call", json.dumps({k: item.get(k) for k in ("arguments", "server", "tool",
                                                                                 "status")},
                                                      ensure_ascii=False, default=str),
                              output=json.dumps(item.get("error") or item.get("result"), ensure_ascii=False,
                                                default=str) if item.get("error") or item.get("result") else ""))
        elif t == "web_search":
            steps.append(Step("tool_call", json.dumps({"tool": "web_search", "query": item.get("query")},
                                                      ensure_ascii=False, default=str)))
        elif t == "todo_list":
            steps.append(Step("tool_call", json.dumps({"tool": "todo_list", "items": item.get("items") or []},
                                                      ensure_ascii=False, default=str)))
        elif t == "collab_tool_call":                  # a sub-agent: spawned with a prompt, waited for, its state
            steps.append(Step("tool_call", json.dumps({"server": "sub_agents", "tool": item.get("tool"),
                                                       "agents": item.get("receiver_thread_ids") or [],
                                                       "prompt": item.get("prompt"), "status": item.get("status")},
                                                      ensure_ascii=False),
                              output=json.dumps(item.get("agents_states") or {}, ensure_ascii=False, default=str)))
        elif t == "error":
            steps.append(Step("error", str(item.get("message") or "")))
    return steps


def _blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def _result_text(content) -> str:
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    return "" if content is None else str(content)


def claude_steps(stream: list[dict]) -> list[Step]:
    """Claude Code's stream-json: the assistant's text, thinking and tool uses, each tool use with its result (a Bash
    call as a command, its exit code 1 when the result is an error), and a failed final result as an error."""
    steps, pending = [], {}
    for event in stream:
        kind, message = event.get("type"), event.get("message") or {}
        if kind == "assistant" and event.get("is_api_error_message"):
            continue                                  # the CLI's report of a failure, which the result repeats: no message
        if kind == "assistant":
            for block in _blocks(message.get("content")):
                t = block.get("type")
                if t == "text" and str(block.get("text") or "").strip():
                    steps.append(Step("message", str(block["text"])))
                elif t == "thinking" and str(block.get("thinking") or "").strip():
                    steps.append(Step("reasoning", str(block["thinking"])))
                elif t == "tool_use":
                    name, args = str(block.get("name") or ""), block.get("input") or {}
                    if name == "Bash":
                        step = Step("command", command=str(args.get("command") or ""))
                    else:
                        step = Step("tool_call", json.dumps({"tool": name, "input": args}, default=str))
                    steps.append(step)
                    pending[block.get("id")] = step
        elif kind == "user":
            for block in _blocks(message.get("content")):
                step = pending.pop(block.get("tool_use_id"), None) if block.get("type") == "tool_result" else None
                if step is not None and step.kind == "command":
                    step.output = _result_text(block.get("content"))
                    step.exit_code = 1 if block.get("is_error") else 0
        elif kind == "result" and (event.get("is_error") or str(event.get("subtype") or "").startswith("error")):
            errors = event.get("errors") if isinstance(event.get("errors"), list) else []
            steps.append(Step("error", str(event.get("result") or "; ".join(map(str, errors))
                                           or event.get("subtype") or "")))
    return steps


PI_FILE_TOOLS = ("edit", "write")


def _pi_text(result) -> str:
    """A Pi tool result's text: its content blocks' text, joined."""
    return _result_text((result or {}).get("content")) if isinstance(result, dict) else _result_text(result)


def pi_steps(stream: list[dict]) -> list[Step]:
    """Pi's `--mode json` events: each assistant message's thinking, text and tool calls (`message_end`, the
    message as it was kept), each call completed by its `tool_execution_end`: bash as a command with its exit code
    and output, edit and write as file changes, an MCP tool (`mcp__<server>__<tool>`) as a tool call with its
    server and arguments and its result as `output`, any other tool as a tool call; an assistant message that
    ended in an error or an abort as an error. Deltas, turn and session events and usage are left out."""
    steps, pending = [], {}
    for event in stream:
        kind = event.get("type")
        if kind == "message_end" and (event.get("message") or {}).get("role") == "assistant":
            message = event["message"]
            for block in _blocks(message.get("content")):
                t = block.get("type")
                if t == "text" and str(block.get("text") or "").strip():
                    steps.append(Step("message", str(block["text"])))
                elif t == "thinking" and str(block.get("thinking") or "").strip():
                    steps.append(Step("reasoning", str(block["thinking"])))
                elif t == "toolCall":
                    name, args = str(block.get("name") or ""), block.get("arguments") or {}
                    if name == "bash":
                        step = Step("command", command=str(args.get("command") or ""))
                    elif name in PI_FILE_TOOLS:
                        step = Step("file_change", json.dumps({"tool": name, "path": args.get("path")},
                                                              ensure_ascii=False, default=str))
                    elif name.startswith("mcp__") and name.count("__") >= 2:
                        _, server, tool = name.split("__", 2)
                        step = Step("tool_call", json.dumps({"arguments": args, "server": server, "tool": tool},
                                                            ensure_ascii=False, default=str))
                    else:
                        step = Step("tool_call", json.dumps({"tool": name, "input": args}, ensure_ascii=False,
                                                            default=str))
                    steps.append(step)
                    pending[block.get("id")] = step
            if message.get("stopReason") in ("error", "aborted"):
                steps.append(Step("error", str(message.get("errorMessage") or f"request {message['stopReason']}")))
        elif kind == "tool_execution_end":
            step = pending.pop(event.get("toolCallId"), None)
            if step is None:
                continue
            result = event.get("result") if isinstance(event.get("result"), dict) else {}
            structured = result.get("structuredContent") if isinstance(result.get("structuredContent"), dict) else {}
            if step.kind == "command":
                step.output = str(structured.get("output")) if "output" in structured else _pi_text(result)
                # A timeout or an abort has no structured result: its exit code is unknown, as Codex reports it.
                step.exit_code = structured.get("exit_code") if "exit_code" in structured else \
                    (None if event.get("isError") else 0)
            elif step.kind == "tool_call":
                step.output = _pi_text(result)
    return steps


OPENCODE_SHELL = ("shell", "bash")                         # 2.x names it shell, 1.x bash
OPENCODE_FILE_TOOLS = ("edit", "write", "apply_patch", "patch", "multiedit")


def opencode_steps(stream: list[dict]) -> list[Step]:
    """OpenCode's `run --format json` events, each a finished part: text as a message, reasoning as reasoning, a tool
    part (its call and its result together) as a command (the shell, with its exit code and output), a file change
    (its tool and path) or a tool call (any other tool, an MCP server's included, named `<server>_<tool>`, with
    its input and its result as `output`); an `error` event as an error. Step boundaries, retries and usage are left
    out."""
    steps = []
    for event in stream:
        kind = event.get("type")
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if kind == "text" and str(part.get("text") or "").strip():
            steps.append(Step("message", str(part["text"])))
        elif kind == "reasoning" and str(part.get("text") or "").strip():
            steps.append(Step("reasoning", str(part["text"])))
        elif kind == "tool_use":
            name = str(part.get("tool") or "")
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            args = state.get("input") if isinstance(state.get("input"), dict) else {}
            failed = state.get("status") == "error"
            output = str(state.get("error") if failed else state.get("output") or "")
            if name in OPENCODE_SHELL:
                meta = (state.get("metadata") or {}).get("metadata") if isinstance(state.get("metadata"), dict) \
                    else None
                code = meta.get("exit") if isinstance(meta, dict) else None
                # a call refused or stopped before it ran has no exit code: unknown, as Codex reports it
                steps.append(Step("command", command=str(args.get("command") or ""), output=output,
                                  exit_code=code if isinstance(code, int) and not failed else None))
            elif name in OPENCODE_FILE_TOOLS:
                steps.append(Step("file_change", json.dumps({"tool": name, "path": args.get("path") or
                                                             args.get("filePath")}, ensure_ascii=False, default=str),
                                  output=output if failed else ""))
            else:
                steps.append(Step("tool_call", json.dumps({"tool": name, "input": args}, ensure_ascii=False,
                                                          default=str), output=output))
        elif kind == "error":
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            steps.append(Step("error", ": ".join(str(x) for x in (error.get("type"), error.get("message")) if x)
                              or "the session failed"))
    return steps


# Each host's stream: the reader of its events, and the event types that mark it (tried in this order when a stream
# is found by its events). A host's stream is added here and in STREAM_HOSTS.
READERS = {"codex": codex_steps, "claude": claude_steps, "pi": pi_steps, "opencode": opencode_steps}
MARKS = (("codex", {"thread.started", "turn.started", "turn.completed", "turn.failed", "item.completed"}),
         ("claude", {"system", "assistant", "user", "result"}),
         ("pi", {"agent_start", "agent_settled", "message_end", "tool_execution_end"}),
         ("opencode", {"step_start", "step_finish", "tool_use"}))


def _host(stream: list[dict]) -> str | None:
    """Which host wrote a stream, by its events (MARKS)."""
    kinds = {str(e.get("type")) for e in stream}
    return next((host for host, marks in MARKS if kinds & marks), None)


def _steps_of(path: Path, name: str | None = None) -> list[Step] | None:
    stream = events(path)
    harbor = "codex" if path.name == "codex.txt" and stream else None          # Harbor's Codex events, by name
    host = STREAM_HOSTS.get(name) or harbor or _host(stream)
    if host is None:
        return None
    return READERS[host](stream)


def log_steps(log: Path) -> list[Step] | None:
    """The steps of a log (a directory, or one file): its host stream's, or None when it holds none. A directory's
    stream is the one at its top under the host's name, else one at a known place of another host's layout (a
    Harbor trial keeps Codex's events as agent/codex.txt); a file is read by its name, or by its events. Nothing
    deeper is searched: a directory of several runs gives no one run's steps."""
    log = Path(log)
    if log.is_file():
        return _steps_of(log, log.name if log.name in STREAMS else None)
    if not log.is_dir():
        return None
    for name in STREAMS:
        if (log / name).is_file():
            return _steps_of(log / name, name)
    for rel in OTHER_STREAMS:
        if (log / rel).is_file():
            found = _steps_of(log / rel)
            if found is not None:
                return found
    return None


def read(record: dict, step_dir: Path) -> list[Step]:
    """A learning-material record's steps, from its copied log (`trajectory`, relative to the step directory)."""
    rel = record.get("trajectory")
    return (log_steps(Path(step_dir) / rel) or []) if rel else []


def tail(value, n: int) -> str:
    """The last `n` characters of a field, stripped, with an ellipsis when cut."""
    value = str(value or "").strip()
    return value if len(value) <= n else "..." + value[-n:]


def text(steps: list[Step], *, message: int = 1200, reasoning: int = 400, command: int = 300, output: int = 600,
         other: int = 300, error: int = 300) -> str:
    """The steps as lines: `[reasoning]`, `[assistant]`, `[command exit N]` with an `output:` line, `[file change]`,
    `[tool call]`, `[error]`; each field keeps its last characters up to its limit."""
    lines = []
    for s in steps:
        if s.kind == "reasoning":
            lines.append(f"[reasoning] {tail(s.text, reasoning)}")
        elif s.kind == "message":
            lines.append(f"[{s.role}] {tail(s.text, message)}")
        elif s.kind == "command":
            lines.append(f"[command exit {s.exit_code}] {tail(s.command, command)}")
            out = tail(s.output, output)
            if out:
                lines.append(f"  output: {out}")
        elif s.kind == "file_change":
            lines.append(f"[file change] {tail(s.text, other)}")
        elif s.kind == "tool_call":
            lines.append(f"[tool call] {tail(s.text, other)}")
        elif s.kind == "error":
            lines.append(f"[error] {tail(s.text, error)}")
    return "\n".join(lines)
