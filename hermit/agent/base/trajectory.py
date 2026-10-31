"""A run's trajectory as host-neutral steps, read from the host's own log.

An acting agent keeps the log its host writes, unchanged (`RunOutput.log`, copied into the learning material as a
record's `trajectory`); what a learner sees of it is the Evolver's choice. This module is the one reader of those
logs, so that every Evolver reads the same steps:

    from hermit.agent.base.trajectory import read, text
    steps = read(record, step_dir)          # [] when the record's log holds no host stream
    text(steps, message=1200, output=600)   # one line per step, each field clipped from its start

The streams read: Codex's `codex exec --json` events (`agent-codex.jsonl`) and Claude Code's stream-json
(`agent-claude.jsonl`). A normalised message list (`RunOutput.messages`) is the pipeline's own rendering and is
left to each Evolver.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

CODEX_STREAM = "agent-codex.jsonl"
CLAUDE_STREAM = "agent-claude.jsonl"
STREAMS = (CODEX_STREAM, CLAUDE_STREAM)


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
    """Codex's events: completed items (reasoning, messages, commands, file changes, MCP tool calls, sub-agent calls,
    errors) and top-level errors; turn and item starts and usage are left out."""
    steps = []
    for event in stream:
        kind, item = event.get("type"), event.get("item") or {}
        if kind == "error":
            steps.append(Step("error", str(event.get("message") or "")))
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
        elif t == "mcp_tool_call":
            steps.append(Step("tool_call", json.dumps({k: item.get(k) for k in ("server", "tool", "status")})))
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
        elif kind == "result" and event.get("is_error"):
            steps.append(Step("error", str(event.get("result") or event.get("subtype") or "")))
    return steps


def log_steps(log: Path) -> list[Step] | None:
    """The steps of a log (a directory, or one file): its host stream's, or None when it holds none."""
    log = Path(log)
    for name in STREAMS:
        path = log / name if log.is_dir() else (log if log.name == name else None)
        if path is not None and path.is_file():
            stream = events(path)
            return codex_steps(stream) if name == CODEX_STREAM else claude_steps(stream)
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
