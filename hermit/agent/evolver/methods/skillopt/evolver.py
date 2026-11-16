"""SkillOptEvolver: SkillOpt (Yang et al., 2026; github.com/microsoft/SkillOpt), one SkillOpt step per training step.

The skill is one learnable file (`file`, ADAPTER.md by default). The step's rollouts are SkillOpt's rollout of the
current skill on a training batch (data.step_size is its batch_size, trainer.epochs its num_epochs). The step runs the
rest of a SkillOpt step with SkillOpt's own functions: minibatch reflection on failures and successes, hierarchical
patch merging, edit ranking under the textual learning rate, bounded edits, and the validation gate (run_agent on the
validation tasks), with the epoch's earlier steps and rejected edits as context. The last step of an epoch also runs
SkillOpt's slow update and meta skill. finalize scores the final skill on validation and hands back the best skill.

SkillOpt's model calls go through the run's metered `llm` tool.
Training State (state/skillopt.json): current and best skill with their scores, the validation cache, the epoch's
step buffer, the skill at the end of each epoch, the meta skill of each epoch, one record per step.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import random
import re
import shutil

from hermit.agent.base import trajectory
from hermit.agent.evolver.base import Evolver, ask, FinalResult, StepContext, StepResult
from hermit.agent.evolver.dependencies import missing_dependency


def task_text(task: dict) -> str:
    fields = {k: v for k, v in task.items() if k not in ("id", "task_id")}
    if len(fields) == 1:
        return str(next(iter(fields.values())))
    return json.dumps(fields, ensure_ascii=False)


def _clip(text: str, limit: int) -> str:
    return text if limit <= 0 or len(text) <= limit else text[:limit] + f" …[{len(text) - limit} characters left out]"


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content or "")


def conversation(record: dict, step_dir: Path, obs_chars: int = 0) -> tuple[list[dict], str, str]:
    """A material record as SkillOpt's reflection reads it: the run's entries (tool calls with their results, and
    messages), the agent's system prompt and its first user prompt. A pipeline that already writes SkillOpt's
    prediction files (conversation.json, target_*_prompt.txt) in its log is passed through unchanged; a host agent's
    log (a Codex or Claude Code stream, hermit.agent.base.trajectory) gives its steps and its prompt; otherwise the entries come
    from the returned messages. The run's outcome comes last."""
    root = step_dir / record["trajectory"] if record.get("trajectory") else None
    if root is not None and (root / "conversation.json").is_file():
        entries = json.loads((root / "conversation.json").read_text())
        prompts = [(root / f).read_text() if (root / f).is_file() else "" for f in ("target_system_prompt.txt",
                                                                                  "target_user_prompt.txt")]
        for e in entries:
            if isinstance(e, dict) and e.get("type") == "tool_call":
                e["obs"] = _clip(str(e.get("obs") or ""), obs_chars)
        return entries, prompts[0], prompts[1]
    steps = trajectory.log_steps(root) if root is not None else None
    if steps is not None and not record.get("messages"):
        out, system = host_entries(steps, obs_chars), ""
        user = (root / "prompt.txt").read_text(errors="replace") if (root / "prompt.txt").is_file() else ""
        return out + outcome(record), system, user
    out, calls, system, user = [], {}, "", ""
    for m in record.get("messages") or []:
        role, content = m.get("role"), _text(m.get("content"))
        if role == "system" and not system:
            system = content
            continue
        if role == "user" and not user:
            user = content
            continue
        if role == "tool" and m.get("tool_call_id") in calls:
            out[calls[m["tool_call_id"]]]["obs"] = _clip(content, obs_chars)
            continue
        if content:
            out.append({"role": role, "content": content})
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            calls[call.get("id")] = len(out)
            out.append({"type": "tool_call", "cmd": f"{fn.get('name')}({fn.get('arguments')})", "obs": ""})
    if not out and root is not None:
        text = "\n".join(f.read_text(errors="replace") for f in sorted(root.rglob("*")) if f.is_file())
        out.append({"role": "assistant", "content": text})
    return out + outcome(record), system, user


def outcome(record: dict) -> list[dict]:
    """The run's outcome, after its entries."""
    score = float(record["judge"]["score"])
    return [{"role": "assistant", "content": f"Final answer: {record.get('output') or '(none)'}"},
            {"role": "system", "content": f"Run status: {record.get('status')}; score: {score:g}"}]


