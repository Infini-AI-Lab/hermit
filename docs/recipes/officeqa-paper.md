# OfficeQA in the SkillOpt paper setting

This recipe uses the paper-style 50 / 24 / 172 train/validation/test split, source-page-limited file tools,
a seed skill, and exact-match scoring. It is a separate protocol from the 133-question
[OfficeQA recipe](officeqa.md).

## Requirements

Obtain authorized access to the gated `databricks/officeqa` dataset: `officeqa_full.csv` and
`treasury_bulletins_parsed/` with its `transformed/` and `jsons/` directories. The configs name GLM 5.3 on Fireworks:
supply the credential through `FIREWORKS_API_KEY`, or bind another backend
([Model backends](../guides/model-backends.md)). Both methods use SkillOpt's acting rollout, so GEPA also needs
the SkillOpt extra.

From the repository root:

```bash
python -m pip install -e '.[skillopt,gepa]'
```

The configs select their acting and optimizer models; inspect them before running and verify the endpoint's
availability and applicable prices. This recipe performs paid model calls.

## Prepare, smoke test, and train

Place or reference the downloaded inputs when running preparation:

```bash
cd examples/officeqa_paper
python utils/prepare.py --csv /absolute/path/to/officeqa_full.csv --docs /absolute/path/to/treasury_bulletins_parsed
harnessadapter check --config skillopt/smoke.yaml
harnessadapter train --config skillopt/smoke.yaml
harnessadapter eval --config skillopt/smoke.yaml --adapter final
```

Once the smoke integration works:

```bash
harnessadapter train --config skillopt/run.yaml
harnessadapter eval --config skillopt/run.yaml --adapter final
harnessadapter eval --config skillopt/run.yaml --adapter none
harnessadapter eval --config skillopt/run.yaml --adapter ../utils/seed_adapter
```

Use `gepa/smoke.yaml` and `gepa/run.yaml` for GEPA. Preserve each config's run-name and tree-directory settings.

## Outputs and included adapters

Outputs are in `examples/officeqa_paper/<method>/runs/<configured-run-name>/`, including `checkout/CARD.md`,
`steps.jsonl`, and `eval/`. Paths in commands above are relative to that recipe directory.

Two included adapters are under `examples/officeqa_paper/skillopt/adapter/` and `gepa/adapter/`.
Evaluate them using a directory selector:

```bash
harnessadapter eval --config skillopt/run.yaml --adapter adapter
harnessadapter eval --config gepa/run.yaml --adapter adapter
```

Read their `CARD.md` files for measured results and training provenance rather than treating a smoke run as a
reproduction. Evaluation repetitions are configured in the YAML. No universal runtime or cost estimate is
established by this guide; use smoke-call records and the configured budget to plan the full run.

## Reproduction limits

This implementation uses GLM acting/optimizer models and SkillOpt's offline file tools. Those differ from the
paper's models and internal search. Preparation records changed source items in `data.json` when relevant to
the dataset revision.

Included adapter examples have training-answer numbers replaced by made-up numbers of the same form; cards
describe scores of the adapters as trained. Re-evaluate the distributed artifact when that distinction matters.

Preserve `slow_update_gate`, `trajectory_view`, `prompt_env`, and final-selection settings when comparing protocols.
See [SkillOpt](../methods/skillopt.md) for those controls and [Evaluation](../guides/evaluation.md) for reporting.
