"""The Agent Debugger's and the Evolve Agent's fixed text, verbatim from AHE's `evolve.py` (arXiv:2604.25850,
china-qijizhifeng/agentic-harness-engineering @ 8b2a55d; see NOTICE).

Upstream assembles the per-task question of its `adb ask` Debugger from four pieces: a query template chosen by mode
(debug / summary) and k (one rollout or several; `evolve.py:1103-1157`), a `⚠️ TIMEOUT INFO` prefix with a per-turn
`⏱️ TIMING BREAKDOWN` for every timed-out trace (`1348-1378`, `1438-1452`), and the verifier's test output for the
non-passing rollouts (`1318-1345`). When the Debugger fails, a fallback answer built from the verifier's FAILED lines
stands in (`1503-1530`). The evolution query's attribution table maps each verdict to a suggested action
(`2826-2861`). Everything here is text and the small pure functions that render it; nothing runs a model. Byte
compatibility with upstream matters because the Evolve Agent's prompt (START.md) was written against this text.

The Debugger stage (debugger.py) renders a question with `debugger_query`; the overview writer (analysis.py) takes
`SUGGESTION`; the ATIF-side timing extractor (trajectory.py) hands its turns to `render_timing_breakdown`, which is
the exact format of upstream's `_extract_trace_timing` on NexAU traces (kept here as `extract_trace_timing`).
"""
from __future__ import annotations

import json
from pathlib import Path

# ---------- the Debugger's query templates (evolve.py:1103-1157) ----------

DEFAULT_DEBUG_QUERY = (
    "This task has {n_total} rollouts: {n_pass} passed, {n_fail} failed.\n"
    "{trace_labels}\n"
    "All traces are provided. Analyze why the failing attempts failed.\n\n"
    "IMPORTANT: If verifier test output is provided below, it shows the REAL external test results "
    "that determined pass/fail. The agent never sees this output. Cross-reference the verifier's "
    "actual failure messages with the agent's trace to find the TRUE root cause.\n\n"
    "Identify:\n"
    "1. ROOT CAUSE: What is the fundamental reason for the failures? "
    "Cross-reference with verifier output if available.\n"
    "2. PASS vs FAIL: If both passing and failing traces exist, what did successful attempts do differently?\n"
    "3. CRITICAL MISTAKE: At which point did the failing attempts go wrong?\n"
    "4. GENERAL MECHANISM: What structural mechanism (NOT task-specific knowledge) would prevent this class of failure?\n\n"
    "Focus on general patterns. Keep concise (under 300 words)."
)

DEFAULT_SUMMARY_QUERY = (
    "This task passed all {n_total} rollouts.\n"
    "{trace_labels}\n"
    "Analyze one representative trace.\n\n"
    "Identify:\n"
    "1. KEY STRATEGY: What was the agent's approach and why did it work?\n"
    "2. REUSABLE PATTERN: What general behavioral pattern could benefit other tasks?\n"
    "3. FRAGILITY RISK: Anything that seems fragile or lucky?\n\n"
    "Keep concise (under 150 words)."
)

DEFAULT_DEBUG_QUERY_K1 = (
    "This task has a single rollout which FAILED.\n"
    "{trace_labels}\n"
    "Analyze the trace carefully and locate where the problem is.\n\n"
    "IMPORTANT: If verifier test output is provided below, it shows the REAL external test results "
    "that determined pass/fail. The agent never sees this output. Cross-reference the verifier's "
    "actual failure messages with the agent's trace to find the TRUE root cause — the agent may "
    "have believed it succeeded when the external test shows a different failure.\n\n"
    "Identify:\n"
    "1. FAILURE POINT: At which exact step did things start going wrong? "
    "Cross-reference with verifier output if available.\n"
    "2. ROOT CAUSE: What is the fundamental reason for the failure? "
    "Distinguish between 'agent thought it succeeded but verifier disagrees' vs 'agent encountered errors'.\n"
    "3. WHAT SHOULD HAVE BEEN DONE: What would the correct approach look like at the failure point?\n"
    "4. GENERAL LESSON: What structural mechanism (NOT task-specific knowledge) would prevent this class of failure?\n\n"
    "Focus on pinpointing the problem. Keep concise (under 300 words)."
)

DEFAULT_SUMMARY_QUERY_K1 = (
    "This task has a single rollout which PASSED.\n"
    "{trace_labels}\n"
    "Analyze the trace and summarize the success experience.\n\n"
    "Identify:\n"
    "1. KEY STRATEGY: What was the agent's approach and why did it succeed?\n"
    "2. SUCCESS FACTORS: What specific decisions or behaviors were critical to the success?\n"
    "3. REUSABLE PATTERN: What general behavioral pattern from this success could benefit other tasks?\n\n"
    "Focus on extracting reusable lessons. Keep concise (under 150 words)."
)

