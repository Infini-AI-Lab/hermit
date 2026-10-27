"""ContinualHarnessEvolver: the Refiner of Continual Harness (Karten et al. 2026, arXiv:2605.09998;
github.com/sethkarten/continual-harness, `agents/utils/harness_evolver.py`) as an Evolver on the `continual-harness`
template.

Continual Harness keeps a harness H = (p, G, K, M): the base policy, sub-agents, skills and memory. A Refiner reads a
window of the agent's own trajectory, identifies failure signatures in it (loops, failed tool calls, stalled
objectives) and emits per-component edits, applied in place with no accept/reject gate: H <- H + delta. Upstream runs
four passes per generation, one LLM call each: the policy is rewritten whole; sub-agents, skills and memory are
edited as create / update / retire deltas on named entries; the memory pass is conservative because the agent itself
writes memory while acting.

On the framework's step model the window is the step's rollouts (`learning_material/current`): each trajectory as the
acting agent left it (a Codex `exec --json` event stream, or a normalised message list), its final output and status.
The failure signatures are structural: a session that erred or timed out, a command that exited non-zero, an answer
that never came. With `feedback` on (the default) the judge's verdict on each task is shown too, as a signal to act
on, not a score to optimise; off, the Refiner sees the trajectories alone, as the paper's setting. Notes the acting
agent wrote to `memory/NOTES.md` during a task come back with its trajectory and are folded into the adapter's memory
before the memory pass curates it.

Each pass is applied to the adapter and checked with `check_spec`; a pass whose result fails the check is dropped and
the others stand (upstream, too, lets a failed pass fail alone). Training State: `state/continual_harness.json`, one
record per step; each step's `output/report.md` says what changed.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil

from hermit.evolver.base import Evolver, FinalResult, StepContext, StepResult

POLICY, SUBAGENTS, SKILLS, MEMORY = "policy", "subagents", "skills", "memory"
PASSES = (POLICY, SUBAGENTS, SKILLS, MEMORY)
ALWAYS_ON = "AGENTS.md"
ALWAYS_ON_MAX = 8_000                    # characters: the template's cap on the always-on text
SKILL_MAX = 24_000                       # bytes, as adapter.yaml
AGENT_MAX = 24_000
NOTES_MAX = 40_000
MAX_SKILLS = 40                          # count, as adapter.yaml (.agents/skills/*/SKILL.md max_files)
MAX_SUBAGENTS = 20                       # count, as adapter.yaml (sub_agents/*/AGENT.md max_files)
NAME_RX = re.compile(r"[a-z0-9][a-z0-9_\-]*")
PLACEHOLDER = "(No notes yet"
OUTPUT_TAIL = 600                        # characters of a command's output shown
STEP_TEXT = 1_200                        # characters of an assistant message shown
REASONING_TEXT = 400


# ---------- names, text ----------

def slug(name: str) -> str:
    """A directory name on the surface: lowercase, [a-z0-9_-], never empty."""
    s = re.sub(r"[^a-z0-9_\-]+", "_", str(name or "").strip().lower()).strip("_-")
    return s if s and NAME_RX.fullmatch(s) else f"entry_{abs(hash(name)) % 10000}"


def clip_middle(text: str, limit: int) -> str:
    """Keep the beginning (the approach) and the end (how it finished)."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit * 2 // 5
    return text[:head] + f"\n[... {len(text) - limit} characters omitted ...]\n" + text[-(limit - head):]


def frontmatter(text: str) -> tuple[dict[str, str], str]:
    """A file's YAML-ish frontmatter as strings, and its body ({} and the whole text without one)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    head: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and re.fullmatch(r"[A-Za-z_][\w-]*", key.strip()):
            head[key.strip()] = _unquote(value.strip())
    body = text[end + 4:]
    return head, body[body.find("\n") + 1:] if "\n" in body else ""


def _unquote(value: str) -> str:
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        try:
            return str(json.loads(value))
        except ValueError:
            pass
    return value.strip("\"'")


def with_frontmatter(description: str, body: str) -> str:
    """`description:` as a double-quoted YAML scalar: a colon or a quote in the text would otherwise break the
    frontmatter, and the framework's check parses it as YAML."""
    description = " ".join(str(description or "").split()) or "(no description)"
    return f"---\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n{body.strip()}\n"


# ---------- the step's rollouts ----------

