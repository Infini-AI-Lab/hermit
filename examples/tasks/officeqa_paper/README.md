# Task: OfficeQA in the SkillOpt paper's setting

OfficeQA as the SkillOpt paper runs it ([arXiv 2605.23904](https://arxiv.org/abs/2605.23904)): its 50 / 24 / 172
split of OfficeQA Full, each question's parsed source pages in the prompt with read/grep tools limited to its source
files (SkillOpt's own rollout, offline mode: `pipeline.py`), and SkillOpt's exact-match scorer (`judge.py`). GLM 5.3
Flash answers; the adapter's `ADAPTER.md` is its skill.

## Prepare

You need access to the gated Hugging Face dataset `databricks/officeqa` (`officeqa_full.csv` and
`treasury_bulletins_parsed/` with `transformed/` and `jsons/`). The pipeline uses SkillOpt's rollout, so install the
`skillopt` extra.

```bash
pip install -e '.[skillopt]'
python examples/tasks/officeqa_paper/prepare.py --csv officeqa_full.csv --docs treasury_bulletins_parsed
```

This writes `tasks.jsonl` (questions only), `splits/`, `data.json` and `seed_adapter/` here, and the answers to
`.private/answers.jsonl`. `seed_adapter/` is SkillOpt's initial OfficeQA skill as a `prompt-only@1.0` adapter; the
configs start training from it.

## Differences from the paper

- GLM 5.3 Flash and GLM 5.3 instead of the paper's GPT-5.x and Qwen models.
- SkillOpt's offline file tools instead of the paper's internal web search.
- Questions and answers from the latest dataset revision. `prepare.py` lists the items whose sources changed since
  the paper's revision in `data.json`.

## Configs

[`gepa/officeqa_paper`](../../gepa/) and [`skillopt/officeqa_paper`](../../skillopt/), each with an adapter trained
by its `run.yaml`.
