"""EvoHarnessEvolver: Evo-Harness (Wei et al., EMNLP 2026, arXiv:2608.15071; A-EVO-Lab/a-evolve, release/evo-harness,
`evo_harness/swe_bench.py::evolve_batch`), as an Evolver on the `evo-harness` template.

Online harness learning: the acting agent is frozen, and a harness of skills at two levels is updated after each batch
of tasks. On the framework's step model the batch is the Trainer's rollouts of the step, and the step does what
`evolve_batch` does after solving:

1. reflect: each failed rollout yields one candidate skill with a topic (`PROPOSE_FAIL`), in parallel;
2. compile per topic: a curator accepts, merges or skips the topic's candidates, under `max_skills_per_topic`;
3. compile across tasks: with two failures or more, a general curator adds, updates or deletes general skills, under
   `max_general_skills`;
4. the general skills' full text and the list of topic skills are rendered into ADAPTER.md's block.

The acting agent reads the general skills in its context, and picks topic skills by their one-line descriptions; there
is no selector call. Nothing is validated or selected: as upstream, the last harness is the final one.

Training State: `state/evo_harness.json`, one record per step; each step's `output/` holds the prompts, the replies and a
`report.md`.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import shutil

from hermit.agent.base import trajectory
from hermit.agent.evolver.base import Evolver, StepContext, StepResult, ask
from hermit.agent.evolver.methods.evo_harness.prompts import (ATTEMPT, CURATOR, DOMAIN_HEADING, GENERAL_CURATOR,
                                                              GENERAL_HEADING, PROPOSE_FAIL, SKILLS_INTRO)
from hermit.trainer.budget import BudgetExceeded

ADAPTER = "ADAPTER.md"
BEGIN, END = "<!-- evo-harness:general -->", "<!-- /evo-harness:general -->"
GENERAL, TOPIC = "general", "skills"       # the two levels' directories
SEP = "--"                                 # skills/<topic>--<name>/SKILL.md: Codex's skills are one level deep
MAX_NAME = 64                              # the longest skill name Codex loads: it skips a longer one, in its log only
MAX_TOPIC = 32                             # the share of it a topic may take; the name has the rest
NO_JUDGE = "No judge assessment available."                 # swe_bench.py's text when no judge verdict exists
GENERAL_MARKERS = ("NEW_GENERAL:", "UPDATE_GENERAL:", "DELETE_GENERAL:", "NO_PATTERNS")


def clip_middle(text: str, limit: int) -> str:
    """Keep the beginning (the approach) and the end (how it finished)."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit * 2 // 5
    return text[:head] + f"\n[... {len(text) - limit} characters left out ...]\n" + text[-(limit - head):]


def task_text(task) -> str:
    if not isinstance(task, dict):
        return str(task)
    fields = {k: v for k, v in task.items() if k not in ("id", "task_id")}
    if len(fields) == 1:
        return str(next(iter(fields.values())))
    return json.dumps(fields, ensure_ascii=False)


def answer_text(output) -> str:
    if isinstance(output, dict):
        return str(output.get("answer") if output.get("answer") is not None else json.dumps(output, ensure_ascii=False))
    return "" if output is None else str(output)


def messages_text(messages: list) -> str:
    """The run's messages, the system prompt (the adapter itself) and the first user message (the task) left out."""
    lines, task_seen = [], False
    for m in messages or []:
        role = m.get("role")
        if role == "system":
            continue
        if role == "user" and not task_seen:
            task_seen = True
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
        if content:
            lines.append(f"[{role}] {content}")
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            lines.append(f"[tool call] {fn.get('name')}({fn.get('arguments')})")
    return "\n".join(lines)


def run_trace(root: Path | None) -> str:
    """A run's log as the framework's standard steps (hermit.agent.base.trajectory): what the agent said and
    reasoned, the commands it ran and their output; "" when the log holds no host stream."""
    steps = trajectory.log_steps(root) if root is not None and root.exists() else None
    if not steps:
        return ""
    return trajectory.text(steps, message=4000, reasoning=1000, command=1000, output=2000, other=1000)


