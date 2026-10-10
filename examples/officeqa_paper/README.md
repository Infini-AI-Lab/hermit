# Example: OfficeQA in the SkillOpt paper's setting

OfficeQA as the SkillOpt paper runs it ([arXiv 2605.23904](https://arxiv.org/abs/2605.23904)): its 50 / 24 / 172
split of OfficeQA Full, each question's parsed source pages in the prompt with read/grep tools limited to its source
files (SkillOpt's own rollout, offline mode), and SkillOpt's exact-match scorer. GLM 5.3 Flash answers; the configs
train the `skillopt` and `gepa` Evolvers with GLM 5.3 as the optimizer, starting from SkillOpt's initial OfficeQA
skill.

Two adapters trained this way are included. Each `CARD.md` says how it was obtained and how it scored.

```text
adapters/skillopt/   trained with skillopt.yaml
adapters/gepa/       trained with gepa.yaml
```

## Run it

You need access to the gated Hugging Face dataset `databricks/officeqa` (`officeqa_full.csv` and
`treasury_bulletins_parsed/` with `transformed/` and `jsons/`) and a Fireworks API key. The pipeline uses
SkillOpt's rollout, so both configs need the `skillopt` extra.

```bash
pip install -e '.[skillopt,gepa]'
cd examples/officeqa_paper
python prepare.py --csv officeqa_full.csv --docs treasury_bulletins_parsed
export FIREWORKS_API_KEY=...
harnessadapter train --config skillopt_smoke.yaml
harnessadapter train --config skillopt.yaml
harnessadapter eval --config skillopt.yaml --adapter final
```

`gepa_smoke.yaml` and `gepa.yaml` do the same with GEPA. To compare on the same test questions:

```bash
harnessadapter eval --config skillopt.yaml --adapter none                  # no skill
harnessadapter eval --config skillopt.yaml --adapter seed_adapter          # SkillOpt's initial skill
harnessadapter eval --config skillopt.yaml --adapter adapters/skillopt     # an included adapter
```

## Differences from the paper

- GLM 5.3 Flash and GLM 5.3 instead of the paper's GPT-5.x and Qwen models.
- SkillOpt's offline file tools instead of the paper's internal web search.
- Questions and answers from the latest dataset revision. `prepare.py` lists the items whose sources changed since
  the paper's revision in `data.json`.
- In the included adapters, numbers the optimizers copied from training answers into examples are replaced with
  made-up numbers of the same form; the cards record the scores of the adapters as trained.