# ---------- the timeout prefix (evolve.py:1438-1447) and timing breakdown (1348-1378) ----------

TIMEOUT_INFO = (
    "\n⚠️ TIMEOUT INFO: {n_timeout} of {n_total} rollout(s) TIMED OUT "
    "(agent exceeded the time limit and was terminated). "
    "The timed-out trace(s) show what the agent did before being killed. "
    "Pay special attention to why the agent ran out of time: "
    "was it stuck in a loop, retrying endlessly, spending too long on a wrong approach, "
    "or was the task inherently too complex for the time limit?\n"
)

TIMING_HEADER = "⏱️ TIMING BREAKDOWN (total {total_s:.0f}s, {n_turns} turns):\n"
TIMING_TURN = "  Turn {i}: {latency_s:.1f}s ({tools})"


def render_timing_breakdown(turns: list[tuple[float, list[str]]], total_s: float | None = None) -> str:
    """The `⏱️ TIMING BREAKDOWN` block of `_extract_trace_timing` (evolve.py:1348-1378) from already extracted
    turns: one (seconds, tool names) pair per assistant turn, in order. `total_s` defaults to their sum, as upstream
    does when the trace carries no total latency. Empty turns render nothing, as upstream."""
    if not turns:
        return ""
    lines = [TIMING_TURN.format(i=i, latency_s=float(sec), tools=", ".join(tools) if tools else "no tools")
             for i, (sec, tools) in enumerate(turns, 1)]
    total = float(total_s) if total_s else sum(float(sec) for sec, _ in turns)
    return TIMING_HEADER.format(total_s=total, n_turns=len(turns)) + "\n".join(lines)


def extract_trace_timing(trace_path: Path) -> str:
    """`_extract_trace_timing` (evolve.py:1348-1378) verbatim: the per-turn timing of a NexAU trace JSON
    (`messages[].role == "assistant"` with `latency` in ms and `tool_calls[].name`). Hermit's ATIF trajectories are
    read by trajectory.py, which renders through `render_timing_breakdown`; this reader stands for traces in
    upstream's own format."""
    try:
        data = json.loads(Path(trace_path).read_text(errors="replace"))
    except (json.JSONDecodeError, OSError):
        return ""
    messages = data.get("messages", [])
    total_latency = data.get("latency", 0)
    turns: list[tuple[float, list[str]]] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        tool_names = [tc.get("name", "?").replace("Tool: ", "") for tc in msg.get("tool_calls", [])]
        turns.append((msg.get("latency", 0) / 1000.0, tool_names))
    if not turns:
        return ""
    total_s = total_latency / 1000.0 if total_latency else None
    return render_timing_breakdown(turns, total_s)


# ---------- trace labels and the verifier wrapper (evolve.py:1403-1412, 1318-1345) ----------

VERIFIER_HEADER = (
    "\n\n⚠️ VERIFIER TEST OUTPUT (this is what the EXTERNAL evaluator actually ran "
    "after the agent completed — the agent NEVER sees this):\n"
)
VERIFIER_TAIL_LINES = 60         # lines of verifier output shown in the query (the detail file shows 80)


def trace_label(reward: float | None) -> str:
    """PASS / FAIL / TIMEOUT from upstream's reward convention: >= 1.0 pass, < 0 timeout, else fail."""
    if reward is not None and reward < 0:
        return "TIMEOUT"
    return "PASS" if reward is not None and reward >= 1.0 else "FAIL"


def trace_labels(trace_rewards: list[float]) -> str:
    """`Trace verdict: trace01=PASS, trace02=TIMEOUT` (evolve.py:1403-1409); empty without traces."""
    parts = [f"trace{i:02d}={trace_label(rv)}" for i, rv in enumerate(trace_rewards, 1)]
    return "Trace verdict: " + ", ".join(parts) if parts else ""


def build_verifier_context(trace_rewards: list[float], verifier_outputs: list[str],
                           tail_lines: int = VERIFIER_TAIL_LINES) -> str:
    """`_build_verifier_context` (evolve.py:1318-1345): the last `tail_lines` lines of each non-passing rollout's
    verifier output under the `⚠️ VERIFIER TEST OUTPUT` header; empty when every rollout passed or no output exists."""
    parts: list[str] = []
    for i, (rv, vout) in enumerate(zip(trace_rewards, verifier_outputs), 1):
        if not vout:
            continue
        label = trace_label(rv)
        if rv is not None and rv >= 1.0:
            continue
        lines = vout.strip().splitlines()
        vout_truncated = "\n".join(lines[-tail_lines:]) if len(lines) > tail_lines else vout.strip()
        parts.append(
            f"--- Verifier test output (rollout {i}, {label}) ---\n"
            f"{vout_truncated}\n"
            f"--- end verifier output ---"
        )
    if not parts:
        return ""
    return VERIFIER_HEADER + "\n\n".join(parts)


