"""OPROEvolver: OPRO, Optimization by PROmpting (Yang et al., ICLR 2024, arXiv:2309.03409; google-deepmind/opro,
`opro/optimization/opt_utils.py::run_evolution`), one OPRO step per training step, on the `prompt-only` template.

OPRO keeps every instruction it has evaluated with its training score. Each step, the optimizer model reads a
meta-prompt (the best `max_num_instructions` old instructions with their scores in ascending order, then
`num_exemplars` random training problems with their answers) and writes `n_candidates` new instructions, one call
each; each new instruction is scored on the whole training set and joins the history. The instruction is ADAPTER.md.

On the framework's step model (`data.step_size` = the size of the training split, so each step's rollouts are the
whole training set):

1. the step's rollouts score the adapter they ran: at the first step, the starting adapter, which is OPRO's initial
   instruction (score recorded at step -1, as upstream); later, the current best again (recorded, not merged:
   upstream scores an instruction once);
2. the exemplars are `num_exemplars` of those training records whose run did not fail, drawn at random per step;
   the answer shown is the agent's answer when the judge scored it right, else the judge's feedback (the task has
   no gold field: the judge holds the reference answer, and OfficeQA's feedback names it);
3. the meta-prompt (meta_prompt.py, the GPT optimizers' version, instruction at Q_begin without "Q:"/"A:"), the
   `n_candidates` calls (a failed call tried once more), the <INS> extraction, polish_sentence and the md5
   de-duplication against every instruction generated before (not the initial one, as upstream), then the filters
   (empty; containing "INS"; longer than `max_instruction_chars`, when set);
4. each new instruction through check_spec, then run_agent on every training task; its mean judge score joins the
   history. A wrong answer scores 0; a run that failed for an infrastructure reason is run again, and an instruction
   whose runs fail twice is not scored (and may be generated again);
5. the instruction with the best training score so far is handed back as the adapter.

finalize hands back the instruction with the best training score (`select: train`, OPRO's), or, with
`select: validation`, the best validation mean of the `top_k` best on training (validation tasks never reach the
meta-prompt).

Training State: `state/opro.json` (the history, the md5s, each candidate's per-task scores, one record per step);
each step's `output/` holds the meta-prompt, the replies and a `report.md`.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import random
import shutil

from hermit.agent.evolver.base import Evolver, FinalResult, StepContext, StepResult, ask
from hermit.agent.evolver.methods.opro import meta_prompt as mp
from hermit.trainer.budget import BudgetExceeded

ADAPTER = "ADAPTER.md"
STATE = "opro.json"
ATTEMPTS = 2           # an optimizer call or a rollout that fails for an infrastructure reason is tried once more


def task_text(task: dict, field: str | None = None) -> str:
    if field and field in task:
        return str(task[field])
    fields = {k: v for k, v in task.items() if k not in ("id", "task_id")}
    if len(fields) == 1:
        return str(next(iter(fields.values())))
    return json.dumps(fields, ensure_ascii=False)


def answer_text(output) -> str:
    if isinstance(output, dict):
        return str(output.get("answer") if output.get("answer") is not None else json.dumps(output, ensure_ascii=False))
    return "" if output is None else str(output)


def exemplar_answer(record: dict, success: float) -> str:
    """The exemplar's "Ground truth answer": the agent's answer when it was judged right, else the judge's feedback."""
    judge = record.get("judge") or {}
    if float(judge.get("score") or 0.0) >= success and record.get("status") in (None, "ok"):
        return answer_text(record.get("output"))
    return str(judge.get("feedback") or "(the answer was judged wrong)")


def _mean(scores: dict) -> float:
    return sum(scores.values()) / len(scores) if scores else 0.0


