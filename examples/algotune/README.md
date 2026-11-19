# AlgoTune

[AlgoTune](https://algotune.io/) through Harbor: 154 numerical programming tasks, each asking for a faster `Solver`
than a reference one, scored by the speedup the verifier times on 100 instances (1 when an output is invalid or the
solver is not faster). AlgoTune's score of a set of tasks is the harmonic mean of the speedups. The task set is the
Harbor registry's `algotune@1.0`, locked in `utils/dataset.lock`. The method is SkillOpt in the SkillOpt paper's
protocol; the baseline is the same acting agent with no adapter, on the same test tasks.

| Folder | What it runs |
|---|---|
| `utils/benchmark.yaml` | the task set (SkillOpt's split) and the settings every config shares: 64 GB a container, thread pools of the task's 8 CPUs, no core files, the verifier's 7,200 s, no web search, the disk gate |
| `utils/judge.py` | a trial's score, 1 - 1/speedup: the mean m of the scores gives the harmonic mean as 1 / (1 - m) |
| `baseline/` | `codex.yaml`, `claude.yaml`: `hermit eval --adapter none` on the 108 test tasks (all 154 with `data.test=../utils/splits/all.txt` and a run name of its own: [Baseline](#baseline)) |
| `skillopt/` | `run_codex.yaml`, `run_claude.yaml`: 4 SkillOpt epochs (`hermit train`), then the final skill's test (`hermit eval --adapter final`); `smoke_*.yaml` first |

Each config comes twice: Codex with GPT-5.6-Luna on a ChatGPT login, and Claude Code with Claude Haiku 5.5 on a Claude
subscription, both at medium effort, both acting and learning.
[Run the Harbor benchmarks](../README.md#harbor-benchmarks) says what a machine needs and how to switch to another
model or CLI.

## Prepare

```bash
python -m hermit.harbor.dataset prepare examples/algotune/utils/benchmark.yaml
```

It downloads the 154 tasks (7 MB; each trial builds its image, Python with the solver packages), checks them against
the lock, and writes `utils/tasks.jsonl` (one row per task, and the smoke rows `algotune-aes-gcm-encryption#0`,
`algotune-count-connected-components#0`) and `utils/splits/`: SkillOpt's 2:1:7 with its seed 42, 31 train, 15
validation and 108 test tasks.

## The score

`utils/judge.py` scores a trial 1 - 1/speedup: 0 with no speedup, 0.5 at 2x, 0.9 at 10x. AlgoTune's own score is the
harmonic mean H of the speedups, and the mean m of these scores is 1 - 1/H, so every mean the framework takes (the
Trainer's, SkillOpt's gate, `hermit eval`'s `mean_score`) moves with AlgoTune's score, and one large speedup cannot hide
slow tasks. `python examples/algotune/utils/judge.py <eval dir>` prints H from an eval's `results.jsonl`, over the
trials that are the agent's result, with the infra ones counted apart as `python -m hermit.harbor.report` counts them
(`hermit eval`'s `mean_score` counts them as 0, as no speedup); each trial's speedup is its judge record's
`extra.speedup`. Harbor's oracle agent runs the reference solver: speedup 1x, score 0. It checks the task set and the
timing verifier, not a ceiling.

## Baseline

```bash
hermit eval --config examples/algotune/baseline/codex.yaml --adapter none
hermit eval --config examples/algotune/baseline/codex.yaml --adapter none \
    data.test=../utils/splits/all.txt trainer.run_name=codex-baseline-all
```

108 trials (the test tasks), or 154 (the whole benchmark, as AlgoTune reports it): `data.test` is relative to the
config's folder, and the run name keeps the two evals apart.

## SkillOpt

Why this method: none of this framework's methods reports AlgoTune. SkillOpt's paper (Yang et al., 2026,
[arXiv:2605.23904v2](https://arxiv.org/abs/2605.23904v2)) reports the largest gains with a coding CLI acting on code
generation, the nearest task to this one: on SpreadsheetBench, where the agent writes and runs code, GPT-5.5 in the
Codex harness goes from 27.5 with no skill to 85.0, and in the Claude Code harness from 22.1 to 80.4. Its skill is a
text the CLI reads, the kind of general advice (profile first, the compiled packages to reach for, checking outputs
against the reference) a skill can carry from one AlgoTune task to the next. GEPA is the other candidate: its paper uses
it as an inference-time search for kernel code (§5.1: KernelBench, scored by speed over a reference, and NPUEval, by
vector utilization), on the task set it reports.

| | The SkillOpt paper | Here |
|---|---|---|
| Split | train / selection / test 2:1:7, seed 42 | the same: 31 / 15 / 108 tasks |
| Epochs, batch | 4 epochs, rollout batch 40, reflection minibatch 8 | the same (one step an epoch: 31 tasks) |
| Edits | learning rate 4, cosine to 2 | the same |
| Gate | strict: a candidate kept only above the current selection score (ties rejected) | the same, the score the mean of 1 - 1/speedup (SkillOpt's soft metric): a speedup has no pass or fail |
| Success | a solved task: what the analysts and the slow update sort the rollouts by | a solver faster than the reference, as the verifier's own test has it (`success_score`: any score above 0) |
| Epoch end | from the second epoch: the slow update on 20 tasks, gated, and the meta skill (the learner's own guidance, never deployed) | the same |
| Final | the best skill | the same |
| Models | GPT-5.5 (medium) as student and teacher | one model for both: GPT-5.6-Luna or Claude Haiku 5.5, medium |
| Trace summary | SkillOpt's Codex harness gives the analysts a summary of the Codex session | none (`codex_trace_to_optimizer: false`): the summary reads SkillOpt's own harness (`task.md`, `SKILL.md`, `<answer>`), not a Harbor task |
| Starting skill | the benchmark's own | none: the prompt-only template's `ADAPTER.md`, a placeholder with no instructions |

```bash
hermit train --config examples/algotune/skillopt/smoke_codex.yaml   # 2 tasks, 2 epochs
hermit eval  --config examples/algotune/skillopt/smoke_codex.yaml --adapter final
hermit train --config examples/algotune/skillopt/run_codex.yaml     # again to resume
hermit eval  --config examples/algotune/skillopt/run_codex.yaml --adapter final
hermit eval  --config examples/algotune/skillopt/run_codex.yaml --adapter root
```

`--adapter final` tests the final skill on the 108 test tasks (`runs/codex-skillopt/eval/final/`) and `--adapter root`
the empty skill, both after `hermit train` (adapters of its tree); report them beside the baseline, as harmonic
means.

SkillOpt's validations (the starting skill's, each gate's and the slow update's comparison) score a trial that is not
the agent's result (infra) 0, as a failed one: one in the starting skill's validation lowers the bar for the whole run
(SkillOpt keeps that score), one in a candidate's can get it rejected. `hermit eval` counts such a trial as 0 in
`mean_score` too, and `utils/judge.py` leaves it out of H and counts it (`infra`): report that H, and give that count
beside each number.

## Costs

A trial takes the agent's hour at most and the verifier's timing (two minutes to over an hour on a loaded host). A
container takes 8 CPUs and up to 64 GB, so 6 at a time need 48 CPUs and 384 GB. SkillOpt's run is at most 364 training
and validation trials, then 108 for the test: 15 for the starting skill's validation; each epoch 31 training rollouts
and 15 for its gate; from the second epoch, 40 for the slow update's comparison of two skills on 20 tasks and 15 for its
gate. A skill SkillOpt has validated is not validated again (it keeps its scores), so a run may take fewer. A resumed
run runs the step it stopped in again, its rollouts and its validations (SkillOpt keeps its state at a step's end), so a
run stopped mid-step takes more. Speedups are timed on the machine that runs the trials, so a profile's three arms (the
baseline, `--adapter root` and SkillOpt's) run on one machine: a loaded host lowers every arm's and makes the 15-task
gate noisier, and a verifier that runs out of its time scores 0, as no speedup (`verifier_timeout: agent`), whatever
slowed it. A learner's call waits for the plan's window however long it holds (`llm.plan_max_wait_s: null`);
`trainer.call_timeout_min` (a week) still bounds a step.

## Results

Not run yet. AlgoTune's harmonic-mean speedup on the 108 test tasks.

| Acting agent and learner | No adapter | Empty skill (`root`) | SkillOpt's final skill |
|---|---|---|---|
| Codex, GPT-5.6-Luna | | | |
| Claude Code, Claude Haiku 5.5 | | | |
