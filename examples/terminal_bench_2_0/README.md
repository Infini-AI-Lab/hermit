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
| `ahe_adapter/` | `run_codex.yaml`, `run_claude.yaml`: ten AHE iterations (`hermit train`), then the final adapter's test (`hermit eval --adapter final`); `smoke_*.yaml` first |

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

The AHE paper (Lin et al., 2026, [arXiv:2604.25850v4](https://arxiv.org/abs/2604.25850v4)) evolves and reports on all 89
tasks: each iteration runs every task k = 2 times (Table 4; §3.3 runs k ≥ 2 rollouts per task), an hour each (§4.1), and
the reported number is pass@1 of the best of ten iterations (§4.2) on those same rollouts (Fig. 1: iteration 8, the last
of App. C's winning rounds, is Table 1's 77.0), a number in sample. This example keeps that, and adds a fresh
measurement: the training steps run rollouts #1 and #2 of each task, and every adapter is tested on rollouts #3 and #4,
which no step ran.

| | The AHE paper | Here |
|---|---|---|
| Tasks | 89, evolved on and reported on | the same |
| Rollouts | k = 2 per task and iteration | k = 2 (#1, #2); the test's two (#3, #4) |
| Time per task | 3,600 s, every task | 3,600 s (`agent_timeout_sec: null` runs the tasks' own 600-12,000 s, as the leaderboard does) |
| Tests' and start's limits | a sandbox lifetime of 3,600 s (Table 4; the released code's `e2b_sandbox_timeout: 3600`), most likely for the start, the agent and the tests together (whether it bounded them is not recorded: Harbor 0.25.0's E2B environment takes its own `sandbox_timeout_secs`); no other limit passed to Harbor: the tasks' own | 5 times the tasks' own for the tests (a container gets its task's CPU quota but sees all of the host's cores, and a thread pool sized to them runs slowly in that quota) and 3 times for the start (the images' pull), for every arm, apart from the agent's 3,600 s: a test that would run out of its time at the tasks' own limit, or past the paper's hour, can pass here |
| Iterations | 10; the best by pass@1 (the released code's `max_iterations` is 100) | the paper's 10; the best (`best_ever`) |
| Early stop | none: all ten (Algorithm 1, Table 4) | the released code's target, 0.95 (the port's `target_pass_rate`), with Hermit's `trainer.allow_early_stop: true`: an iteration at pass@1 0.95 or more evolves nothing and ends the run, far above the paper's best (77.0) |
| Rollback | an edit whose predicted fixes do not materialize, reverted file by file (§1, Fig. 8) | the port's (`rollback: auto`): an edit the next step's attribution judges HARMFUL; `rollback_verdicts: [HARMFUL, INEFFECTIVE]` reverts the ineffective ones too |
| Models | GPT-5.4 for all three, at high (§4.1); Table 4 gives the Code Agent high and the Evolve Agent xhigh, and lists no effort for the Debugger | one model for all three: GPT-5.6-Luna or Claude Haiku 5.5, medium |
| Learners' clocks | the Evolve Agent 500 turns; the Debugger 600 s (Table 4) | the Evolve Agent 7,200 s (`evolve_timeout_s`); the Debugger 600 s an attempt, 3 attempts; each a deadline over the whole call, a wait for the login's plan included (as AHE's own subprocess timeout is for its Debugger) |
| Seed | NexAU0: a shell tool, no skills, no middleware, no memory | the `ahe` template: three seed rules, an empty memory, a middleware that only hands the memory to the model |
| Acting agent | NexAU, whose code AHE edits | Codex or Claude Code, fixed; AHE edits the adapter |
| Concurrency | 96 (E2B) | 6 (one Docker host) |
| Explore Agent | one-shot skill seeding | not ported |

## Baseline

```bash
hermit eval --config examples/terminal_bench_2_0/baseline/codex.yaml --adapter none
```

178 trials (89 tasks x rollouts #3 and #4). pass@1 is the `pass_at_1` of `python -m hermit.harbor.report` on the eval's
folder (`examples/terminal_bench_2_0/baseline/runs/codex-baseline/eval/none/`), over the trials that are the agent's
result; the summary's `mean_score` counts an infra trial as 0. The seed adapter alone (AHE's NexAU0 row) is
`--adapter root` with the method's config, after its `hermit train` (below).

## AHE Adapter

Why this method: of the methods in this framework, three report Terminal-Bench 2 numbers, and AHE's is measured on
this benchmark with the protocol above, at the smaller search of the two whose trials can be counted.

| Method | What its paper reports on Terminal-Bench | Search cost in trials |
|---|---|---|
| AHE ([2604.25850v4](https://arxiv.org/abs/2604.25850v4)) | 2.0: pass@1 69.7 to 77.0 (+7.3) over its seed, GPT-5.4; Codex 71.9 | at most 10 x 178 = 1,780 (its released code, not the paper, stops at a target pass rate) |
| RRSI ([2609.24972v3](https://arxiv.org/abs/2609.24972v3)) | 2.1: 74.2 to 80.2 (+6.0, 95% CI +4.3 to +9.3), Claude Opus 4.8; 64.6 to 78.7 on Gemini 3.5 Flash | about 41 x 178 = 7,298 (1 + 20 rounds x 2 candidates, as Hermit's port counts them: it reuses the incumbent's evaluation; about 61 x 178 if the incumbent ran again each round, which the paper's Algorithm 1 may mean; fewer when its critic rejects a candidate before its evaluation, more with the base harness measured again) |
| Meta-Harness ([2603.28052v1](https://arxiv.org/abs/2603.28052v1)) | 2.0: 76.4 on Claude Opus 4.6, against 74.7 for Terminus-KIRA, one of its two starting harnesses, on the leaderboard (64.4 in the search's own measurement, App. A.2); 37.6 on Claude Haiku 4.5, against the leaderboard's next agent, Goose, at 35.5 (+2.1) | a search of 10 iterations with Claude Opus 4.6 (App. A, Table 8), each candidate run on all 89 tasks (trials per task not given), whose tenth drew on a separate, earlier search run of a size not given (App. A.2); its results are on Claude Opus 4.6 and Claude Haiku 4.5 (§4.3), and it does not say whether Haiku 4.5's harness came from that search |

[`rrsi_adapter`](../../docs/methods/rrsi_adapter.md) is the choice to try second: its regularizers target the
overfitting that in-sample selection invites, at about four times AHE's trials.

```bash
hermit train --config examples/terminal_bench_2_0/ahe_adapter/smoke_codex.yaml   # 2 tasks, 2 steps
hermit eval  --config examples/terminal_bench_2_0/ahe_adapter/smoke_codex.yaml --adapter final
hermit train --config examples/terminal_bench_2_0/ahe_adapter/run_codex.yaml     # again to resume
hermit eval  --config examples/terminal_bench_2_0/ahe_adapter/run_codex.yaml --adapter final
hermit eval  --config examples/terminal_bench_2_0/ahe_adapter/run_codex.yaml --adapter root
```

`--adapter final` tests the final adapter on the test rows (`runs/codex-ahe/eval/final/`) and `--adapter root` the seed
(`runs/codex-ahe/eval/root/`), both adapters of the tree `hermit train` made, so both after it. Report four numbers: no
adapter (the baseline), the seed (`root`), the final adapter, each on rollouts #3 and #4, and the paper's own measure,
the best iteration's pass@1 on its training rollouts (`runs/codex-ahe/workspace/state/best_ever.json`); the last is
selected on the rollouts it reports, so it overstates what the adapter does on fresh ones. The two kinds count two
trials differently. One that is not the agent's result (infra: its environment could not be given or failed): the iteration's
pass@1 counts it as a failure, as the paper does, and so does `hermit eval`'s `mean_score`, while
`python -m hermit.harbor.report` leaves it out of its pass@1 and counts it (`infra`): report that pass@1 for each eval,
and give that count beside each number. And one whose agent ran out of its hour or ended in an error while the tests
passed: the iteration's pass@1 counts it as a failure, as the paper's counts a timed-out trial (§4.1), and `hermit eval`
scores it by the tests' reward, as Harbor does; count those in the eval's `results.jsonl` (an `outcome.status` of
`timeout` or `agent_error` with a `judge.score` of 1) and give that count too.

The learners run on the same plan as the acting agent: on the smoke's two tasks, one step's Debugger and Evolve
Agent read about 450K tokens (Codex) and 840K (Claude Code), and a step of 178 trials gives them more to read. A
learner session waits for the plan's window with no limit of its own (`plan_max_wait_s: null`), but its own clock
(the Evolve Agent's 7,200 s, a Debugger attempt's 600 s) counts the wait, as AHE's own subprocess timeout does for its
Debugger: a hold longer than that ends the session, a Debugger attempt as failed (three attempts a task) and the
Evolve Agent with the edits it made so far kept (none, when the hold took its whole clock). `trainer.call_timeout_min` (a week) bounds a step's
learners, waits included.

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
