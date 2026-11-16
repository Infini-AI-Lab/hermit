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
| `ahe_adapter/` | `run_codex.yaml`, `run_claude.yaml`: ten AHE iterations (`hermit train`), then the final adapter's test (`hermit eval --adapter final`); `smoke_*.yaml` first |

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

140 trials (70 tasks x rollouts #3 and #4, the rollouts every adapter is tested on). pass@1 is, as for
[Terminal-Bench 2.0](../terminal_bench_2_0/README.md#baseline), the `pass_at_1` of `python -m hermit.harbor.report` on
the eval's folder. A task may take its 8 hours: most sessions end well before.

## Transfer: AHE's adapter from Terminal-Bench 2.0

The AHE paper (Lin et al., 2026, [arXiv:2604.25850v4](https://arxiv.org/abs/2604.25850v4)) tests the harness it evolved
on Terminal-Bench 2 on another benchmark, frozen (SWE-bench Verified). Terminal-Bench-Science is of the same family: a
coding CLI in a task's container, a task's tests after it. With the final adapter of
[Terminal-Bench 2.0's AHE run](../terminal_bench_2_0/README.md#ahe-adapter) written out as a directory:

```bash
hermit eval --config examples/terminal_bench_science/baseline/codex.yaml --adapter "$PWD/examples/terminal_bench_2_0/ahe_adapter/runs/ahe-final-codex"
hermit eval --config examples/terminal_bench_science/baseline/codex.yaml --adapter "$PWD/hermit/adapter/templates/ahe"
```

The second tests AHE's seed, so that the transfer is read against the seed as well as against no adapter. The Claude
Code configs take `ahe-final-claude`.

## AHE Adapter

No method of this framework reports this benchmark. The AHE Adapter's protocol is the one its paper ran on the benchmark
nearest to it, Terminal-Bench 2, with the same rows as
[Terminal-Bench 2.0's](../terminal_bench_2_0/README.md#the-protocol): ten iterations, each over every task's rollouts #1
and #2, the best iteration kept, every adapter tested on rollouts #3 and #4, and the same differences from the paper
(its protocol table: the port's rollback, the released code's early stop, the models, the seed). What differs besides:
each task keeps its own 8 hours (the paper's tasks had one hour each). The learners wait for the plan's window, as
[Terminal-Bench 2.0's](../terminal_bench_2_0/README.md#ahe-adapter) do, within their own clocks, which count the wait
(the Evolve Agent's 7,200 s, a Debugger attempt's 600 s).

```bash
hermit train --config examples/terminal_bench_science/ahe_adapter/smoke_codex.yaml   # 2 tasks, 2 steps
hermit eval  --config examples/terminal_bench_science/ahe_adapter/smoke_codex.yaml --adapter final
hermit train --config examples/terminal_bench_science/ahe_adapter/run_codex.yaml     # again to resume
hermit eval  --config examples/terminal_bench_science/ahe_adapter/run_codex.yaml --adapter final
```

It is at most ten times the baseline's 140 trials (AHE's released code, not the paper, stops at a target pass rate),
then 140 for the final adapter's test (`--adapter final`, an adapter of the tree `hermit train` made, so after it): run
the transfer first. AHE's seed is tested once, by the transfer's second command; the tree's root holds the same files.
Report the best iteration's pass@1 (`runs/<run name>/workspace/state/best_ever.json`) beside the final adapter's test as
[Terminal-Bench 2.0's](../terminal_bench_2_0/README.md#ahe-adapter) does: the two count infra trials, and trials whose
agent ran out of its time or ended in an error while the tests passed, differently.

## Results

Not run yet.

| Acting agent and learners | No adapter | AHE's seed | AHE's adapter from Terminal-Bench 2.0 | AHE here: final | AHE here: best iteration in sample |
|---|---|---|---|---|---|
| Codex, GPT-5.6-Luna | | | | | |
| Claude Code, Claude Haiku 5.5 | | | | | |
