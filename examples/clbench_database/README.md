# CL-Bench database exploration

The `database_exploration` venue of [CL-Bench](https://github.com/pgasawa/continual-learning-bench) ([site](https://continual-learning-bench.com/), [paper](https://arxiv.org/abs/2606.05661)): a stream of 40 questions about an undocumented SQLite database whose
schema drifts partway through; the host explores with SQL and answers, and the stream's score is accuracy.
This example trains an adapter on the stream with the mwm and prompt-only methods and compares them with the
benchmark's own baselines.

## Setup

Install the framework first ([Installation](../../docs/get-started/installation.md)). The benchmark runs from a clbench checkout in its own virtualenv: our fork
[clbench-mwm](https://github.com/haizhongzheng/clbench-mwm) of
[continual-learning-bench](https://github.com/pgasawa/continual-learning-bench) (it adds the host-side query channel
the runs use). Also needed: the Codex CLI on PATH, bubblewrap, and the key of the models' backend: for the default
(GLM 5.3 Flash on Fireworks) `FIREWORKS_API_KEY` or the key file `~/code/metaworld/.fireworks`. Host and learner
take any backend reference, `MW_MODEL=<backend>/<model id>` (or `MW_HOST_MODEL` / `MW_EVOLVER_MODEL`); the
learner's backend must serve the Responses API, which Codex speaks. A local server:
`MW_MODEL=vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 VLLM_BASE_URL=http://127.0.0.1:8000/v1`
([Model backends](../../docs/guides/model-backends.md)).

```bash
git clone git@github.com:haizhongzheng/clbench-mwm.git ~/code/clbench && (cd ~/code/clbench && uv sync)
python examples/clbench_database/utils/prepare.py --clbench ~/code/clbench      # the five ordered streams
```

## Run

One `hermit train` per order, in the background, with the host's and the learner's wire meters; logs and
results under `$MW_RUNS/<tag>/`.

```bash
MW_TAG=db_mwm WM_PORT=3401 MW_ORDERS="0 1 2 3 4" examples/clbench_database/mwm/launch.sh            # MWM
MW_TAG=db_prompt_only WM_PORT=3441 MW_ORDERS="0 1 2 3 4" examples/clbench_database/prompt_only/launch.sh   # prompt-only
```

An order's accuracy is the mean score in its run's `steps.jsonl`. A smoke on the first N
questions of one order: `MW_TRAIN_SPLIT=<file with those ids> MW_ORDERS=0 ... launch.sh`. The stateless and ICL
baselines are clbench's own systems, run with the same model and settings.

## Results

Host: GLM 5.3 Flash (reasoning effort high), unlimited queries, five orders of the stream, one run per order.

Two methods are compared with the benchmark's own baselines. **prompt-only** ([`prompt_only/`](prompt_only/)):
the adapter is one instruction file served to the host as its context; after every question a Codex session
with GLM 5.3 Flash reads the trajectories and feedback and rewrites it (the built-in
[Codex Evolver](../../docs/methods/codex.md) on the prompt-only template).
**MWM** ([`mwm/`](mwm/), [method](../../docs/methods/mwm.md)): the adapter is a world model of the venue, dated
knowledge, worked examples, a briefing and tested programs, which the host consults through one `wm` tool;
after every question a Codex session with GLM 5.3 Flash reads what the host did and revises it under a fixed
contract.

| Method | accuracy (mean) |
|---|---|
| stateless: no memory, clbench's own baseline | 0.400 |
| ICL: clbench's in-context baseline | 0.580 |
| **prompt-only** (`prompt_only/`) | 0.470 |
| **MWM** (`mwm/`) | 0.795 |

## Code

```text
utils/agent/        CLBenchDatabaseAgent (agent.py) starts database_runner.py in clbench's virtualenv through
                    clbench_host.py; host.py is the acting host, built over hermit.Session
utils/prepare.py    tasks.jsonl and one ordered split per order      utils/judge.py   the venue's score and feedback
utils/task-info/    the task for the learner; task-info/mwm/ is what MWM needs to know about the venue
utils/tests/        golden tests of the MWM texts (both venues) and the host test
mwm/ prompt_only/   run.yaml and launch.sh per method; mwm/sql_tool.py is the learner's SQL channel
```
