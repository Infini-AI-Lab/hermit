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
| [`harness2/`](harness2/) | Harness² (no judge feedback, no validation split), k = 1, starting from SkillOpt's initial skill | test mean 0.583 after the $40 budget stopped it at step 25 (epoch 3); 0.572 at the epoch-1 checkpoint; trained without the parsed pages (see below); no adapter included |

Re-evaluated together on 2026-10-21 (172 questions × 3), the starting skill scores 0.490 and the same skill plus one
line asking for only the bare value inside `<answer>...</answer>` scores 0.574; the included GEPA and SkillOpt
adapters score 0.562 and 0.558 (they carry made-up numbers in place of training answers), and Harness²'s epoch-1 and
final adapters 0.572 and 0.583. No paired difference among the trained adapters and that control is significant: every
95% interval contains 0, and this test detects differences of about 0.05 to 0.06. For all of them, 0.56 to 0.58 of the
answers contain the reference value, as for the starting skill: under this exact-match scorer, the gains over the
starting skill come from the answer's form, not from finding more right values. Harness² sees no judge feedback, so
`harness2/slots.yaml` names that form; with slots that did not, its epoch-1 adapter scored 0.250 against the starting
skill's 0.380.

Each question's parsed pages must be in its prompt. SkillOpt reads them from `jsons/` beside the directory
`transformed/` resolves to, so a `transformed/` symlinked from a folder without `jsons/` silently drops them, and
every method then scores 0.05 to 0.11 lower. The Harness² training run, the 0.250 and 0.380 above and an earlier
re-evaluation ran without them; `prepare.py` and the agent now refuse to run without the pages.

Differences from the paper: GLM 5.3 Flash and GLM 5.3 instead of GPT-5.x and Qwen; SkillOpt's offline file tools
instead of the paper's web search; questions and answers from the latest dataset revision (`prepare.py` lists the
items whose sources changed in `data.json`).