def reasoning_trace(record: dict, traj: Path | None) -> tuple[str, str]:
    """What a reflection reads of the attempt, and where it came from: the run's messages
    ("messages"), else its trajectory's steps ("trajectory"), else nothing ("none")."""
    text, source = messages_text(record.get("messages") or []), "messages"
    if not text:
        text, source = run_trace(traj), "trajectory"
    if not text:
        source = "none"
    if record.get("status") not in (None, "ok"):
        text += f"\n[run status {record.get('status')}] {str(record.get('error') or '')[:2000]}"
    return text or "(no trajectory recorded)", source


def rollouts(material: Path) -> list[tuple[dict, Path | None]]:
    """The step's rollouts in the order the Trainer ran them (summary.json's task ids)."""
    current = material / "current"
    summary = current / "summary.json"
    order = json.loads(summary.read_text()).get("tasks", []) if summary.is_file() else []
    rank = {t: i for i, t in enumerate(order)}
    out = []
    for f in (current / "tasks").glob("*.json"):
        record = json.loads(f.read_text(encoding="utf-8"))
        traj = record.get("trajectory")
        out.append((record, (current / traj).resolve() if traj else None))
    return sorted(out, key=lambda rt: (rank.get(rt[0].get("id"), len(rank)), str(rt[0].get("id"))))


def truncate(text, limit: int) -> str:
    """swe_bench.py's `_truncate`."""
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "..."


def slug(raw: str, limit: int = MAX_NAME) -> str:
    """A lowercase hyphenated name of at most `limit` characters, cut at a hyphen where there is one."""
    text = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", str(raw).lower())).strip("-")
    if len(text) <= limit:
        return text
    head = text[:limit]
    return (head if text[limit] == "-" or "-" not in head else head.rsplit("-", 1)[0]).strip("-")


# ---------- the harness on disk ----------

def write_skill(path: Path, name: str, description: str, content: str) -> None:
    """A SKILL.md. The description is JSON-quoted: a colon in plain YAML would make Codex drop the whole skill."""
    path.mkdir(parents=True, exist_ok=True)
    description = " ".join(str(description or "").split()) or name
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {json.dumps(description, ensure_ascii=False)}\n"
                                   f"---\n\n{content.strip()}\n", encoding="utf-8")


def read_skill(path: Path) -> tuple[str, str]:
    """(description, body) of a skill directory."""
    text = (path / "SKILL.md").read_text(encoding="utf-8")
    head, _, body = text.partition("\n---\n")
    description = ""
    for line in head.splitlines():
        if line.startswith("description:"):
            description = line.split(":", 1)[1].strip()
            try:
                description = json.loads(description)
            except ValueError:
                pass
    return description, body.strip()


def skill_dirs(root: Path) -> list[Path]:
    return sorted(d for d in root.iterdir() if (d / "SKILL.md").is_file()) if root.is_dir() else []


def render_block(adapter: Path) -> str:
    """The block of ADAPTER.md: the general skills in full (upstream injects them whole) and a line per topic skill."""
    parts = [BEGIN]
    general = skill_dirs(adapter / GENERAL)
    if general:
        parts.append(GENERAL_HEADING)
        parts += [f"### {d.name}\n{read_skill(d)[1]}" for d in general]
    topics = skill_dirs(adapter / TOPIC)
    if topics:
        parts += [DOMAIN_HEADING, SKILLS_INTRO + "\n" + "\n".join(
            f"- `{TOPIC}/{d.name}/SKILL.md`: {read_skill(d)[0]}" for d in topics)]
    return "\n\n".join(parts + [END])


def with_block(text: str, block: str) -> str:
    return text[:text.index(BEGIN)] + block + text[text.index(END) + len(END):]


# ---------- reading a rollout ----------

