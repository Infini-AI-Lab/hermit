"""ACEEvolver: ACE, Agentic Context Engineering (Zhang et al., ICLR 2026, arXiv:2510.04618; ace-agent/ace,
`ace/ace.py::ACE._train_single_sample` and `_offline_train`), as an Evolver on the `ace` template.

ACE keeps a playbook, an itemised context of bullets `[id] helpful=N harmful=N :: content` under sections, and grows
it by incremental deltas instead of rewriting it: a Generator answers with the playbook and names the bullets it used,
a Reflector diagnoses the attempt and tags those bullets helpful, harmful or neutral (the counters are updated without
a model), and a Curator proposes ADD operations that are merged into the playbook deterministically.

On the framework's step model the Generator is the acting agent: the Trainer's rollouts of the step are its
attempts, with ADAPTER.md (the playbook after the generator instructions) as its context. The step then does, for
each rollout in turn, what `_train_single_sample` does after its first generation:

1. collect the bullet ids the agent named (the Generator's regex fallback over its reply);
2. if the judge scored the answer wrong: up to `max_num_rounds` rounds of reflect -> update counters -> run the agent
   again on the task with the reflection appended to its context (run_agent), until it is right; if right: reflect
   once, to tag the bullets;
3. every `curator_frequency` samples, the Curator, from the last reflection; its ADD operations are applied;
4. optionally the BulletpointAnalyzer: embedding similarity >= threshold groups bullets, a model call merges each.

Samples are processed one after another, so with `data.step_size: 1` a step is exactly upstream's per-sample loop;
with a larger step the rollouts of a step share one playbook version (the later ones did not see the earlier
updates), while reflection and curation stay sequential. Validation (`val_every`) scores the playbook as
`_offline_train` does and keeps the best one; finalize hands back the best validation playbook, or the current one
when no validation ran.

Training State: `state/ace.json` (sample counters, the next bullet id, one record per step, validation scores) and
`state/best_playbook.md`; each step's `output/` holds the prompts and replies and a `report.md`.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from hermit.algorithms.evolvers.ace import playbook as pb
from hermit.algorithms.evolvers.ace.prompts import (
    CURATOR_PROMPT,
    CURATOR_PROMPT_NO_GT,
    MERGE_PROMPT,
    REFLECTOR_PROMPT_NO_GT,
)
from hermit.evolver import codex_learner
from hermit.evolver.base import Evolver, FinalResult, StepContext, StepResult, Usage
from hermit.evolver.dependencies import missing_dependency
from hermit.trainer.budget import BudgetExceeded

ADAPTER = "ADAPTER.md"
CORRECT = "Predicted answer matches ground truth"           # _train_single_sample's environment feedback
WRONG = "Predicted answer does not match ground truth"


# ---------- reading a rollout ----------

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


def log_text(root: Path | None) -> str:
    """A run's log as text: a file, or the text files at the top of a directory. Subdirectories are left out (a
    harness may keep the agent's whole workspace there, task material included), and so are the prompts and the
    adapter's own copies."""
    if root is None or not root.exists():
        return ""
    files = [root] if root.is_file() else sorted(p for p in root.iterdir() if p.is_file() and not p.is_symlink())
    parts = []
    for f in files:
        if f.name.endswith("_prompt.txt") or f.name in (ADAPTER, "SKILL.md", "AGENTS.md") or f.stat().st_size > 2_000_000:
            continue
        try:
            parts.append(f"--- {f.name} ---\n" + f.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, OSError):
            continue
    return "\n".join(parts)


def reasoning_trace(record: dict, traj: Path | None) -> str:
    """What the Reflector reads as the Generator's reasoning: the run's messages, else its log."""
    text = messages_text(record.get("messages") or [])
    if not text:
        text = log_text(traj)
    if record.get("status") not in (None, "ok"):
        text += f"\n[run status {record.get('status')}] {str(record.get('error') or '')[:2000]}"
    return text or "(no trajectory recorded)"


# ---------- the Evolver ----------

