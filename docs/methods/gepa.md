# GEPA

GEPA performs reflective text evolution with candidate selection. Choose it when you have a validation split,
text to optimize, and budget for candidate rollouts in addition to the main training batches.

## Setup

Run method installation commands from the Hermit checkout root with **pip 25.1+**;
see [Installation](../get-started/installation.md) for upgrading pip and installing dependency groups.

```bash
python -m pip install -e . --group gepa
```

```yaml
template: prompt-only@1.0
evolver: {name: gepa}
data:
  val: splits/val.txt
```

Add this to a complete task config with `llm` credentials. The
[OfficeQA recipe](../recipes/officeqa.md#check-train-evaluate) has ready-made configurations.

## One framework step

GEPA reflects on the current batch's trajectories, tests a child on the same tasks, and evaluates accepted
candidates on validation. Candidate records persist in the learner state. The configured strategy chooses the
next parent; finalization selects the best validation candidate retained by the method.

`data.step_size` is the reflection minibatch. Validation and candidate calls add cost beyond the main batch.
Keep validation separate from test; repeated validation selection is part of training.

## Parameters and defaults

```{literalinclude} ../../hermit/algorithms/evolvers/gepa/evolver.yaml
:language: yaml
```

## Key controls and their interactions

| Parameter | Values / default | Effect |
|---|---|---|
| `files` | List of learnable text paths / `[ADAPTER.md]` | Proposes a change to one file per reflection, rotating files per parent candidate |
| `candidate_selection_strategy` | `pareto` or `current_best` / `pareto` | Chooses the next parent from per-task validation leaders or the highest validation mean; finalization still chooses the highest mean |
| `acceptance_criterion` | `strict_improvement` or `improvement_or_equal` / strict | Compares child and parent score sums on the current training minibatch; allowing equality can add tied candidates |
| `skip_perfect_score`, `perfect_score` | Boolean and threshold / true, 1.0 | Skips reflection when every parent minibatch score reaches the threshold; set the threshold to match the judge's score scale |
| `trace_chars` | Character limit / 12000 | Clips trajectory evidence supplied to reflection; reducing it can remove failure context |
| `seed` | Integer / 0 | Seeds the method's parent-selection randomness; it does not make hosted model generations deterministic |

### When candidate execution happens

The initial parent is evaluated on validation when it first enters the pool, even if the main batch is perfect.
A proposed child must pass file checks and the training-minibatch criterion before its validation scores enter
the pool. Selection can return an older candidate, so the next acting batch need not use the newest proposal.

A perfect batch or unusable reflection can therefore leave the adapter unchanged without a method failure.
Read `state/gepa.json` in saved step state and `output/report.md` for skipped/rejected outcomes and parent selection.
The method caches retained candidates' validation scores; changing the external evaluator mid-run makes them
incomparable.

A method's best validation candidate is not guaranteed to be best on test. Report held-out results, repetitions,
errors, and cost using the [evaluation guide](../guides/evaluation.md).
