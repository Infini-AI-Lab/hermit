# MetaHarnessAdapter

MetaHarnessAdapter helps you improve the instructions, skills, and tools used by an existing AI agent, using
feedback from tasks it has attempted. These additions are packaged in a directory the agent can load; this package
is called a **harness adapter**. You provide tasks and a way to score the answers. The framework runs the agent,
collects feedback, and manages a learning method that revises the package for future use.

These pages describe the 0.2.0 framework.

- **Try it:** [Install the package](get-started/installation.md), then [deploy your first adapter](get-started/quickstart.md).
- **Understand it:** [What is a harness adapter?](framework/what-is-a-harness-adapter.md) defines the package from both ends; [Overview](framework/overview.md) introduces the project's purpose, main components, and their relationships.
- **Follow the workflow:** [Using and training adapters](framework/adapter-lifecycle.md) shows the files, deployment, and handoffs in an interactive diagram.
- **Do one thing:** the [tutorials](tutorials/deploy-an-adapter.md), one per situation: deploy an adapter, reproduce a result, bring your own task, customize an adapter, write your own Evolver.
- **Bring your task:** [Task integration](framework/task.md) explains the pipeline, judge, and data you supply.
- **Build a learning method:** [Custom Evolver integration](framework/custom-evolver.md) describes the extension interface.

```{toctree}
:maxdepth: 1
:caption: Get Started

framework/what-is-a-harness-adapter
get-started/installation
get-started/quickstart
framework/adapter-lifecycle
```

```{toctree}
:maxdepth: 1
:caption: Tutorials

tutorials/deploy-an-adapter
tutorials/reproduce-a-result
tutorials/your-own-task
tutorials/customize-an-adapter
tutorials/custom-evolver
```

```{toctree}
:maxdepth: 1
:caption: Framework Components

framework/overview
framework/adapter
framework/evolver
framework/trainer
framework/task
```

```{toctree}
:maxdepth: 1
:caption: Recipes

recipes/officeqa
recipes/lean-collaboration
recipes/servelearnbench
```

```{toctree}
:maxdepth: 1
:caption: Developer Guide

framework/custom-evolver
```

```{toctree}
:maxdepth: 1
:caption: Reference

framework/configuration
framework/commands
framework/adapter-manifest
```
