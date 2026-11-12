"""A Claude Code session's event stream in upstream Harness²'s trajectory grammar.

Upstream renders a Codex session with harness2.substrate.codex.render_events (and an OpenCode one with its own
renderer); it has no Claude Code substrate. Without this, every trajectory of a Claude Code execution would reach the
editors, the composer and the reflection as "(no trajectory recorded ...)", while upstream's mission tells them they
read the full trajectories. The grammar is upstream's: [ACTION], [OBSERVATION], [OBSERVATION ERROR], [ASSISTANT],
[REASONING], [HARNESS] and [ERROR] lines, each observation clipped at `obs_trunc` with upstream's own marker, the whole
text at `max_chars`. Every tool's result is kept (Read, Grep and Glob as well as Bash), as Codex's stream keeps every
command's output. Lines of a sub-agent's turns say so.

The stream is what `claude -p --output-format stream-json --verbose --include-hook-events` prints
(hermit.agent.base.cli.Claude): one `assistant` event per content block, `user` events with the tool results and the
text the harness adds (a reminder, a refusal notice), `system` events (what each hook printed: the model reads a
blocking hook's reason as a message of the CLI's, which the stream does not carry; a background sub-agent's start and
hand-back), and a closing `result`.
"""
from __future__ import annotations

import json
from pathlib import Path

TRUNCATED = "…(+{n} chars — render truncation)"          # upstream's marker (harness2/substrate/codex.py)


def clip(text, limit: int) -> str:
    text = "" if text is None else str(text).replace("\x00", "")
    return text if limit <= 0 or len(text) <= limit else text[:limit] + TRUNCATED.format(n=len(text) - limit)


def _blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)] if isinstance(content, list) else []


def _result_text(content) -> str:
    """A tool result's text: a string, or its text blocks (an image or a document is named, not shown)."""
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
        elif block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        else:
            parts.append(f"({block.get('type') or 'block'})")
    return "\n".join(parts)


def _action(name: str, args, limit: int) -> str:
    """A tool call as upstream's [ACTION] line names one: a command as it was run, other tools by their arguments."""
    if isinstance(args, dict) and name == "Bash" and isinstance(args.get("command"), str):
        return clip(args["command"], limit)
    return clip(json.dumps(args, ensure_ascii=False, default=str), limit)


