# CL-Bench exploitable poker

The `exploitable_poker` venue of [CL-Bench](https://github.com/pgasawa/continual-learning-bench) ([site](https://continual-learning-bench.com/), [paper](https://arxiv.org/abs/2606.05661)): a stream of 120 heads-up poker hands against a scripted opponent whose
strategy changes along the stream; the host plays each hand, and the stream's score is big blinds won per hand.
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
python examples/clbench_poker/utils/prepare.py --clbench ~/code/clbench      # the five ordered streams
```

## Run

```bash
MW_TAG=poker_mwm WM_PORT=3411 MW_ORDERS="0 1 2 3 4" examples/clbench_poker/mwm/launch.sh            # MWM
MW_TAG=poker_prompt_only WM_PORT=3451 MW_ORDERS="0 1 2 3 4" examples/clbench_poker/prompt_only/launch.sh   # prompt-only
```

An order's bb/hand is the mean `reward` over `<runs>/<tag>/order<i>/task/outcomes.jsonl`. A smoke on the first N
hands of one order: `MW_TRAIN_SPLIT=<file with those ids> MW_ORDERS=0 ... launch.sh`. The ICL and per-order
stateless baselines are clbench's own systems, launched from the metaworld repository:

```bash
MW_MODEL_ID=fireworks_ai/accounts/fireworks/models/glm-5p3-flash MW_TAG=v1013_poker_flash WM_PORT=3431 ~/code/metaworld/run_poker_baselines_byorder.sh
```

## Results

Host: GLM 5.3 Flash (reasoning effort high), five orders of the stream, one run per order. Learner: the mwm
Evolver (Codex with GLM 5.3 Flash, one session per 5 host hands) or the built-in Codex Evolver with GLM 5.3 Flash
and its tools gated to `check_spec`.

| Method | bb/hand (mean) | runs |
|---|---|---|
| stateless: no memory, clbench's own baseline | 1.76 | nine runs (five orders, four of them twice), `v1013_poker_flash_stateless_o*`, `v1013b_…` |
| ICL: clbench's in-context baseline | 2.51 | five orders, `v1013_poker_flash_icl` |
| **prompt-only** (`prompt_only/`) | 1.65 | five orders, `po_poker_flash` |
| **MWM** (`mwm/`) | 3.08 | five orders, `fw6_poker_mwm_flash` |

The same stateless host replayed on one order moved by up to 2.2 bb/hand, so this venue is noisy; MWM is above
every other method on each order. The framework runs date from commit `0b6745d` (2026-10-12/13); native
metaworld scored 2.30 on the same setting.

## Code

```text
utils/agent/        CLBenchPokerAgent (agent.py) starts poker_runner.py in clbench's virtualenv through
                    clbench_host.py; host.py is the acting host, a port of metaworld's seat over harnessadapter.Session
utils/prepare.py    tasks.jsonl and one ordered split per order      utils/judge.py   the venue's score and feedback
utils/task-info/    the task for the learner; task-info/mwm/ is what MWM needs to know about the venue
mwm/ prompt_only/   run.yaml and launch.sh per method
```

The tests for both venues are in `examples/clbench_database/utils/tests/`.
