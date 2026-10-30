# Hermit

Train the instructions, skills, roles, and tools used by an existing agent, using feedback from its task attempts.
The learned files form a **harness adapter**: a versioned directory your agent consumes. Model weights stay
unchanged. You bring an acting pipeline, tasks, and a judge; the framework manages training, checks, and records.

These docs describe the **0.1.0** code in this checkout.

| Your goal | Start here | What you will produce |
|---|---|---|
| Run a complete training cycle without model access | [Quick Start](get-started/quickstart.md) | A changed adapter, held-out scores, and a host workspace |
| Run with your model API | [First real-model training](get-started/first-model.md) | Acting calls, learner edits, and held-out evaluation on included data |
| Reproduce a benchmark experiment | [Recipe catalog](recipes/index.md) | A run using the recipe's data and model setup |
| Choose a learning method | [Choose a method](methods/index.md) | A suitable template, Evolver, and split plan |
| Improve your own agent | [Bring your task](tutorials/your-own-task.md) | A pipeline, judge, task description, and run config |
| Use an existing adapter | [Deploy an adapter](tutorials/deploy-an-adapter.md) | A host workspace or a native plugin |
| Investigate a run | [Run records](guides/runs.md), [Troubleshooting](guides/troubleshooting.md) | Evidence for what ran, changed, failed, or resumed |
| Point a run at another model | [Model backends](guides/model-backends.md) | One place binds a model to Fireworks, OpenAI, DeepSeek, or a local vLLM or SGLang server |

## The training loop

1. The **pipeline** runs your acting agent with the current adapter on a batch of tasks.
2. The **judge** scores its outputs. The Trainer collects scores and available trajectories as learning material.
3. The **Evolver** uses this evidence to revise the adapter's learnable files.
4. The **Trainer** checks the candidate, records the accepted version, and starts the next batch.
5. You evaluate a selected adapter on held-out tasks and deploy it to an agent.

Passing file checks establishes structural validity. Performance improvement requires evaluation. The
[concept guide](framework/what-is-a-harness-adapter.md) defines the package, and the
[interactive overview](framework/overview.md) shows its relationships.

```{toctree}
:maxdepth: 1
:caption: Get Started

get-started/installation
get-started/quickstart
get-started/first-model
framework/what-is-a-harness-adapter
faq
```

```{toctree}
:maxdepth: 1
:caption: User Guide

framework/overview
framework/adapter-lifecycle
methods/index
guides/training
guides/evaluation
guides/model-backends
guides/runs
guides/troubleshooting
tutorials/your-own-task
tutorials/customize-an-adapter
tutorials/deploy-an-adapter
```

```{toctree}
:maxdepth: 1
:caption: Learning Methods

methods/prompt-only
methods/agent
methods/gepa
methods/skillopt
methods/mwm
methods/harness2
methods/continual_harness
methods/reharness
methods/meta_harness_adapter
methods/ace
```

```{toctree}
:maxdepth: 1
:caption: Recipes and Reproduction

recipes/index
tutorials/reproduce-a-result
recipes/officeqa
recipes/servelearnbench
recipes/clbench
recipes/lean-collaboration
```

```{toctree}
:maxdepth: 1
:caption: Developer Guide

developer/index
developer/reproducing-a-method
developer/service-design
tutorials/custom-evolver
framework/custom-evolver
framework/adapter
framework/evolver
framework/trainer
framework/task
```

```{toctree}
:maxdepth: 1
:caption: Reference

framework/configuration
reference/config-fields
framework/commands
framework/adapter-manifest
reference/glossary
reference/compatibility
```