class OPROEvolver(Evolver):
    def setup(self, ctx) -> None:
        """A run without the model, or with an unknown `select`, stops here, before the first rollouts."""
        if ctx.agent is None:
            raise ValueError("opro needs the run's model: set `llm` in run.yaml")
        if ctx.params.get("select", "validation") not in ("train", "validation"):
            raise ValueError(f"unknown select {ctx.params.get('select')!r}: train or validation")

    def step(self, ctx: StepContext) -> StepResult:
        if ctx.agent is None:
            return StepResult("failed", "opro needs the run's model: set `llm` in run.yaml")
        if ctx.params.get("select", "validation") not in ("train", "validation"):
            return StepResult("failed", f"unknown select {ctx.params.get('select')!r}: train or validation")
        state_file = ctx.workspace.state / STATE
        state = json.loads(state_file.read_text()) if state_file.exists() else \
            {"history": [], "hashes": [], "candidates": [], "train_ids": [], "steps": []}
        try:
            return self._step(ctx, state, state_file)
        except BudgetExceeded:
            # The instructions scored before the budget ran out are paid for: keep them for finalize.
            if state["candidates"]:
                state["steps"].append({"step": ctx.progress.step if ctx.progress else 1, "outcome": "budget exceeded"})
                state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1))
            raise

    def _step(self, ctx: StepContext, state: dict, state_file: Path) -> StepResult:
        p = ctx.params
        n = ctx.progress.step if ctx.progress else 1
        i_step = n - 1                                    # upstream's i_step counts from 0
        adapter = ctx.workspace.adapter
        out = ctx.workspace.output
        current = ctx.workspace.learning_material / "current"
        records = sorted((json.loads(f.read_text()) for f in (current / "tasks").glob("*.json")), key=lambda r: r["id"])
        if not records:
            return StepResult("failed", "no training records in this step's learning material")
        rollout = {r["id"]: float((r.get("judge") or {}).get("score") or 0.0) for r in records}
        if any(not 0.0 <= s <= 1.0 for s in rollout.values()):
            return StepResult("failed", "OPRO shows scores from 0 to 100: the judge's scores must be in [0, 1]")
        text = (adapter / ADAPTER).read_text().strip() if (adapter / ADAPTER).exists() else ""
        record: dict = {"step": n, "i_step": i_step, "rollout_mean": _mean(rollout)}

        # 1. The rollouts: the initial instruction's score at the first step, a re-score of the current one later.
        if not state["candidates"]:
            # One step per epoch means every epoch is one chunk: the step count equals the epoch count.
            if ctx.progress is not None and not (ctx.progress.is_epoch_end and ctx.progress.step == ctx.progress.epoch):
                return StepResult("failed", "OPRO scores every instruction on the whole training split: set "
                                  "data.step_size to the training split's size (one step per epoch)")
            failed = sorted(r["id"] for r in records if r.get("status") not in (None, "ok"))
            if failed:                                    # infrastructure failures: run those tasks again
                try:
                    again = self._scores(ctx, failed, self._write(ctx.workspace.scratch / "initial", adapter, text),
                                         attempts=1)        # the rollout was the first attempt
                except BudgetExceeded:
                    raise
                except Exception as exc:                  # noqa: BLE001 — the initial score would be wrong
                    return StepResult("failed", f"the initial instruction's runs failed on {len(failed)} tasks "
                                      f"({type(exc).__name__}: {str(exc)[:200]}); its score would be wrong")
                rollout.update(again)
                record["rerun"] = failed
            state["train_ids"] = sorted(rollout)
            state["candidates"].append({"text": text, "step": -1, "train": rollout})
            state["history"].append([text, _mean(rollout), -1])     # upstream does not md5 its initial instructions
            record["initial_score"] = _mean(rollout)
        else:
            missing = sorted(set(state["train_ids"]) - set(rollout))
            if missing:
                record["note"] = f"the rollouts missed {len(missing)} training tasks: set data.step_size to the " \
                                 "training split's size"
            record["rescore"] = _mean(rollout)

        # 2. Exemplars: random training problems of this step.
        success = float(p.get("success_score", 1.0))
        rng = random.Random(int(p.get("seed", 0)) * 100003 + i_step)
        usable = [i for i, r in enumerate(records) if r.get("status") in (None, "ok")]
        picked = sorted(rng.sample(usable, min(int(p.get("num_exemplars", 3)), len(usable))))
        exemplars = [(task_text(records[i]["input"], p.get("question_field")), exemplar_answer(records[i], success))
                     for i in picked]
        record["exemplars"] = [records[i]["id"] for i in picked]

        # 3. The meta-prompt and the new instructions.
        threshold = p.get("score_threshold")
        meta = mp.gen_meta_prompt(
            old_instructions_and_scores=[tuple(h) for h in state["history"]],
            instruction_pos="Q_begin",
            old_instruction_score_threshold=float(threshold) if threshold is not None else 0.0,
            max_num_instructions=int(p.get("max_num_instructions", 20)),
            few_shot_qa_pairs=bool(exemplars),
            include_qa=False,
            exemplars=exemplars,
            instructions_before_exemplars=True,
            num_score_buckets=math.inf if p.get("num_score_buckets") is None else int(p["num_score_buckets"]),
        )
        (out / "meta_prompt.md").write_text(meta)
        sampling = {k: p[k] for k in ("max_tokens", "temperature") if p.get(k) is not None}
        raw, errors, failed_calls = [], [], 0
        for k in range(int(p.get("n_candidates", 8))):
            if ctx.cancel is not None and ctx.cancel.is_set():
                return self._stopped(state, state_file, record)
            reply = None
            for _ in range(ATTEMPTS):
                try:
                    reply = ask(ctx.agent, [{"role": "user", "content": meta}], **sampling)
                    break
                except BudgetExceeded:
                    raise
                except Exception as exc:              # noqa: BLE001 — the other calls of the step still count
                    errors.append(f"call {k}: {type(exc).__name__}: {str(exc)[:300]}")
            if reply is None:
                failed_calls += 1
                continue
            content = reply.get("content") or ""
            (out / f"reply.{k}.txt").write_text(content)
            if content.strip():
                raw.append(mp.extract_instruction(content))
        polished = [mp.polish_sentence(s) for s in raw]
        new, seen, skipped = [], set(state["hashes"]), []
        limit = int(p.get("max_instruction_chars") or 0)
        for ins in polished:
            h = mp.instruction_to_filename(ins)
            if h in seen:
                skipped.append("already evaluated")
                continue
            seen.add(h)
            reason = ("empty" if not ins else f"longer than {limit} characters" if limit and len(ins) > limit
                      else "contains 'INS'" if "INS" in ins else None)
            if reason:
                state["hashes"].append(h)                 # upstream: md5 recorded, then the instruction skipped
                skipped.append(reason)
            else:
                new.append(ins)
        record.update(replies=len(raw), errors=errors, skipped=skipped, evaluated=[])

        # 4. Score each new instruction on every training task.
        for k, ins in enumerate(new):
            if ctx.cancel is not None and ctx.cancel.is_set():
                return self._stopped(state, state_file, record)
            cand = self._write(ctx.workspace.scratch / f"candidate_{k}", adapter, ins)
            h = mp.instruction_to_filename(ins)
            try:
                check = ctx.tools["check_spec"](path=str(cand))
                if not check["passed"]:
                    state["hashes"].append(h)
                    record["evaluated"].append({"rejected": "; ".join(i["problem"] for i in check["issues"][:3])})
                    continue
                scores = self._scores(ctx, state["train_ids"], cand)
            except BudgetExceeded:
                raise
            except Exception as exc:                      # noqa: BLE001 — not scored: may be generated again
                if ctx.cancel is not None and ctx.cancel.is_set():
                    return self._stopped(state, state_file, record)
                record["evaluated"].append({"failed": f"{type(exc).__name__}: {str(exc)[:300]}", "chars": len(ins)})
                continue
            state["hashes"].append(h)
            state["candidates"].append({"text": ins, "step": i_step, "train": scores})
            state["history"].append([ins, _mean(scores), i_step])
            record["evaluated"].append({"candidate": len(state["candidates"]) - 1, "train": _mean(scores),
                                        "chars": len(ins)})

        # 5. Hand back the best instruction so far.
        best = self._best_train(state["candidates"])
        if state["candidates"][best]["text"] != text:
            (adapter / ADAPTER).write_text(state["candidates"][best]["text"] + "\n")
        record["best"] = best
        state["steps"].append(record)
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        scored = [e["train"] for e in record["evaluated"] if "train" in e]
        summary = (f"{len(scored)} new instructions scored ({', '.join(f'{s:.3f}' for s in scored) or 'none'}); "
                   f"best: candidate {best}, train {_mean(state['candidates'][best]['train']):.3f}")
        if failed_calls:
            summary += f"; {failed_calls} optimizer calls failed"
        (out / "report.md").write_text(summary + "\n")
        return StepResult("done", summary[:300], extra={"candidates": len(state["candidates"]), "best": best})

    def finalize(self, ctx) -> FinalResult:
        state_file = ctx.workspace.state / STATE
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        cands = state.get("candidates") or []
        if not cands:
            return FinalResult(adapter=ctx.workspace.adapter, summary="no OPRO step ran; current adapter")
        p = ctx.params
        order = sorted(range(len(cands)), key=lambda i: (-_mean(cands[i]["train"]), i))
        best = order[0]
        how = f"the best training score ({_mean(cands[best]['train']):.3f})"
        val_ids = [json.loads(l)["id"] for l in (ctx.access.validation / "tasks.jsonl").read_text().splitlines()
                   if l.strip()] if ctx.access.validation is not None else []
        if p.get("select", "validation") == "validation" and not val_ids:
            how += "; no validation tasks (data.val)"
        elif p.get("select", "validation") == "validation":
            top = order[:max(1, int(p.get("top_k", 3)))]
            stopped = ctx.cancel is not None and ctx.cancel.is_set()
            for i in top:
                if "val" not in cands[i] and not stopped:
                    cand = self._write(ctx.workspace.scratch / f"final_{i}", ctx.workspace.adapter, cands[i]["text"])
                    try:
                        cands[i]["val"] = self._scores(ctx, val_ids, cand)
                    except BudgetExceeded:                # the run ends on its budget: select with what is scored
                        stopped = True
                        continue
                    except Exception as exc:              # noqa: BLE001 — left out of the selection, recorded
                        cands[i]["val_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                        continue
                    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1))
            missing = [i for i in top if "val" not in cands[i]]
            if missing:                                   # a partial comparison could pick below the training best
                how += ("; the budget ran out before validation" if stopped else
                        f"; {len(missing)} of the top {len(top)} could not be scored on validation")
            else:
                best = max(top, key=lambda i: (_mean(cands[i]["val"]), _mean(cands[i]["train"]), -i))
                means = ", ".join(f"{i}: {_mean(cands[i]['val']):.3f}" for i in top)
                how = f"the best validation mean of the top {len(top)} on training ({means})"
        state["final"] = best
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        (ctx.workspace.adapter / ADAPTER).write_text(cands[best]["text"] + "\n")
        return FinalResult(adapter=ctx.workspace.adapter, summary=f"candidate {best} of {len(cands)}, {how}")

    @staticmethod
    def _best_train(cands: list) -> int:
        return min(range(len(cands)), key=lambda i: (-_mean(cands[i]["train"]), i))

    @staticmethod
    def _stopped(state, state_file, record) -> StepResult:
        record["outcome"] = "stopped by the Trainer"
        state["steps"].append(record)
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        return StepResult("failed", "stopped by the Trainer")

    @staticmethod
    def _scores(ctx, task_ids: list, adapter: Path, attempts: int = ATTEMPTS) -> dict:
        """Mean-able judge scores on `task_ids`. A wrong answer scores 0 as upstream; a run that failed for an
        infrastructure reason (status not ok, or run_agent raised) is run once more, and if it fails again the
        instruction is not scored."""
        scores, todo, error = {}, list(task_ids), ""
        for _ in range(attempts):
            try:
                results = ctx.tools["run_agent"](task_ids=todo, adapter=str(adapter))["results"]
            except BudgetExceeded:
                raise
            except Exception as exc:                      # noqa: BLE001 — as failed runs: tried once more
                if ctx.cancel is not None and ctx.cancel.is_set():
                    raise
                results, error = [], f"; {type(exc).__name__}: {str(exc)[:200]}"
            for r in results:
                if r.get("status") in (None, "ok"):
                    scores[r["id"]] = float((r.get("judge") or {}).get("score") or 0.0)
                    if not 0.0 <= scores[r["id"]] <= 1.0:
                        raise ValueError("OPRO shows scores from 0 to 100: the judge's scores must be in [0, 1]")
            todo = [t for t in task_ids if t not in scores]
            if not todo:
                return scores
        raise RuntimeError(f"{len(todo)} runs failed twice ({', '.join(todo[:3])}){error}")

    @staticmethod
    def _write(target: Path, source: Path, text: str) -> Path:
        """A copy of `source` with `text` as ADAPTER.md."""
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        (target / ADAPTER).write_text(text + "\n")
        return target
