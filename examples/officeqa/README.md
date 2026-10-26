# OfficeQA

[OfficeQA](https://github.com/databricks/officeqa) ([dataset](https://huggingface.co/datasets/databricks/officeqa), [paper](https://arxiv.org/abs/2603.08655)): questions about U.S. Treasury Bulletins (1939–2025) whose
answers are figures found in the bulletins, often after a computation. This example runs OfficeQA Full (246
questions) as the [SkillOpt paper](https://arxiv.org/abs/2605.23904) does: its 50 / 24 / 172 train / validation /
test split, each question's parsed source pages in the prompt, and exact match. It trains the agent's skill, the
adapter's `ADAPTER.md`, with the GEPA and SkillOpt methods, and with ReHarness a bank of reflections retrieved below
that skill (direct chat only), and compares them with no skill and with SkillOpt's initial skill, in both of the
paper's harnesses: direct chat, with read and grep tools limited to the question's source files, and Codex, a
`codex exec` session with the whole corpus.

## Setup

Install the framework first ([Installation](../../docs/get-started/installation.md)) with the `skillopt` and `gepa`
extras. Also needed: the gated Hugging Face dataset `databricks/officeqa` (`officeqa_full.csv`, and
`treasury_bulletins_parsed/` with `transformed/` and `jsons/`), and the key of the models' backend: for the default
(GLM 5.3 Flash acting, GLM 5.3 learning, on Fireworks) `FIREWORKS_API_KEY`. `model=<backend>/<model id>` on the
command line moves the agent, `llm=...` the learner ([Model backends](../../docs/guides/model-backends.md)). The Codex
harness also needs Docker, set up once as in the [recipe](../../docs/recipes/officeqa.md#run-in-the-codex-harness).

```bash
pip install -e . -r requirements-skillopt.txt -r requirements-gepa.txt
python examples/officeqa/utils/prepare.py --csv /path/to/officeqa_full.csv --docs /path/to/treasury_bulletins_parsed
```

`prepare.py` writes the questions, the paper's split, SkillOpt's initial skill (`utils/seed_adapter/`) and a link to the
corpus (`utils/documents`, what the agent's sandbox is granted) to `utils/`.

## Run

```bash
hermit train --config examples/officeqa/gepa/run.yaml          # GEPA
hermit train --config examples/officeqa/skillopt/run.yaml      # SkillOpt
hermit eval  --config examples/officeqa/gepa/run.yaml     --adapter final                  # GEPA's skill
hermit eval  --config examples/officeqa/skillopt/run.yaml --adapter final                  # SkillOpt's skill
hermit eval  --config examples/officeqa/skillopt/run.yaml --adapter none                   # no skill
hermit eval  --config examples/officeqa/skillopt/run.yaml --adapter ../utils/seed_adapter  # initial skill
FIREWORKS_API_KEY=... WM_PORT=3601 examples/officeqa/reharness/launch.sh run        # ReHarness: the 50 as one stream
FIREWORKS_API_KEY=... WM_PORT=3601 examples/officeqa/reharness/launch.sh run eval   # its bank on the 172, three passes
FIREWORKS_API_KEY=... WM_PORT=3601 examples/officeqa/reharness/launch.sh stream     # the paper's protocol: the 172 as one stream
```

An adapter's accuracy is `mean_score` in `<method>/runs/<run name>/eval/<selector>/summary.json` (172 test
questions, three passes each), where `<selector>` is `final`, `none`, `adapter` or a directory's name such as
`seed_adapter`; `--adapter adapter` evaluates the trained adapter included in the folder. A smoke on a few
questions: the folder's `smoke.yaml`, after `hermit check --config ...`
([walkthrough](../../docs/recipes/officeqa.md#check-train-evaluate)). The Codex harness: the same commands with
`run_codex.yaml` and `smoke_codex.yaml`, the folder's configs with `agent.params.harness: codex` and no budget.

ReHarness runs through its launcher: it puts the embedding model behind a loopback relay for the adapter's reader
(whose sandbox has no keys), writes `reharness/seed_adapter/` and starts `hermit train`, `eval` or `check` on
`reharness/<config>.yaml`; `launch.sh smoke check --no-probe`, `launch.sh smoke` and `launch.sh smoke eval` are its
check and smoke. `run.yaml` is evaluated like the other methods (`reharness/runs/paper-reharness/eval/final/`);
`stream.yaml` has no test split, and its number is the mean of `rollout.mean_score` over the steps of
`reharness/runs/stream-reharness/steps.jsonl` (step 0, the bootstrap, has none). The
[recipe](../../docs/recipes/officeqa.md#reharness) explains the two protocols.

## Results

Agent and learner: GLM 5.3 Flash (reasoning effort medium), with Codex CLI 0.162.0 and one container per session.
Each evaluation makes one pass over the 172 test questions.

| Method | accuracy |
|---|---|
| no skill | 0.541 |
| initial skill | 0.576 |
| **GEPA** | 0.580 |
| **SkillOpt** | 0.600 |
| **Continual Harness** | 0.634 |
| **Harness²** | 0.651 |

Results use two settings:

- **Train/test split:** [GEPA](../../docs/methods/gepa.md) and [SkillOpt](../../docs/methods/skillopt.md) train
  one `ADAPTER.md` from SkillOpt's initial skill on 50 training questions, select on 24 validation questions,
  and evaluate the fixed final adapter on 172 test questions. Accuracies are means of four independent
  trainings, with one evaluation pass per final adapter. Configs:
  [`gepa/run_codex_flash.yaml`](gepa/run_codex_flash.yaml) and
  [`skillopt/run_codex_flash.yaml`](skillopt/run_codex_flash.yaml).
- **Test streaming:** [Continual Harness](../../docs/methods/continual_harness.md) and
  [Harness²](../../docs/methods/harness2.md) each process the 172 test questions once in the same order,
  updating the adapter as they go, without a separate training split or access to judge verdicts. These
  scores measure performance during adaptation. See [`continual_harness/`](continual_harness/) and
  [`harness2_stream/`](harness2_stream/) for their protocols.

**ReHarness** ([`reharness/`](reharness/), [method](../../docs/methods/reharness.md)) keeps the initial skill as
the harness's text and learns a bank of reflections below it, five retrieved per question by Qwen3-Embedding-8B and
revised after it. It is run on this row under the test-streaming setting (its paper's own protocol) and, as a bank
written from the training questions and then frozen, under the train/test split; its rows are added when the runs
finish.

Paired differences with 95% intervals, each run's selection and cost, the Codex sessions' isolation and the
differences from the paper: the [recipe](../../docs/recipes/officeqa.md#the-flash-row).

ReHarness `run.yaml`, one run on 2026-10-25 inside the offline container of [`docker/`](../../docker/) (Fireworks the
only reachable service; `accounts/fireworks/models/qwen3-embedding-8b` for retrieval): over the 50 training
questions as a stream, each answered before its feedback is seen, the mean score was 0.640, 0.48 over the first 25
and 0.80 over the last 25, in 83 minutes (USD 0.40 of rollouts, USD 0.90 of reflection and revision calls). The
bank after the stream holds 49 reflections (one write was lost to a reply cut off twice at `max_tokens`, before
the reader retried with a larger budget), 8.9 K characters each on average; the 172 x 3 evaluation of that frozen
bank scored 0.579 (95% bootstrap interval over questions [0.514, 0.645]; 0 errors; USD 3.97; 56 minutes at
concurrency 8). The retrieved reflections are the bulk of the context: about 90 K tokens per attempt.

## Code

```text
utils/agent/        OfficeQAAgent (agent.py): SkillOpt's rollout, direct or Codex, with ADAPTER.md as the skill;
                    codex_sandbox.py and docker/ are the Codex harness's container, image and egress proxy
utils/prepare.py    tasks.jsonl, the splits, data.json and seed_adapter/      utils/judge.py   SkillOpt's exact match
utils/task-info/    the task for the learner
gepa/ skillopt/     smoke.yaml and run.yaml per method, smoke_codex.yaml and run_codex.yaml for the Codex harness;
                    adapter/ is the skill run.yaml trained (direct chat), with its CARD.md
harness2_stream/    Harness² under the paper's protocol, by the upstream code, with Codex as the acting agent
                    (run_codex.yaml, slots_codex.yaml; utils/agent/codex_agent.py and codex_session.py; the
                    harness2-codex template); launch.sh starts a stream
reharness/          ReHarness: stream_codex.yaml (the 172 test questions as one stream, Codex as the acting agent),
                    run_codex_flash.yaml (the 50 as one stream, then the frozen bank on the 172), the direct-chat
                    smoke.yaml, run.yaml and stream.yaml; launch.sh starts the embedding relay and the command;
                    make_seed.py writes seed_adapter/, the initial skill over the reharness template
```