def host_entries(steps: list, obs_chars: int) -> list[dict]:
    """A host agent's steps as SkillOpt's entries: each command (or other tool call) with its output, each message;
    the reasoning is left out, as SkillOpt's own harness keeps none."""
    out = []
    for s in steps:
        if s.kind == "command":
            out.append({"type": "tool_call", "cmd": s.command, "obs": _clip(s.output, obs_chars)})
        elif s.kind in ("tool_call", "file_change"):
            out.append({"type": "tool_call", "cmd": s.text, "obs": ""})
        elif s.kind == "message":
            out.append({"role": "assistant", "content": s.text})
        elif s.kind == "error":
            out.append({"role": "assistant", "content": f"[error] {s.text}"})
    return out


def paper_view(entries: list[dict], system: str, user: str) -> tuple[list[dict], str, str]:
    """What SkillOpt's analyst saw when the paper was written (v0.1.0, 244e346): each field clipped (commands and
    messages 500 characters, tool results 800, verification 2,000), the trajectory clipped to its first and last
    6,000 characters, and the agent's prompts to 3,000 characters each."""
    lines = []
    for e in entries:
        if not isinstance(e, dict):
            lines.append(f"[agent] {str(e)[:500]}")
        elif e.get("type") == "tool_call":
            lines += [f"[action] {str(e.get('cmd') or '')[:500]}", f"[obs]    {str(e.get('obs') or '')[:800]}"]
        elif e.get("role") == "system":
            lines.append(f"[verification] {_text(e.get('content'))[:2000]}")
        else:
            lines.append(f"[{e.get('role', 'agent')}] {_text(e.get('content'))[:500]}")
    text = "\n".join(lines)
    if len(text) > 12000:
        text = text[:6000] + "\n...[middle truncated]...\n" + text[-6000:]
    # Rendered by the pinned SkillOpt as "[trajectory] <text>": the one difference from v0.1.0's output.
    return [{"role": "trajectory", "content": text}], system[:3000], user[:3000]


def codex_trace(record: dict, step_dir: Path) -> str:
    """The summary of a Codex run SkillOpt's analyst reads: the one SkillOpt's own Codex harness wrote next to its
    prediction files (codex_trace_summary.txt), else one built from a host agent's Codex stream in the same form."""
    root = step_dir / record["trajectory"] if record.get("trajectory") else None
    if root is None:
        return ""
    if (root / "codex_trace_summary.txt").is_file():
        return (root / "codex_trace_summary.txt").read_text()
    if not (root / trajectory.CODEX_STREAM).is_file():
        return ""
    return trace_summary(trajectory.log_steps(root) or [], record)


def trace_summary(steps: list, record: dict) -> str:
    """SkillOpt's Codex trace summary (its _build_codex_trace_summary) from the steps: whether the session read
    task.md and SKILL.md, its failed commands (and Codex's errors, when the session gave no reply), the final
    answer's form, the tokens."""
    extra = record.get("pipeline_extra") or {}

    def read(name: str) -> str:
        runs = [s for s in steps if s.kind == "command" and name in s.command]
        if any(str(s.exit_code) == "0" for s in runs):
            return "success"
        return "failed" if runs else "unknown"
    errors = [f"{' '.join(s.command.split())[:200]} (exit {s.exit_code})" for s in steps
              if s.kind == "command" and str(s.exit_code) not in ("0", "None", "")]
    replies = [s.text for s in steps if s.kind == "message"]
    response = replies[-1] if replies else ""
    if not response:            # Codex reports notices as errors too; they count only when the session gave no reply
        errors += [" ".join(s.text.split())[:200] for s in steps if s.kind == "error"]
    match = re.search(r"<answer>\s*([A-E])\s*</answer>", response, re.IGNORECASE)
    form = ("well_formed" if match else "tagged_nonlabel" if "<answer>" in response.lower()
            else "plain_text" if response.strip() else "missing")
    parts = ["Codex Trace Summary"]
    if extra.get("effort"):
        parts.append(f"- reasoning: {extra['effort']}")
    parts += [f"- read task.md: {read('task.md')}", f"- read SKILL.md: {read('SKILL.md')}",
              f"- shell/tool errors: {' | '.join(errors[:3]) if errors else 'none'}",
              f"- final answer format: {form}", f"- final answer label: {match.group(1).upper() if match else '(none)'}"]
    if record.get("tokens"):
        parts.append(f"- tokens used: {record['tokens']:,}")
    return "\n".join(parts)


