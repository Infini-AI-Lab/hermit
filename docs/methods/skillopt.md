# SkillOpt

SkillOpt makes bounded edits to a skill document, with an optional validation gate, slower epoch-end updates,
and a meta skill. Choose it when the learnable object is a skill or instruction document and you can afford
validation rollouts.

## Setup

From the repository root, with pip 25.1+ ([Installation](../get-started/installation.md)):

```bash
python -m pip install -e . --group skillopt
```

In a complete task config, select `evolver.name: skillopt`, provide `data.val`, and configure the learner's
`llm`. Start with [OfficeQA](../recipes/officeqa.md), the SkillOpt paper's setting, which also uses SkillOpt on the
acting side.

## Update and selection

Each framework step runs a SkillOpt update on the batch. With `use_gate: true`, the method keeps an update only
if its validation score improves. The slow update and meta skill run at an epoch boundary when enabled.
Finalization considers the method's retained validation candidates.

A slow update can make the last step of an epoch substantially more expensive than other steps. Set
`trainer.call_timeout_min` with that work in mind.

## Parameters and defaults

```{literalinclude} ../../hermit/algorithms/evolvers/skillopt/evolver.yaml
:language: yaml
```

## Key controls and their interactions

| Control | Values / interpretation | Effect |
|---|---|---|
| `file` | One learnable document path | The optimized skill; defaults to `ADAPTER.md` |
| `learning_rate`, `min_learning_rate` | Edit-count bounds / 4 and 2 | Bound selected patches, not model optimizer step size |
| `total_steps`, `lr_scheduler` | Positive step count and `constant`, `linear`, or `cosine` | Null/zero total uses a constant schedule; the update counter advances when patches reach merge/selection |
| `analyst_workers`, `minibatch_size` | Positive parallel-worker and reflection-batch sizes | Control reflection concurrency and grouping independently of `agent.concurrency` |
| `use_gate` | Boolean / true | A candidate must strictly beat the current metric; false still evaluates validation and force-accepts a valid candidate |
| `gate_metric`, `gate_mixed_weight` | `hard`, `soft`, or `mixed`; weight in [0, 1] | Select the metric for validation comparison; see below |
| `use_slow_update`, `slow_update_gate` | Booleans / true and false | Enable epoch-end work and separately choose whether its slow-update candidate must pass validation |
| `promote_final` | Boolean / true | If final current skill differs from best, re-evaluate it at finalization and promote it only on improvement |
| `codex_trace_to_optimizer` | Boolean / true | Show the analyst each run's Codex trace summary (`codex_trace_summary.txt` in the run's log, written by SkillOpt's Codex harness), as SkillOpt does for a Codex target |

### Understand the validation score

This integration converts each judged attempt to a hard success flag using `score >= success_score` (default 1.0).
Its soft value comes from the judge result's `extra.f1` when present, otherwise the numeric score.
`hard` compares aggregate success, `soft` compares aggregate partial credit, and `mixed` uses
`(1 - weight) * hard + weight * soft`. Keep the components on comparable scales.

Ordinary gating requires strict improvement, so ties reject. The
[pinned upstream gate implementation](https://github.com/microsoft/SkillOpt/blob/79124b37e9a6371e13b753f8bcd7adb1e493ade1/skillopt/evaluation/gate.py)
defines that comparison. Disabling `use_gate` does not save the validation calls in this integration.
Validation is cached by skill content, while final promotion deliberately refreshes the final candidate's score.

### Budget the epoch boundary

With the corresponding features enabled, the end of an epoch performs slow-update and meta-skill work after the
ordinary step update. `slow_update_samples` controls additional training comparisons. The default
`slow_update_gate: false` means the ordinary gate setting does not automatically protect a slow update.
The OfficeQA configs explicitly choose their protocol.

Allow enough `trainer.call_timeout_min` for the entire call, not just a single reflection. Inspect saved
`state/` and `output/report.md` to distinguish no patches, invalid edits, gate rejection, and final promotion.

Do not describe a run as a paper reproduction solely because it uses SkillOpt. Data revision, models, tools,
gating, and trajectory truncation must match the claimed protocol. The
[OfficeQA recipe](../recipes/officeqa.md#reproduction-limits) records the current differences.
