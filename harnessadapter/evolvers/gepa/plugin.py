"""GEPA (Agrawal et al., ICLR 2026; github.com/gepa-ai/gepa) as an optimizer
Evolver.

The upstream library does the optimizing: its engine, Pareto candidate
selection, reflective mutation prompt, minibatch acceptance and budget
accounting run unchanged through gepa.optimize(). This file supplies only
what GEPA asks a new system to supply, a GEPAAdapter:

- a candidate is the adapter's text surface, {relative path: text};
- evaluate() runs the batch with the candidate deployed (context.evaluate);
- make_reflective_dataset() turns each rollout into GEPA's
  Inputs / Generated Outputs / Feedback record, with the agent's trajectory
  as the execution trace and the grader's feedback as GEPA's feedback text.

GEPA's run_dir is the Training State: gepa_state.bin is written every
iteration, and a session started from it resumes the search.
"""
from __future__ import annotations

import importlib.metadata
import itertools
import json
import threading

from harnessadapter.evolution.optimizer import Context, Outcome, candidate_hash


def clip_middle(text: str, limit: int) -> str:
    """Keep the beginning (the approach) and the end (how it finished)."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit * 2 // 5
    return text[:head] + f"\n[... {len(text) - limit} characters of the trajectory left out ...]\n" + text[-(limit - head):]


class _Log:
    """GEPA's LoggerProtocol, so GEPA does not redirect this process's stdout."""

    def __init__(self, sink):
        self.sink = sink

    def log(self, message: str):
        self.sink(f"[gepa] {message}")


class HarnessAdapterGEPAAdapter:
    """gepa.core.adapter.GEPAAdapter over a Context."""

    propose_new_texts = None

    def __init__(self, context: Context, *, trace_chars: int, skip_perfect_score: bool = True,
                 perfect_score: float = 1.0):
        self.context = context
        self.prompts = {t.id: t.prompt for t in context.train + context.val}
        self.trace_chars = trace_chars
        self.skip_perfect_score = skip_perfect_score
        self.perfect_score = perfect_score
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def evaluate(self, batch, candidate, capture_traces=False):
        from gepa import EvaluationBatch
        with self._lock:
            number = next(self._counter)
        kind = "minibatch" if capture_traces else "val"
        rollouts = self.context.evaluate(candidate, list(batch),
                                         label=f"gepa/{number:04d}-{kind}-{candidate_hash(candidate)[:8]}")
        return EvaluationBatch(outputs=[{"task_id": r.task_id, "score": r.score, "output": r.output, "status": r.status}
                                        for r in rollouts],
                               scores=[float(r.score) for r in rollouts],
                               trajectories=rollouts if capture_traces else None)

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        # An episode the infrastructure lost is not evidence about the text.
        rollouts = [r for r in (eval_batch.trajectories or []) if not r.infra]
        # No evidence left, or only perfect episodes once the losses are set
        # aside (GEPA's own skip_perfect_score test counted them as 0): an
        # empty dataset makes GEPA skip the proposal, with no model call.
        if not rollouts or (self.skip_perfect_score and all(r.score >= self.perfect_score for r in rollouts)):
            return {name: [] for name in components_to_update}
        records = [{"Inputs": {"Task given to the agent": self.prompts[r.task_id]},
                    "Generated Outputs": {
                        "Final answer": r.output or "(no final answer)",
                        "Run status": r.status,
                        "Agent trajectory (commands, observations and messages)": clip_middle(r.trace, self.trace_chars)},
                    "Feedback": f"Score {r.score:g}. " + (r.feedback or "")}
                   for r in rollouts]
        return {name: records for name in components_to_update}


def run(context: Context) -> Outcome:
    import gepa

    s = context.settings
    try:
        version = importlib.metadata.version("gepa")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown (imported from source without metadata)"
    adapter = HarnessAdapterGEPAAdapter(context, trace_chars=int(s["trace_chars"]),
                                        skip_perfect_score=bool(s["skip_perfect_score"]),
                                        perfect_score=float(s["perfect_score"]))
    context.log(f"[gepa] train={len(context.train)} val={len(context.val)} budget={s['max_metric_calls']} rollouts; "
                f"components={context.components}")
    result = gepa.optimize(
        seed_candidate=context.seed, trainset=context.train, valset=context.val, adapter=adapter,
        reflection_lm=context.model,
        candidate_selection_strategy=s["candidate_selection_strategy"],
        frontier_type=s["frontier_type"],
        skip_perfect_score=bool(s["skip_perfect_score"]), perfect_score=float(s["perfect_score"]),
        reflection_minibatch_size=int(s["reflection_minibatch_size"]),
        module_selector=s["module_selector"],
        use_merge=bool(s["use_merge"]), max_merge_invocations=int(s["max_merge_invocations"]),
        max_metric_calls=int(s["max_metric_calls"]),
        acceptance_criterion=s["acceptance_criterion"],
        cache_evaluation=bool(s["cache_evaluation"]),
        reflection_prompt_template=s["reflection_prompt_template"] or None,
        run_dir=str(context.state_dir), logger=_Log(context.log),
        track_best_outputs=False, display_progress_bar=False,
        raise_on_exception=bool(s["raise_on_exception"]), seed=int(s["seed"]),
    )
    discovered = result.discovery_eval_counts or [None] * len(result.candidates)
    (context.state_dir / "harnessadapter-candidates.json").write_text(json.dumps(
        [{"index": i, "texts": c} for i, c in enumerate(result.candidates)], ensure_ascii=False, indent=1) + "\n")
    return Outcome(candidate=dict(result.best_candidate), summary={
        "algorithm": "gepa", "gepa_version": version,
        "seed_val_score": result.val_aggregate_scores[0],
        "selected_index": result.best_idx, "selected_val_score": result.val_aggregate_scores[result.best_idx],
        "candidates": [{"index": i, "hash": candidate_hash(c)[:16], "parents": result.parents[i],
                        "val_score": result.val_aggregate_scores[i], "discovered_at_rollout": discovered[i]}
                       for i, c in enumerate(result.candidates)],
        "total_metric_calls": result.total_metric_calls, "num_full_val_evals": result.num_full_val_evals,
        "val_subscores_selected": result.val_subscores[result.best_idx]})