def write_prediction(where: Path, entries: list[dict], system: str, user: str, codex_summary: str = "") -> None:
    where.mkdir(parents=True, exist_ok=True)
    (where / "conversation.json").write_text(json.dumps(entries, ensure_ascii=False, indent=1))
    for name, text in (("target_system_prompt.txt", system), ("target_user_prompt.txt", user),
                       ("codex_trace_summary.txt", codex_summary)):
        if text:
            (where / name).write_text(text)


def rollout_result(record: dict, task: dict, success: float) -> dict:
    """SkillOpt's RolloutResult for one scored run. The task's question (else all its fields) describes it; soft is
    the judge's `f1` when it reports one (SkillOpt's soft score), else the score."""
    judge = record["judge"]
    score = float(judge["score"])
    hard = int(score >= success)
    output = record.get("output")
    answer = output.get("answer", "") if isinstance(output, dict) else output
    return {"id": str(record["id"]), "hard": hard, "soft": float((judge.get("extra") or {}).get("f1", score)),
            "fail_reason": "" if hard else (judge.get("feedback") or f"status {record.get('status')}"),
            "task_description": str(task.get("question") or task_text(task)),
            "task_type": str(task.get("task_type") or task.get("category") or "task"),
            "n_turns": sum(1 for m in record.get("messages") or [] if m.get("role") == "assistant"),
            "agent_ok": record.get("status") == "ok", "predicted_answer": answer}


@contextmanager
def skillopt_model(agent, params: dict):
    """Route SkillOpt's optimizer calls (skillopt.model.chat_optimizer) to the run's model (`ctx.agent`, the
    Evolver's `direct` Base Agent), and show the analyst the Codex trace summaries of the runs that have one
    (SkillOpt's codex_trace_to_optimizer)."""
    from skillopt.model import get_optimizer_backend, set_optimizer_backend
    from skillopt.model import openai_compatible_backend as backend

    def chat_optimizer(system, user, max_completion_tokens=16384, retries=5, stage="optimizer",
                       reasoning_effort=None, timeout=None):
        kwargs = {"max_tokens": int(params.get("max_tokens") or max_completion_tokens)}
        if params.get("temperature") is not None:
            kwargs["temperature"] = float(params["temperature"])
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for _ in range(2):
            reply = ask(agent, messages, **kwargs)
            if reply["content"].strip() and reply.get("finish_reason") != "length":
                break
            # A reasoning model's thinking counts toward max_tokens: an empty or cut-off reply gets twice the room.
            kwargs["max_tokens"] *= 2
        usage = reply.get("usage") or {}
        return reply["content"], {"prompt_tokens": usage.get("input_tokens", 0),
                                  "completion_tokens": usage.get("output_tokens", 0)}

    gate = "REFLACT_CODEX_TRACE_TO_OPTIMIZER"         # read by skillopt.gradient.reflect.fmt_minibatch_trajectories
    saved = backend.chat_optimizer, get_optimizer_backend(), os.environ.get(gate)
    backend.chat_optimizer = chat_optimizer
    set_optimizer_backend("openai_compatible")
    os.environ[gate] = "1" if params.get("codex_trace_to_optimizer", True) else "0"
    try:
        yield
    finally:
        backend.chat_optimizer = saved[0]
        set_optimizer_backend(saved[1])
        if saved[2] is None:
            os.environ.pop(gate, None)
        else:
            os.environ[gate] = saved[2]