def render_claude_events(path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 2000) -> str:
    """Render a Claude Code stream-json file into upstream's shared grammar (`obs_trunc` and `max_chars` as upstream's
    renderers take them, `args_trunc` its Codex substrate's default for a tool's input)."""
    try:
        # U+0085, U+2028 and U+2029 end no line: Claude Code's JSON.stringify leaves them unescaped in an event
        raw_lines = Path(path).read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError as exc:
        return f"(failed to read trajectory: {exc})"
    lines: list[str] = []

    def emit(line: str) -> None:
        # one failed request is said twice, by its `assistant` event and by the result: once is enough
        if not (line.startswith("[ERROR] ") and lines and lines[-1] == line):
            lines.append(line)

    for raw in raw_lines:
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        sub = "(sub-agent) " if event.get("parent_tool_use_id") else ""
        if kind == "system":
            subtype = event.get("subtype")
            if subtype == "init":
                found = [f"model={event.get('model')}"]
                for key in ("tools", "agents", "skills"):
                    names = event.get(key)
                    if isinstance(names, list) and names:
                        found.append(f"{key}={','.join(map(str, names))}")
                emit(f"[HARNESS] session started | {' '.join(found)}")
            elif subtype == "api_retry":
                emit(f"[HARNESS] {sub}api retry {event.get('attempt')}/{event.get('max_retries')}: "
                     f"{event.get('error')} {event.get('error_status') or ''}".rstrip())
            elif subtype == "hook_response":             # what a hook printed, as the CLI handed it on
                said = str(event.get("output") or event.get("stdout") or event.get("stderr") or "").strip()
                code, outcome = event.get("exit_code"), event.get("outcome")
                if said or code not in (0, None) or outcome not in ("success", None):
                    name = event.get("hook_name") or event.get("hook_event")
                    head = f"[HARNESS] {sub}hook {name} (exit {code}, {outcome})"
                    emit(f"{head}: {clip(said, obs_trunc)}" if said else head)
            elif str(subtype or "").startswith("task_"):  # a background sub-agent's start, progress and hand-back
                said = [str(event[k]) for k in ("status", "description", "summary", "result")
                        if isinstance(event.get(k), (str, int, float)) and str(event[k]).strip()]
                head = f"[HARNESS] {sub}{subtype}"
                emit(f"{head}: {clip(' | '.join(said), obs_trunc)}" if said else head)
            elif isinstance(event.get("content"), str) and event["content"].strip():   # a notice of the CLI's
                emit(f"[HARNESS] {sub}{clip(event['content'], 1000)}")
        elif kind == "assistant":
            if event.get("error"):                       # the CLI's own message for a failed request
                emit(f"[ERROR] {sub}{clip(_result_text(message.get('content')) or event['error'], 1000)}")
                continue
            for block in _blocks(message.get("content")):
                btype = block.get("type")
                if btype == "text" and str(block.get("text") or "").strip():
                    emit(f"[ASSISTANT] {sub}{clip(block['text'], obs_trunc)}")
                elif btype == "thinking" and str(block.get("thinking") or "").strip():
                    emit(f"[REASONING] {sub}{clip(block['thinking'], obs_trunc)}")
                elif btype in ("tool_use", "server_tool_use"):
                    emit(f"[ACTION] {sub}{block.get('name') or 'tool'}: "
                         f"{_action(str(block.get('name') or ''), block.get('input'), args_trunc)}")
        elif kind == "user":
            for block in _blocks(message.get("content")):
                btype = block.get("type")
                if btype == "tool_result":
                    tag = "OBSERVATION ERROR" if block.get("is_error") else "OBSERVATION"
                    emit(f"[{tag}] {sub}{clip(_result_text(block.get('content')), obs_trunc)}")
                elif btype == "text" and str(block.get("text") or "").strip():
                    # text the harness adds to the conversation: hook feedback, a reminder, a refusal notice
                    emit(f"[HARNESS] {sub}{clip(block['text'], obs_trunc)}")
        elif kind == "result":
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            if event.get("is_error") or str(event.get("subtype") or "").startswith("error"):
                errors = event.get("errors") if isinstance(event.get("errors"), list) else []
                why = event.get("result") or "; ".join(map(str, errors)) or event.get("subtype")
                emit(f"[ERROR] {clip(why, 1000)}")
            # in: everything sent, as Codex counts it (cached included), and of that what was read from the cache
            sent = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                         "cache_read_input_tokens"))
            emit(f"[HARNESS] session completed | turns={event.get('num_turns')} tokens in={sent} "
                 f"cached={usage.get('cache_read_input_tokens')} out={usage.get('output_tokens')}")
    rendered = "\n".join(lines).strip()
    if not rendered:
        return "(no parseable events in trajectory)"
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars] + "\n" + TRUNCATED.format(n=len(rendered) - max_chars)
    return rendered


def render_pi_events(path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 2000) -> str:
    """Render a Pi `--mode json` stream into upstream's shared grammar: each assistant message's text, thinking and
    tool calls, and each call's result as Pi reported it (a failed one, an edit's or a read's too, as an error)."""
    from hermit.agent.base.trajectory import events
    try:
        stream = events(path)
    except OSError as exc:
        return f"(failed to read trajectory: {exc})"
    lines: list[str] = []
    for event in stream:
        kind = event.get("type")
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        if kind == "message_end" and message.get("role") == "assistant":
            for block in _blocks(message.get("content")):
                btype = block.get("type")
                if btype == "text" and str(block.get("text") or "").strip():
                    lines.append(f"[ASSISTANT] {clip(block['text'], obs_trunc)}")
                elif btype == "thinking" and str(block.get("thinking") or "").strip():
                    lines.append(f"[REASONING] {clip(block['thinking'], obs_trunc)}")
                elif btype == "toolCall":
                    name, args = str(block.get("name") or "tool"), block.get("arguments") or {}
                    lines.append(f"[ACTION] {name}: {_action('Bash' if name == 'bash' else name, args, args_trunc)}")
            if message.get("stopReason") in ("error", "aborted"):
                lines.append(f"[ERROR] {clip(message.get('errorMessage') or 'request ' + message['stopReason'], 1000)}")
        elif kind == "tool_execution_end":
            result = event.get("result") if isinstance(event.get("result"), dict) else {}
            structured = result.get("structuredContent") if isinstance(result.get("structuredContent"), dict) else {}
            said = str(structured["output"]) if event.get("toolName") == "bash" and "output" in structured \
                else _result_text(result.get("content"))
            tag = "OBSERVATION ERROR" if event.get("isError") else "OBSERVATION"
            lines.append(f"[{tag}] {clip(said, obs_trunc)}")
    rendered = "\n".join(lines).strip()
    if not rendered:
        return "(no parseable events in trajectory)"
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars] + "\n" + TRUNCATED.format(n=len(rendered) - max_chars)
    return rendered
