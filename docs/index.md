# MetaHarnessAdapter

A continual learning framework for the deployment and evolution of **harness adapters**: small, portable harnesses that attach to an existing agent such as Codex or Claude Code from the outside, and that an Evolver improves from recorded LearningMaterial.

```text
Adapter or Architecture + LearningMaterial
     + Config + Training State (optional)
                     │
                  Evolver
                     │
         ┌───────────┴────────────┐
 Candidate Adapter       Training State (optional)
         │                        │
  review → deploy          next iteration
```

New here? Read the [overview](guide/index.md), then follow the [quickstart](guide/quickstart.md).

```{toctree}
:maxdepth: 1
:caption: Get Started

guide/index
guide/quickstart
```

```{toctree}
:maxdepth: 1
:caption: Concepts

guide/components
guide/architectures
guide/animation-workshop
```

```{toctree}
:maxdepth: 1
:caption: User Guide

guide/deployment
guide/evolution
guide/configuration
```

```{toctree}
:maxdepth: 1
:caption: Design

adapter-contract-v0
core-and-evolution
execution-policy
mwm-fidelity-correction
async-collab-adapter-design-2026-09-27
```

```{toctree}
:maxdepth: 1
:caption: Project

guide/status
```

```{toctree}
:hidden:
:caption: Design notes

design-discussion-2026-09-18
subagent-fields-2026-09-18
minimal-demo-findings-2026-09-18
claude-demo-findings-2026-09-18
mwm-integration-results-2026-09-18
```