def signals(steps: list | None) -> dict:
    """swe_bench.py's `_extract_trajectory_signals` over the framework's standard steps."""
    steps = steps or []
    tools, commands, errors, timeouts = defaultdict(int), [], [], 0
    for s in steps:
        if s.kind in ("command", "tool_call", "file_change"):
            tools[s.kind] += 1
        if s.kind == "command":
            commands.append(s.command[:80])
            if s.exit_code not in (0, None):
                errors.append(s.output[:100])
            if "timed out" in s.output.lower() or "timeout" in s.output.lower():
                timeouts += 1
        elif s.kind == "error":
            errors.append(s.text[:100])
    counts = defaultdict(int)
    for c in commands:
        counts[c] += 1
    return {"n_turns": sum(1 for s in steps if s.kind == "message" and s.role == "assistant"),
            "n_tool_calls": sum(tools.values()), "n_errors": len(errors), "n_timeouts": timeouts,
            "tools_used": dict(tools), "repeated_commands": [c for c, n in counts.items() if n >= 3],
            "error_snippets": errors[:5]}


def format_signals(s: dict, answered: bool) -> str:
    """swe_bench.py's `_format_signals`."""
    lines = [f"Turns: {s['n_turns']}, Tool calls: {s['n_tool_calls']}, Errors: {s['n_errors']}, "
             f"Timeouts: {s['n_timeouts']}",
             f"Tools: {', '.join(f'{k}({v})' for k, v in s['tools_used'].items())}", f"Submitted: {answered}"]
    if s["repeated_commands"]:
        lines.append(f"Repeated commands: {'; '.join(s['repeated_commands'][:3])}")
    if s["error_snippets"]:
        lines.append(f"Error samples: {'; '.join(x[:60] for x in s['error_snippets'][:3])}")
    return "\n".join(lines)


def eval_text(record: dict, level: str) -> str:
    """The evaluation result a reflection reads (swe_bench.py's feedback levels), with the run's own failure."""
    if level == "minimal":
        return "FAILED"
    judge = record.get("judge") or {}
    limit = 500 if level == "standard" else 2000
    run = "" if record.get("status") in (None, "ok") else f" (run status {record.get('status')})"
    return f"FAILED{run}\nEval: {truncate(judge.get('feedback') or record.get('error'), limit)}"


# ---------- parsing a reply ----------

def parse_proposal(reply: str, task_id: str) -> dict | None:
    """swe_bench.py's `_parse_proposal`."""
    if "ACTION: NONE" in reply.upper():
        return None
    p = {"source_task": task_id, "topic": "general", "action": "NEW", "target": "", "name": "", "description": "",
         "content": ""}
    for line in reply.split("\n"):
        s = line.strip()
        u = s.upper()
        if u.startswith("TOPIC:"):
            p["topic"] = slug(s.split(":", 1)[1], MAX_TOPIC) or "general"
        elif u.startswith("ACTION:"):
            p["action"] = s.split(":", 1)[1].strip().upper()
        elif u.startswith("TARGET:"):
            p["target"] = s.split(":", 1)[1].strip()
        elif u.startswith("NAME:"):
            p["name"] = slug(s.split(":", 1)[1])
        elif u.startswith("DESCRIPTION:"):
            p["description"] = s.split(":", 1)[1].strip()[:150]
    at = reply.upper().find("CONTENT:")
    if at >= 0:
        lines = reply[at + len("CONTENT:"):].strip().split("\n")
        while lines and lines[-1].strip().upper().startswith(("TOPIC:", "ACTION:", "TARGET:", "NAME:", "DESCRIPTION:")):
            lines.pop()
        p["content"] = "\n".join(lines).strip()
    if p["action"] == "ENHANCE" and p["target"] and not p["name"]:
        p["name"] = slug(p["target"])
    p["name"] = slug(p["name"] or f"skill-{task_id}"[:26], MAX_NAME - len(p["topic"]) - len(SEP))
    if not p["content"]:
        return None
    action = reply.upper().find("ACTION:")
    p["analysis"] = (reply[:action] if action > 0 else reply).strip()
    return p


