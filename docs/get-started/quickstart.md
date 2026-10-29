# Quick Start: train, evaluate, and deploy

Run a complete training cycle locally with Python and Git. No model account, dataset download, GPU, or agent CLI
is needed. The example is deterministic: a teaching agent recognizes one formatting rule, and a scripted Evolver
learns that rule from judge feedback. It demonstrates the training contract, not real-model learning performance.

## 1. Install and verify

Follow [Installation](installation.md), activate the environment, and run everything below from the repository root:

```bash
hermit --help
hermit check --config docs/_examples/first_training/run.yaml
```

The probe runs one training task without an adapter. Its score should be **0**, because the agent echoes lowercase
text while the judge requires uppercase output. That task score does not mean the integration check failed.

## 2. Measure the baseline

```bash
hermit eval --config docs/_examples/first_training/run.yaml --adapter none
```

The JSON summary should report `tasks: 2`, `mean_score: 0.0`, and `errors: 0`. The held-out inputs differ
from the training inputs. Answers are computed by the judge rather than stored in task rows.

## 3. Train from feedback

```bash
hermit train --config docs/_examples/first_training/run.yaml
```

When the first training task fails, the learner reads the judge's feedback and writes
`Output format: uppercase.` into `ADAPTER.md`. The next rollout uses that changed adapter on a different task.

All inputs and code are under `docs/_examples/first_training/`:

```{literalinclude} ../_examples/first_training/run.yaml
:language: yaml
```

The sandbox is disabled for this local scripted demonstration. It launches no external learner program;
[sandbox boundaries](../framework/configuration.md#sandbox-network-and-warnings) matter when using other learners.

## 4. Evaluate the result

```bash
hermit eval --config docs/_examples/first_training/run.yaml --adapter root
hermit eval --config docs/_examples/first_training/run.yaml --adapter final
```

Expected `mean_score`: **none = 0.0, root = 0.0, final = 1.0**, all with zero execution errors.
`root` is the initial template; `final` is the run's selected adapter. This gain is constructed by the toy task
and its one-rule interpreter. It verifies that feedback, editing, versioning, and evaluation connect correctly.

For a real agent, the pipeline passes adapter context and tools to a model. See
[First training with a real model](first-model.md) for an API-only walkthrough with included data, then
[OfficeQA](../recipes/officeqa.md) for a benchmark integration. See the [evaluation guide](../guides/evaluation.md).

## 5. Inspect and deploy

```bash
cat .demo/first-training/first-training/checkout/ADAPTER.md
cat .demo/first-training/first-training/checkout/.hermit/CARD.md
cat .demo/first-training/first-training/eval/final/summary.json
hermit deploy \
  --adapter .demo/first-training/first-training/checkout \
  --workspace .demo/first-training/workspace \
  --host codex
cat .demo/first-training/workspace/AGENTS.md
```

The generated `AGENTS.md` contains the rule. `deploy` writes a workspace; starting a host is a separate action.
For Claude Code, use `--host claude-code` and a different workspace directory.

## Run it again

An unfinished run resumes automatically. For an independent experiment, choose a new name on every command:

```bash
hermit train --config docs/_examples/first_training/run.yaml trainer.run_name=first-training-02
hermit eval --config docs/_examples/first_training/run.yaml --adapter final trainer.run_name=first-training-02
```

That experiment's outputs are under `.demo/first-training/first-training-02/`.
See [Run records and recovery](../guides/runs.md) for completed runs and branching.

## Choose your next step

- [Choose a method](../methods/index.md) for your task and editable adapter content.
- [Train with a real model](first-model.md) using only an API endpoint and the included small dataset.
- [Run a benchmark recipe](../recipes/index.md) once you have its required assets.
- [Bring your task](../tutorials/your-own-task.md) to connect your acting agent and judge.
- [Deploy an existing adapter](../tutorials/deploy-an-adapter.md) if you only need the learned package.
