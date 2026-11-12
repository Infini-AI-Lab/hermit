# Terminal-Bench-Science

Terminal-Bench-Science through Harbor: 70 research workflows of the life, physical, earth, mathematical and
engineering sciences, each in its own container with its data, scored 1 when the task's hidden tests (run without
network) pass, else 0. The task set is the Harbor hub's `terminal-bench-science/terminal-bench-science@0.1.0`, locked
in `utils/dataset.lock`. Two ways to improve on the baseline: test AHE's adapter from Terminal-Bench 2.0 here, frozen
(cheap), or run the AHE Adapter here in its paper's protocol (long).

| Folder | What it runs |
|---|---|
| `utils/benchmark.yaml` | the task set and the settings every config shares: each task's own 8 hours, no web search, the disk gate |
| `utils/rows/` | the protocol's rows: `train.txt`, every task's rollouts #1 and #2; `test.txt`, rollouts #3 and #4 |
| `baseline/` | `codex.yaml`, `claude.yaml`: `hermit eval` on the test rows, with no adapter or with an adapter directory |
| `ahe_adapter/` | `run_codex.yaml`, `run_claude.yaml`: ten AHE iterations, then the final adapter's test; `smoke_*.yaml` first |

Each config comes twice: Codex with GPT-5.6-Luna on a ChatGPT login, and Claude Code with Claude Haiku 5.5 on a Claude
subscription, both at medium effort, both acting and learning.
[Run the Harbor benchmarks](../README.md#harbor-benchmarks) says what a machine needs and how to switch to another
model or CLI.

## Prepare

```bash
python -m hermit.harbor.dataset prepare examples/terminal_bench_science/utils/benchmark.yaml
```

It downloads the 70 tasks (1.1 GB with their data; each trial builds its image), checks them against the lock, and
writes `utils/tasks.jsonl` (four rows per task, `<task>#1` to `#4`, and the smoke rows `clinical-metadata-recovery#0`,
`variable-star-vetting#0`) and `utils/splits/`.

## Baseline

```bash
hermit eval --config examples/terminal_bench_science/baseline/codex.yaml --adapter none
```

140 trials (70 tasks x rollouts #3 and #4, the rollouts every adapter is tested on). pass@1 is the summary's
`mean_score`, pass@2 its `pass_at_k`. A task may take its 8 hours: most sessions end well before.

## Transfer: AHE's adapter from Terminal-Bench 2.0

The AHE paper (Lin et al., 2026, [arXiv:2604.25850](https://arxiv.org/abs/2604.25850)) tests the harness it evolved on
Terminal-Bench 2 on another benchmark, frozen (SWE-bench Verified). Terminal-Bench-Science is of the same family: a
coding CLI in a task's container, a task's tests after it. With the final adapter of
[Terminal-Bench 2.0's AHE run](../terminal_bench_2_0/README.md#ahe-adapter) written out as a directory:

```bash
hermit eval --config examples/terminal_bench_science/baseline/codex.yaml --adapter "$PWD/ahe-final"
hermit eval --config examples/terminal_bench_science/baseline/codex.yaml --adapter "$PWD/hermit/adapter/templates/ahe"
```

The second tests AHE's seed, so that the transfer is read against the seed as well as against no adapter.

## AHE Adapter

No method of this framework reports this benchmark. The AHE Adapter's protocol is the one its paper ran on the
benchmark nearest to it, Terminal-Bench 2, with the same rows as
[Terminal-Bench 2.0's](../terminal_bench_2_0/README.md#the-protocol): ten iterations, each over every task's rollouts
#1 and #2, the best iteration kept, every adapter tested on rollouts #3 and #4. What differs: each task keeps its own
8 hours (the paper's tasks had one hour each).

```bash
hermit train --config examples/terminal_bench_science/ahe_adapter/smoke_codex.yaml   # 2 tasks, 2 steps
hermit train --config examples/terminal_bench_science/ahe_adapter/run_codex.yaml     # again to resume
hermit eval  --config examples/terminal_bench_science/ahe_adapter/run_codex.yaml --adapter root
```

It is ten times the baseline's 140 trials, then 140 for the test: run the transfer first.

## Results

Not run yet.

| Acting agent and learners | No adapter | AHE's seed | AHE's adapter from Terminal-Bench 2.0 | AHE here: final | AHE here: best iteration in sample |
|---|---|---|---|---|---|
| Codex, GPT-5.6-Luna | | | | | |
| Claude Code, Claude Haiku 5.5 | | | | | |