def fuzzy(raw: str, names) -> str | None:
    """`_execute_topic_curation`'s `_fuzzy`. An empty name matches nothing (upstream: the first name)."""
    clean = slug(raw)
    if not clean:
        return None
    if clean in names:
        return clean
    return next((n for n in names if clean in n or n in clean), None)


def clean_content(raw: str) -> str:
    """`_execute_general_curation`'s `_clean_content`: trailing commentary is not the skill's."""
    lines = raw.split("\n")
    while lines:
        last = lines[-1].strip().lower()
        if not last or last.startswith(("no other", "no new", "no additional", "no further", "no deletion",
                                        "no changes")) or (
                last.startswith("the ") and "skill" in last and any(w in last for w in ("valid", "covered", "needed"))):
            lines.pop()
        else:
            break
    return "\n".join(lines).strip()


def apply_topic_curation(text: str, proposals: list[dict], adapter: Path, topic: str, max_skills: int) -> dict:
    """swe_bench.py's `_execute_topic_curation`."""
    by_name = {p["name"]: p for p in proposals}
    existing = {d.name[len(topic) + len(SEP):]: read_skill(d)[0] for d in skill_dirs(adapter / TOPIC)
                if d.name.startswith(topic + SEP)}
    names, count = set(existing), len(existing)
    stats = {"added": 0, "merged": 0, "skipped": 0}

    def write(name, description, content):
        full = f"{topic}{SEP}{name}"
        write_skill(adapter / TOPIC / full, full, description, content)

    for line in text.split("\n"):
        s = line.strip()
        u = s.upper()
        if u.startswith("ACCEPT:"):
            name = fuzzy(s.split(":", 1)[1], set(by_name))
            if name and name not in names and count < max_skills:
                write(name, by_name[name]["description"], by_name[name]["content"])
                names.add(name)
                count += 1
                stats["added"] += 1
        elif u.startswith("MERGE:") and " INTO " in s.upper():
            parts = re.split(r" into ", s.split(":", 1)[1].strip(), maxsplit=1, flags=re.I)
            name, target = fuzzy(parts[0], set(by_name)), fuzzy(parts[1], names)
            after = text[text.find(s) + len(s):]
            if name and target and "NEW_CONTENT:" in after:
                content = after.split("NEW_CONTENT:", 1)[1]
                for marker in ("ACCEPT:", "MERGE:", "SKIP:", "NO_PROPOSALS"):
                    if marker in content:
                        content = content[:content.index(marker)]
                if content.strip():
                    write(target, existing.get(target) or by_name[name]["description"], content)
                    stats["merged"] += 1
        elif u.startswith("SKIP:"):
            stats["skipped"] += 1
    return stats


def apply_general_curation(text: str, adapter: Path, max_general: int) -> dict:
    """swe_bench.py's `_execute_general_curation`."""
    general = adapter / GENERAL
    existing = {d.name: read_skill(d)[0] for d in skill_dirs(general)}
    names, count = set(existing), len(existing)
    stats = {"added": 0, "updated": 0, "deleted": 0}
    lines = text.split("\n")

    def block(i):
        out = []
        while i < len(lines) and not lines[i].strip().upper().startswith(GENERAL_MARKERS):
            out.append(lines[i])
            i += 1
        return "\n".join(out).strip(), i

    def match(raw):
        name = slug(raw)
        return next((n for n in names if name and (name == n or name in n or n in name)), None)

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        u = s.upper()
        if u.startswith("NEW_GENERAL:"):
            name, description, content = slug(s.split(":", 1)[1]), "", ""
            i += 1
            while i < len(lines):
                sl = lines[i].strip()
                if sl.upper().startswith("DESCRIPTION:"):
                    description = sl.split(":", 1)[1].strip()[:150]
                elif sl.upper().startswith("CONTENT:"):
                    content, i = block(i + 1)
                    break
                i += 1
            if name and content and count < max_general:
                write_skill(general / name, name, description, clean_content(content))
                count += 1
                stats["added"] += 1
            continue
        if u.startswith("UPDATE_GENERAL:"):
            name, content = match(s.split(":", 1)[1]), ""
            i += 1
            while i < len(lines):
                sl = lines[i].strip().upper()
                if sl.startswith(("NEW_CONTENT:", "CONTENT:")):
                    content, i = block(i + 1)
                    break
                i += 1
            if name and content:
                write_skill(general / name, name, existing.get(name, ""), clean_content(content))
                stats["updated"] += 1
            continue
        if u.startswith("DELETE_GENERAL:"):
            name = match(s.split(":", 1)[1])
            if name and (general / name).exists():
                shutil.rmtree(general / name)
                names.discard(name)
                count -= 1
                stats["deleted"] += 1
        i += 1
    return stats


