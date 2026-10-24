"""ContinualHarnessEvolver: Continual Harness's Refiner (Karten et al. 2026; github.com/sethkarten/continual-harness)
as a hermit Evolver.

Continual Harness refines the *whole harness* — the base policy `p`, sub-agents `G`, skills `K`, memory `M` — from
the agent's own trajectory, driven by failure signatures (tool-call errors, loops, no-progress), with **no reward or
labels and no accept/reject gate**: the Refiner just applies the edits, as its reset-free loop does. That is what
sets it apart from the score-driven evolvers here (gepa/skillopt need a judge score and keep a change only if it
improves a metric).

This port keeps that character on hermit's step model. Each training step the Trainer has already rolled the current
adapter out on the step's minibatch; this evolver:

1. reads those trajectories (`learning_material/current/tasks/*.json`) and gathers failure signatures — tool-call
   errors, non-ok runs, and the judge's feedback (used as a signal, not as a score to optimise);
2. if there is nothing to learn from (no signatures), leaves the adapter unchanged;
3. otherwise has the run's `llm` tool CRUD-rewrite the harness document (the base policy plus `## Sub-agents`,
   `## Skills`, `## Memory` sections) to fix the observed failures, codify what worked, and drop what is stale;
4. applies it in place (a `check_spec` guard only, so a malformed rewrite can't break later rollouts — not a score).

A prompt-only adapter has one learnable file (`ADAPTER.md`); G/K/M live as sections of it, which is where a
text-prompt agent reads them anyway. Training State (`state/continual_harness.json`): one record per step.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from hermit.evolver.base import Evolver, FinalResult, StepContext, StepResult

FAILURE_MARKERS = ("error", "exception", "traceback", "not allowed", "no matches", "failed", "timeout",
                   '"success": false', '"success":false', "denied", "refused")


def clip_middle(text: str, limit: int) -> str:
    """Keep the beginning (the approach) and the end (how it finished)."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit * 2 // 5
    return text[:head] + f"\n[... {len(text) - limit} characters omitted ...]\n" + text[-(limit - head):]


