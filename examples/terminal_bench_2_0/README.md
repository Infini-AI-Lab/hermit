# Terminal-Bench 2.0

[Terminal-Bench 2.0](https://www.tbench.ai/) through Harbor: 89 terminal tasks (4 easy, 55 medium, 30 hard), each in
its own container, scored 1 when the task's hidden tests pass, else 0. The task set is the Harbor registry's
`terminal-bench@2.0`, locked in `utils/dataset.lock`. The method is the AHE Adapter in the AHE paper's own protocol on
this benchmark; the baseline is the same acting agent with no adapter, under the same protocol.

| Folder | What it runs |
|---|---|
| `utils/benchmark.yaml` | the task set and the settings every config shares: an hour per task (the AHE paper's), no web search, the verifier's limit x5, the disk gate |
| `utils/rows/` | the protocol's rows: `train.txt`, every task's rollouts #1 and #2; `test.txt`, rollouts #3 and #4 |
| `baseline/` | `codex.yaml`, `claude.yaml`: `hermit eval --adapter none` on the test rows |
| `ahe_adapter/` | `run_codex.yaml`, `run_claude.yaml`: ten AHE iterations, then the final adapter's test; `smoke_*.yaml` first |

Each config comes twice: Codex with GPT-5.6-Luna on a ChatGPT login, and Claude Code with Claude Haiku 5.5 on a Claude
subscription, both at medium effort, both acting and learning.
[Run the Harbor benchmarks](../README.md#harbor-benchmarks) says what a machine needs and how to switch to another
model or CLI.

## Prepare

```bash
python -m hermit.harbor.dataset prepare examples/terminal_bench_2_0/utils/benchmark.yaml
```

It downloads the 89 tasks (47 MB; the images are pulled by the trials), checks them against the lock, and writes
`utils/tasks.jsonl` (four rows per task, `<task>#1` to `#4`, and the smoke rows `fix-git#0`,
`log-summary-date-ranges#0`), `utils/splits/` and `utils/image_sizes.json`.

The images are Docker Hub's (`alexgshaw/<task>`), 49 GB compressed and 120-150 GB on disk. The benchmark file
removes a task's image after its trials (`docker_cleanup: rmi_task_image`), so each AHE step pulls every image again
(89 images; a second pull when a task's two rollouts do not overlap): sign in to Docker Hub (`docker login`), whose
pull limits are lower without an account, or, where Docker's disk holds them all, set `docker_cleanup: images_keep`
in `utils/benchmark.yaml` and pull each image once.

## The protocol

The AHE paper (Lin et al., 2026, [arXiv:2604.25850](https://arxiv.org/abs/2604.25850)) evolves and reports on all 89
tasks: each iteration runs every task k = 2 times, an hour each, and the reported number is pass@1 of the best of ten
iterations on those same rollouts ("in sample", §4.1). This example keeps that, and adds a fresh measurement: the
training steps run rollouts #1 and #2 of each task, and every adapter is tested on rollouts #3 and #4, which no step
ran, so the trial cache never hands a test a training trial.

| | The AHE paper | Here |
|---|---|---|
| Tasks | 89, evolved on and reported on | the same |
| Rollouts | k = 2 per task and iteration | k = 2 (#1, #2); the test's two (#3, #4) |
| Time per task | 3,600 s, every task | 3,600 s (`agent_timeout_sec: null` runs the tasks' own 600-12,000 s, as the leaderboard does) |
| Iterations | 10; the best by pass@1 | the same (`best_ever`) |
| Rollback | an edit whose predicted fixes do not materialize, reverted file by file (§1, Fig. 8) | the port's (`rollback: auto`): an edit the next step's attribution judges HARMFUL; `rollback_verdicts: [HARMFUL, INEFFECTIVE]` reverts the ineffective ones too |
| Models | GPT-5.4 for all three: the Code Agent at high, the Evolve Agent at xhigh (Table 4; the Debugger's effort not given) | one model for all three: GPT-5.6-Luna or Claude Haiku 5.5, medium |
| Seed | NexAU0: a shell tool, no skills, no middleware, no memory | the `ahe` template: three seed rules, an empty memory, a middleware that only hands the memory to the model |
| Acting agent | NexAU, whose code AHE edits | Codex or Claude Code, fixed; AHE edits the adapter |
| Concurrency | 96 (E2B) | 6 (one Docker host) |
| Explore Agent | one-shot skill seeding | not ported |

## Baseline

```bash
hermit eval --config examples/terminal_bench_2_0/baseline/codex.yaml --adapter none
```

178 trials (89 tasks x rollouts #3 and #4). pass@1 is the summary's `mean_score`, pass@2 its `pass_at_k`. Run
`--adapter root` with the method's config too: the seed adapter alone (AHE's NexAU0 row).

## AHE Adapter

Why this method: of the methods in this framework, three report Terminal-Bench 2 numbers, and AHE's is measured on
this benchmark with the protocol above, at the smaller search of the two whose trials can be counted.

| Method | What its paper reports on Terminal-Bench | Search cost in trials |
|---|---|---|
| AHE ([2604.25850](https://arxiv.org/abs/2604.25850)) | 2.0: pass@1 69.7 to 77.0 (+7.3) over its seed, GPT-5.4; Codex 71.9 | 10 x 178 = 1,780 |
| RRSI ([2609.24972](https://arxiv.org/abs/2609.24972)) | 2.1: 74.2 to 80.2 (+6.0, 95% CI +4.3 to +9.3), Claude Opus 4.8; 64.6 to 78.7 on Gemini 3.5 Flash | about 41 x 178 = 7,298 (1 + 20 rounds x 2 candidates; fewer when its critic rejects a candidate before its evaluation, more with the base harness measured again) |
| Meta-Harness ([2603.28052](https://arxiv.org/abs/2603.28052)) | 2.0: 76.4 on Claude Opus 4.6, against 74.7 for Terminus-KIRA, one of its two starting harnesses, on the leaderboard (64.4 in the search's own measurement, App. A.2); 37.6 on Claude Haiku 4.5, against the leaderboard's next agent, Goose, at 35.5 (+2.1) | a 10-iteration search, each candidate run on all 89 tasks (trials per task not given) |

[`rrsi_adapter`](../../docs/methods/rrsi_adapter.md) is the choice to try second: its regularizers target the
overfitting that in-sample selection invites, at about four times AHE's trials.

```bash
hermit train --config examples/terminal_bench_2_0/ahe_adapter/smoke_codex.yaml   # 2 tasks, 2 steps
hermit train --config examples/terminal_bench_2_0/ahe_adapter/run_codex.yaml     # again to resume
hermit eval  --config examples/terminal_bench_2_0/ahe_adapter/run_codex.yaml --adapter root
```

`hermit train` ends by testing the final adapter on the test rows (`runs/codex-ahe/eval/final/`). Report four
numbers: no adapter (the baseline), the seed (`root`), the final adapter, each on rollouts #3 and #4, and the paper's
own measure, the best iteration's pass@1 on its training rollouts (`runs/codex-ahe/workspace/state/best_ever.json`);
the last is selected on the rollouts it reports, so it overstates what the adapter does on fresh ones. The two kinds
count a trial that is not the agent's result (infra) differently: the iteration's pass@1 counts it as a failure, as
the paper does, and `hermit eval` leaves it out of `mean_score` and counts it in its summary's `unscored`; give that
count beside each number.

The learners run on the same plan as the acting agent: on the smoke's two tasks, one step's Debugger and Evolve
Agent read about 450K tokens (Codex) and 840K (Claude Code), and a step of 178 trials gives them more to read. On a
Claude subscription a learner session waits for the plan's window however long it holds (`plan_max_wait_s: null`).

To test the final adapter on another benchmark (SWE-bench Verified and Terminal-Bench-Science do so), write it out
as a directory, one for each profile, under the run's git-ignored `runs/` (`mkdir` refuses a folder that exists, so
an earlier export is never mixed in):

```bash
mkdir examples/terminal_bench_2_0/ahe_adapter/runs/ahe-final-codex && git -C examples/terminal_bench_2_0/ahe_adapter/runs/trees/codex-ahe archive codex-ahe/final | tar -x -C examples/terminal_bench_2_0/ahe_adapter/runs/ahe-final-codex
```

The Claude Code run's is `ahe-final-claude`, from `runs/trees/claude-ahe` and its tag `claude-ahe/final`. Whoever
runs a transfer elsewhere gets that folder and passes its path to `--adapter`.

## Results

Not run yet. Each row is one run of the configs above.

| Acting agent and learners | No adapter | Seed (`root`) | Final | Best iteration in sample |
|---|---|---|---|---|
| Codex, GPT-5.6-Luna | | | | |
| Claude Code, Claude Haiku 5.5 | | | | |