# ---------- the Evolver ----------

class EvoHarnessEvolver(Evolver):
    def bootstrap(self, ctx: StepContext) -> StepResult | None:
        """A starting adapter without the block (for example a task skill) gets the empty block after its text."""
        f = ctx.workspace.adapter / ADAPTER
        text = f.read_text(encoding="utf-8") if f.is_file() else ""
        if BEGIN in text:
            return None
        f.write_text(text.rstrip() + f"\n\n{BEGIN}\n\n{END}\n", encoding="utf-8")
        return StepResult("done", "added the empty skill block after the starting adapter's text")

    def step(self, ctx: StepContext) -> StepResult:
        ws, p = ctx.workspace, ctx.params
        if ctx.agent is None:
            return StepResult("failed", "evo_harness needs the run's model: set `llm` in the run config")
        f = ws.adapter / ADAPTER
        text = f.read_text(encoding="utf-8") if f.is_file() else ""
        if text.count(BEGIN) != 1 or text.count(END) != 1:
            return StepResult("failed", "ADAPTER.md has no usable skill block")
        success = float(p.get("success_score", 1.0))
        records = rollouts(ws.learning_material)
        failed = [(r, t) for r, t in records
                  if not (r.get("status") in (None, "ok") and float((r.get("judge") or {}).get("score") or 0) >= success)]
        step = {"step": ctx.progress.step if ctx.progress else 0, "tasks": len(records), "failed": len(failed)}
        backup = ws.scratch / "before"
        if backup.exists():
            shutil.rmtree(backup)
        shutil.copytree(ws.adapter, backup, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        calls = {"n": 0, "failed": 0}

        with ThreadPoolExecutor(max_workers=max(1, int(p.get("workers") or 1))) as pool:
            summaries = list(pool.map(lambda rt: self._reflect(ctx, rt[0], rt[1], calls), failed))
        if ctx.cancel is not None and ctx.cancel.is_set():
            return StepResult("failed", "stopped by the Trainer")
        proposals = [s["proposal"] for s in summaries if s.get("proposal")]
        step["proposals"] = [{k: x[k] for k in ("source_task", "topic", "action", "name")} for x in proposals]

        step["topics"] = self._compile_topics(ctx, proposals, calls)
        step["general"] = self._compile_general(ctx, summaries, calls)

        f.write_text(with_block(text, render_block(ws.adapter)), encoding="utf-8")
        check = ctx.tools["check_spec"](path=str(ws.adapter)) if "check_spec" in ctx.tools else {"passed": True}
        if not check.get("passed", True):
            for name in (GENERAL, TOPIC, ADAPTER):
                target = ws.adapter / name
                shutil.rmtree(target) if target.is_dir() else target.unlink(missing_ok=True)
                if (backup / name).is_dir():
                    shutil.copytree(backup / name, target, symlinks=True)
                elif (backup / name).exists():
                    shutil.copy2(backup / name, target)
            issues = [i.get("problem", str(i))[:200] for i in check.get("issues", [])[:3]]
            step["outcome"] = "rolled back: the updated harness failed the template check: " + "; ".join(issues)
            self._save(ws, step)
            return StepResult("failed", step["outcome"][:300])

        step["general_skills"] = len(skill_dirs(ws.adapter / GENERAL))
        step["topic_skills"] = len(skill_dirs(ws.adapter / TOPIC))
        step["outcome"] = (f"{len(records)} task(s), {len(failed)} failed; {len(proposals)} proposal(s); harness: "
                           f"{step['general_skills']} general, {step['topic_skills']} topic skill(s)"
                           + (f"; {calls['failed']} learner call(s) failed" if calls["failed"] else ""))
        self._save(ws, step)
        return StepResult("done", step["outcome"][:300], extra={"llm_calls": calls["n"], "failed": len(failed)})

    # ---------- reflect: the per-task part of swe_bench.py's _solve_one_task ----------

    def _reflect(self, ctx, record: dict, traj: Path | None, calls: dict) -> dict:
        p = ctx.params
        level = str(p.get("feedback_level") or "standard")
        tid = str(record.get("id"))
        trace, source = reasoning_trace(record, traj)
        steps = trajectory.log_steps(traj) if traj is not None and traj.exists() else None
        predicted = answer_text(record.get("output"))
        sig = format_signals(signals(steps), bool(predicted))
        summary = {"task_id": tid, "signals": sig, "trace": trace, "eval": eval_text(record, level)}
        if source == "none" or (ctx.cancel is not None and ctx.cancel.is_set()):
            return summary                                 # upstream: no conversation, no proposal
        prompt = ATTEMPT.format(question=clip_middle(task_text(record.get("input")), int(p.get("question_chars") or 6000)),
                                predicted=predicted or "(none)") + PROPOSE_FAIL.format(
            eval_result=summary["eval"], judge_verdict=NO_JUDGE, trajectory_signals=sig,
            compressed_trajectory=clip_middle(trace, int(p.get("trace_chars") or 12000)),
            existing_skills_section=self._skills_section(ctx.workspace.adapter))
        reply = self._ask(ctx, prompt, f"{tid}.reflect", calls)
        proposal = parse_proposal(reply, tid) if reply else None
        if proposal:
            summary["analysis"] = proposal.pop("analysis")
            summary["proposal"] = proposal
            summary["proposal_summary"] = f"[{proposal['action']}] {proposal['name']}: {proposal['description']}"
        return summary

    @staticmethod
    def _skills_section(adapter: Path) -> str:
        rows = [f"- **{d.name}** [general]: {read_skill(d)[0]}" for d in skill_dirs(adapter / GENERAL)]
        for d in skill_dirs(adapter / TOPIC):
            topic, _, name = d.name.partition(SEP)
            rows.append(f"- **{name}** [{topic}]: {read_skill(d)[0]}")
        return "Current skills:\n" + "\n".join(rows) if rows else "No existing skills yet."

    # ---------- compile ----------

    def _compile_topics(self, ctx, proposals: list[dict], calls: dict) -> dict:
        """evolve_batch's per-topic curation, `_curate_topic_proposals` for each topic."""
        limit = int(ctx.params.get("max_skills_per_topic", 5))
        out = {}
        if limit <= 0:
            return out
        by_topic = defaultdict(list)
        for x in proposals:
            by_topic[x["topic"]].append(x)
        for topic, items in by_topic.items():
            adapter = ctx.workspace.adapter
            existing = [(d.name[len(topic) + len(SEP):], read_skill(d)[0]) for d in skill_dirs(adapter / TOPIC)
                        if d.name.startswith(topic + SEP)]
            prompt = CURATOR.format(
                topic=topic, n_skills=len(existing), max_skills=limit,
                existing_skills_list="\n".join(f"- **{n}**: {d}" for n, d in existing) or "(empty)",
                proposals_list="\n\n".join(
                    f"### [{x['action']}] {x['name']}\n  Source: {x['source_task']}\n"
                    f"  Description: {x['description'][:150]}\n  Content: {truncate(x['content'], 300)}" for x in items))
            reply = self._ask(ctx, prompt + "\n\nReview and decide.", f"curate.{topic}", calls)
            out[topic] = apply_topic_curation(reply, items, adapter, topic, limit) if reply else {"failed": True}
        return out

    def _compile_general(self, ctx, summaries: list[dict], calls: dict) -> dict:
        """evolve_batch's general curator, `_curate_general_skills`: only with two failures or more."""
        limit = int(ctx.params.get("max_general_skills", 5))
        if limit <= 0 or len(summaries) < 2:
            return {}
        adapter = ctx.workspace.adapter
        existing = [(d.name, read_skill(d)[0]) for d in skill_dirs(adapter / GENERAL)]
        prompt = GENERAL_CURATOR.format(
            analysis_heading="Failed Task Analysis", n_tasks=len(summaries),
            task_summaries="\n\n".join(self._summary(s, str(ctx.params.get("feedback_level") or "standard"))
                                       for s in summaries[:30]),
            n_general=len(existing), max_general=limit,
            general_skills_list="\n".join(f"- **{n}**: {d}" for n, d in existing) or "(empty)")
        reply = self._ask(ctx, prompt + "\n\nAnalyze and decide.", "curate.general", calls)
        return apply_general_curation(reply, adapter, limit) if reply else {"failed": True}

    @staticmethod
    def _summary(s: dict, level: str) -> str:
        """A failure as `_curate_general_skills` shows it, by feedback level. Upstream's judge line is the evaluation
        result here (no judge call): at `minimal` neither."""
        parts = [f"### {s['task_id']} [FAIL]"]
        if level != "minimal":
            parts.append(f"Judge: {s['eval']}")
        parts.append(f"Signals: {s['signals']}")
        if level != "minimal":
            limit = 1000 if level == "full" else 400
            parts.append(f"Trajectory:\n{clip_middle(s['trace'], limit)}")
            if s.get("analysis"):
                parts.append(f"Analysis:\n{truncate(s['analysis'], limit)}")
            if s.get("proposal_summary"):
                parts.append(f"Proposal: {truncate(s['proposal_summary'], 200)}")
        return "\n".join(parts)

    # ---------- the model and the records ----------

    def _ask(self, ctx, prompt: str, name: str, calls: dict) -> str:
        """One learner call through the run's model. A failed call returns "" and the caller skips, as upstream."""
        p = ctx.params
        out = ctx.workspace.output / "calls"
        out.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.-]", "_", name)                # a task id may hold a path separator
        (out / f"{name}.prompt.md").write_text(prompt, encoding="utf-8")
        calls["n"] += 1
        try:
            sampling = {k: p[k] for k in ("max_tokens", "temperature") if p.get(k) is not None}
            text = str(ask(ctx.agent, [{"role": "user", "content": prompt}], **sampling).get("content") or "")
        except BudgetExceeded:
            raise
        except Exception as exc:                          # noqa: BLE001 — as upstream, that call's update is skipped
            text = ""
            calls["failed"] += 1
            (out / f"{name}.error.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        (out / f"{name}.reply.md").write_text(text, encoding="utf-8")
        return text

    @staticmethod
    def _save(ws, step: dict) -> None:
        f = ws.state / "evo_harness.json"
        state = json.loads(f.read_text()) if f.is_file() else {"steps": []}
        state["steps"].append(step)
        f.write_text(json.dumps(state, ensure_ascii=False, indent=1, default=str))
        (ws.output / "report.md").write_text(
            f"# Evo-Harness, step {step['step']}\n\n{step['outcome']}\n\n"
            f"Topics: {json.dumps(step['topics'])}\n\nGeneral: {json.dumps(step['general'])}\n", encoding="utf-8")
