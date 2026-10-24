"""ContinualHarnessEvolver: Continual Harness's Refiner (Karten et al. 2026; github.com/sethkarten/continual-harness)
as a hermit Evolver.

Continual Harness refines the *whole harness* — the base policy `p`, sub-agents `G`, skills `K`, memory `M` — from
the agent's own trajectory, driven by failure signatures (tool-call errors, loops, no-progress), with **no reward or
labels and no accept/reject gate**: the Refiner just applies the edits, as its reset-free loop does. That label-free,
failure-signature-driven, whole-harness refinement is what sets it apart from the score-driven evolvers here
(gepa/skillopt need a judge score and keep a change only if it improves a metric).

On hermit's step model, each training step the Trainer has already rolled the current adapter out on the step's
minibatch; this evolver reads those trajectories (`learning_material/current/tasks/*.json`), gathers failure
signatures (and the judge's feedback, used as a signal — not a score to optimise), and refines the harness:

- **continual-harness template** (policy.md / subagents.md / skills.md / memory.md): one CRUD pass per component via
  the run's `llm` tool, then re-render ADAPTER.md (the agent's context) from the four parts. This is CH's four passes.
- **prompt-only template** (ADAPTER.md only): one holistic rewrite of ADAPTER.md covering p + G/K/M as sections.

Either way the edits are applied in place, with only a `check_spec` guard (so a malformed rewrite can't break later
rollouts — not a score). Training State (`state/continual_harness.json`): one record per step.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from hermit.evolver.base import Evolver, FinalResult, StepContext, StepResult

FAILURE_MARKERS = ("error", "exception", "traceback", "not allowed", "no matches", "failed", "timeout",
                   '"success": false', '"success":false', "denied", "refused")

# component -> (source file, section heading in the rendered context, the refinement intent for that pass)
COMPONENTS = {
    "prompt":    ("policy.md", None,            "Rewrite the base policy to avoid the failures observed. Keep what "
                                                "already works; be concrete about what to do differently."),
    "subagents": ("subagents.md", "Sub-agents", "CRUD the sub-agent playbooks: create a named playbook for any "
                                                "repeated multi-step pattern, edit ones that led to failures, delete "
                                                "unused ones. Write each as a `## <short-name>` section whose first "
                                                "line says when to delegate to it, followed by the steps (the name "
                                                "and that first line become the sub-agent the acting agent can call)."),
    "skills":    ("skills.md", "Skills",        "CRUD the skills: codify any sequence or procedure that worked into a "
                                                "reusable skill, repair ones that failed, drop stale ones. Write each "
                                                "as a `## <short-name>` section with the steps (so the agent can pull "
                                                "one skill by name)."),
    "memory":    ("memory.md", "Memory",        "CRUD the memory: add facts, constraints or quirks the agent "
                                                "discovered; refresh stale notes; remove ones it has moved past. Put "
                                                "each note under a `## <short-topic>` heading so it can be looked up."),
}
EMPTY = ("(no ", "(none")


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
    """The run as text: the messages the pipeline returned (system prompt and first task message left out)."""
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
    """Self-observable things that went wrong — no ground truth needed: a non-ok status, and any message (typically a
    tool result) that reads like an error."""
    out = []
    if record.get("status") and record["status"] != "ok":
        out.append(f"run status: {record['status']}" + (f" ({record['error']})" if record.get("error") else ""))
    for m in record.get("messages") or []:
        if m.get("role") == "system":
            continue
        text = _content(m)
        if text and any(mark in text.lower() for mark in FAILURE_MARKERS):
            out.append(f"[{m.get('role')}] {text.strip()[:300]}")
    return out


def _extract_block(text: str) -> str | None:
    """The content of the last ``` block, else None."""
    fences = [m.start() for m in re.finditer(r"```", text)]
    if len(fences) >= 2:
        inner = text[fences[-2] + 3:fences[-1]]
        return inner.split("\n", 1)[1] if "\n" in inner else inner      # drop an optional ```lang line
    return None


