# SkillOpt

[SkillOpt](https://github.com/microsoft/SkillOpt) ([paper](https://arxiv.org/abs/2605.23904)) trains a skill
document the way an optimizer trains weights. Analyst model calls read batches of trajectories and propose edits; the
edits are merged, ranked and cut to a learning rate (the most edits per step, decaying over the run), and an update
is kept only if the validation score improves. At each epoch's end, a slow update compares the last two epochs'
skills on the same tasks and rewrites a protected section of the skill, and a meta skill keeps notes that guide the
optimizer's later edits.

`evolver.name: skillopt` runs one SkillOpt step per training step with SkillOpt's own functions; its model calls go
through the run's metered `llm`. Install it with `pip install -e '.[skillopt]'`.

## One step

1. The Trainer runs the current skill on a batch of `data.step_size` training tasks.
2. The analyst reflects on the trajectories in minibatches of `minibatch_size` and proposes patches, merged in groups
   of `merge_batch_size`.
3. The edits are ranked and at most `learning_rate` of them are applied.
4. The gate scores the new skill on the validation tasks and keeps it only if the score improves.
5. At an epoch's last step, the slow update and the meta skill run.

State is kept in `state/skillopt.json`. At the end, `finalize` hands back the skill with the best validation score.

## Knobs

| Setting | Meaning |
|---|---|
| `llm` | The optimizer model |
| `data.step_size` | The batch size |
| `data.val` | Required: the gate scores on it |
| `trainer.epochs` | The number of epochs |
| `evolver.params.total_steps` | Steps in the run (epochs × steps per epoch), for the learning-rate decay |
| `evolver.params.learning_rate`, `min_learning_rate`, `lr_scheduler` | Most edits per step: 4 decaying to 2, `cosine` |
| `evolver.params.minibatch_size`, `merge_batch_size`, `analyst_workers` | Rollouts per reflection call, patches per merge call, parallel calls |
| `evolver.params.slow_update_samples`, `slow_update_gate` | Tasks the slow update compares on; whether the gate checks it too |
| `evolver.params.prompt_env` | `officeqa`: SkillOpt's OfficeQA analyst prompts; empty: its generic prompts |

All parameters and defaults: [`harnessadapter/algorithms/evolvers/skillopt/evolver.yaml`](../../harnessadapter/algorithms/evolvers/skillopt/evolver.yaml).

## Examples

| Folder | Task | Configs |
|---|---|---|
| [`officeqa/`](officeqa/) | [OfficeQA](../tasks/officeqa/); GLM 5.3 Flash acts and optimizes | `smoke.yaml`: 4 training questions, 2 epochs of 2 steps, 2 validation, 2 test<br>`run.yaml`: 60 training questions in batches of 20, 2 epochs, 13 validation, 60 test |
| [`officeqa_paper/`](officeqa_paper/) | [OfficeQA in the SkillOpt paper's setting](../tasks/officeqa_paper/); GLM 5.3 Flash acts, GLM 5.3 optimizes; starts from SkillOpt's initial skill | `smoke.yaml`: 4 training questions, 2 epochs of 2 steps, 2 validation, 4 test<br>`run.yaml`: the paper's hyperparameters: 50 training questions in batches of 40, 4 epochs, minibatch 8, merge 8, learning rate 4 to 2 (cosine), gated slow update, meta skill; 24 validation, 172 test × 3<br>`adapter/`: trained with `run.yaml` |

## Run

```bash
pip install -e '.[skillopt]'
python examples/tasks/officeqa/prepare.py /path/to/officeqa/prepared
export FIREWORKS_API_KEY=...
harnessadapter train --config examples/skillopt/officeqa/smoke.yaml
harnessadapter train --config examples/skillopt/officeqa/run.yaml
harnessadapter eval  --config examples/skillopt/officeqa/run.yaml --adapter final
```

In the paper's setting (prepare it as in [`tasks/officeqa_paper`](../tasks/officeqa_paper/)):

```bash
harnessadapter train --config examples/skillopt/officeqa_paper/smoke.yaml
harnessadapter train --config examples/skillopt/officeqa_paper/run.yaml
harnessadapter eval  --config examples/skillopt/officeqa_paper/run.yaml --adapter final
harnessadapter eval  --config examples/skillopt/officeqa_paper/run.yaml --adapter adapter
harnessadapter eval  --config examples/skillopt/officeqa_paper/run.yaml --adapter ../../tasks/officeqa_paper/seed_adapter
harnessadapter eval  --config examples/skillopt/officeqa_paper/run.yaml --adapter none
```

The three `eval` lines after `final` score the included adapter, SkillOpt's initial skill, and the agent without a
skill. In `officeqa_paper/adapter/ADAPTER.md`, numbers the optimizer copied from training answers into examples are
replaced with made-up numbers of the same form; `CARD.md` records the scores of the adapter as trained.
