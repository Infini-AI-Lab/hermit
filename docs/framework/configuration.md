# Configuration

A YAML file connects a task, template, Evolver, and training budget. `check`, `train`, and `eval` read the same
configuration. Paths are relative to that file's directory, and `${VAR}` in path values is expanded.

## Start from an existing recipe

The [OfficeQA recipe](../recipes/officeqa.md) uses this config. Its dataset and model-access prerequisites are
described in the recipe; this file alone does not supply them.

```{literalinclude} ../../examples/officeqa/gepa/smoke.yaml
:language: yaml
:caption: examples/officeqa/gepa/smoke.yaml
```

`agent.name` names the acting agent: a built-in one (`react`, `codex`, `claude-code`, `pi`, `mini-swe-agent`, `opencode`, `dsh`) or the task's own class or
function. `judge.path` names its scoring module.
`template` chooses the adapter's files and editing rules. `evolver.name` chooses the learning method.
The `llm` section configures the run's model tool, which the GEPA learner uses.

Models are named by reference, `<backend>/<model id>` (`fireworks/accounts/fireworks/models/glm-5p3-flash`,
`vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8`); the backend decides the endpoint, the key variable and the price,
so the same config runs against another provider or a local vLLM or SGLang server with one override:
`hermit train --config run.yaml model=vllm/...` for every caller that names no model of its own (a caller that names
one keeps it: the OfficeQA configs name `llm`, which takes `llm=...` too). See
[Model backends](../guides/model-backends.md).

The acting agent owns its own model configuration. Changing `llm.model` does not change the acting agent's model;
a built-in agent takes it from `agent.params.model`, and so does OfficeQA's own agent (its configs name GLM 5.3 Flash).
The top-level `model` is the default for both.

For every field, its default, type, and path rules, use the [complete field reference](../reference/config-fields.md). Method defaults are included in the [method guides](../methods/index.md).

## Defaults and overrides

The framework merges `hermit/defaults.yaml`, your YAML, and command-line overrides, in that order.
Only write settings you want to override:

```bash
hermit train --config examples/officeqa/gepa/smoke.yaml trainer.run_name=smoke-02 trainer.epochs=2
hermit eval --config examples/officeqa/gepa/smoke.yaml --adapter final trainer.run_name=smoke-02
```

Use the same run name for training and evaluation. Unknown configuration keys are rejected. Fields inside
`evolver.params` are defined by the selected Evolver.

## What each section controls

