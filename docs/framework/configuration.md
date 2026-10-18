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

Models are named by reference, `<backend>/<model id>` (`fireworks/accounts/fireworks/models/glm-5p3-flash`,
`vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8`); the backend decides the endpoint, the key variable and the price,
so the same config runs against another provider or a local vLLM or SGLang server with one override:
`harnessadapter train --config run.yaml model=vllm/...`. See [Model backends](../guides/model-backends.md).

The acting agent owns its own model configuration. Changing `llm.model` does not change the acting agent's model;
OfficeQA configures that in `agent.params.model`. The top-level `model` is the default for both.

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

Every process the framework starts runs in a sandbox, in one of five roles. Only the Trainer itself and the judge
(which scores in the Trainer, on the task's answer) stay outside. It requires Linux and bubblewrap.

| Role | What runs in it |
|---|---|
| `learner` | The Evolver's learner program (a coding agent or a script). |
| `rollout` | The task pipeline during training: each task's agent runs in its own worker process. |
| `eval` | The same worker, for `harnessadapter eval`. |
| `check` | The same worker, for the probe of `harnessadapter check`. |
| `tool` | A tool the template or task declares for the Evolver; its code is never imported in the Trainer. |

A custom Evolver's own Python code runs in the Trainer, as before; what it starts goes through the learner sandbox.

It is a positive allowlist: inside, only what is granted exists. Read-only: the system's `/usr` and a few `/etc`
files, the Python interpreter, the Python distributions the framework depends on (not every package installed beside
it), the `harnessadapter` package (not the checkout around it), the learner program's installation, the Evolver's
directory, the granted accesses and `sandbox.read`. Read-write: the adapter, state, scratch and output directories.
The task's data, other runs and the user's home are not there at all.

A rollout worker sees the run's directory read-only, the adapter under test, and a work directory of its own (its
HOME, logs and workspaces). The agent's own code directory is writable, because real agents keep logs beside their
code. The run's secrets inside the visible directory are covered: the task, split and judge files read as empty, and
the runs output directory, the tree directory and the agent's other logs are empty directories. The agent reaches its
model at the bound `base_url` (a `127.0.0.1` endpoint is forwarded, any other host goes through the allowlist proxy),
with the key variable passed on; a CLI agent's login is copied into its own `CODEX_HOME` or `CLAUDE_CONFIG_DIR`. The
result comes back on a pipe that only the worker can write, so nothing the agent starts can report a result.

A pipeline that needs what the worker does not give it (a GPU, a docker socket, a path elsewhere) gets it through
`sandbox.profiles.<role>.read` or `.write`, or runs unconfined with `profiles.<role>.enabled: false`.

| Setting | Effect |
|---|---|
| `sandbox.enabled` | Wrap launched learner programs in the sandbox; false runs them unconfined. |
| `sandbox.enforce: required` | Default. Refuse to run when bubblewrap is missing. |
| `sandbox.enforce: best_effort` | Run unconfined when bubblewrap is missing, and record that the sandbox was not applied. |
| `sandbox.network: isolated` | Default. A separate network namespace with loopback only; see below. |
| `sandbox.network: open` | Use the host network, unrestricted. |
| `sandbox.allow` | Hosts reachable through the allowlist proxy when the network is isolated; empty means no proxy. |
| `sandbox.read` | Additional filesystem paths those programs may read. |
| `sandbox.packages` | Additional Python distributions to show, by name, beside the framework's own dependencies. |
| `sandbox.env` | Environment variables passed on, in addition to required runtime variables; null inherits the environment. |
| `sandbox.profiles.<role>` | Per role: `enabled`, `read`, `write`, `allow`, `packages`; `inherit: false` drops the shared lists. |
| `sandbox.audit: files` | Trace every file a sandboxed process opens, with strace running outside, beside its record. |

The older values still work: `network: restricted` means `isolated`, and `network: none` means `isolated` without the
proxy.

With an isolated network the only ways out are forwarded ports: every `127.0.0.1` URL in the learner's command or
environment (the run's tool endpoint, a tool gate, a model meter) and the allowlist proxy. Each is re-created inside
and relayed over a Unix socket, so a program that ignores the proxy variables has no route out. Before the learner
starts, a check inside the sandbox confirms that an outside connection fails, that every forwarded port answers, and
that the task's data file and the home's `.ssh` and `.bashrc` are absent; the learner does not start unless it passes.

Every wrapped program leaves a record, `sandbox/<role>-<n>.json` in the run's directory (`eval` writes its own beside
its results): what was bound read-only and read-write, what was covered, which ports were forwarded, which
environment variables passed, and the result of that check, which for a worker also proves the covered paths empty.
Each role's proxy writes one line per request, allowed or refused, to `sandbox/network*.jsonl`. With `audit: files`,
`<role>-<n>.strace` lists the file accesses; strace is bubblewrap's parent, out of the sandbox's reach.

Giving more read paths or inheriting credentials changes what the learner can access. Warnings are recorded with the
run.

`evolver_access.send_material_out` declares that sending learning material to an outside service is allowed; it is
checked when an Evolver declares that need. It is not a network filter. Likewise, the framework does not supply test
tasks through the learner interface, but broad filesystem grants or disabled isolation can expose them.

## What is checked at startup?

Config parsing checks required fields and basic values. `train` also loads the template, data, pipeline, judge, and
Evolver and checks the Evolver's declared needs and session-resume support. It does not run the full standalone
`check` command for you. Use [Commands](commands.md) for preflight checks, the optional probe, and evaluation.
