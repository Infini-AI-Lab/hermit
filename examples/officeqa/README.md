# OfficeQA

[OfficeQA](https://github.com/databricks/officeqa) ([dataset](https://huggingface.co/datasets/databricks/officeqa), [paper](https://arxiv.org/abs/2603.08655)): questions about U.S. Treasury Bulletins (1939–2025) whose
answers are figures found in the bulletins, often after a computation. This example runs OfficeQA Full (246
questions) as the [SkillOpt paper](https://arxiv.org/abs/2605.23904) does: its 50 / 24 / 172 train / validation /
test split, each question's parsed source pages in the prompt, and exact match. It trains the agent's skill, the
adapter's `ADAPTER.md`, with the GEPA and SkillOpt methods and compares them with no skill and with SkillOpt's
initial skill, in both of the paper's harnesses: direct chat, with read and grep tools limited to the question's
source files, and Codex, a `codex exec` session with the whole corpus.

## Setup

Install the framework first ([Installation](../../docs/get-started/installation.md)) with the `skillopt` and `gepa`
extras. Also needed: the gated Hugging Face dataset `databricks/officeqa` (`officeqa_full.csv`, and
`treasury_bulletins_parsed/` with `transformed/` and `jsons/`), and the key of the models' backend: for the default
(GLM 5.3 Flash acting, GLM 5.3 learning, on Fireworks) `FIREWORKS_API_KEY`. `model=<backend>/<model id>` on the
command line moves the agent, `llm=...` the learner ([Model backends](../../docs/guides/model-backends.md)). The Codex
harness also needs Docker, set up once as in the [recipe](../../docs/recipes/officeqa.md#run-in-the-codex-harness).

```bash
pip install -e '.[skillopt,gepa]'
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
```

An adapter's accuracy is `mean_score` in `<method>/runs/<run name>/eval/<selector>/summary.json` (172 test
questions, three passes each), where `<selector>` is `final`, `none`, `adapter` or a directory's name such as
`seed_adapter`; `--adapter adapter` evaluates the trained adapter included in the folder. A smoke on a few
questions: the folder's `smoke.yaml`, after `hermit check --config ...`
([walkthrough](../../docs/recipes/officeqa.md#check-train-evaluate)). The Codex harness: the same commands with
`run_codex.yaml` and `smoke_codex.yaml`, the folder's configs with `agent.params.harness: codex` and no budget.

## Results

Agent: GLM 5.3 Flash (reasoning effort medium) in SkillOpt's rollout, in direct chat (`run.yaml`) and in SkillOpt's
Codex harness (`run_codex.yaml`; Codex CLI 0.134 for Linux, one container per session). Learner: GLM 5.3 (medium).
172 test questions, three passes each. Both methods train one `ADAPTER.md` from SkillOpt's initial skill: **GEPA**
([`gepa/`](gepa/), [method](../../docs/methods/gepa.md)), and **SkillOpt** ([`skillopt/`](skillopt/),
[method](../../docs/methods/skillopt.md)) with the paper's SkillOpt hyperparameters.

| Method | accuracy, direct chat | accuracy, Codex harness |
|---|---|---|
| no skill: the agent without an adapter | 0.415 | 0.543 |
| initial skill: SkillOpt's, where both methods start (`utils/seed_adapter/`) | 0.465 | 0.564 |
| **GEPA** (`gepa/`) | 0.564 | 0.595 |
| **SkillOpt** (`skillopt/`) | 0.576 | 0.628 |

[Harness²](../../docs/methods/harness2.md) is not in this table: it trains nothing and processes the test questions as one stream, each question refined and answered by Codex; its result is reported with [`harness2_stream/`](harness2_stream/).

On one row of settings for every method (GLM 5.3 Flash acts and learns, Codex 0.162.0, one evaluation pass; the
`run_codex_flash.yaml` configs), not comparable with the table above:

| Method | accuracy |
|---|---|
| no skill | 0.541 |
| initial skill | 0.576 |
| **GEPA**, mean of 4 trainings | 0.580 |
| **SkillOpt**, mean of 4 trainings | 0.600 |
| **Harness²**, one stream over the test questions | 0.651 |

Paired differences with 95% intervals, each run's selection and cost, the Codex sessions' isolation and the
differences from the paper: the [recipe](../../docs/recipes/officeqa.md#results).

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
```