def _content(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


def trajectory_text(record: dict) -> str:
    """The run as text: the messages the pipeline returned (system prompt and first task message left out — the
    refinement prompt already shows the policy and the task)."""
    lines, task_seen = [], False
    for m in record.get("messages") or []:
        role = m.get("role")
        if role == "system":
            continue
        if role == "user" and not task_seen:
            task_seen = True
            continue
        text = _content(m)
        if text:
            lines.append(f"[{role}] {text}")
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            lines.append(f"[tool call] {fn.get('name')}({fn.get('arguments')})")
    return "\n".join(lines)


def failure_signatures(record: dict) -> list[str]:
    """Self-observable things that went wrong in a run — no ground truth needed: a non-ok status, and any message
    (typically a tool result) that reads like an error."""
    out = []
    if record.get("status") and record["status"] != "ok":
        out.append(f"run status: {record['status']}" + (f" ({record['error']})" if record.get("error") else ""))
    for m in record.get("messages") or []:
        if m.get("role") == "system":
            continue
        text = _content(m)
        low = text.lower()
        if text and any(mark in low for mark in FAILURE_MARKERS):
            out.append(f"[{m.get('role')}] {text.strip()[:300]}")
    return out


class ContinualHarnessEvolver(Evolver):
    def step(self, ctx: StepContext) -> StepResult:
        p = ctx.params
        if "llm" not in ctx.tools:
            return StepResult("failed", "continual_harness needs the run's llm tool: set `llm` in the run config")
        file = p.get("file", "ADAPTER.md")
        components = list(p.get("components") or ["prompt", "subagents", "skills", "memory"])
        n = ctx.progress.step if ctx.progress else 0
        adapter = ctx.workspace.adapter
        doc = (adapter / file).read_text() if (adapter / file).exists() else ""

        current = ctx.workspace.learning_material / "current"
        records = [json.loads(f.read_text()) for f in sorted((current / "tasks").glob("*.json"))]
        state_file = ctx.workspace.state / "continual_harness.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else {"steps": []}
        record = {"step": n, "tasks": len(records)}

        # Gather failure signatures + feedback across the step's rollouts (the signal, no labels).
        signals, feedback = [], []
        for r in records:
            for s in failure_signatures(r):
                signals.append(f"- task {r.get('id')}: {s}")
            fb = (r.get("judge") or {}).get("feedback")
            if fb:
                feedback.append(f"- task {r.get('id')}: {fb}")

        min_signals = int(p.get("min_signals", 1))
        if len(signals) + len(feedback) < min_signals:
            record["outcome"] = "unchanged: no failure signatures this step"
            state["steps"].append(record)
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1))
            return StepResult("done", record["outcome"])

        trace_chars = int(p.get("trace_chars", 12000))
        runs = "\n\n".join(
            f"### Task {r.get('id')} (status {r.get('status')})\n"
            f"Final output: {str(r.get('output') or '(none)')[:500]}\n"
            f"Trajectory:\n{clip_middle(trajectory_text(r), trace_chars)}"
            for r in records)
        section_lines = {
            "prompt": "the base policy / instructions `p`",
            "subagents": "a `## Sub-agents` section (named playbooks for repeated multi-step patterns)",
            "skills": "a `## Skills` section (reusable step sequences / procedures that worked)",
            "memory": "a `## Memory` section (facts and notes the agent discovered; drop ones it has moved past)",
        }
        maintain = "\n".join(f"- {section_lines[c]}" for c in components if c in section_lines)
        prompt = f"""You are the Refiner of a self-improving agent harness (Continual Harness).

Below is the agent's CURRENT harness document, then the most recent rollouts with the failures observed in them.
Rewrite the harness document so the agent does better next time. You are improving the WHOLE harness via CRUD — keep
what works, fix what failed, add what's missing, delete what's stale. Maintain these parts of the document:
{maintain}

Base your edits on the observed FAILURE SIGNATURES and feedback — not on any score. Concretely: rewrite the policy
to avoid the failures seen; codify any sequence that worked into Skills; record discovered facts/constraints in
Memory; capture repeated multi-step patterns as Sub-agents. Be specific to what the trajectories show.

=== CURRENT HARNESS DOCUMENT ===
{doc or "(empty)"}

=== RECENT ROLLOUTS ===
{runs}

=== FAILURE SIGNATURES ===
{chr(10).join(signals) or "(none explicit; use the feedback below)"}

=== JUDGE FEEDBACK (signal, not a target to game) ===
{chr(10).join(feedback) or "(none)"}

Reply with the COMPLETE new harness document inside a single ``` code block, and nothing else."""

        sampling = {k: p[k] for k in ("max_tokens", "temperature") if p.get(k) is not None}
        reply_text, new_doc, error = "", None, None
        messages = [{"role": "user", "content": prompt}]
        for _ in range(2):      # a reasoning model may spend a reply thinking; ask once more for the block
            try:
                reply = ctx.tools["llm"](messages=messages, **sampling)
            except Exception as exc:                        # noqa: BLE001 — skip the step, as CH tolerates a failed pass
                error = f"{type(exc).__name__}: {exc}"
                break
            reply_text = reply.get("content") or ""
            new_doc = _extract_block(reply_text)
            if new_doc:
                break
            messages += [{"role": "assistant", "content": reply_text},
                         {"role": "user", "content": "Reply now with only the complete new document in one ``` block."}]
        (ctx.workspace.output / "refiner_prompt.md").write_text(prompt)
        (ctx.workspace.output / "refiner_reply.txt").write_text(reply_text)

        if error:
            record["outcome"] = f"unchanged: the refiner call failed ({error})"
        elif not new_doc or new_doc.strip() == doc.strip():
            record["outcome"] = "unchanged: the refiner proposed no new document"
        else:
            (adapter / file).write_text(new_doc.strip() + "\n")
            check = ctx.tools["check_spec"](path=str(adapter)) if "check_spec" in ctx.tools else {"passed": True}
            if not check.get("passed", True):
                (adapter / file).write_text(doc)            # keep the working document; a broken rewrite is dropped
                record["outcome"] = "unchanged: the rewrite failed check_spec: " + \
                    "; ".join(i["problem"] for i in check.get("issues", [])[:3])
            else:
                record["outcome"] = (f"refined {file} from {len(signals)} failure signature(s) and "
                                     f"{len(feedback)} feedback note(s): {len(doc)} -> {len(new_doc)} chars")

        state["steps"].append(record)
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        (ctx.workspace.output / "report.md").write_text(record["outcome"] + "\n")
        return StepResult("done", record["outcome"][:300], extra={"signals": len(signals), "feedback": len(feedback)})

    def finalize(self, ctx) -> FinalResult:
        state_file = ctx.workspace.state / "continual_harness.json"
        steps = json.loads(state_file.read_text())["steps"] if state_file.exists() else []
        changed = sum(1 for s in steps if s.get("outcome", "").startswith("refined"))
        return FinalResult(adapter=ctx.workspace.adapter,
                           summary=f"continual-harness refinement: {changed} of {len(steps)} steps changed the harness")


def _extract_block(text: str) -> str | None:
    """The content of the last ``` block, else None (or the whole reply if it has no fence but has content)."""
    fences = [m.start() for m in re.finditer(r"```", text)]
    if len(fences) >= 2:
        inner = text[fences[-2] + 3:fences[-1]]
        return inner.split("\n", 1)[1] if "\n" in inner else inner      # drop an optional ```lang line
    return None
