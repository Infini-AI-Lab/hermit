# ServeLearnBench with MWM

Train MWM on a scenario of [ServeLearnBench](https://github.com/Infini-AI-Lab/ServeLearnBench) and score it the
benchmark's way. Host and learner are GLM 5.3 flash on Fireworks at reasoning effort high. Real model calls:
retail_l3 takes about six hours.

## Run

Install the benchmark, then run one scenario from the repository root:

```bash
.venv/bin/pip install "git+https://github.com/Infini-AI-Lab/ServeLearnBench"
FIREWORKS_API_KEY=... examples/servelearnbench/mwm/launch.sh retail_l3
```

The launcher exports the scenario as a task (once), starts the training in the background, and when the training
ends plays the per-window tests and prints the benchmark's scores:

| Where | What |
|---|---|
| `~/code/metaworld-runs/slb_retail_l3/slb_retail_l3/steps.jsonl` | progress: one line per block of 12 cases, with the block's scores |
| `~/code/metaworld-runs/slb_retail_l3/test.log` | the Hidden and Fully Specified scores per window, and `slb score`'s table |
| `~/code/metaworld-runs/slb_retail_l3/task/results/retail_l3_mwm.jsonl` | the benchmark's result file, readable by `slb score` |

`examples/servelearnbench/mwm/launch.sh retail_l3 test` replays the tests alone, also while the training is still
going (windows not trained yet are skipped).

Requirements: the framework in `.venv/` (Python 3.12+); the `codex` CLI and bubblewrap (`bwrap`) on the PATH. Both
sides reach Fireworks through the framework's wire meter (`harnessadapter/runtime/wire_meter.py`), a local proxy
the launcher starts: it records the tokens per run and rewrites the learner's requests the way Fireworks needs.

## What happens

The benchmark: a scenario is a chronological stream of tasks in environment windows. A hidden policy governs each
window and changes between windows. After each serving task the agent is told only a score; held-out test tasks at
the end of every window measure what it learned.

The benchmark's own code plays every episode here: its agent loop, its budgets, its tools and its verifier. MWM
enters where the benchmark lets a learning method in: its context is appended to the system prompt, and its `wm`
tool is added beside the environment's tools. After every block of 12 serving cases the learner, a Codex session
in its own namespace, revises the world model from the records of those cases; it never plays a task and has no
channel into the environment.

The tests are replayed afterwards on the adapter the run had at each window (one commit per block in the adapter
tree), which gives the same scores as the benchmark's inline tests, since test tasks never update a method. A block
of 12 does not divide every window's serving count (74 to 84 in retail_l3), so up to 11 cases of a window are
learned only after that window's test.

The task integration (`examples/servelearnbench/`: what is exported, how an episode is played, how the
windows are scored) is described in its README.
