"""An instance's trajectory tau_t = (x_t, y_t, r_t) as text, from a learning-material record.

The record is `learning_material/step_NNNN/tasks/<id>.json`: `input` (the task row), `messages` (a chat trajectory,
when the pipeline returns one), `trajectory` (the pipeline's log, copied), `output`, `judge` (score, feedback and
the judge's other fields), `status`/`error`, `pipeline_extra`. The rendering has four parts:

    [TASK]           the instance's input (the context tool's query, else the task's text fields)
    [TRAJECTORY]     the agent's actions and what the environment answered, in order
    [OUTPUT]         what the agent handed in
    [OUTCOME]        the reward: the judge's score and feedback, and its other fields

The agent's system prompt is left out: it holds the adapter's context, the harness state itself, which the paper's
tau_t does not contain. A CL-Bench record (a list of turns: query, response.action, observation) renders each turn's
action and observation and a prompt only where it changed, since the venue repeats the question on every turn.
"""
from __future__ import annotations

import json
from pathlib import Path

INPUT_FIELDS = ("question", "prompt", "instruction", "input", "query")


def clip_middle(text: str, limit: int) -> str:
    """Keep the beginning (the task and the approach) and the end (how it finished)."""
    if limit is None or limit <= 0 or len(text) <= limit:
        return text
    head = limit * 3 // 5
    return text[:head] + f"\n[... {len(text) - limit} characters left out ...]\n" + text[-(limit - head):]


def query_of(record: dict) -> str:
    """x_t: what the context tool compared (recorded by the pipeline), else the task row's text."""
    info = (record.get("pipeline_extra") or {}).get("context_tool") or {}
    if isinstance(info, dict) and str(info.get("query") or "").strip():
        return str(info["query"])
    task = record.get("input") or {}
    for key in INPUT_FIELDS:
        if isinstance(task.get(key), str) and task[key].strip():
            return task[key]
    fields = {k: v for k, v in task.items() if k not in ("id", "task_id")}
    return json.dumps(fields, ensure_ascii=False) if fields else str(task.get("id", ""))


