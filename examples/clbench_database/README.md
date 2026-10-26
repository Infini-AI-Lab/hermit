# CL-Bench database exploration

The `database_exploration` venue of [CL-Bench](https://github.com/pgasawa/continual-learning-bench) ([site](https://continual-learning-bench.com/), [paper](https://arxiv.org/abs/2606.05661)): a stream of 40 questions about an undocumented SQLite database whose
schema drifts partway through; the host explores with SQL and answers, and the stream's score is accuracy.
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
python examples/clbench_database/utils/prepare.py --clbench ~/code/clbench      # the five ordered streams
```

## Run

One `hermit train` per order, in the background, with the host's and the learner's wire meters (ReHarness adds
an embedding relay on `WM_PORT+2`); logs and results under `$MW_RUNS/<tag>/`.

```bash
MW_TAG=db_mwm WM_PORT=3401 MW_ORDERS="0 1 2 3 4" examples/clbench_database/mwm/launch.sh            # MWM
MW_TAG=db_prompt_only WM_PORT=3441 MW_ORDERS="0 1 2 3 4" examples/clbench_database/prompt_only/launch.sh   # prompt-only
MW_TAG=db_reharness WM_PORT=3481 MW_ORDERS="0 1 2 3 4" examples/clbench_database/reharness/launch.sh    # ReHarness
```

An order's accuracy is the mean score in its run's `steps.jsonl`. A smoke on the first N
questions of one order: `MW_TRAIN_SPLIT=<file with those ids> MW_ORDERS=0 ... launch.sh`. The stateless and ICL
baselines are clbench's own systems, run with the same model and settings.

## Results

Host: GLM 5.3 Flash (reasoning effort high), unlimited queries, five orders of the stream, one run per order.

Three methods are compared with the benchmark's own baselines. **prompt-only** ([`prompt_only/`](prompt_only/)):
the adapter is one instruction file served to the host as its context; after every question a Codex session
with GLM 5.3 Flash reads the trajectories and feedback and rewrites it (the built-in
[Codex Evolver](../../docs/methods/codex.md) on the prompt-only template).
**MWM** ([`mwm/`](mwm/), [method](../../docs/methods/mwm.md)): the adapter is a world model of the venue, dated
knowledge, worked examples, a briefing and tested programs, which the host consults through one `wm` tool;
after every question a Codex session with GLM 5.3 Flash reads what the host did and revises it under a fixed
contract. **ReHarness** ([`reharness/`](reharness/), [Xiao et al.](https://arxiv.org/abs/2610.12668),
[method](../../docs/methods/reharness.md)): the adapter is a bank of reflections, one per
past question; before a question the harness retrieves the five whose questions are closest to it
(Qwen3-Embedding-8B, cosine) and appends them to the host's context; after it, GLM 5.3 Flash revises each
retrieved reflection from its own source trajectory, with the new trajectory as feedback, and writes the
question's reflection with the revised ones as examples. One pass over the stream, no validation set, no Codex.

| Method | accuracy (mean) |
|---|---|
| stateless: no memory, clbench's own baseline | 0.400 |
| ICL: clbench's in-context baseline | 0.580 |
| **prompt-only** (`prompt_only/`) | 0.470 |
| **MWM** (`mwm/`) | 0.795 |
| **ReHarness** (`reharness/`) | 0.740 |

ReHarness, one run per order on 2026-10-25, inside the offline container of [`docker/`](../../docker/) (Fireworks the
only reachable service; the embedding model `accounts/fireworks/models/qwen3-embedding-8b`): accuracies 0.750,
0.775, 0.725, 0.650 and 0.800 over the five orders, 36 to 42 minutes per order, about USD 0.12 of host calls and
USD 0.45 of reflection and revision calls per order at the backend's prices. The learner made 5 revisions and 1
reflection per question once the bank held five; 3 of the 200 reflection writes were lost to replies that were not
the JSON asked for, and 35 more parsed only after the one reminder (1163 agent calls in all). Reflections grow under
revision, to about 7 K characters each on average after the stream (the five retrieved ones add about 35 K characters
to the host's context). No rollout errors; the 79 to 80 embedding calls per order carry no price (the run's card
counts them as calls of unknown cost). The runs were made from the files committed as the branch's first ReHarness
commit (the launcher stamped the working tree `1b0161e-dirty`), before the commit that made the reader tolerant of
what the lost replies contained and retries a reply cut off at `max_tokens` with a larger budget. Each run's card is
`<tag>-o<order>/checkout/CARD.md` under `$MW_RUNS`; no bank is included in the repository, because the reflections
quote the questions' reference answers from the judge's feedback.

## Code

```text
utils/agent/        CLBenchDatabaseAgent (agent.py) starts database_runner.py in clbench's virtualenv through
                    clbench_host.py; host.py is the acting host, built over hermit.Session
utils/prepare.py    tasks.jsonl and one ordered split per order      utils/judge.py   the venue's score and feedback
utils/task-info/    the task for the learner; task-info/mwm/ is what MWM needs to know about the venue
utils/tests/        golden tests of the MWM texts (both venues) and the host test, which runs under clbench's own
                    interpreter: PYTHONPATH=<hermit> MWM_CLBENCH=<clbench> <clbench>/.venv/bin/python -m pytest utils/tests/test_host.py
mwm/ prompt_only/   run.yaml and launch.sh per method; mwm/sql_tool.py is the learner's SQL channel
reharness/          run.yaml and launch.sh for ReHarness; its template and Evolver are the framework's built-ins
```
