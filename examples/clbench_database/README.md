# CL-Bench database exploration

The `database_exploration` venue of [CL-Bench](https://github.com/pgasawa/continual-learning-bench) ([site](https://continual-learning-bench.com/), [paper](https://arxiv.org/abs/2606.05661)): a stream of 40 questions about an undocumented SQLite database whose
schema drifts partway through; the host explores with SQL and answers, and the stream's score is accuracy.
This example trains an adapter on the stream with the mwm and prompt-only methods and compares them with the
benchmark's own baselines.

## Setup

The benchmark runs from a clbench checkout in its own virtualenv: our fork
[clbench-mwm](https://github.com/haizhongzheng/clbench-mwm) of
[continual-learning-bench](https://github.com/pgasawa/continual-learning-bench) (it adds the host-side query channel
the runs use). Also needed: the Codex CLI on PATH, bubblewrap, and a Fireworks key in `FIREWORKS_API_KEY` or
`~/code/metaworld/.fireworks`.

```bash
git clone git@github.com:haizhongzheng/clbench-mwm.git ~/code/clbench && (cd ~/code/clbench && uv sync)
python examples/clbench_database/utils/prepare.py --clbench ~/code/clbench      # the five ordered streams
```

## Run

One `harnessadapter train` per order, in the background, with the host's and the learner's wire meters; logs and
results under `~/code/metaworld-runs/<tag>/`.

```bash
MW_TAG=db_mwm WM_PORT=3401 MW_ORDERS="0 1 2 3 4" examples/clbench_database/mwm/launch.sh            # MWM
MW_TAG=db_prompt_only WM_PORT=3441 MW_ORDERS="0 1 2 3 4" examples/clbench_database/prompt_only/launch.sh   # prompt-only
```

An order's accuracy is the mean of `rollout.scores` in `<runs>/<tag>/<tag>-o<i>/steps.jsonl`. A smoke on the first N
questions of one order: `MW_TRAIN_SPLIT=<file with those ids> MW_ORDERS=0 ... launch.sh`. The stateless and ICL
baselines are clbench's own systems, launched from the metaworld repository:

```bash
MW_MODEL_ID=fireworks_ai/accounts/fireworks/models/glm-5p3-flash MW_TAG=flash_nq WM_PORT=3139 ~/code/metaworld/run_clbench_baselines_nq.sh
```

## Results

Host: GLM 5.3 Flash (reasoning effort high), unlimited queries, five orders of the stream, one run per order.
Learner: the mwm Evolver (Codex with GLM 5.3 Flash, one session per question) or the built-in Codex Evolver with
GLM 5.3 Flash and its tools gated to `check_spec`.

| Method | accuracy (mean) | runs |
|---|---|---|
| stateless: no memory, clbench's own baseline | 0.400 | one run, `clbench_flash_nq_1013_stateless` |
| ICL: clbench's in-context baseline | 0.580 | five orders, `clbench_flash_nq_1013_icl` |
| **prompt-only** (`prompt_only/`) | 0.470 | five orders, `po_db_flash` |
| **MWM** (`mwm/`) | 0.795 | five orders, `fw6_mwm_flash` |

The five orders of one method differ by up to 0.175, so smaller differences between methods are noise. The
framework runs date from commit `0b6745d` (2026-10-12/13); native metaworld scored 0.835 on the same setting.

## Code

```text
utils/agent/        CLBenchDatabaseAgent (agent.py) starts database_runner.py in clbench's virtualenv through
                    clbench_host.py; host.py is the acting host, a port of metaworld's seat over harnessadapter.Session
utils/prepare.py    tasks.jsonl and one ordered split per order      utils/judge.py   the venue's score and feedback
utils/task-info/    the task for the learner; task-info/mwm/ is what MWM needs to know about the venue
utils/tests/        parity with native metaworld (both venues) and the host test
mwm/ prompt_only/   run.yaml and launch.sh per method; mwm/sql_tool.py is the learner's SQL channel
```
