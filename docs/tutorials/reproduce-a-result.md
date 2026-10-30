# Reproduce a result

Start from a specific experiment and artifact, then match its data, model, and evaluation protocol.
An existing launcher saves setup work; it does not by itself guarantee the same measurements.

## Choose the protocol

| Experiment | Starting page | Evidence to preserve |
|---|---|---|
| OfficeQA, SkillOpt paper setting | [OfficeQA](../recipes/officeqa.md) | Dataset revision, seed skill, 50/24/172 split, source-file tools and scorer, acting and learner settings |
| OfficeQA, ReHarness (the Flash row: the seed-0 stream, or the frozen bank) | [OfficeQA](../recipes/officeqa.md#reharness) | Which protocol and footing (judge feedback or none), the stream order, the embedding model and relay ledger, the Codex image, framework commit |
| ServeLearnBench retail_l3 | [ServeLearnBench](../recipes/servelearnbench.md) | Scenario revision, ordered serving stream, block size, per-window adapter selection |
| CL-Bench database/poker | [CL-Bench](../recipes/clbench.md) | Venue checkout, order, environment reset behavior, model settings, outcome ledger; for ReHarness the embedding model, `embed_wire_ledger.json` and the launcher's setting stamp in `order<i>/run.log` (framework and clbench revisions) |
| Lean collaboration | [Lean recipe](../recipes/lean-collaboration.md) | Adapter version, theorem set, Lean/Mathlib, host/team-service configuration |

## Before spending on a full run

1. Read the included adapter's `.hermit/CARD.md` and the recipe's limitations.
2. Record framework and dependency revisions, dataset preparation, split ids, and seed adapter.
3. Verify the acting model and learner model separately, including reasoning settings, tool budgets, and endpoint prices.
4. Run the recipe's smoke configuration or prepared prefix. Inspect task outputs, judge feedback, and usage.
5. Preserve the original config and choose a new run name/output location for your reproduction.

A smoke run verifies wiring. It does not reproduce a full result. The deterministic
[Quick Start](../get-started/quickstart.md) is a separate contract demonstration.

## Compare like with like

Evaluate no adapter, the seed, and the learned adapter using the same acting pipeline and held-out protocol.
Validation selection is part of training; test data must not become a checkpoint-selection set.
For streams, use the benchmark's time/window semantics rather than replacing them with one final-adapter test.

Preserve `steps.jsonl`, evaluation summaries and per-task records, adapter commit, `.hermit/CARD.md`, and model-usage
ledgers. Report errors and unknown cost. A changed hosted model or external dataset revision can prevent an exact
reproduction even with the same framework commit.

See [Evaluation](../guides/evaluation.md) for selectors and metrics, [Run records](../guides/runs.md) for artifact
locations, and the [recipe catalog](../recipes/index.md) for prerequisites.
