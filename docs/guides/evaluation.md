# Evaluate an adapter

Evaluation answers whether a selected adapter helps the acting agent on a fixed task protocol. Training scores,
file-check success, and a learner's own claims are different evidence.

## Choose the comparison

| Selector | What the pipeline receives | Question |
|---|---|---|
| `none` | `adapter=None` | How does the agent perform without this adapter? |
| `root` | The template's initial adapter | What does the seed already contribute? |
| `final` | The run's final selection | What did this training procedure deliver? |
| Tag or commit | That valid adapter version | How did an earlier checkpoint perform? |
| Adapter directory | That directory, resolved relative to the config | How does an included or external adapter perform? |

`final` is the Evolver's final selection, defaulting to the current adapter. It is not a universal best-score
checkpoint. GEPA and SkillOpt use their retained validation candidates for final selection.

## Keep the protocol fixed

Keep acting model, model settings, tools, task splits, judge, budgets, and repetitions comparable.
The pipeline must handle `None` as a genuine no-adapter baseline. A tool-exporting seed can already help;
report both `none` and `root` when that distinction matters.

Train tasks supply learning evidence. Validation tasks guide candidate selection. Test tasks measure the selected
result. Do not select a checkpoint on test and then report the same test as untouched evaluation.
Every task field is visible in learning material when that task is used for training; private labels should live
in judge-owned storage rather than task rows. Filesystem grants must also preserve the intended boundary.

## Run repeated tests

```bash
hermit eval --config run.yaml --adapter none eval.repeats=3
hermit eval --config run.yaml --adapter root eval.repeats=3
hermit eval --config run.yaml --adapter final eval.repeats=3
```

Results are written under `<output_dir>/<run_name>/eval/<selector>/`. Re-evaluating the same selector replaces its
`results.jsonl` and `summary.json`; archive records or use separate output directories when preserving comparisons.

| Summary field | Meaning |
|---|---|
| `tasks`, `repeats` | Test task count and attempts per task |
| `mean_score` | Arithmetic mean across all task attempts |
| `solved_once` | Fraction of tasks with at least one attempt scoring **at least 1.0** |
| `errors` | Pipeline or judge failures, recorded with score zero |
| `cost_usd` | Reported evaluation cost, or null if some task costs are unknown |
| `unknown_cost_tasks` | Calls/tasks with unknown cost in the evaluator's accounting |
| `commit` | Resolved Git commit for tree selectors; null for a directory or no adapter |

The `solved_once` threshold is fixed at 1.0. For a 0–100 score or a non-success metric, it is not a meaningful
solved rate; use the benchmark's own definition and raw scores. It is also distinct from mean per-attempt accuracy.

## Preserve the artifact and evidence

With the literal `--adapter final`, evaluation updates `.hermit/CARD.md` and may make a new card commit and move the
final tag. Preserve the evaluation summary's `commit`: it identifies the version used for that measurement.
A directory selector records no Git commit, so record its revision or content separately.

Report dataset revision, split sizes, baseline, final mean, repeats, errors, and known/unknown cost. The built-in
summary does not compute uncertainty intervals or significance tests. Inspect per-task outcomes before interpreting
small changes.

## Ordered streams

ServeLearnBench and CL-Bench have task-specific state and ordering. Preserve stream order and benchmark rules.
ServeLearnBench tests each environment window using the corresponding adapter checkpoint through its window
evaluator; its recipe then runs `slb score`. Do not substitute one aggregate `eval --adapter final` for that protocol.

See [ServeLearnBench](../recipes/servelearnbench.md) and [CL-Bench](../recipes/clbench.md). A smoke subset is a wiring
check, and the deterministic [Quick Start](../get-started/quickstart.md) is a teaching demonstration. Neither
establishes real-model learning performance.