def read_records(material: Path) -> list[tuple[dict, Path | None]]:
    """Each task record of the current step with its trajectory directory (None without one)."""
    current = material / "current"
    out = []
    for f in sorted((current / "tasks").glob("*.json")):
        record = json.loads(f.read_text(encoding="utf-8"))
        traj = record.get("trajectory")
        out.append((record, (current / traj).resolve() if traj else None))
    return out


def codex_events(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []:
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _tail(text: str, n: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= n else "..." + text[-n:]


def codex_trajectory(events: list[dict]) -> str:
    """A Codex event stream as the Refiner reads it: reasoning, messages, commands with their exit codes and output."""
    lines = []
    for e in events:
        kind = e.get("type")
        item = e.get("item") or {}
        if kind == "error":
            lines.append(f"[error] {_tail(e.get('message'), 300)}")
            continue
        if kind != "item.completed":
            continue
        t = item.get("type")
        if t == "reasoning":
            lines.append(f"[reasoning] {_tail(item.get('text'), REASONING_TEXT)}")
        elif t == "agent_message":
            lines.append(f"[assistant] {_tail(item.get('text'), STEP_TEXT)}")
        elif t == "command_execution":
            code = item.get("exit_code")
            lines.append(f"[command exit {code}] {_tail(item.get('command'), 300)}")
            out = _tail(item.get("aggregated_output"), OUTPUT_TAIL)
            if out:
                lines.append(f"  output: {out}")
        elif t == "file_change":
            lines.append(f"[file change] {_tail(json.dumps(item.get('changes') or item, default=str), 300)}")
        elif t == "mcp_tool_call":
            lines.append(f"[tool call] {_tail(json.dumps({k: item.get(k) for k in ('server', 'tool', 'status')}), 300)}")
        elif t == "error":
            lines.append(f"[error] {_tail(item.get('message'), 300)}")
    return "\n".join(lines)


def _content(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


def message_trajectory(messages: list) -> str:
    """A normalised message list as text (system prompt and the first task message left out)."""
    lines, task_seen = [], False
    for m in messages or []:
        role = m.get("role")
        if role == "system":
            continue
        if role == "user" and not task_seen:
            task_seen = True
            continue
        text = _content(m)
        if text:
            lines.append(f"[{role}] {_tail(text, STEP_TEXT)}")
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            lines.append(f"[tool call] {fn.get('name')}({_tail(fn.get('arguments'), 300)})")
    return "\n".join(lines)


def trajectory_text(record: dict, traj: Path | None, limit: int) -> str:
    stream = traj / "agent-codex.jsonl" if traj else None
    if stream and stream.is_file():
        text = codex_trajectory(codex_events(stream))
    elif record.get("messages"):
        text = message_trajectory(record["messages"])
    else:
        text = "(no trajectory recorded)"
    return clip_middle(text, limit)


def failure_signatures(record: dict, traj: Path | None, feedback: bool) -> list[str]:
    """What went wrong, read from the run itself: status, timeout, failed commands, a missing answer; and, with
    `feedback`, the judge's verdict on a task scored below 1."""
    out = []
    if record.get("status") and record["status"] != "ok":
        out.append(f"run status {record['status']}" + (f": {_tail(record.get('error'), 200)}" if record.get("error") else ""))
    extra = record.get("pipeline_extra") or {}
    if extra.get("session_timed_out"):
        out.append("the session hit its wall clock")
    elif extra.get("exit_code"):
        out.append(f"the session exited with code {extra['exit_code']}")
    output = record.get("output")
    if isinstance(output, dict) and output.get("fail_reason"):
        out.append(f"no answer delivered: {_tail(output['fail_reason'], 200)}")
    stream = traj / "agent-codex.jsonl" if traj else None
    if stream and stream.is_file():
        failed, errors = [], []
        for e in codex_events(stream):
            item = e.get("item") or {}
            if e.get("type") == "item.completed" and item.get("type") == "command_execution" \
                    and str(item.get("exit_code")) not in ("0", "None", ""):
                failed.append(f"`{_tail(item.get('command'), 120)}` exited {item.get('exit_code')}: "
                              f"{_tail(item.get('aggregated_output'), 200)}")
            elif (e.get("type") == "error") or (e.get("type") == "item.completed" and item.get("type") == "error"):
                errors.append(_tail(item.get("message") or e.get("message"), 200))
        if failed:
            out.append(f"{len(failed)} command(s) failed: " + " | ".join(failed[:3]))
        if errors:
            out.append(f"{len(errors)} error event(s): " + " | ".join(errors[:3]))
    judge = record.get("judge") or {}
    if feedback and judge and judge.get("score", 1) < 1 and judge.get("feedback"):
        out.append(f"judged wrong: {_tail(judge['feedback'], 300)}")
    return out


def agent_notes(traj: Path | None) -> str | None:
    """The memory file the acting agent left in its workspace, handed back with the trajectory."""
    f = traj / "memory" / "NOTES.md" if traj else None
    return f.read_text(encoding="utf-8", errors="replace") if f and f.is_file() else None


# ---------- memory as `## <topic>` sections ----------

def sections(text: str) -> list[tuple[str, str]]:
    """(title, body) per `## ` heading; text before the first heading is dropped (the file's own title line)."""
    out: list[tuple[str, str]] = []
    title, buf = None, []
    for line in (text or "").splitlines():
        if line.startswith("## "):
            if title is not None:
                out.append((title, "\n".join(buf).strip()))
            title, buf = line[3:].strip(), []
        elif title is not None:
            buf.append(line)
    if title is not None:
        out.append((title, "\n".join(buf).strip()))
    return [(t, b) for t, b in out if t]


def render_notes(entries: list[tuple[str, str]]) -> str:
    if not entries:
        return "# Notes\n\n(No notes yet. Add one as `## <topic>` followed by what to remember.)\n"
    return "# Notes\n\n" + "\n\n".join(f"## {t}\n\n{b}".rstrip() for t, b in entries) + "\n"


def merge_notes(current: str, incoming: str, limit: int = NOTES_MAX) -> tuple[str, int]:
    """The agent's notes folded into the adapter's: a section with a known title replaces it, a new title is
    appended. Returns the text and how many sections changed. The result stays within `limit` bytes (the
    template's cap on the file): when it would not, the oldest sections are dropped first."""
    have = sections(current)
    changed = 0
    for title, body in sections(incoming):
        if not body or PLACEHOLDER in body:
            continue
        for i, (t, b) in enumerate(have):
            if t.lower() == title.lower():
                if b != body:
                    have[i] = (t, body)
                    changed += 1
                break
        else:
            have.append((title, body))
            changed += 1
    return fit_notes(have, limit), changed


def fit_notes(entries: list[tuple[str, str]], limit: int = NOTES_MAX) -> str:
    """The notes rendered within `limit` bytes: the oldest sections go first, a single oversize section is cut."""
    entries = list(entries)
    while entries and len(render_notes(entries).encode()) > limit:
        if len(entries) == 1:
            title, body = entries[0]
            entries = [(title, body.encode()[:max(0, limit - 200)].decode(errors="ignore").rstrip())]
            break
        entries.pop(0)
    return render_notes(entries)


# ---------- the harness as the Refiner sees it ----------

def skills_of(adapter: Path) -> dict[str, dict]:
    out = {}
    for f in sorted((adapter / ".agents" / "skills").glob("*/SKILL.md")):
        meta, body = frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        scripts = {p.name: p.read_text(encoding="utf-8", errors="replace")
                   for p in sorted((f.parent / "scripts").glob("*")) if p.is_file()}
        out[f.parent.name] = {"description": meta.get("description", ""), "body": body.strip(), "scripts": scripts}
    return out


def subagents_of(adapter: Path) -> dict[str, dict]:
    out = {}
    for f in sorted((adapter / "sub_agents").glob("*/AGENT.md")):
        meta, body = frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        out[f.parent.name] = {"description": meta.get("description", ""), "instructions": body.strip()}
    return out


def notes_of(adapter: Path) -> str:
    f = adapter / "memory" / "NOTES.md"
    return f.read_text(encoding="utf-8", errors="replace") if f.is_file() else render_notes([])


def overview(adapter: Path) -> str:
    skills = skills_of(adapter)
    roles = subagents_of(adapter)
    notes = sections(notes_of(adapter))
    lines = [f"- policy AGENTS.md: {len((adapter / ALWAYS_ON).read_text(encoding='utf-8', errors='replace'))} characters",
             f"- skills ({len(skills)}): " + (", ".join(f"{n} ({s['description'][:60]})" for n, s in skills.items()) or "none"),
             f"- sub-agents ({len(roles)}): " + (", ".join(f"{n} ({r['description'][:60]})" for n, r in roles.items()) or "none"),
             f"- memory notes ({len(notes)}): " + (", ".join(t for t, _ in notes) or "none")]
    return "\n".join(lines)


# ---------- the Refiner ----------

class ContinualHarnessEvolver(Evolver):
    def step(self, ctx: StepContext) -> StepResult:
        p, ws = ctx.params, ctx.workspace
        if "llm" not in ctx.tools:
            return StepResult("failed", "continual_harness needs the run's llm tool: set `llm` in the run config")
        adapter = ws.adapter
        if not (adapter / ALWAYS_ON).is_file():
            return StepResult("failed", "the adapter is not a continual-harness adapter (no AGENTS.md): use the "
                                        "continual-harness template")
        n = ctx.progress.step if ctx.progress else 0
        feedback = bool(p.get("feedback", True))
        limit = int(p.get("trace_chars") or 12000)
        records = read_records(ws.learning_material)

        # The window: every rollout of the step, its failure signatures, and the notes the agent wrote.
        runs, signals, notes_from = [], [], []
        for record, traj in records:
            tid = record.get("id")
            sig = failure_signatures(record, traj, feedback)
            signals += [f"- task {tid}: {s}" for s in sig]
            judge = record.get("judge") or {}
            verdict = f", judged {judge.get('score')}" if feedback and judge else ""
            output = record.get("output")
            final = output.get("answer", output) if isinstance(output, dict) else output
            runs.append(f"### Task {tid} (status {record.get('status')}{verdict})\n"
                        f"Final output: {_tail(json.dumps(final, ensure_ascii=False, default=str), 500)}\n"
                        f"Trajectory:\n{trajectory_text(record, traj, limit)}")
            incoming = agent_notes(traj)
            if incoming:
                merged, changed = merge_notes(notes_of(adapter), incoming)
                if changed:
                    (adapter / "memory").mkdir(exist_ok=True)
                    (adapter / "memory" / "NOTES.md").write_text(merged, encoding="utf-8")
                    notes_from.append(f"{tid} ({changed})")
        record = {"step": n, "tasks": [r.get("id") for r, _ in records], "signals": len(signals),
                  "notes_from_agents": notes_from, "passes": {}}
        if len(signals) < int(p.get("min_signals") or 0) and not notes_from:
            record["outcome"] = f"unchanged: {len(signals)} failure signature(s), below min_signals"
            return self._finish(ctx, record)

        task_info = ws.task_info / "README.md"
        task_text = clip_middle(task_info.read_text(encoding="utf-8", errors="replace"),
                                int(p.get("task_info_chars") or 3000)) if task_info.is_file() else "(not given)"
        evidence = (f"=== THE TASK ===\n{task_text}\n\n=== RECENT ROLLOUTS ({len(runs)}) ===\n" + "\n\n".join(runs)
                    + "\n\n=== FAILURE SIGNATURES ===\n" + ("\n".join(signals) or "(none: every run finished cleanly)"))
        sampling = {k: p[k] for k in ("max_tokens", "temperature") if p.get(k) is not None}
        passes = [c for c in (p.get("components") or PASSES) if c in PASSES]
        for comp in passes:
            record["passes"][comp] = self._pass(comp, ctx, adapter, evidence, sampling)
        changed = [f"{c}: {', '.join(r['changed'])}" for c, r in record["passes"].items() if r.get("changed")]
        dropped = [c for c, r in record["passes"].items() if r.get("rejected") or r.get("error")]
        record["outcome"] = ("refined " + "; ".join(changed) if changed else "unchanged: no pass changed the harness") \
            + (f"; dropped {', '.join(dropped)}" if dropped else "") \
            + f"; {len(signals)} failure signature(s)" + (f"; notes from {', '.join(notes_from)}" if notes_from else "")
        return self._finish(ctx, record)

    def finalize(self, ctx) -> FinalResult:
        state_file = ctx.workspace.state / "continual_harness.json"
        steps = json.loads(state_file.read_text())["steps"] if state_file.exists() else []
        changed = sum(1 for s in steps if str(s.get("outcome", "")).startswith("refined"))
        return FinalResult(adapter=ctx.workspace.adapter,
                           summary=f"continual-harness refinement: {changed} of {len(steps)} steps changed the harness")

    # ---------- bookkeeping ----------

    def _finish(self, ctx: StepContext, record: dict) -> StepResult:
        ws = ctx.workspace
        self._sync_context(ws.adapter)
        state_file = ws.state / "continual_harness.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else {"steps": []}
        state["steps"].append(record)
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        (ws.output / "report.md").write_text(self._report(record), encoding="utf-8")
        return StepResult("done", record["outcome"][:300],
                          extra={"signals": record["signals"], "passes": record["passes"]})

    @staticmethod
    def _report(record: dict) -> str:
        lines = [f"# Continual Harness, step {record['step']}", "", record["outcome"], ""]
        for comp, r in record.get("passes", {}).items():
            lines.append(f"- {comp}: " + (f"changed {', '.join(r['changed'])}" if r.get("changed") else "unchanged")
                         + (f"; rejected by the template check: {r['rejected']}" if r.get("rejected") else "")
                         + (f"; error: {r['error']}" if r.get("error") else "")
                         + (f"; analysis: {r['analysis']}" if r.get("analysis") else ""))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _sync_context(adapter: Path) -> None:
        """ADAPTER.md, the framework's context export, is AGENTS.md."""
        text = (adapter / ALWAYS_ON).read_text(encoding="utf-8", errors="replace")
        if not (adapter / "ADAPTER.md").is_file() or (adapter / "ADAPTER.md").read_text(encoding="utf-8") != text:
            (adapter / "ADAPTER.md").write_text(text, encoding="utf-8")

    # ---------- one pass ----------

    def _pass(self, comp: str, ctx: StepContext, adapter: Path, evidence: str, sampling: dict) -> dict:
        """Run one component's pass on the adapter; drop it (restore the adapter) if the result fails the check."""
        snapshot = ctx.workspace.scratch / f"before_{comp}"
        shutil.rmtree(snapshot, ignore_errors=True)
        shutil.copytree(adapter, snapshot, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        fn = {POLICY: self._policy, SUBAGENTS: self._subagents, SKILLS: self._skills, MEMORY: self._memory}[comp]
        try:
            result = fn(ctx, adapter, evidence, sampling)
        except Exception as exc:                              # noqa: BLE001 — a failed pass fails alone
            self._restore(snapshot, adapter)
            return {"changed": [], "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        if result.get("changed"):
            self._sync_context(adapter)
            check = ctx.tools["check_spec"](path=str(adapter)) if "check_spec" in ctx.tools else {"passed": True}
            if not check.get("passed", True):
                self._restore(snapshot, adapter)
                result["rejected"] = [i.get("problem", str(i))[:200] for i in check.get("issues", [])[:3]]
                result["changed"] = []
        shutil.rmtree(snapshot, ignore_errors=True)
        return result

    @staticmethod
    def _restore(snapshot: Path, adapter: Path) -> None:
        for p in list(adapter.iterdir()):
            if p.name == ".git":
                continue
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() and not p.is_symlink() else p.unlink(missing_ok=True)
        for p in snapshot.iterdir():
            if p.is_dir() and not p.is_symlink():
                shutil.copytree(p, adapter / p.name, symlinks=True)
            else:
                shutil.copy2(p, adapter / p.name, follow_symlinks=False)

    # ---------- the model ----------

    def _ask(self, ctx: StepContext, prompt: str, sampling: dict, kind: str) -> str | dict | None:
        """One Refiner call. kind "block": the content of the reply's last ``` block; "json": the reply's JSON
        object. A reply without one is asked again, twice."""
        messages = [{"role": "user", "content": prompt}]
        for _ in range(3):
            reply = ctx.tools["llm"](messages=messages, **sampling)
            text = str(reply.get("content") or "")
            parsed = _last_block(text) if kind == "block" else _json_object(text)
            if parsed is not None:
                return parsed
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "Reply now with only the complete content in one ``` block."
                          if kind == "block" else "Reply now with only the JSON object, no prose and no fences."}]
        return None

    # ---------- the four passes ----------

    def _policy(self, ctx: StepContext, adapter: Path, evidence: str, sampling: dict) -> dict:
        current = (adapter / ALWAYS_ON).read_text(encoding="utf-8", errors="replace")
        soft = int(ALWAYS_ON_MAX * 0.85)
        prompt = (
            "You are the Refiner of a self-improving agent harness (Continual Harness). The harness has four parts: the "
            "base policy (AGENTS.md, read by the agent every turn), sub-agents it can delegate to, skills it can open "
            "and run, and a memory of notes. This pass rewrites the BASE POLICY from the agent's recent rollouts.\n\n"
            "Improve the policy so the failures observed do not recur and what worked is kept: add specific guidance "
            "against the mistakes seen, keep the elements that led to correct, complete work, and state how to use "
            "the skills, sub-agents and memory the harness has. Write for the agent, in the imperative; no commentary "
            "about this rewrite. Keep it sharp, not long: consolidate and prune rather than append.\n"
            f"Stay within about {soft} characters ({ALWAYS_ON_MAX} is a hard cap; a longer policy is rejected).\n\n"
            f"=== THE HARNESS NOW ===\n{overview(adapter)}\n\n=== CURRENT BASE POLICY (AGENTS.md) ===\n{current}\n\n"
            f"{evidence}\n\nReply with the COMPLETE new AGENTS.md inside a single ``` code block, and nothing else.")
        for _ in range(2):
            new = self._ask(ctx, prompt, sampling, "block")
            if new is None:
                return {"changed": []}
            if len(new) <= ALWAYS_ON_MAX:
                break
            prompt += (f"\n\nYour draft was {len(new)} characters, over the cap. Reply again with the COMPLETE policy "
                       f"compressed to under {soft} characters, merging and cutting, in one ``` block.")
        else:
            return {"changed": [], "error": f"the policy stayed over {ALWAYS_ON_MAX} characters"}
        new = new.strip() + "\n"
        if new.strip() == current.strip() or not new.strip():
            return {"changed": []}
        (adapter / ALWAYS_ON).write_text(new, encoding="utf-8")
        return {"changed": [ALWAYS_ON]}

    def _subagents(self, ctx: StepContext, adapter: Path, evidence: str, sampling: dict) -> dict:
        roles = subagents_of(adapter)
        listing = "\n\n".join(f"### {n}\ndescription: {r['description']}\n{clip_middle(r['instructions'], 3000)}"
                              for n, r in roles.items()) or "(none)"
        prompt = (
            "You are the Refiner of a self-improving agent harness (Continual Harness). This pass maintains the "
            "SUB-AGENTS: delegated roles the agent can spawn in their own context for a recurring multi-step job "
            "(for example a careful reader of long documents, a checker of a computed figure, a planner when the "
            "agent is stuck). Each is a file sub_agents/<name>/AGENT.md: a one-line description (how the agent knows "
            "when to delegate) and the role's instructions.\n\n"
            "From the rollouts: create a role for a repeated multi-step pattern the agent did by hand and did "
            "poorly, update a role that was used and led to failures, retire a role that was used repeatedly with "
            "poor results. Do not create roles for one-off situations. Keep the registry small: it holds at most "
            f"{MAX_SUBAGENTS} sub-agents and has {len(roles)} now; if it is at the limit, retire one in the same "
            "response before you create.\n\n"
            f"=== THE HARNESS NOW ===\n{overview(adapter)}\n\n=== CURRENT SUB-AGENTS ===\n{listing}\n\n{evidence}\n\n"
            "Respond with ONLY a JSON object (no prose, no fences):\n"
            '{"analysis": "what you observed, one or two sentences",\n'
            ' "create": [{"name": "short_name", "description": "when to delegate to it", "instructions": "the role\'s '
            'instructions"}],\n'
            ' "update": [{"name": "existing_name", "description": "optional", "instructions": "optional, complete"}],\n'
            ' "retire": ["existing_name"]}\n'
            "Only include what you recommend; empty lists are fine. Names: lowercase letters, digits, _ and -.")
        rec = self._ask(ctx, prompt, sampling, "json")
        if not isinstance(rec, dict):
            return {"changed": []}
        changed = []
        # Retire first so a retire+create in the same pass stays within the registry cap (a create that would push
        # sub_agents/*/AGENT.md over max_files makes check 4 fail and _pass drops the WHOLE pass, losing the edits).
        for name in rec.get("retire") or []:
            name = slug(name)
            if name in roles:
                shutil.rmtree(adapter / "sub_agents" / name, ignore_errors=True)
                roles.pop(name, None)
                changed.append(f"-{name}")
        for upd in rec.get("update") or []:
            if not isinstance(upd, dict):
                continue
            name = slug(upd.get("name"))
            if name not in roles:
                continue
            role = roles[name]
            text = with_frontmatter(upd.get("description") or role["description"],
                                    str(upd.get("instructions") or role["instructions"]))[:AGENT_MAX]
            (adapter / "sub_agents" / name / "AGENT.md").write_text(text, encoding="utf-8")
            changed.append(f"~{name}")
        for spec in rec.get("create") or []:
            if not isinstance(spec, dict):
                continue
            name = slug(spec.get("name"))
            if name not in roles and len(roles) >= MAX_SUBAGENTS:    # registry full: skip this one, keep the rest
                changed.append(f"!{name} (registry full)")
                continue
            d = adapter / "sub_agents" / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "AGENT.md").write_text(with_frontmatter(spec.get("description"), str(spec.get("instructions") or ""))
                                        [:AGENT_MAX], encoding="utf-8")
            roles[name] = roles.get(name) or {"description": "", "instructions": ""}
            changed.append(f"+{name}")
        return {"changed": changed, "analysis": _tail(rec.get("analysis"), 300)}

    def _skills(self, ctx: StepContext, adapter: Path, evidence: str, sampling: dict) -> dict:
        skills = skills_of(adapter)
        listing = "\n\n".join(
            f"### {n}\ndescription: {s['description']}\n{clip_middle(s['body'], 3000)}"
            + "".join(f"\n--- scripts/{f} ---\n{clip_middle(src, 2000)}" for f, src in s["scripts"].items())
            for n, s in skills.items()) or "(none)"
        prompt = (
            "You are the Refiner of a self-improving agent harness (Continual Harness). This pass maintains the "
            "SKILLS: reusable procedures the agent opens by name when the situation matches the skill's description. "
            "Each is a directory .agents/skills/<name>/ with SKILL.md (a one-line description, then the steps) and, "
            "when a step is better done by a program, an executable helper under scripts/ (a Python file the agent "
            "runs as `python3 .agents/skills/<name>/scripts/<file>`; it must be complete and must compile).\n\n"
            "From the rollouts: record a sequence that worked as a skill when it will recur; fix a skill that failed "
            "or whose helper errored (give the FULL replacement body or code); retire a skill that misled the agent "
            "repeatedly. Prefer one good skill to several thin ones; a skill must be specific enough to follow. The "
            f"registry holds at most {MAX_SKILLS} skills and has {len(skills)} now; if it is at the limit, retire one "
            "in the same response before you add.\n\n"
            f"=== THE HARNESS NOW ===\n{overview(adapter)}\n\n=== CURRENT SKILLS ===\n{listing}\n\n{evidence}\n\n"
            "Respond with ONLY a JSON object (no prose, no fences):\n"
            '{"analysis": "what you observed, one or two sentences",\n'
            ' "add": [{"name": "short_name", "description": "when to use it", "body": "the steps, markdown", '
            '"scripts": {"helper.py": "complete python source"}}],\n'
            ' "update": [{"name": "existing_name", "description": "optional", "body": "optional, complete", '
            '"scripts": {"helper.py": "optional, complete replacement"}}],\n'
            ' "retire": ["existing_name"]}\n'
            "Only include what you recommend; empty lists are fine. Names: lowercase letters, digits, _ and -.")
        rec = self._ask(ctx, prompt, sampling, "json")
        if not isinstance(rec, dict):
            return {"changed": []}
        changed = []

        def write(name: str, description: str, body: str, scripts: dict) -> None:
            d = adapter / ".agents" / "skills" / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(with_frontmatter(description, body)[:SKILL_MAX], encoding="utf-8")
            for fname, src in (scripts or {}).items():
                fname = re.sub(r"[^A-Za-z0-9_.\-]+", "_", str(fname)).strip("._-") or "helper.py"
                (d / "scripts").mkdir(exist_ok=True)
                (d / "scripts" / fname).write_text(str(src or ""), encoding="utf-8")

        # Retire first so a retire+add in the same pass stays within the registry cap (an add that would push
        # .agents/skills/*/SKILL.md over max_files makes check 4 fail and _pass drops the WHOLE pass, losing the edits).
        for name in rec.get("retire") or []:
            name = slug(name)
            if name in skills:
                shutil.rmtree(adapter / ".agents" / "skills" / name, ignore_errors=True)
                skills.pop(name, None)
                changed.append(f"-{name}")
        for upd in rec.get("update") or []:
            if not isinstance(upd, dict):
                continue
            name = slug(upd.get("name"))
            if name not in skills:
                continue
            s = skills[name]
            write(name, upd.get("description") or s["description"], str(upd.get("body") or s["body"]),
                  upd.get("scripts") or {})
            changed.append(f"~{name}")
        for spec in rec.get("add") or []:
            if not isinstance(spec, dict):
                continue
            name = slug(spec.get("name"))
            if name not in skills and len(skills) >= MAX_SKILLS:     # registry full: skip this one, keep the rest
                changed.append(f"!{name} (registry full)")
                continue
            write(name, spec.get("description"), str(spec.get("body") or ""), spec.get("scripts") or {})
            skills[name] = skills.get(name) or {"description": "", "body": "", "scripts": {}}
            changed.append(f"+{name}")
        return {"changed": changed, "analysis": _tail(rec.get("analysis"), 300)}

    def _memory(self, ctx: StepContext, adapter: Path, evidence: str, sampling: dict) -> dict:
        current = notes_of(adapter)
        entries = sections(current)
        limit = int(ctx.params.get("memory_max_changes") or 5)
        used = len(current.encode())
        room = (f"The file is {used:,} of its {NOTES_MAX:,}-byte cap"
                + (": it is nearly full, so merge overlapping notes and delete the least useful ones before adding "
                   "any (an update that replaces two notes by one counts as one change)." if used > NOTES_MAX * 0.8
                   else "."))
        prompt = (
            "You are the memory curator of a self-improving agent harness (Continual Harness). The MEMORY is "
            "memory/NOTES.md: notes under `## <topic>` headings that the agent reads at the start of a task and may "
            "write to during it. The agent writes most of the memory itself; you fill gaps and clean up. Be "
            f"conservative: at most {limit} changes, and nothing that duplicates a note already there. {room}\n\n"
            "From the rollouts: add a note for a fact, constraint or quirk the agent discovered and will need again "
            "(where things are, how a source is laid out, a pitfall); update a note the rollouts contradict; delete a "
            "note that is stale or wrong. Facts only; instructions belong in the policy.\n\n"
            f"=== THE HARNESS NOW ===\n{overview(adapter)}\n\n=== CURRENT MEMORY ===\n{clip_middle(current, 12000)}\n\n"
            f"{evidence}\n\n"
            "Respond with ONLY a JSON object (no prose, no fences):\n"
            '{"analysis": "the memory\'s state, one sentence",\n'
            ' "add": [{"title": "topic", "content": "the note"}],\n'
            ' "update": [{"title": "existing topic", "content": "the complete new note"}],\n'
            ' "delete": ["existing topic"]}\n'
            "Only include what you recommend; empty lists are fine.")
        rec = self._ask(ctx, prompt, sampling, "json")
        if not isinstance(rec, dict):
            return {"changed": []}
        changed, budget = [], limit
        titles = {t.lower(): i for i, (t, _) in enumerate(entries)}
        for name in rec.get("delete") or []:
            key = str(name).strip().lower()
            if key in titles and budget > 0:
                entries[titles[key]] = None
                changed.append(f"-{name}")
                budget -= 1
        for upd in rec.get("update") or []:
            if not isinstance(upd, dict) or budget <= 0:
                continue
            key = str(upd.get("title") or "").strip().lower()
            if key in titles and entries[titles[key]] is not None and str(upd.get("content") or "").strip():
                entries[titles[key]] = (entries[titles[key]][0], str(upd["content"]).strip())
                changed.append(f"~{upd['title']}")
                budget -= 1
        for spec in rec.get("add") or []:
            if not isinstance(spec, dict) or budget <= 0:
                continue
            title, content = str(spec.get("title") or "").strip(), str(spec.get("content") or "").strip()
            if title and content and title.lower() not in titles:
                entries.append((title, content))
                titles[title.lower()] = len(entries) - 1
                changed.append(f"+{title}")
                budget -= 1
        if changed:
            text = fit_notes([e for e in entries if e is not None])       # within the cap: the oldest notes go first
            (adapter / "memory").mkdir(exist_ok=True)
            (adapter / "memory" / "NOTES.md").write_text(text, encoding="utf-8")
        return {"changed": changed, "analysis": _tail(rec.get("analysis"), 300)}


# ---------- parsing replies ----------

def _last_block(text: str) -> str | None:
    """The content of the outermost ``` block — the first opening fence to the last closing fence — so a document
    that itself contains nested ``` blocks (e.g. a policy with a shell example) is kept whole instead of being
    truncated to the fragment after an inner fence. None if there is no fenced block."""
    fences = [m.start() for m in re.finditer(r"```", text)]
    if len(fences) >= 2:
        inner = text[fences[0] + 3:fences[-1]]
        return inner.split("\n", 1)[1] if "\n" in inner else inner      # drop the opening ```lang line
    return None


def _json_object(text: str) -> dict | None:
    """The reply's JSON object: the text itself, the inside of a ``` block, or the outermost {...}."""
    candidates = [text.strip()]
    block = _last_block(text)
    if block:
        candidates.append(block.strip())
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None