def debugger_query(mode: str, k: int, n_pass: int, n_fail: int, n_timeout: int, trace_rewards: list[float],
                   verifier_outputs: list[str], timings: list[str] | None = None, *,
                   templates: dict[str, str] | None = None, verifier_tail_lines: int = VERIFIER_TAIL_LINES) -> str:
    """The Debugger's question for one task, assembled as `_run_single_adb_ask` does (evolve.py:1398-1457): the
    template by `mode` ("debug" | "summary") and k, the `⚠️ TIMEOUT INFO` prefix when a rollout timed out followed by
    one `⏱️ TIMING BREAKDOWN` per timed-out trace (`timings`, already rendered, in trace order; empty strings are
    skipped), then the verifier context. `templates` overrides a template by its config key (`qa_query_debug`,
    `qa_query_debug_k1`, `qa_query_summary`, `qa_query_summary_k1`), as upstream's config does."""
    n_total = n_pass + n_fail + n_timeout
    fmt_vars = {"n_total": n_total, "n_pass": n_pass, "n_fail": n_fail, "n_timeout": n_timeout,
                "trace_labels": trace_labels(trace_rewards)}
    templates = templates or {}
    if mode == "debug":
        template = templates.get("qa_query_debug_k1", DEFAULT_DEBUG_QUERY_K1) if k == 1 \
            else templates.get("qa_query_debug", DEFAULT_DEBUG_QUERY)
    else:
        template = templates.get("qa_query_summary_k1", DEFAULT_SUMMARY_QUERY_K1) if k == 1 \
            else templates.get("qa_query_summary", DEFAULT_SUMMARY_QUERY)
    try:
        query = template.format_map(fmt_vars)
    except (KeyError, ValueError):
        query = template
    if n_timeout > 0:
        query = TIMEOUT_INFO.format(n_timeout=n_timeout, n_total=n_total) + query
        for timing in timings or []:
            if timing:
                query += f"\n\n{timing}"
    verifier_ctx = build_verifier_context(trace_rewards, verifier_outputs, verifier_tail_lines)
    if verifier_ctx:
        query += verifier_ctx
    return query


# ---------- the fallback answer when the Debugger fails (evolve.py:1503-1530) ----------

FALLBACK_PREFIX = "[adb"            # upstream's marker: an answer starting with it is a failure summary, not analysis
FALLBACK_NOTE = (
    "\n**NOTE**: Debugger LLM analysis was not available for this task. "
    "The evolve agent should read the raw trace directly if deeper analysis is needed."
)


def fallback_response(response: str, verifier_outputs: list[str], timings: list[str] | None = None) -> str:
    """The stand-in answer for a task whose Debugger call failed (evolve.py:1503-1530): the `[adb ...]` error line,
    the verifier's FAILED lines, the timing breakdowns of the timed-out traces (`timings`, rendered) and the NOTE.
    `response` must start with `[adb` (`[adb timeout] ...`, `[adb error] ...`, `[adb failed] ...`,
    `[adb exception] ...`); the reason word is "timed out" for a timeout and "failed" otherwise."""
    reason = "timed out" if response.startswith("[adb timeout]") else "failed"
    fallback_parts: list[str] = [response, ""]
    failed_lines: list[str] = []
    for vout in verifier_outputs:
        if not vout:
            continue
        for line in vout.strip().splitlines():
            stripped = line.strip()
            if stripped.startswith("FAILED ") or stripped.startswith("FAILED\t"):
                failed_lines.append(stripped)
    if failed_lines:
        fallback_parts.append(f"The debugger analysis {reason}. Verifier test failures:")
        for fl in failed_lines:
            fallback_parts.append(f"  - {fl}")
        fallback_parts.append("(Full verifier output is in the Verifier Test Output section below.)")
    for timing in timings or []:
        if timing:
            fallback_parts.append(f"\n{timing}")
    fallback_parts.append(FALLBACK_NOTE)
    return "\n".join(fallback_parts)


# ---------- the attribution table's suggested actions (evolve.py:2826-2861) ----------

SUGGESTION = {
    "EFFECTIVE": "✅ Keep",
    "PARTIALLY_EFFECTIVE": "⚠️ Keep, continue monitoring",
    "MIXED": "⚠️ Keep effective parts, rollback harmful parts",
    "INEFFECTIVE": "🔸 Rollback or redesign",
    "HARMFUL": "❌ Must rollback",
}
