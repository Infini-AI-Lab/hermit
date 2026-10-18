# OfficeQA

[OfficeQA](https://github.com/databricks/officeqa) ([dataset](https://huggingface.co/datasets/databricks/officeqa), [paper](https://arxiv.org/abs/2603.08655)): questions about U.S. Treasury Bulletins (1939–2025) whose
answers are figures found in the bulletins, often after a computation. This example runs OfficeQA Full (246
questions) as the [SkillOpt paper](https://arxiv.org/abs/2605.23904) does: its 50 / 24 / 172 train / validation /
test split, each question's parsed source pages in the prompt with read and grep tools limited to its source files,
and exact match. The adapter's `ADAPTER.md` is the agent's skill. Four methods train it, one folder each:
prompt-only, Codex, GEPA and SkillOpt; they are compared with no skill and with SkillOpt's initial skill.

## Setup

Install the framework first ([Installation](../../docs/get-started/installation.md)), with the `skillopt` extra (the
agent is SkillOpt's own rollout and the judge its scorer) and the `gepa` extra. Also needed: access to the gated
Hugging Face dataset `databricks/officeqa` (`officeqa_full.csv`, and `treasury_bulletins_parsed/` with
`transformed/` and `jsons/`), and a Fireworks key in `FIREWORKS_API_KEY`, or another backend
([Model backends](../../docs/guides/model-backends.md)). The Codex method also needs the Codex CLI and bubblewrap.

```bash
pip install -e '.[skillopt,gepa]'
python examples/officeqa/utils/prepare.py --csv /path/to/officeqa_full.csv --docs /path/to/treasury_bulletins_parsed
```

`prepare.py` writes the questions, the paper's splits, `data.json` and `seed_adapter/` (SkillOpt's initial OfficeQA
skill as a prompt-only adapter, where every config starts) into `utils/`, and the answers into `utils/.private/`.
It fetches the split from SkillOpt's repository at a pinned commit; `--split-dir` reads a local copy instead.

## Run

```bash
harnessadapter train --config examples/officeqa/skillopt/run.yaml        # SkillOpt
harnessadapter train --config examples/officeqa/gepa/run.yaml            # GEPA
harnessadapter train --config examples/officeqa/prompt_only/run.yaml     # prompt-only
harnessadapter train --config examples/officeqa/codex/smoke.yaml         # Codex: a smoke run only
harnessadapter eval  --config examples/officeqa/skillopt/run.yaml --adapter final   # SkillOpt's trained skill
harnessadapter eval  --config examples/officeqa/gepa/run.yaml     --adapter final   # GEPA's trained skill
harnessadapter eval  --config examples/officeqa/skillopt/run.yaml --adapter none    # no skill
harnessadapter eval  --config examples/officeqa/skillopt/run.yaml --adapter ../utils/seed_adapter   # initial skill
```

An adapter's accuracy is `mean_score` in `<method>/runs/<run name>/eval/<adapter>/summary.json` (172 test
questions, three passes each); `--adapter adapter` evaluates the adapter included in `gepa/` or `skillopt/`.
Before a `run.yaml`, run its folder's `smoke.yaml` (a few questions) and `harnessadapter check --config ...` (add
`--no-probe` to skip its one model call). The Codex learner has a shell, so its config hides `examples/` from it
(the answers are in `utils/.private/`) and writes its run to `~/.cache/harnessadapter/officeqa-codex/` instead.

## Results

Agent: GLM 5.3 Flash (reasoning effort medium) in SkillOpt's rollout. 172 test questions, three passes each.

All four methods train the same adapter, one `ADAPTER.md` (the prompt-only template), from SkillOpt's initial
skill; each folder is named after its Evolver.
**prompt-only** ([`prompt_only/`](prompt_only/), [method](../../docs/methods/prompt-only.md)): after every 10
training questions, one call to GLM 5.3 rewrites the skill from the failures and the judge's feedback; there is no
validation gate, and the final skill is the last one.
**Codex** ([`codex/`](codex/), [method](../../docs/methods/agent.md)): after every step, a Codex session with
GLM 5.3 Flash (`codex/codex_glm/`) reads the trajectories and feedback, edits `ADAPTER.md` with its own tools,
checks it against the adapter's specification and writes a report; the session continues across steps.
**GEPA** ([`gepa/`](gepa/), [method](../../docs/methods/gepa.md)): GLM 5.3 reflects on three trajectories at a time
to propose a new skill, and the candidates are kept on a Pareto front over the 24 validation questions.
**SkillOpt** ([`skillopt/`](skillopt/), [method](../../docs/methods/skillopt.md)), with the paper's
hyperparameters: GLM 5.3 analysts propose edits from batches of 40 questions, a learning rate bounds how many are
applied, and the validation questions gate every update.

| Method | accuracy (mean) |
|---|---|
| no skill: the agent without an adapter | 0.415 |
| SkillOpt's initial skill (`utils/seed_adapter/`) | 0.465 |
| **prompt-only** (`prompt_only/`) | not run yet |
| **Codex** (`codex/`) | not run yet |
| **GEPA** (`gepa/`) | 0.564 |
| **SkillOpt** (`skillopt/`) | 0.576 |

Paired per question with 95% bootstrap intervals, both trained skills beat no skill (GEPA +14.9 [+9.5, +20.5]
points, SkillOpt +16.1 [+10.3, +22.1]) and the initial skill (+9.9, +11.0); SkillOpt − GEPA is +1.2 [−3.1, +5.4],
so the two methods are not separated.

The trained adapters are in `gepa/adapter/` and `skillopt/adapter/`, with their cards. Numbers they copied from
training answers are replaced by made-up numbers of the same form, so the cards' scores are those of the adapters
as trained. Differences from the paper: GLM models instead of GPT-5.x and Qwen; SkillOpt's offline file tools
instead of the paper's web search; questions and answers from the latest dataset revision (`data.json` lists the
items whose sources changed: 6 test and 2 training questions; the results hold without them).

## Code

```text
utils/agent/        OfficeQAAgent (agent.py): SkillOpt's rollout with the adapter's context as the skill
utils/prepare.py    tasks.jsonl, the splits, data.json and seed_adapter/      utils/judge.py   SkillOpt's exact match
utils/task-info/    the task for the learner
prompt_only/ codex/ gepa/ skillopt/   smoke.yaml and run.yaml per method (codex/: smoke.yaml and its codex_glm/
                    Evolver); gepa/ and skillopt/ include the adapter their run.yaml trained
```
