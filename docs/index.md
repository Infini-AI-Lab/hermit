# MetaHarnessAdapter

A framework for training **harness adapters**: portable harnesses (context, tools, roles, skills) that attach to an
existing agent such as Codex or Claude Code from the outside. An adapter is trained the way a model is: a template is
the starting point, an Evolver is the learning algorithm, the Trainer runs the loop, and the user supplies the task,
the data, a judge and the acting-agent pipeline.

```text
run.yaml ──► Trainer ──► rollouts (your pipeline + judge) ──► learning material ──► Evolver ──► adapter ──► git tree
```

New here? Read the [overview](framework/overview.md), then the [commands](framework/commands.md) and the
[OfficeQA example](https://github.com/haizhongzheng/MetaHarnessAdapter/tree/main/examples/officeqa).

```{toctree}
:maxdepth: 1
:caption: Evolution Framework

framework/overview
framework/adapter
framework/evolver
framework/custom-evolver
framework/trainer
framework/task
framework/configuration
framework/commands
framework/prototype-differences
```

```{toctree}
:maxdepth: 1
:caption: Design records

framework-implementation-audit-2026-10-05
evolution-framework-design-2026-10-05
async-collab-adapter-design-2026-09-27
adapter-contract-v0
core-and-evolution
execution-policy
mwm-fidelity-correction
```

```{toctree}
:hidden:
:caption: Older design notes

design-discussion-2026-09-18
subagent-fields-2026-09-18
minimal-demo-findings-2026-09-18
claude-demo-findings-2026-09-18
mwm-integration-results-2026-09-18
```
