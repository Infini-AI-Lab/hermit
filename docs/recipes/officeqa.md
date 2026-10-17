# OfficeQA

This recipe makes real model calls. It runs OfficeQA as the SkillOpt paper does: the paper's 50 / 24 / 172
train/validation/test split of OfficeQA Full, each question's parsed source pages with file tools limited to its
source files, a seed skill, and exact-match scoring. A GLM agent answers questions about Treasury Bulletins with the
adapter's `ADAPTER.md` as its skill, and the `prompt_only` Evolver revises that skill from the scored answers;
GEPA and SkillOpt train the same skill afterwards.

Complete [Installation](../get-started/installation.md), activate the virtual environment, and run the commands from
the repository root. For an API-only walkthrough without external dataset assets, use [First real-model training](../get-started/first-model.md).
For local training without model access, use [Quick Start](../get-started/quickstart.md).

## Prepare the data and model access

Obtain authorized access to the gated `databricks/officeqa` dataset: `officeqa_full.csv` and
`treasury_bulletins_parsed/` with its `transformed/` and `jsons/` directories. The acting agent is SkillOpt's own
rollout and the judge is SkillOpt's scorer, so every method needs the SkillOpt extra:

```bash
python -m pip install -e '.[skillopt,gepa]'
python examples/officeqa/utils/prepare.py --csv /absolute/path/to/officeqa_full.csv --docs /absolute/path/to/treasury_bulletins_parsed
```

This writes `tasks.jsonl` (questions only), split files, `data.json`, and `seed_adapter/` (SkillOpt's initial
OfficeQA skill as a `prompt-only@1.0` adapter) inside `examples/officeqa/utils/`, and the answers to
`utils/.private/answers.jsonl`. It fetches the split from SkillOpt's repository at a pinned commit unless
`--split-dir` names a local copy. The smoke splits are the first four training, two validation, and four test
questions.

Set `FIREWORKS_API_KEY` in your shell before continuing. The acting agent (GLM 5.3 Flash, set in
`utils/agent/agent.py`) and the learner in each config use Fireworks; the models must be available to your account.

## Check, train, evaluate

Inspect `examples/officeqa/prompt_only/smoke.yaml`. It selects `prompt-only@1.0`, starts from
`../utils/seed_adapter`, uses the `prompt_only` Evolver with GLM 5.3 as its model, batches of two tasks, one epoch,
and a training budget of USD 3 / one hour. Dollar accounting uses the prices stored in the example; verify them for
your endpoint. Usage becomes visible at call boundaries, so the budget is not a provider-side spending cap. The
separate `check` probe and `eval` calls are outside the training budget.

```bash
harnessadapter check --config examples/officeqa/prompt_only/smoke.yaml
harnessadapter train --config examples/officeqa/prompt_only/smoke.yaml
harnessadapter eval --config examples/officeqa/prompt_only/smoke.yaml --adapter final
harnessadapter eval --config examples/officeqa/prompt_only/smoke.yaml --adapter ../utils/seed_adapter
harnessadapter eval --config examples/officeqa/prompt_only/smoke.yaml --adapter none
```

`check` runs one training task without an adapter to probe the pipeline and judge; use `--no-probe` to skip that model
call. `train` executes two steps. On each step it runs the acting agent, scores the answers, lets the learner
rewrite the skill, and checks and saves the returned adapter. `eval` separately measures a selected version on the
four test tasks, three passes each (`eval.repeats`): `final`, the seed skill the run started from, and no skill.
A smoke run checks that the integration works; four test questions cannot establish a reliable performance gain.

The example requests sandboxing. On a machine without `bwrap`, programs launched through that sandbox fail; see
[Installation](../get-started/installation.md) before using an agent Evolver such as `examples/officeqa/codex/`.
That learner has a shell, so its config hides `examples/` from it (`evolver.params.hide`: the answers are in
`utils/.private/`) and writes its run outside the checkout, to `~/.cache/harnessadapter/officeqa-codex/`.
The in-process `prompt_only` method calls the run's model tool directly; enabling the sandbox does not turn the whole
Python process into an isolated environment.

## Inspect the result

Paths in `smoke.yaml` are relative to that file, so the outputs are under `examples/officeqa/prompt_only/runs/`:

