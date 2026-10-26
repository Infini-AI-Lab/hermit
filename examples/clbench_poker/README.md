# CL-Bench exploitable poker

The `exploitable_poker` venue of [CL-Bench](https://github.com/pgasawa/continual-learning-bench) ([site](https://continual-learning-bench.com/), [paper](https://arxiv.org/abs/2606.05661)): a stream of 120 heads-up poker hands against a scripted opponent whose
strategy changes along the stream; the host plays each hand, and the stream's score is big blinds won per hand.
This example trains an adapter on the stream with the mwm, prompt-only and ReHarness methods and compares them with
the benchmark's own baselines.

## Setup

Install the framework first ([Installation](../../docs/get-started/installation.md)). The benchmark runs from a clbench checkout in its own virtualenv: our fork
[clbench-mwm](https://github.com/haizhongzheng/clbench-mwm) of
[continual-learning-bench](https://github.com/pgasawa/continual-learning-bench) (it adds the host-side query channel
the runs use). Also needed: the Codex CLI on PATH, bubblewrap, and the key of the models' backend: for the default
(GLM 5.3 Flash on Fireworks) `FIREWORKS_API_KEY` or the key file `~/code/metaworld/.fireworks`. Host and learner
take any backend reference, `MW_MODEL=<backend>/<model id>` (or `MW_HOST_MODEL` / `MW_EVOLVER_MODEL`); the
learner's backend must serve the Responses API, which Codex speaks (ReHarness needs no Codex: its learner is the
run's `llm` tool, chat completions, and its embedding model is `MW_EMBED_MODEL`, default Qwen3-Embedding-8B on
Fireworks). A local server:
`MW_MODEL=vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 VLLM_BASE_URL=http://127.0.0.1:8000/v1`
([Model backends](../../docs/guides/model-backends.md)).

```bash
git clone git@github.com:haizhongzheng/clbench-mwm.git ~/code/clbench && (cd ~/code/clbench && uv sync)
python examples/clbench_poker/utils/prepare.py --clbench ~/code/clbench      # the five ordered streams
```

## Run

```bash
MW_TAG=poker_mwm WM_PORT=3411 MW_ORDERS="0 1 2 3 4" examples/clbench_poker/mwm/launch.sh            # MWM
MW_TAG=poker_prompt_only WM_PORT=3451 MW_ORDERS="0 1 2 3 4" examples/clbench_poker/prompt_only/launch.sh   # prompt-only
MW_TAG=poker_reharness WM_PORT=3491 MW_ORDERS="0 1 2 3 4" examples/clbench_poker/reharness/launch.sh    # ReHarness
```

Each launcher starts the host's and the learner's wire meters on `WM_PORT` and `WM_PORT+1`; ReHarness adds an
embedding relay on `WM_PORT+2`.

An order's bb/hand is the mean reward in its `task/outcomes.jsonl`. A smoke on the first N
hands of one order: `MW_TRAIN_SPLIT=<file with those ids> MW_ORDERS=0 ... launch.sh`. The stateless and ICL
baselines are clbench's own systems, run with the same model and settings.

## Results

Host: GLM 5.3 Flash (reasoning effort high), five orders of the stream, one run per order.

Three methods are compared with the benchmark's own baselines. **prompt-only** ([`prompt_only/`](prompt_only/)):
the adapter is one instruction file served to the host as its context; after every five hands a Codex session
with GLM 5.3 Flash reads the trajectories and feedback and rewrites it (the built-in
[Codex Evolver](../../docs/methods/codex.md) on the prompt-only template).
**MWM** ([`mwm/`](mwm/), [method](../../docs/methods/mwm.md)): the adapter is a world model of the venue, dated
knowledge, worked examples, a briefing and tested programs, which the host consults through one `wm` tool;
after every five hands a Codex session with GLM 5.3 Flash reads what the host did and revises it under a fixed
contract. **ReHarness** ([`reharness/`](reharness/), [Xiao et al.](https://arxiv.org/abs/2610.12668),
[method](../../docs/methods/reharness.md)): the adapter is a bank of reflections, one per
past hand; before a hand the harness retrieves the five whose opening prompts are closest to it
(Qwen3-Embedding-8B, cosine) and appends them to the host's context; after every hand, not every five, GLM 5.3
Flash revises each retrieved reflection from its own source trajectory, with the new trajectory as feedback, and
writes the hand's reflection with the revised ones as examples. One pass over the stream, no validation set, no
Codex.

| Method | bb/hand (mean) |
|---|---|
| stateless: no memory, clbench's own baseline | 1.76 |
| ICL: clbench's in-context baseline | 2.51 |
| **prompt-only** (`prompt_only/`) | 1.65 |
| **MWM** (`mwm/`) | 3.08 |
| **ReHarness** (`reharness/`) | 0.28 |

ReHarness, one run per order on 2026-10-25, inside the offline container of [`docker/`](../../docker/) (Fireworks the
only reachable service; `accounts/fireworks/models/qwen3-embedding-8b` for retrieval): −0.36, 0.75, 0.08, −0.13 and
1.06 bb/hand over the five orders (the mean reward of each order's 120 hands in `task/outcomes.jsonl`; the stream
has 104 instances), 86 to 108 minutes per order, about USD 0.28 of host calls and USD 1.17 of reflection and
revision calls per order at the backend's prices, no rollout errors, every instance's reflection written (58 of the
3,050 agent replies were read from the field alone, a bare quote or an escape inside their JSON). Below the
stateless baseline: the paper itself reports only marginal gains on exploitable poker, where hand-level rewards are
weakly coupled with strategy quality; here the five retrieved reflections, about 7 K characters each after revision,
add about 37 K characters of other hands' evidence and strategies to every hand's context, and in every order the
last quarter of the hands scores below the first, after the opponent variant has shifted. Each run's card is
`<tag>-o<order>/checkout/CARD.md` under `$MW_RUNS`; no bank is included.

## Code

```text
utils/agent/        CLBenchPokerAgent (agent.py) starts poker_runner.py in clbench's virtualenv through
                    clbench_host.py; host.py is the acting host, built over hermit.Session
utils/prepare.py    tasks.jsonl and one ordered split per order      utils/judge.py   the venue's score and feedback
utils/task-info/    the task for the learner; task-info/mwm/ is what MWM needs to know about the venue
mwm/ prompt_only/   run.yaml and launch.sh per method
reharness/          run.yaml and launch.sh for ReHarness; its template and Evolver are the framework's built-ins
```

The tests for both venues are in `examples/clbench_database/utils/tests/`.
