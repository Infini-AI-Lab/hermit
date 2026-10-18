"""One acting trajectory as bounded text for the toolsmith learner: a header of counters (result.json), one entry per
tool call (tools/NN.json), the agent's final message (final.md).

File operations take one line; commands and adapter tools keep their exits, timings, output tails and image counts;
dispatch errors ("Unknown tool", invalid arguments) stay verbatim. When the text exceeds the limit, calls are dropped in
this order of preference: the rest first, then everything but the last 8 calls, then non-zero exits, then adapter-tool
calls; the header and the footer always stay.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

FILE_OPS = ("write_file", "read_file", "replace_text")
HEADER_KEYS = ("case", "status", "stop_reason", "model_calls", "tool_calls", "run_commands", "adapter_tool_calls",
               "image_bearing_calls", "looked_calls", "first_image_call", "tools_exported", "seen_final")
COMMAND_CHARS = ARGS_CHARS = 300
STDOUT_CHARS, STDERR_CHARS = 300, 800     # why: a command's first lines say what ran, its last lines why it failed
TEXT_CHARS, TAIL_CHARS = 1500, 1500       # why: a tool's text is what the agent read; its stderr is for the learner
ERROR_CHARS = 1500
FOOTER_CHARS = 1500
LAST_CALLS = 8
OMITTED_LISTED = 20


class Entry(NamedTuple):
    number: str
    text: str
    adapter_tool: bool
    failed: bool


def build(trajectory_dir: str | Path, limit: int = 20000) -> str:
    """The digest of one trajectory directory (tools/NN.json, result.json, final.md), at most `limit` characters."""
    d = Path(trajectory_dir)
    header = _header(d)
    footer = _footer(d)
    entries = _entries(d)
    return _assemble(header, footer, entries, max(int(limit), 400))


# --- parts ------------------------------------------------------------------------------------------------------------
def _header(d: Path) -> str:
    result = _read_json(d / "result.json")
    if not isinstance(result, dict):
        return "Run: (no result.json)"
    parts = [f"{k}={_short(result.get(k, '?'), 120)}" for k in HEADER_KEYS]
    if result.get("elapsed_seconds") is not None:
        parts.append(f"elapsed_seconds={result['elapsed_seconds']}")
    if result.get("tool_errors") not in (None, 0, [], {}):
        parts.append(f"tool_errors={_short(result['tool_errors'], 200)}")
    return "Run: " + " ".join(parts)


def _footer(d: Path) -> str:
    final = d / "final.md"
    if not final.is_file():
        return ""
    text = final.read_text("utf-8", errors="replace").strip()
    return "Agent's final message: " + _clip(text, FOOTER_CHARS) if text else ""


def _entries(d: Path) -> list[Entry]:
    tools = d / "tools"
    if not tools.is_dir():
        return []
    out = []
    for path in sorted(tools.glob("*.json")):
        number = path.stem
        record = _read_json(path)
        if not isinstance(record, dict):
            out.append(Entry(number, f"{number} (unreadable record)", False, True))
            continue
        name = str(record.get("name") or "?")
        if name in FILE_OPS:
            out.append(_file_op(number, name, record))
        elif name == "run_command":
            out.append(_command(number, record))
        else:
            out.append(_adapter_tool(number, name, record))
    return out


def _file_op(number: str, name: str, record: dict) -> Entry:
    args = record.get("arguments") if isinstance(record.get("arguments"), dict) else {}
    result = _as_dict(record.get("result"))
    error = _error_of(result)
    target = args.get("path") or result.get("file") or ""
    detail = ""
    if name == "write_file":
        detail = f" ({len(str(args.get('content', '')))} chars)"
    elif name == "read_file":
        detail = f" (from line {args.get('start_line', 1)}, {args.get('max_lines', '?')} lines)"
    status = f"ERROR {_one_line(error, 200)}" if error else "ok"
    return Entry(number, f"{number} {name} {target}{detail} -> {status}", False, bool(error))


def _command(number: str, record: dict) -> Entry:
    args = record.get("arguments") if isinstance(record.get("arguments"), dict) else {}
    result = _as_dict(record.get("result"))
    command = _one_line(args.get("command", ""), COMMAND_CHARS)
    exit_code = result.get("exit_code", record.get("exit_code"))
    error = _error_of(result)
    head = f"{number} run_command `{command}` -> " + _status(exit_code, result, record, error)
    lines = [head]
    if result.get("stdout"):
        lines.append("    stdout: " + _indent(_clip(str(result["stdout"]), STDOUT_CHARS)))
    if result.get("stderr"):
        lines.append("    stderr: " + _indent(_tail(str(result["stderr"]), STDERR_CHARS)))
    if error and not result.get("stderr"):
        lines.append("    error: " + _indent(_clip(error, ERROR_CHARS)))
    lines.append("    images: " + _images(record, result))
    failed = bool(error) or bool(result.get("timed_out")) or (exit_code not in (0, None))
    return Entry(number, "\n".join(lines), False, failed)


def _adapter_tool(number: str, name: str, record: dict) -> Entry:
    args = record.get("arguments")
    result = _as_dict(record.get("result"))
    exit_code = result.get("exit_code", record.get("exit_code"))
    error = _error_of(result)
    text = result.get("text") if isinstance(result.get("text"), str) else None
    if text is None and not error and set(result) - {"images"}:
        text = json.dumps(result, ensure_ascii=False, default=str)     # an older record: the whole result is the text
    head = f"{number} {name} {_one_line(json.dumps(args, ensure_ascii=False, default=str), ARGS_CHARS)} -> " \
        + _status(exit_code, result, record, error)
    lines = [head]
    if text:
        verbatim = "Unknown tool" in text or text.startswith("Error")
        lines.append("    text: " + _indent(_clip(text, ERROR_CHARS if verbatim else TEXT_CHARS)))
    tail = result.get("stderr_tail") or result.get("stderr")
    if tail:
        lines.append("    stderr_tail: " + _indent(_tail(str(tail), TAIL_CHARS)))
    lines.append("    images: " + _images(record, result))
    failed = bool(error) or bool(result.get("timed_out")) or (exit_code not in (0, None)) \
        or (text is not None and ("Unknown tool" in text or text.startswith("Error")))
    return Entry(number, "\n".join(lines), True, failed)


def _status(exit_code, result: dict, record: dict, error: str | None) -> str:
    parts = [f"exit {exit_code if exit_code is not None else '?'}"]
    seconds = result.get("seconds", record.get("seconds"))
    if seconds is not None:
        parts.append(f"{seconds} s")
    if result.get("timed_out") or record.get("timed_out"):
        parts.append("TIMED OUT")
    lock = result.get("lock_wait_s", record.get("lock_wait_s"))
    if lock:
        parts.append(f"lock wait {lock} s")
    if record.get("browser_launched") is not None:
        parts.append(f"browser_launched={record['browser_launched']}")
    if error:
        parts.append("error: " + _one_line(error, 200))
    return ", ".join(parts)


def _images(record: dict, result: dict) -> str:
    records = record.get("image_records")
    if not isinstance(records, list):
        records = result.get("images") if isinstance(result.get("images"), list) else []
    names = []
    for item in records:
        if isinstance(item, dict):
            label = str(item.get("file", "?"))
            if item.get("nonblank") is False:
                label += " (blank)"
            if item.get("packed_for_model") is False:
                label += " (not shown)"
            names.append(label)
        else:
            names.append(str(item))
    text = f"{len(names)}" + (f" ({', '.join(names[:8])}{', …' if len(names) > 8 else ''})" if names else "")
    rejected = record.get("images_rejected") or result.get("images_rejected")
    if rejected:
        text += f"; rejected: {_one_line(json.dumps(rejected, ensure_ascii=False, default=str), 300)}"
    return text


# --- assembly ---------------------------------------------------------------------------------------------------------
def _assemble(header: str, footer: str, entries: list[Entry], limit: int) -> str:
    n = len(entries)

    def rank(i: int) -> int:
        e = entries[i]
        if e.adapter_tool:
            return 1
        if e.failed:
            return 2
        return 3 if i >= n - LAST_CALLS else 4

    def render(keep: set[int]) -> str:
        lines = [header, f"Tool calls ({n}):"]
        omitted = []
        for i, e in enumerate(entries):
            if i in keep:
                lines.append(e.text)
            else:
                omitted.append(e.number)
        if omitted:
            listed = ", ".join(omitted[:OMITTED_LISTED]) + (", …" if len(omitted) > OMITTED_LISTED else "")
            lines.append(f"({len(omitted)} calls omitted for length: {listed})")
        if footer:
            lines.append(footer)
        return "\n".join(lines)

    order = sorted(range(n), key=lambda i: (rank(i), -i))      # best rank first; within a rank the latest calls first
    budget = limit - len(header) - len(footer) - 80           # why: the "Tool calls" line and an omitted line need room
    keep: set[int] = set()
    for i in order:
        size = len(entries[i].text) + 1
        if size <= budget:
            keep.add(i)
            budget -= size
    text = render(keep)
    while len(text) > limit and keep:        # the omitted line grew past the reserve: drop the least valuable entries
        keep.discard(max(keep, key=lambda i: (rank(i), -i)))
        text = render(keep)
    if len(text) > limit and footer:
        footer = _clip(footer, max(0, len(footer) - (len(text) - limit) - 40))
        text = render(keep)
    return text


# --- helpers ----------------------------------------------------------------------------------------------------------
def _read_json(path: Path):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _as_dict(value) -> dict:
    """A tool record's result as a dict: a dict as is, a JSON-object string parsed, any other text under `text`."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                pass
        return {"text": value}
    if value is None:
        return {}
    return {"text": json.dumps(value, ensure_ascii=False, default=str)}


def _error_of(result: dict) -> str | None:
    if result.get("ok") is False:
        return str(result.get("error") or "failed")
    if result.get("error"):
        return str(result["error"])
    return None


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f" …[+{len(text) - n} chars]"


def _tail(text: str, n: int) -> str:
    return text if len(text) <= n else f"[{len(text) - n} chars]… " + text[-n:]


def _one_line(value, n: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return _clip(" ".join(text.split()), n)


def _short(value, n: int) -> str:
    return _one_line(value, n)


def _indent(text: str) -> str:
    return text.replace("\n", "\n        ")
