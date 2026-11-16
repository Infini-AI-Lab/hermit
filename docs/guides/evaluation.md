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
checkpoint. GEPA and SkillOpt use their retained validation candidates for final selection. A Harbor task set whose
acting agent is one of Harbor's own takes no adapter: only `--adapter none` evaluates it, and `hermit check` reports it
for a training config.

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
A config for `hermit eval` alone (no `data.train`) needs none of the training keys, but an adapter of a run's tree
(`final`, `root`, a tag or a commit) needs `template` or `trainer.tree_dir`, and the tree a `hermit train` made: without
one, `eval` refuses it before anything runs. With `eval.repeats` above 1, each repeat hands a Harbor task set's
pipeline, and so the judge, copies of the test rows that name it (`repeat`, which no record's `input` keeps; any other
pipeline gets the rows as they are): with its `trial_cache` each repeat is a trial of its own, and each repeat of a
multi-step task, in each run of `hermit eval` (one into the same output included), a
stream of its own, stopped when the eval ends, fails or is stopped with Ctrl-C (a process killed by a signal leaves it
up for `stream.idle_s`) ([Streams](../framework/task.md#benchmarks)).

| Summary field | Meaning |
|---|---|
| `tasks`, `repeats` | Test rows and attempts per row (`eval.repeats`): a Harbor task set indexed with k rollouts (`dataset index --rollouts k`, or `rollouts: k` in a benchmark file's `dataset:` block) has k rows per task, `<task>#1..k`, each counted |
| `mean_score` | Arithmetic mean across the scored attempts (null when none was scored) |
| `solved_once` | Fraction of the test tasks with a scored attempt (each id once: a row a split names twice counts once) that have at least one attempt scoring **at least 1.0** |
| `scored`, `unscored` | Attempts with a verdict, and those that were not the agent's result (status `infra`: the host, the provider), left out of the summary's means and shares |
| `errors` | Pipeline or judge failures, recorded with score zero |
| `infra_rerun` | With `eval.infra_retries`: the attempts run again because they ended `infra` |
| `cost_usd` | Reported evaluation cost, or null if some task costs are unknown |
| `unknown_cost_tasks` | Calls/tasks with unknown cost in the evaluator's accounting |
| `commit` | Resolved Git commit for tree selectors; null for a directory or no adapter |

The `solved_once` threshold is fixed at 1.0. For a 0–100 score or a non-success metric, it is not a meaningful
solved rate; use the benchmark's own definition and raw scores. It is also distinct from mean per-attempt accuracy.

`eval.infra_retries: N` runs the `infra` rows again, up to N passes, `eval.infra_wait_s` before each; a halt (a lost
login, a usage limit) stops the reruns until a person restores it. `results.jsonl` keeps each row's latest run; the
summary's `cost_usd` counts the runs it replaced too, and `infra_rerun` how many ran again. A multi-step task's step
rows are not run again (a step runs only in its task's stream, after the steps before it), nor is a task Harbor runs
for none of Hermit's agents (`unsupported_task`: a rerun would be refused the same way). A split that lists a step
ahead of an earlier step of its stream, or without an earlier step of its stream, is refused before any run.

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