class SkillOptEvolver(Evolver):
    def setup(self, ctx) -> None:
        try:
            import skillopt  # noqa: F401
        except ImportError:
            raise missing_dependency("skillopt", "SkillOpt", "skillopt") from None

    def step(self, ctx: StepContext) -> StepResult:
        from skillopt.engine.trainer import _extract_failure_patterns, _format_step_buffer, _normalise_patches
        from skillopt.evaluation.gate import GateResult, evaluate_gate
        from skillopt.gradient.aggregate import merge_patches
        from skillopt.gradient.reflect import run_minibatch_reflect
        from skillopt.optimizer.clip import rank_and_select
        from skillopt.optimizer.scheduler import build_scheduler
        from skillopt.optimizer.skill import apply_patch_with_report
        from skillopt.optimizer.update_modes import get_payload_items, short_item_summary
        from skillopt.prompts import load_prompt
        from skillopt.utils import compute_score

        p = ctx.params
        if ctx.agent is None:
            return StepResult("failed", "skillopt needs the run's model: set `llm` in run.yaml")
        if ctx.access.validation is None:
            return StepResult("failed", "skillopt needs validation tasks: set data.val")
        n, epoch = ctx.progress.step, ctx.progress.epoch
        st = self._load(ctx)
        name, out, scratch = p.get("file", "ADAPTER.md"), ctx.workspace.output, ctx.workspace.scratch
        skill = (ctx.workspace.adapter / name).read_text()
        current = ctx.workspace.learning_material / "current"
        records = [json.loads(f.read_text()) for f in sorted((current / "tasks").glob("*.json"))]
        for r in records:
            st["tasks"][str(r["id"])] = r["input"]
        if st["epoch"] != epoch:
            st["epoch"], st["buffer"] = epoch, []
        meta = st["meta"].get(str(epoch - 1), "") if p.get("use_meta_skill", True) else ""
        rec = {"step": n, "epoch": epoch}
        if st["current_skill"] is not None and skill != st["current_skill"]:
            # The Trainer did not keep the last skill handed back (a failed or stopped step): continue from its own.
            rec["reconciled"] = True
            st["current_skill"] = skill
            st["current_score"] = self._gate_score(*self._validate(ctx, skill, st), skill, p)

        with skillopt_model(ctx.agent, p):
            if st["current_score"] is None:          # SkillOpt's baseline: the initial skill on validation
                hard, soft = self._validate(ctx, skill, st)
                st.update(current_skill=skill, current_score=self._gate_score(hard, soft, skill, p),
                          best_skill=skill, best_step=0)
                st["best_score"] = st["current_score"]
            # 1. the rollouts, as SkillOpt's results and trajectories
            success = float(p.get("success_score", 1.0))
            results = [rollout_result(r, r["input"], success) for r in records]
            for r in records:
                view = conversation(r, current, int(p.get("obs_chars", 0)))
                write_prediction(scratch / "predictions" / str(r["id"]),
                                 *(paper_view(*view) if p.get("trajectory_view") == "paper" else view),
                                 codex_summary=codex_trace(r, current))
            rec["rollout_hard"], rec["rollout_soft"] = compute_score(results)
            # 2. reflect
            env = p.get("prompt_env") or None
            raw = run_minibatch_reflect(
                results=results, skill_content=skill, prediction_dir=str(scratch / "predictions"),
                patches_dir=str(out / "patches"), workers=int(p["analyst_workers"]),
                failure_only=bool(p["failure_only"]), minibatch_size=int(p["minibatch_size"]),
                edit_budget=int(p["learning_rate"]), random_seed=int(p["seed"]) + n,
                error_system=load_prompt("analyst_error", env=env), success_system=load_prompt("analyst_success", env=env),
                step_buffer_context=_format_step_buffer(st["buffer"]), meta_skill_context=meta, update_mode="patch")
            failure, success_patches = _normalise_patches(raw, update_mode="patch")
            rec.update(n_failure_patches=len(failure), n_success_patches=len(success_patches))
            if ctx.cancel is not None and ctx.cancel.is_set():
                return StepResult("failed", "stopped by the Trainer")
            if not failure and not success_patches:
                rec["action"] = "skip_no_patches"
            else:
                # 3. merge, 4. select under the learning rate, 5. apply
                merged = merge_patches(skill, failure, success_patches, batch_size=int(p["merge_batch_size"]),
                                       verbose=False, workers=int(p["analyst_workers"]), update_mode="patch",
                                       meta_skill_context=meta)
                st["lr_steps"] += 1
                total = int(p.get("total_steps") or 0)
                lr = build_scheduler(mode=p["lr_scheduler"] if total else "constant", max_lr=int(p["learning_rate"]),
                                     min_lr=int(p["min_learning_rate"]), total_steps=max(total, 1)).get_lr(st["lr_steps"])
                ranked = rank_and_select(skill, merged, max_edits=lr, update_mode="patch", meta_skill_context=meta)
                candidate, report = apply_patch_with_report(skill, ranked)
                for file, value in (("merged_patch.json", merged), ("ranked_edits.json", ranked),
                                    ("edit_apply_report.json", report)):
                    (out / file).write_text(json.dumps(value, ensure_ascii=False, indent=1))
                (out / "candidate_skill.md").write_text(candidate)
                rec.update(edit_budget=lr, n_edits_merged=len(get_payload_items(merged, "patch")),
                           n_edits_ranked=len(get_payload_items(ranked, "patch")))
                # 6. the validation gate
                check = ctx.tools["check_spec"](path=str(self._write(scratch / "candidate", ctx, {name: candidate})))
                if not check["passed"]:
                    rec["action"] = "reject_check_spec"
                    rec["check"] = [i["problem"] for i in check["issues"][:3]]
                    cand_score = None
                else:
                    hard, soft = self._validate(ctx, candidate, st)
                    rec.update(selection_hard=hard, selection_soft=soft)
                    cand_score = self._gate_score(hard, soft, candidate, p)
                    if p.get("use_gate", True):
                        gate = evaluate_gate(candidate_skill=candidate, cand_hard=hard, current_skill=skill,
                                             current_score=st["current_score"], best_skill=st["best_skill"],
                                             best_score=st["best_score"], best_step=st["best_step"], global_step=n,
                                             cand_soft=soft, metric=p.get("gate_metric", "hard"),
                                             mixed_weight=float(p.get("gate_mixed_weight", 0.5)))
                    else:                           # validation still runs; the candidate is kept regardless
                        better = cand_score > st["best_score"]
                        gate = GateResult("force_accept", candidate, cand_score,
                                          candidate if better else st["best_skill"],
                                          cand_score if better else st["best_score"], n if better else st["best_step"])
                    rec["action"] = gate.action
                    skill = gate.current_skill
                    st.update(current_score=gate.current_score, best_skill=gate.best_skill,
                              best_score=gate.best_score, best_step=gate.best_step)
                entry = {"step": n, "action": rec["action"], "n_total": len(results),
                         "n_fail": sum(1 for r in results if not r["hard"]),
                         "failure_patterns": _extract_failure_patterns(results, str(out))}
                if "reject" in rec["action"]:
                    entry.update(score_before=st["current_score"], score_after=cand_score,
                                 rejected_edits=[short_item_summary(i, "patch")
                                                 for i in get_payload_items(ranked, "patch") if isinstance(i, dict)])
                st["buffer"].append(entry)
            if ctx.progress.is_epoch_end:
                skill = self._end_epoch(ctx, skill, st, rec)
        if ctx.cancel is not None and ctx.cancel.is_set():
            return StepResult("failed", "stopped by the Trainer")
        (ctx.workspace.adapter / name).write_text(skill)
        st["current_skill"] = skill
        rec.update(current_score=st["current_score"], best_score=st["best_score"], best_step=st["best_step"])
        st["history"].append(rec)
        self._save(ctx, st)
        summary = (f"{rec['action']}: validation current {st['current_score']:.3f}, best {st['best_score']:.3f} "
                   f"(step {st['best_step']})")
        (out / "report.md").write_text(summary + "\n\n```json\n" + json.dumps(rec, indent=1) + "\n```\n")
        return StepResult("done", summary, extra={k: rec.get(k) for k in ("action", "rollout_hard", "selection_hard")})

    def _end_epoch(self, ctx, skill: str, st: dict, rec: dict) -> str:
        """SkillOpt's slow update and meta skill, from rollouts of the previous and this epoch's skill."""
        from skillopt.evaluation.gate import evaluate_gate
        from skillopt.optimizer.meta_skill import run_meta_skill
        from skillopt.optimizer.slow_update import (extract_slow_update_field, inject_empty_slow_update_field,
                                                    replace_slow_update_field, run_slow_update)
        from skillopt.utils import skill_hash

        p, epoch = ctx.params, ctx.progress.epoch
        prev = st["epoch_skills"].get(str(epoch - 1))
        if epoch > 1 and prev is None:
            rec["epoch_end"] = "no skill recorded for the previous epoch (its last step failed): no slow update or meta skill"
        epoch_last, pairs = skill, None
        if p.get("use_slow_update", True):
            if epoch == 1:
                skill = inject_empty_slow_update_field(skill)
                rec["slow_update"] = "inject_placeholder"
            elif prev is not None:
                pairs, prev_results, curr_results, items, dirs = self._compare(ctx, prev, skill, st)
                slow = run_slow_update(skill, prev_results, curr_results, items, prev_skill=prev,
                                       prev_slow_update_content=extract_slow_update_field(skill),
                                       prev_rollout_dir=str(dirs[0]), curr_rollout_dir=str(dirs[1]),
                                       comparison_pairs=pairs)
                (ctx.workspace.output / "slow_update.json").write_text(json.dumps(slow, ensure_ascii=False, indent=1))
                if slow and slow.get("slow_update_content") and p.get("slow_update_gate", False):
                    # The paper's protocol: the slow update passes the same validation gate as a step's edits.
                    candidate = replace_slow_update_field(skill, slow["slow_update_content"])
                    hard, soft = self._validate(ctx, candidate, st)
                    gate = evaluate_gate(candidate_skill=candidate, cand_hard=hard, current_skill=skill,
                                         current_score=st["current_score"], best_skill=st["best_skill"],
                                         best_score=st["best_score"], best_step=st["best_step"],
                                         global_step=ctx.progress.step, cand_soft=soft,
                                         metric=p.get("gate_metric", "hard"),
                                         mixed_weight=float(p.get("gate_mixed_weight", 0.5)))
                    skill = gate.current_skill
                    st.update(current_score=gate.current_score, best_skill=gate.best_skill,
                              best_score=gate.best_score, best_step=gate.best_step)
                    rec.update(slow_update=gate.action, slow_update_selection_hard=hard)
                elif slow and slow.get("slow_update_content"):
                    # SkillOpt's code default: force-accepted into the current skill only; the best skill stays.
                    skill = replace_slow_update_field(skill, slow["slow_update_content"])
                    st["cache"][skill_hash(skill)] = [st["current_score"], 0.0]
                    rec["slow_update"] = "force_accept"
                else:
                    rec["slow_update"] = "no_content"
        if p.get("use_meta_skill", True) and prev is not None:
            if pairs is None:
                pairs = self._compare(ctx, prev, epoch_last, st)[0]
            result = run_meta_skill(prev_skill=prev, curr_skill=epoch_last, comparison_pairs=pairs,
                                    prev_meta_skill_content=st["meta"].get(str(epoch - 1), ""))
            (ctx.workspace.output / "meta_skill.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
            if result and result.get("meta_skill_content"):
                st["meta"][str(epoch)] = result["meta_skill_content"]
                rec["meta_skill"] = "written"
        st["epoch_skills"][str(epoch)] = skill
        return skill

    def _compare(self, ctx, prev: str, curr: str, st: dict):
        """Run the previous and the current skill on the same sample of training tasks; SkillOpt's comparison pairs.
        run_agent returns answers and judge feedback, not trajectories: those are what the pairs show."""
        from skillopt.optimizer.slow_update import build_comparison_pairs

        p = ctx.params
        ids = sorted(st["tasks"])
        rng = random.Random(int(p["seed"]) + ctx.progress.epoch * 2000)
        ids = rng.sample(ids, min(int(p.get("slow_update_samples", 20)), len(ids)))
        items = [{"id": i, "question": task_text(st["tasks"][i])} for i in ids]
        success = float(p.get("success_score", 1.0))
        results, dirs = [], []
        for label, text in (("prev", prev), ("curr", curr)):
            where = ctx.workspace.scratch / "slow_update" / label
            adapter = self._write(where / "adapter", ctx, {p.get("file", "ADAPTER.md"): text})
            rows = ctx.tools["run_agent"](task_ids=ids, adapter=str(adapter))["results"]
            for row in rows:
                view = conversation({**row, "trajectory": row.get("log")}, where, int(p.get("obs_chars", 0)))
                write_prediction(where / "predictions" / str(row["id"]),
                                 *(paper_view(*view) if p.get("trajectory_view") == "paper" else view),
                                 codex_summary=codex_trace({**row, "trajectory": row.get("log")}, where))
            results.append([rollout_result(row, st["tasks"][row["id"]], success) for row in rows])
            dirs.append(where)
        pairs = build_comparison_pairs(results[0], results[1], items, prev_rollout_dir=str(dirs[0]),
                                       curr_rollout_dir=str(dirs[1]))
        return pairs, results[0], results[1], items, dirs

    def finalize(self, ctx) -> FinalResult:
        """The best skill. With promote_final (SkillOpt at 79124b37), the final skill is first scored on validation
        and promoted if it beats the best; the paper-time code handed back the best skill as is."""
        from skillopt.utils import skill_hash

        st = self._load(ctx)
        name = ctx.params.get("file", "ADAPTER.md")
        if st["current_score"] is None:
            return FinalResult(adapter=ctx.workspace.adapter, summary="no SkillOpt step ran; current adapter")
        best, note = st["best_skill"], f"best skill (step {st['best_step']}, validation {st['best_score']:.3f})"
        if ctx.params.get("promote_final", True) and skill_hash(st["current_skill"]) != skill_hash(best):
            st["cache"].pop(skill_hash(st["current_skill"]), None)      # a real validation run, as SkillOpt does
            try:
                hard, soft = self._validate(ctx, st["current_skill"], st)
            except Exception as exc:                                    # noqa: BLE001 — keep the best skill
                note += f"; the final skill's validation failed ({type(exc).__name__}: {exc})"
            else:
                score = self._gate_score(hard, soft, st["current_skill"], ctx.params)
                if score > st["best_score"]:
                    best, note = st["current_skill"], f"final skill, promoted (validation {score:.3f} > {st['best_score']:.3f})"
                    st.update(best_skill=best, best_score=score, best_step="final")
                self._save(ctx, st)
        (ctx.workspace.adapter / name).write_text(best)
        return FinalResult(adapter=ctx.workspace.adapter, summary=note)

    # ---------- helpers ----------
    def _validate(self, ctx, skill: str, st: dict) -> tuple[float, float]:
        """(hard, soft) of a skill on the validation tasks, cached by the skill's hash."""
        from skillopt.utils import compute_score, skill_hash

        key = skill_hash(skill)
        if key not in st["cache"]:
            p = ctx.params
            ids = [json.loads(l)["id"] for l in (ctx.access.validation / "tasks.jsonl").read_text().splitlines() if l]
            adapter = self._write(ctx.workspace.scratch / "validation", ctx, {p.get("file", "ADAPTER.md"): skill})
            rows = ctx.tools["run_agent"](task_ids=ids, adapter=str(adapter))["results"]
            st["cache"][key] = list(compute_score([rollout_result(r, {}, float(p.get("success_score", 1.0)))
                                                   for r in rows]))
        return tuple(st["cache"][key])

    @staticmethod
    def _gate_score(hard: float, soft: float, skill: str, p: dict) -> float:
        from skillopt.evaluation.gate import select_gate_score
        return select_gate_score(hard, soft, p.get("gate_metric", "hard"), float(p.get("gate_mixed_weight", 0.5)),
                                 skill_content=skill)

    @staticmethod
    def _write(target: Path, ctx, files: dict) -> Path:
        """The working adapter with `files` in place, at `target` (inside the run, as run_agent requires)."""
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(ctx.workspace.adapter, target, symlinks=True)
        for name, text in files.items():
            (target / name).write_text(text)
        return target

    @staticmethod
    def _load(ctx) -> dict:
        path = ctx.workspace.state / "skillopt.json"
        if path.exists():
            return json.loads(path.read_text())
        return {"current_skill": None, "current_score": None, "best_skill": None, "best_score": None,
                "best_step": 0, "cache": {}, "epoch": None, "buffer": [], "lr_steps": 0, "tasks": {},
                "epoch_skills": {}, "meta": {}, "history": []}

    @staticmethod
    def _save(ctx, st: dict) -> None:
        (ctx.workspace.state / "skillopt.json").write_text(json.dumps(st, ensure_ascii=False, indent=1))
