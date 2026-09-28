"""GEPA (Agrawal et al., ICLR 2026; github.com/gepa-ai/gepa) as an optimizer
Evolver.

The upstream library does the optimizing: its engine, Pareto candidate
selection, reflective mutation prompt, minibatch acceptance and budget
accounting run unchanged through gepa.optimize(). This module supplies only
what GEPA asks a new system to supply, a GEPAAdapter:

- a candidate is the adapter's text surface, {relative path: text};
- evaluate() materializes the candidate as an adapter directory and runs the
  batch through the Environment (the acting harness with that adapter);
- make_reflective_dataset() turns each rollout into GEPA's
  Inputs / Generated Outputs / Feedback record, with the agent's trajectory
  as the execution trace and the grader's feedback as GEPA's feedback text.

GEPA's run directory is the session's Training State: gepa_state.bin is
written every iteration, and a session started from it resumes the search.
"""
from __future__ import annotations

import importlib.metadata
import itertools
import json
import threading

from harnessadapter.optimize.plugin import OptimizeContext, Outcome
from harnessadapter.optimize.surface import candidate_hash


def clip_middle(text: str, limit: int) -> str:
    """Keep the beginning (the approach) and the end (how it finished)."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit * 2 // 5
    tail = limit - head
    return text[:head] + f"\n[... {len(text) - limit} characters of the trajectory left out ...]\n" + text[-tail:]


class _Log:
    """GEPA's LoggerProtocol, so gepa does not redirect this process's stdout."""

    def __init__(self, sink):
        self.sink = sink

    def log(self, message: str):
        self.sink(f"[gepa] {message}")


class HarnessAdapterGEPAAdapter:
    """gepa.core.adapter.GEPAAdapter for harness adapters in an Environment."""

    propose_new_texts = None

    def __init__(self, context: OptimizeContext, tasks, trace_chars: int, *, skip_perfect_score: bool = True,
                 perfect_score: float = 1.0):
        self.context = context
        self.prompts = {t.id: t.prompt for t in tasks}
        self.trace_chars = trace_chars
        self.skip_perfect_score = skip_perfect_score
        self.perfect_score = perfect_score
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def evaluate(self, batch, candidate, capture_traces=False):
        from gepa import EvaluationBatch
        with self._lock:
            number = next(self._counter)
        adapter = self.context.store.path(candidate)
        kind = "minibatch" if capture_traces else "val"
        label = f"gepa/{number:04d}-{kind}-{candidate_hash(candidate)[:8]}"
        rollouts = self.context.environment.evaluate(adapter, list(batch), label=label)
        return EvaluationBatch(outputs=[r.public() for r in rollouts],
                               scores=[float(r.score) for r in rollouts],
                               trajectories=list(rollouts) if capture_traces else None)

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        # An episode the infrastructure lost is not evidence about the text.
        rollouts = [r for r in (eval_batch.trajectories or []) if not r.infra]
        # With no evidence left, or only perfect episodes once losses are set
        # aside (GEPA's own skip_perfect_score test counted the losses as 0),
        # an empty dataset makes GEPA skip the proposal: no reflection call,
        # no child rollouts.
        if not rollouts or (self.skip_perfect_score and all(r.score >= self.perfect_score for r in rollouts)):
            return {name: [] for name in components_to_update}
        records = []
        for rollout in rollouts:
            records.append({
                "Inputs": {"Task given to the agent": self.prompts[rollout.task_id]},
                "Generated Outputs": {
                    "Final answer": rollout.output or "(no final answer)",
                    "Run status": rollout.status,
                    "Agent trajectory (commands, observations and messages)": clip_middle(rollout.trace, self.trace_chars),
                },
                "Feedback": f"Score {rollout.score:g}. " + (rollout.feedback or ""),
            })
        return {name: records for name in components_to_update}


class GEPAPlugin:
    name = "gepa"

    def run(self, context: OptimizeContext) -> Outcome:
        import gepa

        s = context.settings
        try:
            version = importlib.metadata.version("gepa")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown (imported from source without metadata)"
        train = context.environment.tasks("train")
        val = context.environment.tasks("val")
        adapter = HarnessAdapterGEPAAdapter(context, train + val, int(s["trace_chars"]),
                                            skip_perfect_score=bool(s["skip_perfect_score"]),
                                            perfect_score=float(s["perfect_score"]))
        context.log(f"[gepa] train={len(train)} val={len(val)} budget={s['max_metric_calls']} rollouts; "
                    f"components={context.surface.components}")
        result = gepa.optimize(
            seed_candidate=context.seed(), trainset=train, valset=val, adapter=adapter,
            reflection_lm=context.lm,
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
        candidates = [{"index": i, "hash": candidate_hash(c)[:16], "parents": result.parents[i],
                       "val_score": result.val_aggregate_scores[i],
                       "discovered_at_rollout": (result.discovery_eval_counts or [None] * len(result.candidates))[i]}
                      for i, c in enumerate(result.candidates)]
        (context.state_dir / "harnessadapter-candidates.json").write_text(json.dumps(
            [{"index": i, "texts": c} for i, c in enumerate(result.candidates)], ensure_ascii=False, indent=1) + "\n")
        summary = {"algorithm": "gepa", "gepa_version": version,
                   "seed_val_score": result.val_aggregate_scores[0],
                   "selected_index": result.best_idx, "selected_val_score": result.val_aggregate_scores[result.best_idx],
                   "candidates": candidates, "total_metric_calls": result.total_metric_calls,
                   "num_full_val_evals": result.num_full_val_evals,
                   "val_subscores_selected": result.val_subscores[result.best_idx]}
        return Outcome(candidate=dict(result.best_candidate), summary=summary)
