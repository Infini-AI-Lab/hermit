"""Trajectory renderers: one readable grammar for every acting agent the framework runs.

    [USER] ...            the task as the agent received it
    [ASSISTANT] ...       the model's own text
    [THINKING] ...        reasoning the host exposed (rare)
    [ACTION] name(args)   a tool call
    [OBSERVATION] ...     the tool's result, clipped per observation with an explicit marker
    [OBSERVATION ERROR]   a failed tool call
    [ERROR] ...           the run's own failure

Sources: the react agent's `messages` (OpenAI chat format, in its JSON log), a Codex `exec --json` event stream and
a Claude Code `stream-json` stream (both JSON lines). The system prompt is never rendered: it is the harness, which
the editor has under `context/harness/`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

MARKER = "\n…[observation truncated by the renderer: {omitted:,} chars omitted; this marker is ours, not the agent's]"


def clip(text: str, limit: int) -> str:
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    if limit and len(text) > limit:
        head = max(limit - 200, limit // 2)
        return text[:head] + MARKER.format(omitted=len(text) - head)
    return text


def _args(raw) -> str:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return raw
    return json.dumps(raw, ensure_ascii=False, default=str)


_ERROR = re.compile(r"\s*(?:\[(?:read|grep|glob|tool|search|bash)[ _]error|error\b|traceback)", re.I)


def _is_error(content) -> bool:
    """A failed tool call: "Error: ...", SkillOpt's "[read error: ...]", a traceback."""
    return isinstance(content, str) and _ERROR.match(content) is not None


def _action(name: str, arguments) -> str:
    """One tool call; a call whose name is the whole command (SkillOpt's logs) prints as is."""
    if arguments in ("", None, {}) and "(" in str(name):
        return f"[ACTION] {name}"
    return f"[ACTION] {name}({_args(arguments)})"


def render_messages(messages: list[dict], *, obs_trunc: int) -> str:
    """The react agent's message list."""
    out = ["(system prompt omitted: the harness under context/harness/ is rendered into it; "
           "context/agent_prompt.md shows it whole when present)"]
    names: dict[str, str] = {}
    for m in messages or []:
        role = m.get("role")
        if role == "system":
            continue
        if role == "user":
            out.append(f"[USER] {m.get('content') or ''}")
        elif role == "assistant":
            if m.get("reasoning_content"):
                out.append(f"[THINKING] {clip(m['reasoning_content'], obs_trunc)}")
            if m.get("content"):
                out.append(f"[ASSISTANT] {m['content']}")
            for call in m.get("tool_calls") or []:
                fn = call.get("function") or {}
                names[str(call.get("id"))] = fn.get("name", "?")
                out.append(_action(fn.get("name", "?"), fn.get("arguments", "")))
        elif role == "tool":
            content = m.get("content") or ""
            name = names.get(str(m.get("tool_call_id")), "")
            tag = "[OBSERVATION ERROR]" if _is_error(content) else "[OBSERVATION]"
            out.append(f"{tag}{f' ({name})' if name else ''} {clip(content, obs_trunc)}")
    return "\n".join(out) + "\n"


def render_codex_events(lines: list[dict], *, obs_trunc: int) -> str:
    """A Codex `exec --json` stream: item.completed events."""
    out = []
    for ev in lines:
        if ev.get("type") != "item.completed":
            if ev.get("type") == "error":
                out.append(f"[ERROR] {ev.get('message') or ev}")
            continue
        item = ev.get("item") or {}
        t = item.get("type")
        if t == "agent_message":
            out.append(f"[ASSISTANT] {item.get('text') or ''}")
        elif t == "reasoning":
            out.append(f"[THINKING] {clip(item.get('text') or '', obs_trunc)}")
        elif t == "command_execution":
            out.append(f"[ACTION] bash({_args(item.get('command'))})")
            code = item.get("exit_code")
            tag = "[OBSERVATION]" if code in (0, None) else f"[OBSERVATION ERROR] (exit {code})"
            out.append(f"{tag} {clip(item.get('aggregated_output') or '', obs_trunc)}")
        elif t == "mcp_tool_call":
            out.append(f"[ACTION] {item.get('server')}.{item.get('tool')}({_args(item.get('arguments'))})")
            out.append(f"[OBSERVATION] {clip(item.get('result') or item.get('error') or '', obs_trunc)}")
        elif t == "file_change":
            out.append(f"[ACTION] file_change({_args(item.get('changes'))})")
        elif t == "error":
            out.append(f"[ERROR] {item.get('message') or ''}")
    return "\n".join(out) + "\n"


