# GEPA

[GEPA](https://github.com/gepa-ai/gepa) ([paper](https://arxiv.org/abs/2507.19457)) evolves text by reflection. It
runs a candidate on a few tasks, asks a model to read the trajectories and the judge's feedback and propose a better
text, keeps the new text only if it does better on the same tasks, and picks the next candidate to improve from the
Pareto front: the candidates that are best on at least one validation task.

`evolver.name: gepa` runs one GEPA iteration per training step. Install it with `pip install -e '.[gepa]'`.

## One step

1. The Trainer runs the current candidate (the parent) on a minibatch of `data.step_size` training tasks.
2. Unless the parent solved them all, the `llm` reflects on the trajectories and feedback with GEPA's
   instruction-proposal prompt and writes a child.
3. The child runs on the same minibatch. It is kept only if it scores higher, and is then scored on the validation
   tasks.
4. The next parent comes from the Pareto front. Candidates and their scores are kept in `state/gepa.json`.

At the end, `finalize` hands back the candidate with the best validation mean.

## Knobs

| Setting | Meaning |
|---|---|
| `llm` | The reflection model |
| `data.step_size` | The reflection minibatch (GEPA's default: 3) |
| `data.val` | Required: candidates are compared on it |
| `trainer.epochs` | Passes over the training tasks |
| `evolver.params.candidate_selection_strategy` | `pareto` (default) or `current_best` |
| `evolver.params.acceptance_criterion` | `strict_improvement` (default) or `improvement_or_equal` |
| `evolver.params.files` | Learnable text files, improved in turn (default `[ADAPTER.md]`) |
| `evolver.params.trace_chars` | Characters of each trajectory shown to the model (12000) |
| `evolver.params.reflection_prompt_template` | Empty: GEPA's default prompt |

All parameters and defaults: [`harnessadapter/evolvers/gepa/evolver.yaml`](../../harnessadapter/evolvers/gepa/evolver.yaml).

## Examples

| Folder | Task | Configs |
|---|---|---|
| [`officeqa/`](officeqa/) | [OfficeQA](../tasks/officeqa/); GLM 5.3 Flash acts and reflects | `smoke.yaml`: 4 training questions in 2 steps, 2 validation, 2 test<br>`run.yaml`: 60 training questions in steps of 3, 13 validation, 1 epoch, 60 test |
| [`officeqa_paper/`](officeqa_paper/) | [OfficeQA in the SkillOpt paper's setting](../tasks/officeqa_paper/); GLM 5.3 Flash acts, GLM 5.3 reflects; starts from SkillOpt's initial skill | `smoke.yaml`: 4 training questions in 2 steps, 2 validation, 4 test<br>`run.yaml`: 50 training questions in steps of 3, 24 validation, 4 epochs, 172 test × 3<br>`adapter/`: trained with `run.yaml` |

The SkillOpt paper does not publish its GEPA settings; `officeqa_paper/run.yaml` uses GEPA's defaults (minibatch 3,
Pareto selection).

## Run

```bash
pip install -e '.[gepa]'
python examples/tasks/officeqa/prepare.py /path/to/officeqa/prepared
export FIREWORKS_API_KEY=...
harnessadapter train --config examples/gepa/officeqa/smoke.yaml
harnessadapter train --config examples/gepa/officeqa/run.yaml
harnessadapter eval  --config examples/gepa/officeqa/run.yaml --adapter final
```

In the SkillOpt paper's setting (prepare it as in [`tasks/officeqa_paper`](../tasks/officeqa_paper/)):

```bash
pip install -e '.[gepa,skillopt]'
harnessadapter train --config examples/gepa/officeqa_paper/smoke.yaml
harnessadapter train --config examples/gepa/officeqa_paper/run.yaml
harnessadapter eval  --config examples/gepa/officeqa_paper/run.yaml --adapter final
harnessadapter eval  --config examples/gepa/officeqa_paper/run.yaml --adapter adapter
harnessadapter eval  --config examples/gepa/officeqa_paper/run.yaml --adapter none
```

`--adapter adapter` evaluates the included adapter, and `--adapter none` the agent without a skill. In
`officeqa_paper/adapter/ADAPTER.md`, numbers the optimizer copied from training answers into examples are replaced
with made-up numbers of the same form; `CARD.md` records the scores of the adapter as trained.