class ACEEvolver(Evolver):
    def __init__(self) -> None:
        self._embedder = None
        self._usage: list[Usage] = []      # the codex learner's sessions (the llm tool meters its own calls)

    def bootstrap(self, ctx: StepContext) -> StepResult | None:
        """A starting adapter without a playbook (for example a task skill) gets ACE's empty playbook after it."""
        f = ctx.workspace.adapter / ADAPTER
        text = f.read_text(encoding="utf-8") if f.is_file() else ""
        if pb.has_playbook(text):
            return None
        f.write_text(pb.with_playbook(text), encoding="utf-8")
        return StepResult("done", "added ACE's empty playbook after the starting adapter's text")

    def step(self, ctx: StepContext) -> StepResult:
        ws = ctx.workspace
        self._usage = []
        learner = ctx.params.get("learner") or "llm"
        if learner == "llm" and "llm" not in ctx.tools:
            return StepResult("failed", "ace needs the run's llm tool: set `llm` in the run config "
                                        "(or evolver.params.learner: codex)")
        if learner == "codex" and not (ctx.params.get("learner_container") or {}).get("image") \
                and not shutil.which("codex"):
            return StepResult("failed", "learner: codex needs the codex CLI on PATH")
        if learner == "codex" and codex_learner.unconfined(ctx):
            return StepResult("failed", codex_learner.unconfined(ctx))
        if learner not in ("llm", "codex"):
            return StepResult("failed", f"unknown learner {learner!r}: llm or codex")
        f = ws.adapter / ADAPTER
        text = f.read_text(encoding="utf-8") if f.is_file() else ""
        problems = pb.playbook_problems(text)
        if problems:
            return StepResult("failed", "ADAPTER.md has no usable ACE playbook: " + "; ".join(problems[:2]))
        prefix, playbook, suffix = pb.split_adapter(text)
        state = self._state(ws)
        state.setdefault("initial_playbook", playbook)
        progress = ctx.progress
        epoch = progress.epoch if progress else 1
        if state["epoch"] != epoch:
            state["epoch_sizes"][str(state["epoch"])] = state["index"]
            state["epoch"], state["index"], state["last_val_index"] = epoch, 0, 0
        state["next_id"] = max(int(state.get("next_id") or 1), pb.get_next_global_id(playbook))

        records = self._records(ws.learning_material)
        step_record = {"step": progress.step if progress else 0, "epoch": epoch, "samples": [],
                       "bullets_before": len(pb.bullet_ids_of(playbook)), "chars_before": len(playbook)}
        calls = {"n": 0, "failed": 0}
        for record, traj in records:
            if ctx.cancel is not None and ctx.cancel.is_set():
                return StepResult("failed", "stopped by the Trainer", usage=self._usage)
            state["index"] += 1
            sample = self._sample(ctx, record, traj, playbook, prefix, suffix, state, calls)
            playbook = sample.pop("playbook")
            step_record["samples"].append(sample)

        new_text = pb.join_adapter(prefix, playbook, suffix)
        f.write_text(new_text, encoding="utf-8")
        check = ctx.tools["check_spec"](path=str(ws.adapter)) if "check_spec" in ctx.tools else {"passed": True}
        if not check.get("passed", True):
            f.write_text(text, encoding="utf-8")
            issues = [i.get("problem", str(i))[:200] for i in check.get("issues", [])[:3]]
            step_record["outcome"] = "rolled back: the updated playbook failed the template check: " + "; ".join(issues)
            self._save(ws, state, step_record)
            return StepResult("failed", step_record["outcome"][:300], usage=self._usage)

        step_record |= {"bullets_after": len(pb.bullet_ids_of(playbook)), "chars_after": len(playbook),
                        "stats": pb.get_playbook_stats(playbook)}
        val = self._maybe_validate(ctx, state, playbook, progress)
        if val is not None:
            step_record["validation"] = val
        added = sum(len(s.get("added", [])) for s in step_record["samples"])
        success = float(ctx.params.get("success_score", 1.0))
        right = sum(1 for s in step_record["samples"] if float(s.get("score") or 0.0) >= success)
        step_record["outcome"] = (f"{len(records)} sample(s), {right} right before reflection; {added} bullet(s) added;"
                                  f" playbook {step_record['bullets_before']} -> {step_record['bullets_after']} bullets"
                                  + (f"; validation {val['score']:.3f}" + (" (new best)" if val["best"] else "")
                                     if val is not None else "")
                                  + (f"; {calls['failed']} learner call(s) failed" if calls["failed"] else ""))
        self._save(ws, state, step_record)
        return StepResult("done", step_record["outcome"][:300], usage=self._usage,
                          extra={"added": added, "bullets": step_record["bullets_after"], "llm_calls": calls["n"]})

    def finalize(self, ctx) -> FinalResult:
        ws = ctx.workspace
        auth = ws.state.parent / "agent_home" / "auth.json"
        if auth.is_file():                     # the codex learner's copy of the login, as AgentEvolver.finalize
            auth.unlink()
        state = self._state(ws)
        f = ws.adapter / ADAPTER
        best_file = ws.state / "best_playbook.md"
        self._keep_last(ws)
        if not state.get("validations") or not f.is_file():
            return FinalResult(adapter=ws.adapter, summary="no validation ran: the current playbook")
        prefix, _, suffix = pb.split_adapter(f.read_text(encoding="utf-8"))
        best = state["best"]
        # As _offline_train: the best starts as the initial playbook at accuracy 0, so validations that all scored 0
        # hand back the initial playbook.
        chosen = best_file.read_text(encoding="utf-8") if best_file.is_file() else state["initial_playbook"]
        f.write_text(pb.join_adapter(prefix, chosen, suffix), encoding="utf-8")
        return FinalResult(adapter=ws.adapter, summary=f"the best validation playbook ({best['source'] or 'initial'}, "
                           f"validation {best['score']:.3f}; {len(state['validations'])} validation(s))",
                           extra={"best": best})

    @staticmethod
    def _keep_last(ws) -> None:
        """The adapter as training left it, before finalize picks the best validation playbook: a complete adapter
        directory (state/last_adapter) that `hermit eval --adapter <path>` can evaluate beside `final`. With a
        validation split as small as OfficeQA's 24 questions, selecting on it is mostly noise, so both are worth
        evaluating."""
        last = ws.state / "last_adapter"
        if last.exists():
            shutil.rmtree(last)
        shutil.copytree(ws.adapter, last, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))

    # ---------- one sample: ACE._train_single_sample after its first generation ----------

    def _sample(self, ctx, record, traj, playbook, prefix, suffix, state, calls) -> dict:
        p = ctx.params
        feedback = bool(p.get("feedback", True))
        success = float(p.get("success_score", 1.0))
        tid = record.get("id")
        question = clip_middle(task_text(record.get("input")), int(p.get("question_chars") or 6000))
        trace = reasoning_trace(record, traj)
        predicted = answer_text(record.get("output"))
        judge = record.get("judge") or {}
        correct = feedback and record.get("status") in (None, "ok") and float(judge.get("score") or 0.0) >= success
        cited = pb.cited_bullet_ids(trace + "\n" + predicted, playbook)
        # `score` is kept for the record only; without `feedback` the Reflector and the Curator never see it.
        out = {"id": tid, "score": judge.get("score"), "correct_before": bool(correct), "cited": cited, "rounds": []}

        rounds = int(p.get("max_num_rounds", 3) or 0)
        can_retry = feedback and not correct and rounds > 0 and "run_agent" in ctx.tools
        reflection = "(empty)"
        if can_retry:
            for r in range(rounds):
                if ctx.cancel is not None and ctx.cancel.is_set():
                    break
                reflection, tags = self._reflect(ctx, question, trace, predicted, self._env(record, correct, feedback),
                                                 pb.extract_playbook_bullets(playbook, cited), f"{tid}.round{r}", calls)
                playbook = pb.update_bullet_counts(playbook, tags)
                retry = self._retry(ctx, tid, prefix, playbook, suffix, reflection)
                trace, predicted = retry["trace"], retry["predicted"]
                record = retry["record"]
                cited = pb.cited_bullet_ids(trace + "\n" + predicted, playbook)
                correct = retry["correct"]
                out["rounds"].append({"tags": tags, "score": retry["score"], "cited": cited, "error": retry.get("error")})
                if correct or retry.get("error"):
                    break
        else:
            reflection, tags = self._reflect(ctx, question, trace, predicted, self._env(record, correct, feedback),
                                             pb.extract_playbook_bullets(playbook, cited), f"{tid}.reflect", calls)
            playbook = pb.update_bullet_counts(playbook, tags)
            out["tags"] = tags
        out["correct_after_rounds"] = bool(correct)

        frequency = max(int(p.get("curator_frequency") or 1), 1)
        if state["index"] % frequency == 0:
            playbook, cur = self._curate(ctx, playbook, reflection, question, state, feedback, f"{tid}.curate", calls)
            out |= cur
        if p.get("use_bulletpoint_analyzer"):
            playbook, merged = self._dedup(ctx, playbook, f"{tid}.dedup", calls)
            out["dedup"] = merged
        out["playbook"] = playbook
        return out

    @staticmethod
    def _env(record: dict, correct: bool, feedback: bool) -> str:
        """The Reflector's environment feedback. With `feedback`, upstream's verdict string and the judge's own
        feedback (on OfficeQA it names the expected answer); without, only what the run itself reports."""
        status = record.get("status")
        run = "" if status in (None, "ok") else f"The run ended with status {status}: {str(record.get('error'))[:500]}"
        if not feedback:
            return run or "(no environment feedback)"
        judge = record.get("judge") or {}
        lines = [CORRECT if correct else WRONG]
        if judge.get("feedback"):
            lines.append(f"Judge feedback: {judge['feedback']}")
        if run:
            lines.append(run)
        return "\n".join(lines)

    def _reflect(self, ctx, question, trace, predicted, env, bullets, name, calls) -> tuple[str, list]:
        """Reflector.reflect with REFLECTOR_PROMPT_NO_GT: the framework passes no reference answer of its own; with
        `feedback` the judge's verdict reaches the Reflector as environment feedback."""
        limit = int(ctx.params.get("trace_chars") or 12000)
        prompt = REFLECTOR_PROMPT_NO_GT.format(question, clip_middle(trace, limit), predicted, env, bullets)
        response = self._ask(ctx, prompt, name, calls)
        tags = pb.extract_bullet_tags(response)
        return response or "(empty)", tags

    def _retry(self, ctx, tid, prefix, playbook, suffix, reflection) -> dict:
        """The Generator again, with the reflection: the agent runs the task with the reflection after the playbook."""
        cand = ctx.workspace.scratch / "retry"
        if cand.exists():
            shutil.rmtree(cand)
        shutil.copytree(ctx.workspace.adapter, cand, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        text = pb.join_adapter(prefix, playbook, suffix).rstrip() + (
            "\n\n# Reflection\n\nA diagnosis of your previous attempt at this task:\n\n" + reflection.strip() + "\n")
        (cand / ADAPTER).write_text(text, encoding="utf-8")
        try:
            res = ctx.tools["run_agent"](task_ids=[tid], adapter=str(cand))["results"][0]
        except BudgetExceeded:
            raise
        except Exception as exc:                          # noqa: BLE001 — the sample keeps its reflection
            return {"record": {"id": tid}, "trace": "", "predicted": "", "correct": False, "score": None,
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        judge = res.get("judge") or {}
        score = float(judge.get("score") or 0.0)
        log = Path(res["log"]) if res.get("log") else None
        rec = {"id": tid, "status": res.get("status"), "output": res.get("output"), "judge": judge}
        return {"record": rec, "trace": log_text(log) or "(no trajectory recorded)",
                "predicted": answer_text(res.get("output")), "score": score,
                "correct": res.get("status") in (None, "ok") and score >= float(ctx.params.get("success_score", 1.0))}

    def _curate(self, ctx, playbook, reflection, question, state, feedback, name, calls) -> tuple[str, dict]:
        """Curator.curate: one call; a reply that does not validate leaves the playbook as it was."""
        p = ctx.params
        total = p.get("total_samples") or state["epoch_sizes"].get("1") or "unknown"
        prompt = (CURATOR_PROMPT if feedback else CURATOR_PROMPT_NO_GT).format(
            token_budget=int(p.get("playbook_token_budget") or 80000), current_step=state["index"],
            total_samples=total, playbook_stats=json.dumps(pb.get_playbook_stats(playbook), indent=2),
            recent_reflection=reflection, current_playbook=playbook, question_context=question)
        response = self._ask(ctx, prompt, name, calls)
        if not response:
            return playbook, {"curator": "skipped: empty response"}
        try:
            info = pb.extract_and_validate_operations(response)
        except ValueError as exc:
            return playbook, {"curator": f"skipped: {exc}"}
        ops = info["operations"]
        new, state["next_id"], added = pb.apply_curator_operations(playbook, ops, int(state["next_id"]))
        ignored = [str(op.get("type")) for op in ops if op.get("type") != "ADD"]
        return new, {"curator": "applied", "added": added, "ignored_operations": ignored}

    def _dedup(self, ctx, playbook, name, calls) -> tuple[str, dict]:
        """BulletpointAnalyzer.analyze(merge=True)."""
        bullets = [pb.parse_playbook_line(l) for l in playbook.split("\n")]
        bullets = [b for b in bullets if b]
        if len(bullets) < 2:
            return playbook, {"groups": 0}
        vectors = self._embed(ctx, [b["content"] for b in bullets])
        groups = pb.find_similar_groups(vectors, float(ctx.params.get("bulletpoint_analyzer_threshold") or 0.90))
        merged = {}
        for k, g in enumerate(groups):
            group = [bullets[i] for i in g]
            h, x = sum(b["helpful"] for b in group), sum(b["harmful"] for b in group)
            prompt = MERGE_PROMPT.format(
                bullets_text="\n".join(f"{i + 1}. {pb.format_playbook_line(b['id'], b['helpful'], b['harmful'], b['content'])}"
                                       for i, b in enumerate(group)),
                base_id=group[0]["id"], total_helpful=h, total_harmful=x)
            reply = self._ask(ctx, prompt, f"{name}.{k}", calls, temperature=0.3).strip()
            m = pb.MERGED_RX.match(reply)
            # Upstream keeps the id and counts the model wrote; here the first id and the summed counts are kept
            # (the prompt asks for exactly those), so a merge can never duplicate an id.
            merged[g[0]] = pb.format_playbook_line(group[0]["id"], h, x, pb.clean_content(m.group(4))) if m else None
        return pb.merge_groups(playbook, groups, merged), {
            "groups": len(groups), "removed": sum(len(g) - 1 for g in groups),
            "failed_merges": sum(1 for v in merged.values() if v is None)}

    def _embed(self, ctx, texts: list[str]) -> list[list[float]]:
        """sentence-transformers embeddings, L2-normalised (upstream normalises with faiss)."""
        if self._embedder is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                raise missing_dependency("ace", "sentence-transformers for use_bulletpoint_analyzer", "ace") from None
            self._embedder = SentenceTransformer(str(ctx.params.get("embedding_model") or "all-mpnet-base-v2"))
        vectors = self._embedder.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        out = []
        for v in vectors:
            norm = float(sum(float(x) * float(x) for x in v)) ** 0.5 or 1.0
            out.append([float(x) / norm for x in v])
        return out

    # ---------- validation: _offline_train's periodic evaluation ----------

    def _maybe_validate(self, ctx, state, playbook, progress) -> dict | None:
        p = ctx.params
        every = p.get("val_every", "epoch")
        if ctx.access.validation is None or "run_agent" not in ctx.tools or every in (None, 0, "0", "never"):
            return None
        due = bool(progress and progress.is_epoch_end) if every == "epoch" else \
            (state["index"] // int(every) > state.get("last_val_index", 0) // int(every))
        if not due:
            return None
        if p.get("score_initial") and not state["validations"]:
            initial = state["initial_playbook"]
            score = self._val_score(ctx, ctx.workspace.adapter, initial)
            state["validations"].append({"step": 0, "epoch": 0, "score": score, "source": "initial"})
            self._keep_best(ctx.workspace, state, initial, score, "initial")
        score = self._val_score(ctx, ctx.workspace.adapter, playbook)
        source = f"epoch {state['epoch']} sample {state['index']}"
        state["validations"].append({"step": progress.step if progress else 0, "epoch": state["epoch"],
                                     "index": state["index"], "score": score, "source": source})
        state["last_val_index"] = state["index"]
        best = self._keep_best(ctx.workspace, state, playbook, score, source)
        return {"score": score, "best": best}

    def _val_score(self, ctx, adapter: Path, playbook: str) -> float:
        cand = ctx.workspace.scratch / "validate"
        if cand.exists():
            shutil.rmtree(cand)
        shutil.copytree(adapter, cand, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        prefix, _, suffix = pb.split_adapter((cand / ADAPTER).read_text(encoding="utf-8"))
        (cand / ADAPTER).write_text(pb.join_adapter(prefix, playbook, suffix), encoding="utf-8")
        ids = [json.loads(l)["id"] for l in (ctx.access.validation / "tasks.jsonl").read_text().splitlines() if l]
        results = ctx.tools["run_agent"](task_ids=ids, adapter=str(cand))["results"]
        return sum(float((r.get("judge") or {}).get("score") or 0.0) for r in results) / max(len(results), 1)

    @staticmethod
    def _keep_best(ws, state, playbook: str, score: float, source: str) -> bool:
        """_offline_train keeps a playbook whose validation accuracy is strictly higher than the best so far, which
        starts at 0."""
        if score > float(state["best"]["score"]):
            state["best"] = {"score": score, "source": source}
            (ws.state / "best_playbook.md").write_text(playbook, encoding="utf-8")
            return True
        return False

    # ---------- the model, the records, the state ----------

    def _ask(self, ctx, prompt: str, name: str, calls: dict, temperature: float | None = None) -> str:
        """One Reflector, Curator or merge call: the run's `llm` tool, or (`learner: codex`) one `codex exec`
        session. A failed call returns "" and the caller skips, as upstream."""
        p = ctx.params
        out = ctx.workspace.output / "calls"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{name}.prompt.md").write_text(prompt, encoding="utf-8")
        calls["n"] += 1
        try:
            if (p.get("learner") or "llm") == "codex":
                text = self._codex(ctx, prompt, out / name)
            else:
                sampling = {k: p[k] for k in ("max_tokens", "temperature") if p.get(k) is not None}
                if temperature is not None:
                    sampling["temperature"] = temperature
                reply = ctx.tools["llm"](messages=[{"role": "user", "content": prompt}], **sampling)
                text = str(reply.get("content") or "")
        except BudgetExceeded:
            raise
        except Exception as exc:                          # noqa: BLE001 — as upstream, the call's step is skipped
            text = ""
            calls["failed"] = calls.get("failed", 0) + 1
            (out / f"{name}.error.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        (out / f"{name}.reply.md").write_text(text, encoding="utf-8")
        return text

    def _codex(self, ctx, prompt: str, stem: Path) -> str:
        """The prompt as one confined `codex exec` session under the user's Codex login (hermit.evolver.codex_learner).
        Temperature and max_tokens do not apply."""
        text, usage = codex_learner.ask(ctx, prompt, stem)
        self._usage += usage
        return text

    @staticmethod
    def _records(material: Path) -> list[tuple[dict, Path | None]]:
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

    @staticmethod
    def _state(ws) -> dict:
        f = ws.state / "ace.json"
        state = json.loads(f.read_text()) if f.is_file() else {}
        state.setdefault("epoch", 1)
        state.setdefault("index", 0)
        state.setdefault("epoch_sizes", {})
        state.setdefault("next_id", 1)
        state.setdefault("steps", [])
        state.setdefault("validations", [])
        state.setdefault("best", {"score": 0.0, "source": None})
        return state

    @staticmethod
    def _save(ws, state: dict, step_record: dict) -> None:
        state["steps"].append(step_record)
        (ws.state / "ace.json").write_text(json.dumps(state, ensure_ascii=False, indent=1, default=str))
        lines = [f"# ACE, step {step_record['step']} (epoch {step_record['epoch']})", "", step_record["outcome"], ""]
        for s in step_record["samples"]:
            lines.append(f"- {s['id']}: right before reflection {s['correct_before']}, after rounds "
                         f"{s.get('correct_after_rounds')}; cited {', '.join(s['cited']) or 'none'}; "
                         f"{len(s.get('rounds', []))} retry round(s); curator {s.get('curator', 'not run')}; "
                         f"added {', '.join(a['id'] for a in s.get('added', [])) or 'none'}")
        (ws.output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