| Section | Meaning |
|---|---|
| `data` | Task JSONL file, split files, tasks per step, whether to shuffle the train split (`shuffle`, default true), and shuffle seed. |
| `task_info` | Directory explaining the task and objective to the learner. |
| `agent.name` | A built-in agent (`react`, `codex`, `claude-code`, `pi`, `mini-swe-agent`, `opencode`, `dsh`, or `harbor`: a benchmark's task set in Harbor's format, [Benchmarks](task.md#benchmarks)), `file.py:Class` for the task's own `Agent` subclass, or `file.py:function` for a plain `run(task, adapter)`. `pipeline.run` is the older spelling. |
| `agent.concurrency` | Maximum concurrent task executions during rollout and evaluation. |
| `agent.params` | The agent's settings: for a built-in agent its model, prompt and tools; for the task's own agent whatever it reads. |
| `judge.path`, `judge.name` | Scoring module/file and function name; the default name is `score`. |
| `template` | Built-in template such as `prompt-only@1.0`, or a directory with `.hermit/adapter.yaml`. |
| `start_from` | Optional tag, commit, or adapter directory from the same template; defaults to the root. |
| `evolver.name` | Built-in name or a path to an Evolver definition. |
| `evolver.params` | Parameters layered over that Evolver's defaults. |
| `evolver.resume` | Request a continued learner session between steps; the Evolver must support it. |
| `evolver_access` | Additional task resources and tools given to the learner. |
| `llm` | Optional model endpoint, model ID, key environment variable, prices, and `extra_body` (fields added to every request, such as `reasoning_effort`) for the run's `llm` tool; or `cli: codex` or `cli: claude` with that Base Agent's settings (`model`, `login`, `effort`, `web_search`, ...), `isolation` and `timeout`, for a model only that CLI reaches: each call is one session in a bubblewrap sandbox of its own (`isolation: sandbox`, the default: its empty workspace, its model's hosts through a proxy, its key or the login's token, nothing of the run; its model is a `<backend>/<model>` reference, `login: chatgpt` (Codex) or `login: claude` (Claude Code), whose hosts it reaches) or in a container (`isolation: container` with `container`). A Codex session is an agent that can run commands; a Claude Code session has no tools and can only reply ([Agent layers](agent-layers.md#layer-3-agent), [Claude subscription](../guides/claude-subscription.md)). |
| `trainer` | Run name and directories, epochs, budgets, checkpoint frequency, and recovery mode. |
| `run` | How the run is done: its mode, what the learner is shown of each verdict, a stream's orders and checkpoint tests (below). |
| `eval.repeats` | Repetitions per test row in `hermit eval`, and in the tests `hermit train` runs itself: `run.mode: split`'s test of the final adapter and the checkpoint tests. |
| `eval.infra_retries`, `eval.infra_wait_s` | How many times `hermit eval`, and the tests `hermit train` runs itself, run again the rows that were not the agent's result (status `infra`; never a multi-step task's step rows, which run only in their task's stream, nor a task Harbor runs for none of Hermit's agents, `unsupported_task`), and how long they wait before each pass. `hermit check` names them only where the config tests an adapter ([startup](#what-is-checked-at-startup)), on its line for the run block ([check](commands.md#check)). |

## How the run is done: `run`

```yaml
run:
  mode: stream                 # eval | split | stream; null: train only, as before
  feedback: score              # full | score | none: what the learner is shown of each verdict
  orders: [splits/order_a.txt, splits/order_b.txt]   # stream: order files (each order's train split) or seeds
  checkpoints:                 # stream: the commit after the step that ends with `after`, tested on `split`
    - {name: w1, after: t42, split: splits/test_w1.txt, adapters: [checkpoint, root]}   # t42 ends a step in each order
    - {name: end, after: end, split: splits/test.txt, adapters: [final]}
  checkpoints_during: false    # true: each checkpoint's test right after the step that reaches it
  state: hooks.py:state        # (run_dir, step record, commit) -> environment overrides for a checkpoint's test
  score: hooks.py:score        # (records, name) -> the benchmark's own scores of a checkpoint's test
```

- `eval`: the config is for `hermit eval` alone (a fixed adapter on `data.test`, `eval.repeats` times); it needs none
  of the training keys (`data.train`, `evolver`, `task_info`, a budget), and `hermit train` refuses it.
- `split`: train (validation optional, the Evolver may pick its final adapter on it), then the final adapter on
  `data.test` when training ends, as `hermit eval --adapter final` would (`eval/final/`).
- `stream`: each row of the train split is scored once by the adapter the earlier rows taught, before the learner
  sees it, then learned from: one epoch, no shuffle, no final selection (the final adapter is the one the last step
  left), and `run_agent` runs only rows already reached (and validation rows), never a training row that is a
  multi-step task's step (in any mode: [run_agent](evolver.md#interaction-with-the-trainer)). Its prequential score,
  overall, by the stream's halves and by step, is in `stream_summary.json`; the summary's `stream` keeps its headline
  fields (all but the per-step scores). An Evolver whose protocol scores its own steps (Harness²: the deliverable it
  chose without the labels) gives that score for the step's tasks (`Evolver.step_scores`), or `None` for a row its
  protocol leaves unscored, which the stream leaves out of its means and counts in `unscored`; `own_scores` counts
  the rows its protocol scored, and `rollout_mean`, the rollouts' mean, is given beside them whenever its verdict
  covers any row (the adapter's card says the same). A stream must name its `feedback`. With `orders`, each
  order is a run of its own (`<run_name>-o<i>`), combined in `<run>/streams.json`: an order file is that order's train
  split, a seed orders the train split (a task's rows together, in the split's order). A split whose order is kept (an
  order file, a stream's train split, one with `data.shuffle: false`, a test or checkpoint split) that lists a step of
  a multi-step Harbor task ahead of an earlier step of its stream, which it would wait for, and any split that lists a
  step but not every earlier step of its stream, are refused before any run that takes them (`hermit eval` refuses
  its split at start), and `hermit check` reports them for the splits the config's own run takes (a training config's
  train order, a checkpoint split, the test split under `run.mode` split or eval).
  A checkpoint whose `after` falls inside a step (the step's last row is another) is refused before training, or, when
  an Evolver chooses its tasks, stops the run once it is reached; since a seed moves every row, an `after` other than
  `end` goes with order files, which fix where it falls, or with `data.step_size: 1`. `hermit eval --checkpoints` runs
  the checkpoint tests that have no results.
- `feedback`: `full` (null) shows the judge's whole verdict; `score` its score alone, without the feedback text or
  anything else the judge returned; `none` no verdict at all. Under `score` and `none` a record also loses what
  carries a Harbor trial's verdict: the reward fields (`reward`, `rewards`), the trial's verifier status (`status`,
  `failure_cause`, `exception_type`, ...), its verifier, result and exception files (`verifier/`, `result.json`,
  `exception.txt`, ...) and, for a solution its verifier ran out of time on (`verifier_timeout: agent`), the timeout:
  its `timed_out` reads false, as for a run that did not time out, while a session that ran out of the agent's time
  still says so; a pipeline of your own whose output, `extra` or log carries its verdict under other names
  shows it as it is. It applies to the learning material (its README says which), `run_agent`'s results and the logs
  handed to the learner. An Evolver class with `hides_labels = True` (Harness²) keeps the labels from its sessions
  itself and is given the full records. An Evolver declares the least policy it works with, `Evolver.min_feedback`
  (`score` by default; `none` for the CLI Evolvers and a `CommandEvolver`'s program, which read the material's files,
  and for Harness², MWM, ReHarness and Continual Harness; `full` for AHE Adapter, EvoSafeHarness and the
  documentation's example Evolvers that read the judge's feedback, the Quick Start's learner among them), or under its
  params, `Evolver.min_feedback_for(params)` (SkillOpt: `full` for a soft or mixed `gate_metric`), and `hermit train`
  refuses a `run.feedback` below it before training.

A run that is not the agent's result (the pipeline's `RunOutput.extra["unscored"]`: Harbor's infra trials) has status
`infra`: it is never judged, its score is null in `steps.jsonl`, and the Trainer's and `hermit eval`'s means leave it
out and count it (`unscored`). An Evolver that averages the records itself sees such a run as a 0 with status
`infra`: GEPA, SkillOpt, ACE, Meta-Harness Adapter, AHE Adapter (an exception trial, in its pass@1) and RRSI Adapter
(a missing trial with reward 0, in its score and pass rate); OPRO runs it again.

## Starting from an adapter versus resuming a run

`start_from` initializes a new run from an existing adapter. It does not load the previous Evolver's state.
`trainer.resume_mode: auto` resumes an unfinished run with the same name from its last completed step, including
saved learner state. `disable` starts over and renames the previous run directory and branch. A resume meets the
streams of a Harbor task set's multi-step tasks again while their drivers still wait for a next step (`stream.idle_s`);
a run started over gets new ones, and a training run that finishes (its `summary.json` written: a finished run is not
resumed), all epochs done or stopped early, stops those it left unfinished. The streams of `hermit eval`, of the tests
`hermit train` runs itself and of `hermit check`'s probe, which no resume meets, are stopped when their run ends, fails
or is stopped with Ctrl-C (a process killed by a signal leaves them up for `stream.idle_s`)
([Streams](task.md#benchmarks)).

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
`check` probe and `eval` are outside this training budget, and so are the tests `hermit train` runs itself:
`run.mode: split`'s test of the final adapter and the checkpoint tests, those run during training
(`run.checkpoints_during`) included, whose time is not counted against `hours`. See [Trainer](trainer.md#budgets) for
the observation points.

## Sandboxes

Every process the framework starts runs in a sandbox ([how it works](../guides/sandbox.md),
[design](../developer/sandbox-design.md)). It requires Linux and bubblewrap. Each sandbox is an allowlist: inside, only what is declared exists, besides the Python environment the
framework runs in (shown as installed; `sandbox.hide` covers a module of it in every sandbox but the judge's).
Only the Trainer, `eval` and `check` themselves stay outside, holding the data, the answers, the run records and the
model keys.

| Sandbox | What runs in it | Declared by |
|---|---|---|
| task | the acting agent, one worker per task (rollouts, `run_agent`, `eval`, the probe of `check`), and a task's declared tools | `environment` |
| judge | the judge, when `judge.mode` is `shared` or `separate` | `judge` |
| evolver | the learner's sessions and programs | the Trainer's layout, `sandbox.evolver`, `evolver_access` |

A template's declared tools run in a strict sandbox (their code and the template read-only, the adapter they act on
writable, no network, no variables); the spec check and host-side renders run the adapter's modules in the same
strict preset with the adapter read-only. A task's declared tools run with `environment`, like its agent.

### The task sandbox: `environment`

| Setting | Effect |
|---|---|
| `environment.read` | Task paths shown read-only (relative to the config; `$VAR` names a launcher's variable, an unset one is an error): the agent's code, its data, a corpus. A path that holds or lies in the data and split files, the judge, its tests, or the runs and trees is refused. |
| `environment.write` | Paths the agent may write besides its own work directory. |
| `environment.packages` | Python distributions shown besides the framework's environment (whose site directories every sandbox shows as installed), with their dependencies: an editable install's package, one on `PYTHONPATH`. |
| `environment.env` | Environment variables passed in: names or patterns (`MWM_*`). The agent's model key variable passes by itself. |
| `environment.network` | `no-network` (default), `allowlist` (with `environment.allowed_hosts`, through a proxy), or `public`. The agent's model provider (as an allowed host) and the tool endpoint stay reachable. |
| `environment.resources` | `memory_mb`, `file_mb`, `cpu_s` per process (positive whole numbers; null: no limit); not with `environment.image`, whose container keeps its own. |
| `environment.image` | A Docker image: a Codex or Claude Code acting agent runs in a container of its own; `environment.container` gives its network and egress proxy. |
| `environment.runner: host` | The pipeline runs in the Trainer's process, for a task whose own code isolates the adapter's code it runs (AgentDyn's hooks in containers). Warned. `environment.read`, `write` and `packages` then build only the sandbox of the task's declared tools. |

Each call gets a work directory of its own (its HOME, logs and workspaces), and a fresh directory in place of the
`.logs` beside a task's agent file. The agent gets its model as Harbor's agents do: its key variable passes, its
provider's host is allowed and a model server or meter on the loopback is forwarded. A Codex ChatGPT login is copied in;
its hosts are allowed. A Claude subscription (`login: claude`) copies nothing: `CLAUDE_CODE_OAUTH_TOKEN` passes by name
for the call and `api.anthropic.com` is allowed. What the run keeps (the learning material, the adapter) is scrubbed of
key values. The adapter's modules, when the agent's code runs them (its tools, a CLI agent's deploy render), run in a
nested strict sandbox. The result comes back on a pipe only the worker can write, and its `log` must lie where the call
could write; a link is never followed. A Harbor task set (`agent.name: harbor`) runs in Harbor's container.
`agent.timeout_min` stops a call that runs longer.

### The judge: `judge.mode`

| Mode | Where the judge runs |
|---|---|
| `host` (default) | the Trainer's process, on the output |
| `shared` | the task sandbox, after the agent, over its work directory, with the judge's file and `judge.tests` shown to it alone |
| `separate` | a sandbox holding only the paths `judge.collect` names, copied from the agent's work directory through a sanitized archive |

With `sandbox.enabled: false` every mode runs the judge in the Trainer's process.

### The evolver sandbox: `sandbox`

The learner works in `/w`: `/w/adapter`, `/w/material`, `/w/task-info`, `/w/history`, `/w/state`, `/w/scratch`,
`/w/output`, `/w/root`, `/w/method`, `/w/agent-logs` (the logs `run_agent` returns, read-only), and `evolver_access` as
`/w/validation` and `/w/environment/<name>`; its prompt names them relative to `/w`. Its network reaches only its
models (their providers' hosts, a meter on the loopback; their key variables pass), the Trainer's tool endpoint and the
hosts below; a login's hosts are added (a Codex ChatGPT login's; `api.anthropic.com` on `login: claude`, whose
token passes by name with each session).

| Setting | Effect |
|---|---|
| `sandbox.enabled` | False runs everything unconfined (warned; not for reported results). |
| `sandbox.enforce: required` | Default. Refuse to run when bubblewrap is missing. |
| `sandbox.enforce: best_effort` | Run unconfined when bubblewrap is missing, and record that the sandbox was not applied. |
| `sandbox.hide` | Modules or packages of the framework's environment (`pkg.scoring`) covered in every bubblewrap sandbox but the judge's: a benchmark's scoring code the agent does not import. Each must be installed, not from a zip or wheel. A container (`environment.image`, `llm.cli` with `isolation: container`) holds its own image's copies, which it does not reach. |
| `sandbox.evolver.allowed_hosts` | More hosts the learner may reach, through the allowlist proxy. |
| `sandbox.evolver.env` | Environment variables passed to the learner's sessions (names or patterns); its models' key variables pass by themselves. |
| `sandbox.evolver.resources` | `memory_mb`, `file_mb`, `cpu_s` per process (positive whole numbers; null: no limit). |

An old key (`sandbox.profiles`, `sandbox.network`, `sandbox.allow`, `sandbox.read`, `sandbox.packages`,
`sandbox.env`, `sandbox.audit`, `evolver.params.hide`, the acting agent's `isolation: container`) fails with where
it went.

### Records

Before a task, judge, tool or learner sandbox starts its command, a check inside it confirms that an outside
connection fails (with an isolated network), that every forwarded port answers, and that the run's secrets and the
home's `.ssh` and `.bashrc` are absent; the command does not start unless it passes. Each such start leaves a
record, `sandbox/<role>-<n>.json` in the run's directory (`eval` writes its own beside its results): what was bound
and under which names, which ports were forwarded, which variables passed, and that check. The strict spec and
render checks (which show only the adapter and the template), `hermit check`'s probes (which run that check but keep
no run directory), a container's start (`environment.image`, Harbor) and a pipeline in the host process keep no such
record; the run's card records which ran. The allowlist proxies log
each request to `sandbox/network*.jsonl`.

Giving more read paths or passing more variables changes what a sandbox can access. Warnings are recorded with the
run.

`evolver_access.send_material_out` declares that sending learning material to an outside service is allowed; it is
checked when an Evolver declares that need. It is not a network filter.

## What is checked at startup?

Config parsing checks required fields and basic values, and the `eval` section only where the config tests an
adapter (`hermit eval`, a `run.mode: eval` config, `run.mode: split`'s test on `data.test`, `run.checkpoints`), for
`hermit check` and `Config.load` as for `train`. `train` also loads the template, data, pipeline, judge, and
Evolver and checks the Evolver's declared needs and session-resume support, and it refuses a `run.feedback` below
what the Evolver reads under its params (`Evolver.min_feedback_for`, [above](#how-the-run-is-done-run)) and an acting
agent of Harbor's own, which takes no adapter ([Benchmarks](task.md#benchmarks)). It does not run the full standalone
`check` command for you. Use [Commands](commands.md) for preflight checks, the optional probe, and evaluation.
