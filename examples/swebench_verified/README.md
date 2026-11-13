# SWE-bench Verified

[SWE-bench Verified](https://www.swebench.com/) through Harbor: 500 GitHub issues of 12 Python repositories, each in its
own container with the repository at the issue's commit, scored 1 when the issue's tests (those the fix must make
pass, and those it must keep passing) pass, else 0. The task set is the Harbor registry's `swebench-verified@1.0`,
locked in `utils/dataset.lock`. Nothing trains here: the method's protocol is a transfer, the frozen adapter that AHE
evolved on Terminal-Bench 2.0 tested on all 500 issues, against the same acting agent with no adapter.

| Folder | What it runs |
|---|---|
| `utils/benchmark.yaml` | the task set and the settings every config shares: each task's own 3,000 s, no web search, thread pools of one CPU, the disk gate |
| `baseline/` | `codex.yaml`, `claude.yaml`: `hermit eval` on the 500 tasks, with no adapter or with an adapter directory |

Each config comes twice: Codex with GPT-5.6-Luna on a ChatGPT login, and Claude Code with Claude Haiku 5.5 on a Claude
subscription, both at medium effort. [Run the Harbor benchmarks](../README.md#harbor-benchmarks) says what a machine
needs and how to switch to another model or CLI.

## Prepare

```bash
python -m hermit.harbor.dataset prepare examples/swebench_verified/utils/benchmark.yaml
```

It downloads the 500 tasks (22 MB; each trial builds its image on the SWE-bench image of its repository), checks them
against the lock, and writes `utils/tasks.jsonl` (one row per task, and the smoke rows `psf__requests-1142#0`,
`pallets__flask-5014#0`) and `utils/splits/` (`test.txt`: all 500).

## Baseline

```bash
hermit eval --config examples/swebench_verified/baseline/codex.yaml --adapter none
```

500 trials, once each. The resolve rate is the summary's `mean_score`.

## Transfer: AHE's adapter from Terminal-Bench 2.0

Why: SWE-bench Verified is where the papers of this framework's methods test a harness they evolved elsewhere; none
of them trains on it (DGM, SICA and HGM do, and are not ported).

| Method | Evolved on | SWE-bench Verified |
|---|---|---|
| AHE ([2604.25850](https://arxiv.org/abs/2604.25850)) | Terminal-Bench 2, GPT-5.4 | 75.2 (the seed) to 75.6, with 461K tokens a trial against the seed's 526K |
| RRSI ([2609.24972](https://arxiv.org/abs/2609.24972)) | Terminal-Bench 2.1, Claude Opus 4.8 | 82.0 (H_0) to 83.8, never scored during the search |

The gains are small at this level; the token cost is the other number to report. Neither paper says how many runs a
task had; here each runs once. With the final adapter of
[Terminal-Bench 2.0's AHE run](../terminal_bench_2_0/README.md#ahe-adapter) written out as a directory (that README
says how), the same acting agent tests it:

```bash
hermit eval --config examples/swebench_verified/baseline/codex.yaml --adapter "$PWD/examples/terminal_bench_2_0/ahe_adapter/runs/ahe-final-codex"
hermit eval --config examples/swebench_verified/baseline/codex.yaml --adapter "$PWD/hermit/adapter/templates/ahe"
```

The first tests the evolved adapter, the second AHE's seed (the template's own adapter), each in its own
`runs/codex-baseline/eval/<directory name>/` (`ahe-final-codex/`, `ahe/`). A relative `--adapter` path is the
config's folder's. The Claude Code configs take `ahe-final-claude`.

## Known limits

- A task's instruction is the issue as its reporter wrote it, as Harbor publishes it (and as Harbor's own agents get
  it): it does not ask for a change to `/testbed`. A CLI with no adapter may answer the issue in text and change
  nothing; Codex did so on `psf__requests-1142` in this example's smoke, which scored 0. The task set is kept as
  published, so its numbers compare with Harbor's. AHE's seed, whose `ADAPTER.md` tells the agent to inspect the
  repository, edit files, run the tests and finish the work, is the arm the transfer is read against; its gap to no
  adapter is what such a statement alone adds.
- Some tasks cannot pass on a given host whatever the agent does. `psf__requests-1724`'s tests call `httpbin.org` over
  HTTPS with the 2014 `requests` its repository pins, whose certificates no longer verify: its reference fix scores 0
  here (`SSLError: CERTIFICATE_VERIFY_FAILED`). Harbor's oracle agent finds such tasks before a run spends model calls
  on them: `hermit eval ... --adapter none "agent.params.acting_agent={name: oracle}" agent.params.login=null
  agent.params.model=null agent.params.effort=null trainer.run_name=oracle` runs each task's reference fix, in
  `runs/oracle/` (without the run name it would write over the baseline's `runs/codex-baseline/eval/none/`).
- Both arms of a comparison meet the same broken tasks, so differences stay comparable; absolute rates are lower
  than the leaderboard's by those tasks.

## Results

Not run yet.

| Acting agent | No adapter | AHE's seed | AHE's final adapter from Terminal-Bench 2.0 |
|---|---|---|---|
| Codex, GPT-5.6-Luna | | | |
| Claude Code, Claude Haiku 5.5 | | | |
