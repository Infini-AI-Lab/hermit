# Overview

Hermit improves the instructions, skills, and tools used by an existing AI agent through feedback from
task executions. It packages those additions as a **harness adapter**, and provides the loop for running tasks,
learning from results, and saving revised adapters.

## Using an adapter

An adapter provides context, skills, roles, and callable tools to an existing agent. An optional independent service
can exchange information with the agent and coordinate its execution through a host integration. The available
interactions depend on the service and host capabilities. Click a component below to see its role and connections.

```{raw} html
:file: ../_includes/adapter-use-interactive.html
```

An instruction-only adapter uses just the context path. Using a saved adapter does not require the Trainer or Evolver.

## Evolving an adapter

The framework repeats a feedback loop: run the acting agent with an adapter, collect evidence, let an Evolver
revise a working copy, then check and save the version used by the next batch.

```{raw} html
:file: ../_includes/adapter-evolve-interactive.html
```

The task owner supplies data, the acting pipeline, and any scoring or feedback sources. The template defines which adapter files may change.
Human feedback can be supplied through task material or tools; it is not a separate built-in feedback service.
Passing the specification check establishes validity; improved task performance must be measured separately.

Scoring feedback is included on the Agent → Learning Material path. The current `train` and `eval` implementation
still requires a scoring callback through `judge.path`.

Start with [Installation](../get-started/installation.md) and [Quick Start](../get-started/quickstart.md).
For file layouts and exact handoffs, see [Using and training adapters](adapter-lifecycle.md). Component details:
[Adapter](adapter.md), [Evolver](evolver.md), [Trainer](trainer.md), and [Task](task.md).
