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

The agent's roles (`rollout`, `eval`, `check`) use a preset for task code, which needs far more than a learner: its
helpers beside the config, its benchmark's packages, the variables its launcher exported, the endpoints those name.
What the preset keeps from the agent is the run's secrets:

- read-only: the task's project (the deepest directory holding the config, the data, the task info, the judge and
  the agent; an example's `<method>/` config and its `../utils/`), the whole Python environment, the adapter under
  test, `sandbox.read` and `profiles.rollout.read`; a granted directory holding a virtual environment (an external
  benchmark's `.venv`) comes with the Python installation that environment was made from. "Every package" means the
  environment's site directories and what their `.pth` files add, not the directory a program was started from;
- read-write: only `.logs` beside the agent's own code (examples/officeqa_paper keeps its logs there), a work
  directory per call (its HOME, logs and workspaces) and `profiles.rollout.write`; the agent's code, the config and
  the task's helpers are read-only, so an agent cannot change what later calls run;
- covered, wherever they fall in the above and under every name they are shown by (a directory reached through a
  link): the task, split and judge files read as empty; every `runs` and `.logs` directory in the project and any
  directory holding a run (this run's and the other methods' beside it, whose judge feedback can carry answers,
  found again for each call), the other calls' work, the tree directory, and `profiles.rollout.mask` (a judge's
  answers kept beside the task) are empty. A project too large to search is shown path by path instead;
- environment: inherited, unless `profiles.rollout.env` lists the variables (`sandbox.env` is the learner's);
- network: open, unless `profiles.rollout.network: isolated`, which allows the agent's bound model endpoint (a
  `127.0.0.1` endpoint is forwarded) and the role's `allow` through its own proxy;
- a CLI agent (codex, claude-code) gets its CLI as installed: the links to it, its package, the interpreter it
  names (node) and what a short wrapper script runs; its login is copied into its own `CODEX_HOME` or
  `CLAUDE_CONFIG_DIR` for the call and removed after it;
- the adapter's tools never run in the agent's process: a `Session` made there serves them from a sandbox of its
  own (the adapter is the learner's work, and the agent's process reports the result).

Every sandbox has its own process namespace: the host's processes, their environment and their files are not
reachable through `/proc`. `agent.timeout_min` stops a task call that runs longer and records it as an error. The
agent is never imported in the framework's process while its role is sandboxed.

The result's `log` is copied into the learner's material outside any sandbox, so it must lie where the call could
write (its work directory, the agent's logs, the role's `write`); another path is dropped with a warning, and a link
is never followed.

`eval` and `check` take `profiles.rollout` unless they have one of their own. A profile path may name an environment
variable (`$SLB_WORKDIR`, set by a launcher); an unset one is an error. The result comes back on a pipe only the worker
can write, so nothing the agent starts can report a result. A pipeline that needs what the preset does not give (a
GPU, a docker socket) gets it through the profile's `read` or `write`, or runs unconfined with
`profiles.rollout.enabled: false`. The examples' configs show each case: officeqa grants its prepared corpus (a link
`prepare.py` makes) and not the answers beside it, officeqa_paper covers `.private/`, clbench grants its checkout
and task directory from the launcher's variables.

The `learner` and `tool` roles are strict: `sandbox.network`, `sandbox.env` and the framework's own packages. A
declared tool that needs more says so in `profiles.tool` (examples/clbench_database/mwm's `sql` tool reads the
registered database and writes the task directory).

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
| `sandbox.profiles.<role>` | Per role: `enabled`, `read`, `write`, `mask`, `allow`, `packages`, `network`, `env`; `inherit: false` drops the shared lists. |
| `sandbox.audit: files` | Trace every file a sandboxed process opens, with strace running outside, beside its record. |

The older values still work: `network: restricted` means `isolated`, and `network: none` means `isolated` without the
proxy.

With an isolated network the only ways out are forwarded ports: every `127.0.0.1` URL in the learner's command or
environment (the run's tool endpoint, a tool gate, a model meter) and the allowlist proxy. Each is re-created inside
and relayed over a Unix socket, so a program that ignores the proxy variables has no route out. Before the learner
starts, a check inside the sandbox confirms that an outside connection fails, that every forwarded port answers, and
that the task's data file and the home's `.ssh` and `.bashrc` are absent; the learner does not start unless it passes.
On an open network (the agent's default) the same check runs for the paths only: what must be absent is, and every
covered secret is empty.

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
