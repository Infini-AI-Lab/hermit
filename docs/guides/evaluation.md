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
acting agent is one of Harbor's own takes no adapter: only `--adapter none` evaluates it (given any other, its
rollouts fail).

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
On a Harbor task set each repeat runs a multi-step task's stream again from its first step, in a new environment,
once the repeat before it has finished the stream ([Benchmarks](../framework/task.md#benchmarks)).

| Summary field | Meaning |
|---|---|
| `tasks`, `repeats` | Test task count and attempts per task (a Harbor task set indexed with k rollouts, `dataset index --rollouts k`, has k rows per task, `<task>#1..k`, each counted) |
| `mean_score` | Arithmetic mean across all task attempts |
| `solved_once` | Fraction of tasks with at least one attempt scoring **at least 1.0** |
| `errors` | Pipeline or judge failures, recorded with score zero (on a Harbor task set, a trial that was not the agent's result among them, below) |
| `cost_usd` | Reported evaluation cost, or null if some task costs are unknown |
| `unknown_cost_tasks` | Calls/tasks with unknown cost in the evaluator's accounting |
| `commit` | Resolved Git commit for tree selectors; null for a directory or no adapter |

The `solved_once` threshold is fixed at 1.0. For a 0–100 score or a non-success metric, it is not a meaningful
solved rate; use the benchmark's own definition and raw scores. It is also distinct from mean per-attempt accuracy.

On a Harbor task set, a trial that is not the agent's result (`infra`: the host, the model provider, a session that
never started) runs again in the runner itself (`agent.params.infra_retries`, default 2, each after `infra_wait_s`,
default 300 s), unless its cause runs no better again: a disk gate that waited its time out, a provider's refusal of
the run's configuration (`model_rejected`), a host failure (`host_failed`: a jobs directory that cannot be made, or
past the trial's staging, where its session may have run), a task Harbor runs for none of Hermit's agents, or a
multi-step task's step. One that ends `infra` all the
same gets no score: the judge refuses it, so its record has status `judge_error`, its cost and log kept, and it counts
in `errors` and as 0 in `mean_score`. A benchmark's own figure leaves such trials out: `python -m hermit.harbor.report
<output_dir>/<run_name>/eval/<selector>` reads the eval's `results.jsonl` and gives, over all repeats and for each,
the trials scored, infra and otherwise failed, pass@1 over the scored ones (the judge's `passed`), their mean score
and `mean_score`. A lost sign-in, an API account's usage limit, or a provider that does not bill the account (its
quota or credit spent), stops the eval
([Benchmarks](../framework/task.md#benchmarks)): it writes no row of the repeat it stopped in (a repeat's rows are
written once all its trials are done), so its `results.jsonl` holds the repeats it finished, none with one repeat,
beside the `summary.json` of the eval before, if any (the eval writes its summary when it ends). The report says such
an eval did not finish (`unfinished`, and `finished: false` with `--json`). Run it again before reading either. Its
content cannot tell an eval stopped in a later repeat whose finished repeats happen to match the summary of the eval
before (as many rows, failed runs and the same mean: a smoke run again with more repeats); where `results.jsonl` was
written more than a minute after `summary.json`, which a finished eval in place never does, the report adds a `note`
saying so (a copy of the folder can do it too).

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