def render_claude_events(lines: list[dict], *, obs_trunc: int) -> str:
    """A Claude Code `stream-json` stream."""
    out = []
    for ev in lines:
        t = ev.get("type")
        content = (ev.get("message") or {}).get("content")
        if t == "assistant" and isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    out.append(f"[ASSISTANT] {block.get('text') or ''}")
                elif block.get("type") == "thinking":
                    out.append(f"[THINKING] {clip(block.get('thinking') or '', obs_trunc)}")
                elif block.get("type") == "tool_use":
                    out.append(f"[ACTION] {block.get('name')}({_args(block.get('input'))})")
        elif t == "user" and isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    body = block.get("content")
                    if isinstance(body, list):
                        body = "\n".join(str(b.get("text", "")) for b in body if isinstance(b, dict))
                    tag = "[OBSERVATION ERROR]" if block.get("is_error") else "[OBSERVATION]"
                    out.append(f"{tag} {clip(body or '', obs_trunc)}")
        elif t == "result" and ev.get("is_error"):
            out.append(f"[ERROR] {ev.get('result') or ''}")
    return "\n".join(out) + "\n"


def render_conversation(entries: list[dict], *, obs_trunc: int) -> str:
    """A SkillOpt prediction directory's conversation.json: the user prompt, then messages and tool calls."""
    out = ["(system prompt omitted: the harness is rendered into it; see context/agent_prompt.md)"]
    for e in entries:
        if e.get("role") == "user":
            out.append(f"[USER] {e.get('content') or ''}")
        elif e.get("type") == "tool_call" or "cmd" in e:
            out.append(f"[ACTION] {e.get('cmd', '')}")
            obs = e.get("obs", "")
            tag = "[OBSERVATION ERROR]" if _is_error(obs) else "[OBSERVATION]"
            out.append(f"{tag} {clip(obs, obs_trunc)}")
        elif e.get("type") == "error":
            out.append(f"[ERROR] {e.get('content') or e.get('message') or ''}")
        elif e.get("content"):
            out.append(f"[ASSISTANT] {e['content']}")
    return "\n".join(out) + "\n"


def conversation_of(path: Path | str) -> list[dict] | None:
    """The entries of a SkillOpt prediction directory (or a run directory holding one), else None."""
    path = Path(path)
    if path.is_dir():
        found = sorted(path.rglob("conversation.json"))
        path = found[0] if found else path
    if path.name == "conversation.json" and path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            return None
        return data if isinstance(data, list) else None
    return None


def system_prompt_of(path: Path | str) -> str:
    """The acting agent's system prompt, when the log keeps it (SkillOpt: target_system_prompt.txt)."""
    path = Path(path)
    if path.is_dir():
        found = sorted(path.rglob("target_system_prompt.txt"))
        if found:
            return found[0].read_text(encoding="utf-8", errors="replace")
    return ""


def final_message(messages: list[dict] | None = None, log: Path | str | None = None) -> str:
    """The agent's last non-empty assistant text, from messages or from a log."""
    if not messages and log:
        entries = conversation_of(log)
        if entries is not None:
            texts = [e.get("content") for e in entries if e.get("type") == "message" and e.get("content")]
            return texts[-1] if texts else ""
        p = Path(log)
        if p.is_file() and p.suffix == ".json":
            try:
                messages = (json.loads(p.read_text(encoding="utf-8", errors="replace")) or {}).get("messages")
            except (ValueError, AttributeError):
                messages = None
    for m in reversed(messages or []):
        if m.get("role") == "assistant" and m.get("content"):
            return str(m["content"])
    return ""


def render_log(path: Path | str, *, obs_trunc: int) -> str:
    """Render a run's log by its content: a SkillOpt prediction directory, a react JSON log, a JSON-lines event
    stream, or plain text."""
    path = Path(path)
    entries = conversation_of(path)
    if entries is not None:
        return render_conversation(entries, obs_trunc=obs_trunc)
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file() and p.suffix in (".json", ".jsonl", ".log", ".txt"))
        if not files:
            return "(no trajectory file in the log directory)\n"
        path = max(files, key=lambda p: p.stat().st_size)
    text = path.read_text(encoding="utf-8", errors="replace")
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get("messages"), list):
                return render_messages(data["messages"], obs_trunc=obs_trunc)
        except ValueError:
            pass
        events = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
        if events:
            if any(e.get("type") in ("item.completed", "thread.started", "turn.completed") for e in events):
                return render_codex_events(events, obs_trunc=obs_trunc)
            if any(e.get("type") in ("assistant", "user", "system", "result") for e in events):
                return render_claude_events(events, obs_trunc=obs_trunc)
    return clip(text, max(obs_trunc * 4, 200_000)) + "\n"
