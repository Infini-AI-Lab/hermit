# Prompt-only

Prompt-only learns one instruction file. The adapter uses the `prompt-only@1.0` template: a single `ADAPTER.md`
given to the acting agent as context, with no tools. After every step a fresh session of the built-in Codex
Evolver reads the step's learning material (each task's input, the agent's output, the judge's score and feedback,
and the trajectory) and rewrites `ADAPTER.md`. Start here for an instruction-only adapter whose changes are easy to
inspect.

## Setup

The learner is a [Codex agent learner](agent.md), so it needs the Codex CLI on `PATH`, Linux with bubblewrap for
its sandbox, and a learner model on a backend that serves the Responses API. The `claude` Evolver does the same on
Claude Code, on a backend's key or [your subscription](../guides/claude-subscription.md).

```yaml
template: prompt-only@1.0
evolver:
  name: codex
  resume: false                  # a fresh session every step
  params:
    model: fireworks/accounts/fireworks/models/glm-5p3-flash   # the learner: <backend>/<model id>
    args: ["-c", 'model_reasoning_effort="high"']
    home: []                     # no login files: the provider key is the backend's variable
    tools: [check_spec]          # the specification check only
sandbox:
  enabled: true                  # the learner sees its workspace under /w and nothing else
trainer:
  output_dir: ~/runs/my-task
```

This fragment belongs in a complete [run config](../framework/configuration.md). The acting model is configured by
your agent, separately from the learner.

## How an update works

Each step is one Codex (or, with the `claude` Evolver, Claude Code) session. It reads the task description and the
adapter's `.hermit/EVOLVER.md`, studies the step's results, edits `ADAPTER.md`, calls `check_spec`, and writes a report
whose first line says what it changed. The run records each session as one call with its token totals; to count the
session's individual model requests, put a metering relay in front of the learner, as the examples' `launch.sh` does
(`hermit/runtime/wire_meter.py`). The Trainer then applies its specification checks. There is no validation-based
improvement gate in this method: a structurally valid update can reduce task performance. The final adapter is the
current one.

## Parameters and defaults

```{literalinclude} ../../hermit/agent/evolver/methods/codex/evolver.yaml
:language: yaml
:start-at: params:
```

`model` binds the learner to a backend, which supplies its endpoint, key variable and price. `args` passes Codex
settings, such as the reasoning effort or the model's context window (`-c model_context_window=...`).
`tools: [check_spec]` leaves the session only the specification check. The session sees its workspace under `/w`
(the adapter, the learning material, the task description, its state) and nothing else of the machine: the
framework's checkout, the task's data and other runs are not there.

`examples/clbench_poker/prompt_only/`, `examples/clbench_database/prompt_only/` and
`examples/servelearnbench/prompt_only/` run this method as their streams' baseline; their `launch.sh` sets the
learner's model and the run's paths. `examples/libero/prompt_only/` runs it on LIBERO, with a method-neutral acting
prompt. Use a small smoke run before a long one, and evaluate with the
[same held-out protocol](../guides/evaluation.md).
