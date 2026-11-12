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

It downloads the 89 tasks (47 MB; the images, 120-150 GB in all, are pulled by the trials), checks them against the
lock, and writes `utils/tasks.jsonl` (four rows per task, `<task>#1` to `#4`, and the smoke rows `fix-git#0`,
`log-summary-date-ranges#0`), `utils/splits/` and `utils/image_sizes.json`.

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
| Time per task | 3,600 s, every task | 3,600 s (`agent_timeout_sec: null` runs the tasks' own 900-12,000 s, as the leaderboard does) |
| Iterations | 10; the best by pass@1 | the same (`best_ever`) |
| Models | GPT-5.4 (high) for the Code Agent, the Debugger and the Evolve Agent | one model for all three: GPT-5.6-Luna or Claude Haiku 5.5, medium |
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
this benchmark with the protocol above at the lowest cost.

| Method | What its paper reports on Terminal-Bench | Search cost in trials |
|---|---|---|
| AHE ([2604.25850](https://arxiv.org/abs/2604.25850)) | 2.0: pass@1 69.7 to 77.0 (+7.3) over its seed, GPT-5.4; Codex 71.9 | 10 x 178 = 1,780 |
| RRSI ([2609.24972](https://arxiv.org/abs/2609.24972)) | 2.1: 74.2 to 80.2 (+6.0, 95% CI +4.3 to +9.3), Claude Opus 4.8; 64.6 to 78.7 on Gemini 3.5 Flash | 1 + 20 rounds x 2 candidates = 41 x 178 = 7,298 |
| Meta-Harness ([2603.28052](https://arxiv.org/abs/2603.28052)) | 2.0: 76.4 against Terminus-KIRA's 74.7 (+1.7; Terminus-KIRA and Terminus 2 are its two starting harnesses) on Claude Opus 4.6, 37.6 against 33.7 (+3.9) on Claude Haiku 4.5 | a 10-iteration search, each candidate run on all 89 tasks (trials per task not given) |

[`rrsi_adapter`](../../docs/methods/rrsi_adapter.md) is the choice to try second: its regularizers target the
overfitting that in-sample selection invites, at four times AHE's cost.

```bash
hermit train --config examples/terminal_bench_2_0/ahe_adapter/smoke_codex.yaml   # 2 tasks, 2 steps
hermit train --config examples/terminal_bench_2_0/ahe_adapter/run_codex.yaml     # again to resume
hermit eval  --config examples/terminal_bench_2_0/ahe_adapter/run_codex.yaml --adapter root
```

`hermit train` ends by testing the final adapter on the test rows (`runs/codex-ahe/eval/final/`). Report four
numbers: no adapter (the baseline), the seed (`root`), the final adapter, each on rollouts #3 and #4, and the paper's
own measure, the best iteration's pass@1 on its training rollouts (`runs/codex-ahe/workspace/state/best_ever.json`);
the last is selected on the rollouts it reports, so it overstates what the adapter does on fresh ones.

To test the final adapter on another benchmark (SWE-bench Verified and Terminal-Bench-Science do so), write it out
as a directory:

```bash
mkdir -p ahe-final && git -C examples/terminal_bench_2_0/ahe_adapter/runs/trees/codex-ahe archive codex-ahe/final | tar -x -C ahe-final
```

## Results

Not run yet. Each row is one run of the configs above.

| Acting agent and learners | No adapter | Seed (`root`) | Final | Best iteration in sample |
|---|---|---|---|---|
| Codex, GPT-5.6-Luna | | | | |
| Claude Code, Claude Haiku 5.5 | | | | |
