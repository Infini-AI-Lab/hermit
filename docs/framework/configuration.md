# Configuration

A YAML file connects a task, template, Evolver, and training budget. `check`, `train`, and `eval` read the same
configuration. Paths are relative to that file's directory, and `${VAR}` in path values is expanded.

## Start from an existing recipe

The [OfficeQA recipe](../recipes/officeqa.md) uses this config. Its dataset and model-access prerequisites are
described in the recipe; this file alone does not supply them.

```{literalinclude} ../../examples/officeqa/prompt_only/smoke.yaml
:language: yaml
:caption: examples/officeqa/prompt_only/smoke.yaml
```

`agent.name` names the acting agent: a built-in one (`react`, `codex`, `claude-code`) or the task's own class or
function. `judge.path` names its scoring module.
`template` chooses the adapter's files and editing rules. `evolver.name` chooses the learning method.
The `llm` section configures the run's model tool, which the prompt-only learner uses.

The acting agent owns its own model configuration. Changing `llm.model` does not change the acting agent's model;
a built-in agent takes it from `agent.params.model`, and OfficeQA's own agent sets it in `utils/agent/agent.py`.

For every field, its default, type, and path rules, use the [complete field reference](../reference/config-fields.md). Method defaults are included in the [method guides](../methods/index.md).

## Defaults and overrides

The framework merges `harnessadapter/defaults.yaml`, your YAML, and command-line overrides, in that order.
Only write settings you want to override:

```bash
harnessadapter train --config examples/officeqa/prompt_only/smoke.yaml trainer.run_name=smoke-02 trainer.epochs=2
harnessadapter eval --config examples/officeqa/prompt_only/smoke.yaml --adapter final trainer.run_name=smoke-02
```

Use the same run name for training and evaluation. Unknown configuration keys are rejected. Fields inside
`evolver.params` are defined by the selected Evolver.

## What each section controls

| Section | Meaning |
|---|---|
| `data` | Task JSONL file, split files, tasks per step, whether to shuffle the train split (`shuffle`, default true), and shuffle seed. |
| `task_info` | Directory explaining the task and objective to the learner. |
| `agent.name` | A built-in agent (`react`, `codex`, `claude-code`), `file.py:Class` for the task's own `Agent` subclass, or `file.py:function` for a plain `run(task, adapter)`. `pipeline.run` is the older spelling. |
| `agent.concurrency` | Maximum concurrent task executions during rollout and evaluation. |
| `agent.params` | The agent's settings: for a built-in agent its model, prompt and tools; for the task's own agent whatever it reads. |
| `judge.path`, `judge.name` | Scoring module/file and function name; the default name is `score`. |
| `template` | Built-in template such as `prompt-only@1.0`, or a directory with `adapter.yaml`. |
| `start_from` | Optional tag, commit, or adapter directory from the same template; defaults to the root. |
| `evolver.name` | Built-in name or a path to an Evolver definition. |
| `evolver.params` | Parameters layered over that Evolver's defaults. |
| `evolver.resume` | Request a continued learner session between steps; the Evolver must support it. |
| `evolver_access` | Additional task resources and tools given to the learner. |
| `llm` | Optional model endpoint, model ID, key environment variable, prices, and `extra_body` (fields added to every request, such as `reasoning_effort`) for the run's `llm` tool. |
| `trainer` | Run name and directories, epochs, budgets, checkpoint frequency, and recovery mode. |
| `eval.repeats` | Repetitions per test task in the separate evaluation command. |

## Starting from an adapter versus resuming a run

`start_from` initializes a new run from an existing adapter. It does not load the previous Evolver's state.
`trainer.resume_mode: auto` resumes an unfinished run with the same name from its last completed step, including
saved learner state. `disable` starts over and renames the previous run directory and branch.

Use `auto` or `disable`; selecting a recovery step/tag through `resume_mode` is not implemented. To branch a new
experiment from a checkpoint, use `start_from` and a new run name. See [Trainer](trainer.md#resuming-after-a-crash).

## Budgets and checkpoint frequency

```yaml
trainer:
  epochs: 3
  save_freq: {steps: 5, epochs: 1}
  budget: {usd: 20, tokens: null, hours: 2}
  call_timeout_min: 15
  allow_early_stop: false
```

The Trainer accounts for rollouts and learner calls. It stops at the next point where it observes a limit, so
in-flight work can overshoot it. Dollar accounting needs reported usage and applicable prices. The separate
`check` probe and `eval` are outside this training budget. See [Trainer](trainer.md#budgets) for the observation points.

## Sandbox, network and warnings

The sandbox wraps learner programs started through the framework. It does not isolate the task pipeline or
arbitrary code in an in-process custom Evolver. The current backend requires Linux and bubblewrap.

| Setting | Effect |
|---|---|
| `sandbox.enabled` | Wrap launched learner programs in the sandbox; false runs them unconfined. |
| `sandbox.read` | Additional filesystem paths those programs may read. |
| `sandbox.env` | Environment variables passed on, in addition to required runtime variables; null inherits the environment. |
| `sandbox.network: none` | Use a separate network namespace for sandboxed programs. |
| `sandbox.network: restricted` | Use an allowlist proxy and proxy environment variables; a program can bypass this by ignoring the proxy. |
| `sandbox.network: open` | Use the host network without that restriction. |
| `sandbox.allow` | Endpoints allowed by the restricted-mode proxy. |

The sandbox exposes the required runtime, workspace, learner installation, and granted resources. Adapter, state,
scratch, and output directories are writable; learning material and history are read-only. Giving more read paths
or inheriting credentials changes what the learner can access. Warnings are recorded with the run.

`evolver_access.send_material_out` declares that sending learning material to an outside service is allowed; it is
checked when an Evolver declares that need. It is not a network filter. Likewise, the framework does not supply test
tasks through the learner interface, but broad filesystem grants or disabled isolation can expose them.

## What is checked at startup?

Config parsing checks required fields and basic values. `train` also loads the template, data, pipeline, judge, and
Evolver and checks the Evolver's declared needs and session-resume support. It does not run the full standalone
`check` command for you. Use [Commands](commands.md) for preflight checks, the optional probe, and evaluation.
