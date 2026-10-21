# OfficeQA

[OfficeQA](https://github.com/databricks/officeqa) ([dataset](https://huggingface.co/datasets/databricks/officeqa), [paper](https://arxiv.org/abs/2603.08655)): questions about U.S. Treasury Bulletins (1939–2025) whose
answers are figures found in the bulletins, often after a computation. This example runs OfficeQA Full (246
questions) as the [SkillOpt paper](https://arxiv.org/abs/2605.23904) does: its 50 / 24 / 172 train / validation /
test split, each question's parsed source pages in the prompt, and exact match. It trains the agent's skill, the
adapter's `ADAPTER.md`, with the GEPA and SkillOpt methods and compares them with no skill and with SkillOpt's
initial skill, in both of the paper's harnesses: direct chat, with read and grep tools limited to the question's
source files, and Codex, a `codex exec` session with the whole corpus.

## Setup

Install the framework first ([Installation](../../docs/get-started/installation.md)) with the `skillopt` extra (the
agent is SkillOpt's own rollout and the judge its scorer) and the `gepa` extra. Also needed: the gated Hugging Face
dataset `databricks/officeqa` (`officeqa_full.csv`, and `treasury_bulletins_parsed/` with `transformed/` and
`jsons/`), and the key of the models' backend: for the default (GLM 5.3 Flash acting, GLM 5.3 learning, on
Fireworks) `FIREWORKS_API_KEY`. `model=<backend>/<model id>` on the command line moves the agent, `llm=...` the
learner ([Model backends](../../docs/guides/model-backends.md)). The Codex harness also needs Docker: each session
runs in a container of its own that sees only its workspace and the corpus and reaches only the model provider, set
up once as in the [recipe](../../docs/recipes/officeqa.md#run-in-the-codex-harness).

```bash
pip install -e '.[skillopt,gepa]'
python examples/officeqa/utils/prepare.py --csv /path/to/officeqa_full.csv --docs /path/to/treasury_bulletins_parsed
```

`prepare.py` writes into `utils/` the questions, the paper's split (from SkillOpt's repository at a pinned commit),
`data.json` and `seed_adapter/` (SkillOpt's initial skill, where every config starts), and the answers into
`utils/.private/`.

## Run

```bash
harnessadapter train --config examples/officeqa/gepa/run.yaml          # GEPA
harnessadapter train --config examples/officeqa/skillopt/run.yaml      # SkillOpt
harnessadapter eval  --config examples/officeqa/gepa/run.yaml     --adapter final                  # GEPA's skill
harnessadapter eval  --config examples/officeqa/skillopt/run.yaml --adapter final                  # SkillOpt's skill
harnessadapter eval  --config examples/officeqa/skillopt/run.yaml --adapter none                   # no skill
harnessadapter eval  --config examples/officeqa/skillopt/run.yaml --adapter ../utils/seed_adapter  # initial skill
```

An adapter's accuracy is `mean_score` in `<method>/runs/<run name>/eval/<selector>/summary.json` (172 test
questions, three passes each), where `<selector>` is `final`, `none`, `adapter` or a directory's name such as
`seed_adapter`; `--adapter adapter` evaluates the trained adapter included in the folder. A smoke on a few
questions: the folder's `smoke.yaml`, after `harnessadapter check --config ...`
([walkthrough](../../docs/recipes/officeqa.md#check-train-evaluate)). The Codex harness: the same commands with
`run_codex.yaml` and `smoke_codex.yaml`, the folder's configs with `agent.params.harness: codex` and no budget.

## Results

Agent: GLM 5.3 Flash (reasoning effort medium) in SkillOpt's rollout, in direct chat (`run.yaml`) and in SkillOpt's
Codex harness (`run_codex.yaml`; Codex CLI 0.134 for Linux, one container per session). Learner: GLM 5.3 (medium).
172 test questions, three passes each.

Both methods train the same adapter, one `ADAPTER.md` (the `prompt-only@1.0` template) given to the agent as its
skill, from SkillOpt's initial skill. **GEPA** ([`gepa/`](gepa/), [method](../../docs/methods/gepa.md)): GLM 5.3
reflects on three trajectories at a time to propose a new skill, and the candidates are kept on a Pareto front over
the 24 validation questions. **SkillOpt** ([`skillopt/`](skillopt/), [method](../../docs/methods/skillopt.md)), with
the paper's hyperparameters: GLM 5.3 analysts propose edits from batches of 40 questions, a learning rate bounds how
many are applied, and the validation questions gate every update.

| Method | accuracy, direct chat | accuracy, Codex harness |
|---|---|---|
| no skill: the agent without an adapter | 0.415 | 0.543 |
| initial skill: SkillOpt's, where both methods start (`utils/seed_adapter/`) | 0.465 | 0.564 |
| **GEPA** (`gepa/`) | 0.564 | 0.595 |
| **SkillOpt** (`skillopt/`) | 0.576 | 0.628 |

Paired per question with 95% bootstrap intervals, both methods beat no skill in direct chat (GEPA +14.9 [+9.5, +20.5]
points, SkillOpt +16.1 [+10.3, +22.1]); in the Codex harness SkillOpt does (+8.5 [+3.1, +14.0]) and GEPA is borderline
(+5.2 [−0.2, +10.5]). SkillOpt − GEPA is +1.2 [−3.1, +5.4] in direct chat and +3.3 [−0.8, +7.4] in the Codex harness:
the two methods are not separated.

`gepa/adapter/` and `skillopt/adapter/` are the skills the direct-chat runs trained, with their cards
([GEPA](gepa/adapter/CARD.md), [SkillOpt](skillopt/adapter/CARD.md)). The recipe has every paired difference, the
paper's own Codex-harness numbers, what each run selected, what the direct-chat runs cost, and the Codex sessions'
timeouts in its [Results](../../docs/recipes/officeqa.md#results), the sessions' isolation in [Run in the Codex
harness](../../docs/recipes/officeqa.md#run-in-the-codex-harness), and the differences from the paper in [Reproduction
limits](../../docs/recipes/officeqa.md#reproduction-limits).

## Code

```text
utils/agent/        OfficeQAAgent (agent.py): SkillOpt's rollout, direct or Codex, with ADAPTER.md as the skill;
                    codex_sandbox.py and docker/ are the Codex harness's container, image and egress proxy
utils/prepare.py    tasks.jsonl, the splits, data.json and seed_adapter/      utils/judge.py   SkillOpt's exact match
utils/task-info/    the task for the learner
gepa/ skillopt/     smoke.yaml and run.yaml per method, smoke_codex.yaml and run_codex.yaml for the Codex harness;
                    adapter/ is the skill run.yaml trained (direct chat), with its CARD.md
```
