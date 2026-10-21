# OfficeQA in the SkillOpt paper's setting

OfficeQA as the SkillOpt paper runs it ([arXiv 2605.23904](https://arxiv.org/abs/2605.23904)): its 50 / 24 / 172
split of OfficeQA Full, each question's parsed source pages in the prompt with read/grep tools limited to its source
files, SkillOpt's own rollout in offline mode, and SkillOpt's exact-match scorer. GLM 5.3 Flash answers; the
adapter's `ADAPTER.md` is its skill.

## Layout

```text
utils/agent/agent.py            OfficeQAPaperAgent: SkillOpt's rollout (skillopt.envs.officeqa.rollout) with the adapter's context as the skill
utils/judge.py      SkillOpt's exact-match scorer
utils/prepare.py    writes tasks.jsonl, splits/, data.json and seed_adapter/ beside itself
utils/task-info/                the task as the learner reads it
gepa/ skillopt/                 one folder per method: smoke.yaml, run.yaml, and the adapter trained by run.yaml
```

## Prepare

You need access to the gated Hugging Face dataset `databricks/officeqa` (`officeqa_full.csv` and
`treasury_bulletins_parsed/` with `transformed/` and `jsons/`). The agent uses SkillOpt's rollout, so install the
`skillopt` extra.

```bash
pip install -e '.[skillopt]'
python examples/officeqa_paper/utils/prepare.py --csv officeqa_full.csv --docs treasury_bulletins_parsed
```

The answers go to `.private/answers.jsonl`. `seed_adapter/` is SkillOpt's initial OfficeQA skill as a
`prompt-only@1.0` adapter; the configs start training from it.

## Run

```bash
export FIREWORKS_API_KEY=...
harnessadapter train --config examples/officeqa_paper/skillopt/run.yaml
harnessadapter eval  --config examples/officeqa_paper/skillopt/run.yaml --adapter final
```

## Methods

| Folder | Evolver | Result |
|---|---|---|
| [`gepa/`](gepa/) | GEPA, starting from SkillOpt's initial skill | test mean 0.564 (172 questions × 3), adapter in `gepa/adapter/` |
| [`skillopt/`](skillopt/) | SkillOpt with the paper's hyperparameters | test mean 0.576, adapter in `skillopt/adapter/` |
| [`harness2/`](harness2/) | Harness² (no judge feedback, no validation split), k = 1, starting from SkillOpt's initial skill | test mean 0.488 after the $40 budget stopped it at step 25 (epoch 3); 0.516 and 0.512 at the epoch-1 and epoch-2 checkpoints; no adapter included |

Re-evaluated on 2026-10-20 and 21 with the same protocol, the starting skill scores 0.380 and the same skill plus one
line asking for only the bare value inside `<answer>...</answer>` scores 0.492; the included GEPA and SkillOpt
adapters score 0.500 and 0.504 (they carry made-up numbers in place of training answers). For all of them, 0.49 to
0.52 of the answers contain the reference value: under this exact-match scorer, the gains over the starting skill
come from the answer's form, not from finding more right values. Harness² sees no judge feedback, so
`harness2/slots.yaml` names that form; with slots that did not, its epoch-1 adapter scored 0.250.

Differences from the paper: GLM 5.3 Flash and GLM 5.3 instead of GPT-5.x and Qwen; SkillOpt's offline file tools
instead of the paper's web search; questions and answers from the latest dataset revision (`prepare.py` lists the
items whose sources changed in `data.json`).