class ContinualHarnessEvolver(Evolver):
    def step(self, ctx: StepContext) -> StepResult:
        p = ctx.params
        if "llm" not in ctx.tools:
            return StepResult("failed", "continual_harness needs the run's llm tool: set `llm` in the run config")
        components = list(p.get("components") or ["prompt", "subagents", "skills", "memory"])
        n = ctx.progress.step if ctx.progress else 0
        adapter = ctx.workspace.adapter
        structured = (adapter / "policy.md").exists()        # the continual-harness template vs prompt-only

        current = ctx.workspace.learning_material / "current"
        records = [json.loads(f.read_text()) for f in sorted((current / "tasks").glob("*.json"))]
        state_file = ctx.workspace.state / "continual_harness.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else {"steps": []}
        record = {"step": n, "tasks": len(records), "mode": "structured" if structured else "flat"}

        signals, feedback = [], []
        for r in records:
            signals += [f"- task {r.get('id')}: {s}" for s in failure_signatures(r)]
            fb = (r.get("judge") or {}).get("feedback")
            if fb:
                feedback.append(f"- task {r.get('id')}: {fb}")
        if len(signals) + len(feedback) < int(p.get("min_signals", 1)):
            record["outcome"] = "unchanged: no failure signatures this step"
            state["steps"].append(record)
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1))
            return StepResult("done", record["outcome"])

        evidence = self._evidence(records, signals, feedback, int(p.get("trace_chars", 12000)))
        sampling = {k: p[k] for k in ("max_tokens", "temperature") if p.get(k) is not None}
        before = {f: (adapter / f).read_text() if (adapter / f).exists() else None
                  for f in ("ADAPTER.md", "policy.md", "subagents.md", "skills.md", "memory.md")}

        if structured:
            changed = []
            for comp in components:
                if comp not in COMPONENTS:
                    continue
                fname, _, intent = COMPONENTS[comp]
                if not (adapter / fname).exists():
                    continue
                cur = (adapter / fname).read_text()
                new = self._refine(ctx, sampling, evidence, cur, f"the {comp} component (`{fname}`)", intent)
                if new and new.strip() != cur.strip():
                    (adapter / fname).write_text(new.strip() + "\n")
                    changed.append(comp)
            (adapter / "ADAPTER.md").write_text(self._render(adapter))
            record["changed"] = changed
            record["outcome"] = (f"refined {', '.join(changed)}" if changed else "unchanged: no component changed") + \
                f" from {len(signals)} failure signature(s), {len(feedback)} feedback note(s)"
        else:
            cur = before["ADAPTER.md"] or ""
            intent = ("Rewrite the whole harness document — keep what works, fix what failed, codify successful "
                      "sequences under a `## Skills` section, record discovered facts under `## Memory`, capture "
                      "repeated multi-step patterns under `## Sub-agents`, and improve the base policy.")
            new = self._refine(ctx, sampling, evidence, cur, "the harness document (`ADAPTER.md`)", intent)
            if new and new.strip() != cur.strip():
                (adapter / "ADAPTER.md").write_text(new.strip() + "\n")
                record["outcome"] = f"refined ADAPTER.md: {len(cur)} -> {len(new)} chars"
            else:
                record["outcome"] = "unchanged: the refiner proposed no new document"

        check = ctx.tools["check_spec"](path=str(adapter)) if "check_spec" in ctx.tools else {"passed": True}
        if not check.get("passed", True):
            for f, text in before.items():                   # a broken rewrite is dropped; keep the working harness
                (adapter / f).write_text(text) if text is not None else None
            record["outcome"] = "unchanged: the rewrite failed check_spec: " + \
                "; ".join(i["problem"] for i in check.get("issues", [])[:3])

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

    # ---------- helpers ----------

    @staticmethod
    def _evidence(records: list, signals: list, feedback: list, trace_chars: int) -> str:
        runs = "\n\n".join(
            f"### Task {r.get('id')} (status {r.get('status')})\n"
            f"Final output: {str(r.get('output') or '(none)')[:500]}\n"
            f"Trajectory:\n{clip_middle(trajectory_text(r), trace_chars)}" for r in records)
        return (f"=== RECENT ROLLOUTS ===\n{runs}\n\n"
                f"=== FAILURE SIGNATURES ===\n{chr(10).join(signals) or '(none explicit; use the feedback below)'}\n\n"
                f"=== JUDGE FEEDBACK (a signal, not a target to game) ===\n{chr(10).join(feedback) or '(none)'}")

    def _refine(self, ctx, sampling: dict, evidence: str, current: str, what: str, intent: str) -> str | None:
        prompt = (f"You are the Refiner of a self-improving agent harness (Continual Harness). Improve {what} from the "
                  f"agent's recent rollouts and the failures seen in them — base your edits on the failure signatures "
                  f"and feedback, not on any score.\n\n{intent}\n\n=== CURRENT {what} ===\n{current or '(empty)'}\n\n"
                  f"{evidence}\n\nReply with the COMPLETE new content of {what} inside a single ``` code block, and "
                  f"nothing else.")
        messages = [{"role": "user", "content": prompt}]
        for _ in range(2):      # a reasoning model may spend a reply thinking; ask once more for the block
            try:
                reply = ctx.tools["llm"](messages=messages, **sampling)
            except Exception:                                # noqa: BLE001 — skip this pass, as CH tolerates a failed one
                return None
            text = reply.get("content") or ""
            block = _extract_block(text)
            if block is not None:
                return block
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "Reply now with only the complete new content in one ``` block."}]
        return None

    @staticmethod
    def _render(adapter: Path) -> str:
        """The agent's context: the base policy followed by the non-empty sub-agents / skills / memory sections."""
        policy = (adapter / "policy.md").read_text().strip() if (adapter / "policy.md").exists() else ""
        parts = [policy]
        for comp in ("subagents", "skills", "memory"):
            fname, heading, _ = COMPONENTS[comp]
            if not (adapter / fname).exists():
                continue
            body = (adapter / fname).read_text().strip()
            if body and not body.lower().startswith(EMPTY):
                parts.append(f"## {heading}\n\n{body}")
        return "\n\n".join(parts) + "\n"