```text
runs/
  trees/smoke-prompt-only/  Git history of adapter versions
  smoke-prompt-only/
    checkout/                current adapter, including ADAPTER.md and CARD.md
    steps.jsonl              scores, changes, costs, and commit IDs per step
    learning_material/       results and available logs shown to the learner
    step_0001/state/         learner state saved after step 1
    summary.json             final tag, checkpoints, and stop reason
    eval/                    results for final, seed_adapter, and none; mean_score in each summary.json
```

```bash
cat examples/officeqa/prompt_only/runs/smoke-prompt-only/checkout/ADAPTER.md
cat examples/officeqa/prompt_only/runs/smoke-prompt-only/checkout/CARD.md
```

Deploy the selected adapter into a separate generated workspace:

```bash
harnessadapter deploy --adapter examples/officeqa/prompt_only/runs/smoke-prompt-only/checkout --workspace .demo/officeqa-codex --host codex
```

Inspect its `AGENTS.md` before starting the host. This checks host integration; effectiveness in another host needs
its own evaluation. Keep the adapter directory available if exported tools refer to its files.

Rerunning `train` resumes an unfinished run at its last completed step. For another experiment after this one finishes,
choose another name, for example `trainer.run_name=smoke-02`, and use the same override on the corresponding `eval`
commands. The framework does not automatically choose the best checkpoint:
`final` is the Evolver's final selection, which defaults to the last adapter.

## Train with GEPA or SkillOpt

The same task trains with the `gepa` or `skillopt` Evolver instead of `prompt_only`. Both score candidate
adapters on validation questions, so their configs add a validation split. Run the smoke config first, then the
full one:

```bash
harnessadapter train --config examples/officeqa/skillopt/smoke.yaml
harnessadapter train --config examples/officeqa/skillopt/run.yaml
harnessadapter eval --config examples/officeqa/skillopt/run.yaml --adapter final
harnessadapter eval --config examples/officeqa/skillopt/run.yaml --adapter none
harnessadapter eval --config examples/officeqa/skillopt/run.yaml --adapter ../utils/seed_adapter
```

Use `examples/officeqa/gepa/smoke.yaml` and `gepa/run.yaml` for GEPA. `skillopt/run.yaml` uses the paper's
SkillOpt hyperparameters (4 epochs, batches of 40, learning rate 4 decaying to 2, gated slow update, meta skill);
`gepa/run.yaml` uses GEPA's default reflection minibatch of 3 and 4 epochs, since the paper does not publish its
GEPA settings. Each method has its own config and output directory; do not mix their run-name or tree settings.
Outputs are in `examples/officeqa/<method>/runs/<configured-run-name>/`, including `checkout/CARD.md`,
`steps.jsonl`, `eval/`, and `workspace/state/` (saved per step in `step_NNNN/state/`) with each method's
candidates and records.

## Included adapters

The adapters trained by `skillopt/run.yaml` and `gepa/run.yaml` are under `examples/officeqa/skillopt/adapter/` and
`gepa/adapter/`. Evaluate them with a directory selector:

```bash
harnessadapter eval --config examples/officeqa/skillopt/run.yaml --adapter adapter
harnessadapter eval --config examples/officeqa/gepa/run.yaml --adapter adapter
```

Their measured scores are in the [example's README](https://github.com/haizhongzheng/MetaHarnessAdapter/tree/main/examples/officeqa)
and in each `CARD.md`. Training-answer numbers in them are replaced by made-up numbers of the same form; the cards
describe scores of the adapters as trained. Re-evaluate the distributed artifact when that distinction matters.

## Reproduction limits

This implementation uses GLM acting and learner models and SkillOpt's offline file tools. Those differ from the
paper's models and internal search. Preparation records items whose sources changed since the paper's dataset
revision in `data.json`.

Preserve `slow_update_gate`, `trajectory_view`, `prompt_env`, and final-selection settings when comparing protocols.
See [SkillOpt](../methods/skillopt.md) for those controls and [Evaluation](../guides/evaluation.md) for reporting.

For your own task, implement the agent and judge described in [Task integration](../framework/task.md). For how
the components exchange data, continue to [Using and training adapters](../framework/adapter-lifecycle.md).
