"""One rollout as the text trace RRSI's analyst, digesters and proposer read (upstream: domains/*/render.py).

A rollout is the learning material's record (`tasks/<row>.json`: input, output, judge, status, error, tokens, wall
time) and its trajectory directory (`trajectories/<row>/`, the host's log copied unchanged). Its steps are read with
the framework's one reader of those logs (hermit.agent.base.trajectory): Codex's `codex exec --json` events
(`agent-codex.jsonl`) or Claude Code's stream; a log that holds Codex's events under another name (Harbor's Codex:
`agent/codex.txt`) is found by name below the directory. The trace is

    === TASK <task> (trial <k>, row <row>) ===            the task as the agent got it
    [step N] AGENT / REASONING / COMMAND (exit) / OUTPUT / FILES / TOOL / ERROR    one block per step, in order
    === RUN METADATA ===                                  status, error, tokens, wall time, steps
    === GRADING ===                                       the judge's score and feedback, last, as upstream's
                                                           VERIFIER / RUBRIC block is

With `detail=False` long outputs are cut to their head and tail, as upstream's renderers cap per-message text.
"""
from __future__ import annotations

import json
from pathlib import Path

from hermit.agent.base.trajectory import STREAMS, Step, claude_steps, codex_steps, events, log_steps

TASK_CAP = 6000
OUTPUT_CAP = 2500
OUTPUT_CAP_DETAIL = 12000
MESSAGE_CAP = 4000
OTHER_LOGS = ("codex.txt", "agent-codex.txt")      # Codex's events under another name (Harbor's agent/codex.txt)


def _cut(text: str, cap: int) -> str:
    text = str(text or "")
    if len(text) <= cap:
        return text
    half = cap // 2
    return f"{text[:half]}\n[... {len(text) - cap} chars cut ...]\n{text[-half:]}"


def _steps(trajectory: Path | None) -> list[Step]:
    """The steps of a rollout's trajectory (a directory or one log): its host stream's, else the first log found by
    name below it."""
    if trajectory is None or not trajectory.exists():
        return []
    found = log_steps(trajectory)
    if found is not None:
        return found
    if trajectory.is_file():
        return codex_steps(events(trajectory))
    for name in (*STREAMS, *OTHER_LOGS):
        hits = sorted(trajectory.rglob(name))
        if hits:
            stream = events(hits[0])
            return claude_steps(stream) if name == STREAMS[1] else codex_steps(stream)
    return []


def _task_text(record: dict, trajectory: Path | None) -> str:
    for name in ("task.md", "prompt.txt", "instruction.md"):
        hits = sorted(trajectory.rglob(name)) if trajectory is not None and trajectory.is_dir() else []
        if hits:
            return hits[0].read_text(errors="replace")
    value = record.get("input")
    if isinstance(value, dict):
        for key in ("question", "instruction", "prompt", "task"):
            if value.get(key):
                return str(value[key])
    return json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value


def _files(text: str) -> str:
    try:
        changes = json.loads(text)
    except ValueError:
        return _cut(text, 600)
    if isinstance(changes, list):
        return ", ".join(f"{c.get('kind')} {c.get('path')}" for c in changes if isinstance(c, dict))
    return _cut(text, 600)


def steps(found: list[Step], detail: bool) -> list[str]:
    cap = OUTPUT_CAP_DETAIL if detail else OUTPUT_CAP
    out = []
    for n, step in enumerate(found, 1):
        if step.kind == "message":
            out.append(f"[step {n}] AGENT: {_cut(step.text, MESSAGE_CAP)}")
        elif step.kind == "reasoning":
            if step.text:
                out.append(f"[step {n}] REASONING: {_cut(step.text, 1500)}")
        elif step.kind == "command":
            out.append(f"[step {n}] COMMAND (exit {step.exit_code}): {_cut(step.command, 1500)}\n"
                       f"[step {n}] OUTPUT:\n{_cut(step.output, cap)}")
        elif step.kind == "file_change":
            out.append(f"[step {n}] FILES: {_files(step.text)}")
        elif step.kind == "tool_call":
            out.append(f"[step {n}] TOOL: {_cut(step.text, 1500)}"
                       + (f"\n[step {n}] RESULT: {_cut(step.output, cap)}" if step.output else ""))
        elif step.kind == "error":
            out.append(f"[step {n}] ERROR: {_cut(step.text, 600)}")
        else:
            out.append(f"[step {n}] {step.kind.upper()}: {_cut(step.text, 800)}")
    return out


def render(rec: dict, detail: bool = False) -> str:
    """The trace of one rollout. `rec`: {task_id, trial, row, record (the material record), trajectory (its
    directory or None), grading (extra lines for the GRADING block, e.g. the verifier's output)}."""
    record = rec.get("record") or {}
    trajectory = Path(rec["trajectory"]) if rec.get("trajectory") else None
    found = _steps(trajectory)
    judge = record.get("judge") or {}
    lines = [f"=== TASK {rec.get('task_id')} (trial {rec.get('trial')}, row {rec.get('row')}) ===",
             _cut(_task_text(record, trajectory), TASK_CAP), "", "=== TRAJECTORY ==="]
    body = steps(found, detail)
    lines += body or ["(no agent events were recorded)"]
    output = record.get("output")
    lines += ["", "=== RUN METADATA ===",
              f"status: {record.get('status')}" + (f" | error: {_cut(record.get('error'), 600)}"
                                                   if record.get("error") not in (None, "", "None") else ""),
              f"tokens: {record.get('tokens')} | wall_s: {record.get('wall_s')} | steps: {len(found)}",
              f"final output: {_cut(json.dumps(output, ensure_ascii=False) if not isinstance(output, str) else output, 3000)}",
              "", "=== GRADING ===",
              f"score: {judge.get('score')}",
              f"feedback: {_cut(judge.get('feedback'), 6000 if detail else 2500)}"]
    lines += [str(x) for x in rec.get("grading") or []]
    return "\n".join(lines) + "\n"
