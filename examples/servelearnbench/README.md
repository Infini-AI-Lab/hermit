# ServeLearnBench

A scenario of [ServeLearnBench](https://github.com/Infini-AI-Lab/ServeLearnBench): a chronological stream of
customer requests at an online retailer's support desk, governed by a hidden policy that changes between windows;
the agent is told only a score after each request, and held-out tests at the end of every window measure what it
learned. This example trains an adapter on the serving stream with the mwm and prompt-only methods, scores it the
benchmark's way, and compares it with the benchmark's leaderboard. The benchmark's own agent loop plays every
episode; the adapter enters through its context (on the system prompt) and its tools (MWM's `wm`).

## Setup

Install the framework first ([Installation](../../docs/get-started/installation.md)).

```bash
pip install "git+https://github.com/Infini-AI-Lab/ServeLearnBench"      # the benchmark
python examples/servelearnbench/utils/prepare.py --scenario retail_l3 --block 12   # tasks.jsonl, splits/, windows.json, the documents
```

Also needed for training: the Codex CLI on PATH, bubblewrap, and the key of the models' backend (`FIREWORKS_API_KEY`
for the default, GLM 5.3 Flash on Fireworks; `MW_MODEL=<backend>/<model id>` binds another, for a local server
`MW_MODEL=vllm/... VLLM_BASE_URL=http://127.0.0.1:8000/v1`; see
[Model backends](../../docs/guides/model-backends.md)).

## Run

```bash
examples/servelearnbench/mwm/launch.sh retail_l3            # MWM: train on the serving stream, then the per-window tests
examples/servelearnbench/prompt_only/launch.sh retail_l3    # prompt-only, same stream and host
```

Each launcher starts the wire meters and one `hermit train` in the background (`$MW_RUNS/<tag>/run.log`),
then replays every window's held-out tests on the adapter the run had after that window and scores the result file
with `slb score` (`test.log` and `task/results/retail_l3_<method>.jsonl` in the run directory). The tests can be rerun with
`launch.sh retail_l3 test`. A smoke on the first 12 serving tasks: `MW_TRAIN_SPLIT=examples/servelearnbench/utils/splits/smoke_serving.txt`.

## Results

retail_l3: 624 serving requests in 8 windows, 537 held-out tests. Host: GLM 5.3 Flash (reasoning effort high),
the benchmark's own loop and budgets. H is the benchmark's score on the tests that need the hidden policy, F on
the fully specified ones; one run each.

Two methods are compared with the benchmark's leaderboard. **prompt-only** ([`prompt_only/`](prompt_only/)):
the adapter is one instruction file served to the host as its context; after every block of 12 requests a Codex
session with GLM 5.3 Flash reads the trajectories and feedback and rewrites it (the built-in
[Codex Evolver](../../docs/methods/codex.md) on the prompt-only template).
**MWM** ([`mwm/`](mwm/), [method](../../docs/methods/mwm.md)): the adapter is a world model of the venue, dated
knowledge, worked examples, a briefing and tested programs, which the host consults through one `wm` tool;
after every block of 12 requests a Codex session with GLM 5.3 Flash reads what the host did and revises it under
a fixed contract.

| Method | H | F |
|---|---|---|
| Blind (leaderboard: no learning) | 4.5 | 94.3 |
| RAG (leaderboard) | 14.3 | 95.2 |
| Mem0 (leaderboard) | 13.5 | 92.7 |
| SkillOpt (leaderboard) | 22.0 | 93.3 |
| Prime (leaderboard) | 26.9 | 97.5 |
| CH (leaderboard) | 28.7 | 92.0 |
| **prompt-only** (`prompt_only/`) | 18.8 | 97.1 |
| **MWM** (`mwm/`) | 45.3 | 98.4 |
| Oracle (leaderboard: the policy given) | 89.2 | 95.5 |

Leaderboard rows are the benchmark's published numbers for GLM 5.3 Flash on retail L3.

## Code

```text
utils/agent/agent.py   ServeLearnBenchAgent: one episode through the benchmark's loop, the adapter plugged in through Session
utils/prepare.py       exports one scenario: tasks.jsonl, splits/ (serving, test_w<w>, smoke), windows.json, the documents
utils/judge.py         the benchmark's reward; the feedback is the one line its methods see ("Score: n/100.")
utils/windows.py       the per-window test replay and the `slb score` result file
utils/task-info/       the task for the learner; task-info/mwm/ is what MWM needs to know about the venue (see docs/methods/mwm.md)
mwm/ prompt_only/      retail_l3.yaml and launch.sh per method
```

The agent reads its settings from the environment the launcher sets: `SLB_WORKDIR` (the run's task directory),
`SLB_MODEL`, `SLB_BASE_URL`, `SLB_API_KEY`, `SLB_REASONING_EFFORT` (the launcher derives them from `MW_MODEL`'s backend).
