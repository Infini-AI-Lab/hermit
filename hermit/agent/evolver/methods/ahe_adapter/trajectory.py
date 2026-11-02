"""A rollout's trajectory as the Agent Debugger reads it: the standard steps of the Codex agent's event stream
exploded one file per step, and Harbor's ATIF trajectories (`agent/trajectory.json`, a trial Harbor recorded): a
per-turn timing breakdown and an optional one-file-per-step explode.

Upstream's Debugger (`adb ask`) read NexAU traces: `{"trace_id", "messages": [{role, content, tool_calls, latency}]}`
with one file per message in its working directory, and `_extract_trace_timing` (evolve.py:1348-1378) summed the
assistant messages' `latency` fields. The framework's Codex agent keeps Codex's own event stream instead
(`agent-codex.jsonl`), which hermit.agent.base.trajectory reads as standard steps (message, reasoning, command with
its exit code and output, file change, tool call, error); Harbor records a trial as ATIF-v1.7: `{"schema_version",
"session_id", "agent", "steps": [{step_id, timestamp, source (system | user | agent), message, tool_calls
[{tool_call_id, function_name, arguments}], observation {results [{source_call_id, content}]}, metrics}],
"final_metrics"}`. The helpers here bridge that:

- `explode_steps`: `NNNN-<kind>.md` per standard step, so the Debugger can read and grep a rollout the way the
  paper's one-file-per-message environment allowed, citing `trace0i #NNNN`.
- `timing_breakdown`: one line per ATIF agent step. ATIF carries no per-call latency, so a turn's seconds are the
  time from the previous step's timestamp (whatever its source) to the agent step's own: model latency plus the
  previous tool's execution, which is what a timeout analysis needs. The total is the span from the first to the last
  step. The text is rendered by `queries.render_timing_breakdown`, the exact format of upstream's block. Codex's
  event stream carries no timestamps, so a rollout without an ATIF trajectory has no breakdown.
- `explode`: `NNNN-<source>.md` per ATIF step (message, tool calls as JSON, each observation result).

The ATIF helpers take a loaded trajectory dict or a path; a missing or unreadable file renders nothing, as upstream.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable

AGENT = "agent"


def load(source: dict | str | Path) -> dict | None:
    """The trajectory as a dict; None when the file is missing or not JSON."""
    if isinstance(source, dict):
        return source
    try:
        data = json.loads(Path(source).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_time(stamp) -> float | None:
    """Seconds since the epoch of an ATIF timestamp (ISO 8601, `Z` or an offset); None when absent or malformed."""
    if not isinstance(stamp, str) or not stamp:
        return None
    text = stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp
    try:
        t = datetime.fromisoformat(text)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.timestamp()


def tool_names(step: dict) -> list[str]:
    """The function names of a step's tool calls (`exec`, `shell`, `apply_patch`, ...); `?` for a nameless call."""
    names = []
    for call in step.get("tool_calls") or []:
        if isinstance(call, dict):
            names.append(str(call.get("function_name") or call.get("name") or "?"))
    return names


def turns(trajectory: dict) -> list[tuple[float, list[str]]]:
    """One (seconds, tool names) pair per agent step, in order: the input of `queries.render_timing_breakdown`.
    Seconds are measured from the previous step's timestamp; a step without a usable timestamp counts 0."""
    out: list[tuple[float, list[str]]] = []
    previous: float | None = None
    for step in trajectory.get("steps") or []:
        if not isinstance(step, dict):
            continue
        now = parse_time(step.get("timestamp"))
        if step.get("source") == AGENT:
            seconds = max(0.0, now - previous) if now is not None and previous is not None else 0.0
            out.append((seconds, tool_names(step)))
        if now is not None:
            previous = now
    return out


def total_seconds(trajectory: dict) -> float | None:
    """The span from the first to the last timestamped step; None without two timestamps."""
    stamps = [parse_time(s.get("timestamp")) for s in trajectory.get("steps") or [] if isinstance(s, dict)]
    stamps = [t for t in stamps if t is not None]
    return max(0.0, stamps[-1] - stamps[0]) if len(stamps) >= 2 else None


def timing_breakdown(source: dict | str | Path, render: Callable | None = None) -> str:
    """The `⏱️ TIMING BREAKDOWN` block for one trial, or "" (no file, no agent steps). `render` defaults to
    `queries.render_timing_breakdown`; a test may pass its own."""
    trajectory = load(source)
    if trajectory is None:
        return ""
    if render is None:
        from hermit.agent.evolver.methods.ahe_adapter import queries
        render = queries.render_timing_breakdown
    return render(turns(trajectory), total_seconds(trajectory))


def _result_text(result) -> str:
    if isinstance(result, dict):
        return str(result.get("content") if result.get("content") is not None else result)
    return str(result)


def step_markdown(step: dict, index: int) -> str:
    """One step as Markdown: a header naming the step and its source, the message, the tool calls as JSON and the
    observation's results verbatim."""
    source = str(step.get("source") or "unknown")
    lines = [f"# Step {step.get('step_id', index)} — {source} — {step.get('timestamp') or ''}".rstrip(" —"), ""]
    if step.get("message"):
        lines += [str(step["message"]), ""]
    for call in step.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        call_id = call.get("tool_call_id")
        lines += [f"## Tool call `{call.get('function_name') or call.get('name') or '?'}`"
                  + (f" ({call_id})" if call_id else ""),
                  "```json", json.dumps(call.get("arguments"), ensure_ascii=False, indent=1), "```", ""]
    observation = step.get("observation")
    if isinstance(observation, dict):
        for result in observation.get("results") or []:
            call_id = result.get("source_call_id") if isinstance(result, dict) else None
            lines += [f"## Observation {call_id or ''}".rstrip(), "```", _result_text(result), "```", ""]
    elif observation:
        lines += ["## Observation", "```", str(observation), "```", ""]
    return "\n".join(lines)


def explode(source: dict | str | Path, out_dir: Path) -> list[Path]:
    """Write `NNNN-<source>.md` per step under `out_dir` and return the paths (empty for an unreadable file)."""
    trajectory = load(source)
    if trajectory is None:
        return []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, step in enumerate(trajectory.get("steps") or [], 1):
        if not isinstance(step, dict):
            continue
        role = "".join(c if c.isalnum() else "_" for c in str(step.get("source") or "unknown"))
        path = out_dir / f"{int(step.get('step_id') or index):04d}-{role}.md"
        path.write_text(step_markdown(step, index), encoding="utf-8")
        paths.append(path)
    return paths


def step_text(step, index: int) -> str:
    """One standard step (hermit.agent.base.trajectory.Step) as Markdown: a header naming its index and kind, then
    its text; a command with its exit code and output, verbatim."""
    lines = [f"# Step {index} — {step.kind}", ""]
    if step.kind == "command":
        lines += ["```sh", step.command, "```", "", f"exit code: {step.exit_code}", "", "## Output", "```",
                  step.output, "```", ""]
    elif step.kind in ("tool_call", "file_change"):
        lines += ["```json", step.text, "```", ""] + (["## Output", "```", step.output, "```", ""] if step.output else [])
    else:
        lines += [step.text, ""]
    return "\n".join(lines)


def explode_steps(steps: list, out_dir: Path) -> list[Path]:
    """Write `NNNN-<kind>.md` per standard step under `out_dir` and return the paths (none for no steps)."""
    if not steps:
        return []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, step in enumerate(steps, 1):
        path = out_dir / f"{index:04d}-{step.kind}.md"
        path.write_text(step_text(step, index), encoding="utf-8")
        paths.append(path)
    return paths