def _content(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ("" if content is None else str(content))


def messages_text(messages: list, obs_chars: int) -> str:
    lines = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        text = _content(m)
        if text:
            lines.append(f"[{role}] {clip_middle(text, obs_chars)}")
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            lines.append(f"[tool call] {fn.get('name')}({clip_middle(str(fn.get('arguments') or ''), obs_chars)})")
    return "\n".join(lines)


def turns_text(turns: list, obs_chars: int) -> str:
    """CL-Bench's native turn records (host.py): the environment's prompt where it changes, the host's action, the
    observation that answered it."""
    lines, last_prompt = [], None
    for t in turns:
        query = t.get("query") or {}
        prompt = query.get("prompt")
        if prompt and prompt != last_prompt:
            lines.append(f"[environment] {clip_middle(str(prompt), obs_chars)}")
            last_prompt = prompt
        action = (t.get("response") or {}).get("action")
        if action is not None:
            tag = "harness tool" if t.get("channel") else "action"
            text = action if isinstance(action, str) else json.dumps(action, ensure_ascii=False)
            lines.append(f"[{tag}] {clip_middle(text, obs_chars)}")
        observation = t.get("observation")
        content = observation.get("content") if isinstance(observation, dict) else observation
        if content:
            lines.append(f"[observation] {clip_middle(str(content), obs_chars)}")
    return "\n".join(lines)


def _is_turns(data) -> bool:
    return isinstance(data, list) and bool(data) and all(isinstance(t, dict) and ("response" in t or "query" in t)
                                                           for t in data)


CODEX_STREAM = "agent-codex.jsonl"                 # a Codex session's `codex exec --json` event stream
CODEX_SKIP = ("prompt.txt", "harness.json")        # a Codex log directory's static prompt and deployment record


def _tail(text, n: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= n else "..." + text[-n:]


def codex_text(lines: list[str], obs_chars: int) -> str:
    """A Codex event stream as the agents read it: reasoning summaries, messages, commands with their exit codes and
    output, file changes and tool calls, in order; the rest of the stream (turn and item starts, usage) is left out."""
    out = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind, item = event.get("type"), event.get("item") or {}
        if kind == "error":
            out.append(f"[error] {_tail(event.get('message'), 300)}")
        elif kind == "item.completed":
            t = item.get("type")
            if t == "reasoning":
                out.append(f"[reasoning] {_tail(item.get('text'), 600)}")
            elif t == "agent_message":
                out.append(f"[assistant] {_tail(item.get('text'), obs_chars)}")
            elif t == "command_execution":
                out.append(f"[command exit {item.get('exit_code')}] {_tail(item.get('command'), 400)}")
                output = _tail(item.get("aggregated_output"), obs_chars)
                if output:
                    out.append(f"  output: {output}")
            elif t == "file_change":
                out.append(f"[file change] {_tail(json.dumps(item.get('changes') or item, default=str), 400)}")
            elif t == "mcp_tool_call":
                out.append(f"[tool call] {_tail(json.dumps({k: item.get(k) for k in ('server', 'tool', 'status')}), 400)}")
            elif t == "error":
                out.append(f"[error] {_tail(item.get('message'), 300)}")
    return "\n".join(out)


def log_text(record: dict, step_dir: Path, obs_chars: int) -> str:
    """The copied log: turn records rendered as such, a Codex event stream as its items, other JSON as JSON, text as
    text (a Codex log directory's static prompt and deployment record left out)."""
    rel = record.get("trajectory")
    if not rel:
        return ""
    root = Path(step_dir) / rel
    files = sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else ([root] if root.is_file() else [])
    parts = []
    for path in files:
        if path.name in CODEX_SKIP:
            continue
        raw = path.read_text(errors="replace")
        if path.name == CODEX_STREAM:
            parts.append(f"[{path.name}]\n{codex_text(raw.splitlines(), obs_chars) or '(no events)'}")
            continue
        if path.suffix == ".json":
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if _is_turns(data):
                parts.append(turns_text(data, obs_chars))
                continue
            if isinstance(data, dict) and _is_turns(data.get("interactions")):
                parts.append(turns_text(data["interactions"], obs_chars))
                continue
        parts.append(f"[{path.name}]\n{clip_middle(raw, obs_chars * 4)}")
    return "\n".join(parts)


NO_FEEDBACK = "(not shown: this stream gives the learner no verdict, score or reference answer)"


def outcome_text(record: dict, feedback: bool = True) -> str:
    """The [OUTCOME] block: the judge's score and feedback (the environment's outcome, the paper's footing), or, with
    `feedback` off, a line saying the learner sees none: the trajectory and the output alone are its evidence."""
    if not feedback:
        lines = [NO_FEEDBACK]
        if record.get("status") and record["status"] != "ok":
            lines.append(f"run status: {record['status']}" + (f" ({record['error']})" if record.get("error") else ""))
        return "\n".join(lines)
    judge = record.get("judge") or {}
    lines = [f"reward: {judge.get('score')}"]
    if judge.get("feedback"):
        lines.append(f"feedback: {judge['feedback']}")
    for key, value in judge.items():
        if key in ("score", "feedback", "extra"):
            continue
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False, default=str)}")
    for key, value in (judge.get("extra") or {}).items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False, default=str)}")
    if record.get("status") and record["status"] != "ok":
        lines.append(f"run status: {record['status']}" + (f" ({record['error']})" if record.get("error") else ""))
    return "\n".join(lines)


def render(record: dict, step_dir: Path, *, obs_chars: int = 4000, feedback: bool = True) -> str:
    """tau_t as text, unclipped as a whole (callers clip it for a prompt with clip_middle); `feedback` off leaves the
    judge's verdict out of the [OUTCOME] block."""
    body = messages_text(record.get("messages") or [], obs_chars) or log_text(record, step_dir, obs_chars)
    output = record.get("output")
    output = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
    return (f"[TASK]\n{query_of(record).strip()}\n\n[TRAJECTORY]\n{body.strip() or '(no trajectory was recorded)'}\n\n"
            f"[OUTPUT]\n{(output or '(no output)').strip()}\n\n[OUTCOME]\n{outcome_text(record, feedback)}\n")
