# MetaWorld Model (MWM)

MWM maintains dated, scoped environment knowledge, a briefing, examples, an open-questions docket, and callable
programs. Acting agents use its context and one `wm` tool. A Codex learner reviews evidence after each block
of episodes and maintains the world model.

Choose MWM for an ordered environment stream with a task-owned evidence store. It requires more task integration
than a context-only optimizer; it is not a drop-in switch for an arbitrary prompt-only config.

## Required integration

| Resource | Contract |
|---|---|
| Template and method | `template: mwm`, `evolver.name: mwm` |
| Episode order | `data.shuffle: false`; `data.step_size` is the learning block |
| Evidence | The pipeline writes the native evidence layout and returns `RunOutput.extra["mwm"]` |
| Access | Evidence path in `evolver.params.evidence` and `evolver_access.environment` |
| Deployment description | `task-info/deployment/` describing the venue, host, and environment |
| Learner environment | Codex CLI and Linux namespace/sandbox support; model endpoint configured for the session |
| Channels | Task tools declared through `evolver_access.tools`, as required by the venue |

Use [ServeLearnBench](../recipes/servelearnbench.md) or [CL-Bench](../recipes/clbench.md) as a complete integration
reference. The task pipeline, rather than the Trainer, owns the benchmark's acting loop and evidence writing.

## Block lifecycle

The Trainer first serves the block with the current adapter. The learner then reviews its evidence, performs
maintenance, and revises memory and programs. The native bootstrap is part of the first step's learner processing;
the first serving block uses the seed adapter. The next block consumes the accepted update.

When `block_size` is set, a shorter last block receives no learner session. Align learning and test boundaries
deliberately. In ServeLearnBench, a window boundary may occur inside a block, so some cases are not learned
before that window's checkpoint is tested.

## Parameters and defaults

```{literalinclude} ../../harnessadapter/algorithms/evolvers/mwm/evolver.yaml
:language: yaml
```

`session_timeout_s` bounds a native session; the Trainer has its own call timeout. Provider arguments and model
settings belong to the learner session, while acting-model settings belong to the task integration.
`network_isolation` is a separate MWM setting; understand both that and the outer sandbox configuration.

Use benchmark-specific scoring for streaming experiments. A single aggregate test call cannot express all
per-window or environment-state protocols; see [Evaluation](../guides/evaluation.md#ordered-streams).
