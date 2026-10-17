# Configuration field reference

The framework merges built-in defaults, your YAML, and command-line overrides, in that order. This page covers
every field in the default configuration. Method-specific parameters are included directly from their manifests
in the [learning-method pages](../methods/index.md).

## Paths, values, and overrides

Filesystem paths interpreted by the framework are relative to the **config file's directory**, not the shell's
working directory. They support home and environment-variable expansion through the config path resolver.
File callables such as `pipeline.py:run` also resolve against the config directory. Dotted Python modules use
the active Python environment. Values inside free-form method parameters can have their own path rules.

```bash
harnessadapter train --config path/to/run.yaml trainer.run_name=trial-02 data.step_size=4
harnessadapter eval --config path/to/run.yaml --adapter final trainer.run_name=trial-02 eval.repeats=3
```

Overrides are YAML values; quote shell-sensitive lists and dictionaries. Unknown framework keys are rejected.
`evolver.params`, `agent.params`, `pipeline.params`, `llm` and `backends` are free-form mappings at merge time; this is not a
guarantee that their consumer supports arbitrary fields.

## Data and task

| Field | Type / default | Meaning and constraints |
|---|---|---|
| `data.tasks` | path / null; required | JSONL with unique `id` or `task_id` values |
| `data.train` | path / null; required | One training task id per line |
| `data.val` | path / null | Validation ids; required by methods declaring validation-data needs |
| `data.test` | path / null | Test ids; a nonempty test split is required by `eval` |
| `data.step_size` | integer / 8 | Positive number of task executions in a main training batch |
| `data.shuffle` | boolean / true | Shuffle training order; set false for ordered streams |
| `data.shuffle_seed` | integer / 0 | Training shuffle seed; does not seed every model or method |
| `task_info` | directory / null | Learner-facing task description; provide `README.md` |
| `agent.name` | string / null; required | Built-in `react`, `codex`, `claude-code`, an Agent class, or a `file.py:function` / `module:function` |
| `agent.concurrency` | integer / 4 | Maximum concurrent rollout/evaluation task executions; use a positive value |
| `agent.params` | mapping / {} | Passed to Agent constructors; plain functions receive it when they declare a `params` argument |
| `pipeline.run` | string / null | Legacy alias for `agent.name`, applied only when `agent.name` is unset |
| `pipeline.concurrency` | integer / null | Legacy concurrency override when the legacy pipeline alias is used |
| `pipeline.params` | mapping / {} | Legacy parameters merged into `agent.params` when the pipeline alias is used |
| `judge.path` | file/module / null; required | Module containing the scoring callable |
| `judge.name` | string / `score` | Callable accepting `(task, RunOutput)` |

Split ids must exist in the task data. Use disjoint train/validation/test splits; `check` reports overlaps.
Task row fields become training material, so private labels require judge-owned storage.

## Adapter and learning method

| Field | Type / default | Meaning and constraints |
|---|---|---|
| `template` | string / null; required | Built-in id, `id@version`, or directory containing `adapter.yaml` |
| `start_from` | string / null | Initial adapter tag, commit, or compatible directory; does not restore learner state |
| `evolver.name` | string / null; required | Built-in method or directory containing its Evolver definition |
| `evolver.resume` | boolean / false | Request learner-session continuation; method must support it |
| `evolver.params` | mapping / {} | Overrides merged with the chosen method's defaults |
| `evolver_access.environment` | list / [] | Granted environment resources; required for MWM's evidence integration |
| `evolver_access.tools` | list / [] | Task tool declarations offered to the learner |
| `evolver_access.send_material_out` | boolean / false | Consent declaration checked for methods requesting that need; not a network filter |

The template defines what is editable. Method selection does not change those rules. See
[adapter.yaml](../framework/adapter-manifest.md) for exports, learnable files, and checkers.

## Models and backends

A model is named by a reference, `<backend>/<model id>`; a backend is an endpoint (base URL, key variable, the
dialects it serves, prices). See [Model backends](../guides/model-backends.md).

| Field | Type / default | Meaning and constraints |
|---|---|---|
| `model` | string / null | The run's default model, a `<backend>/<model id>` reference; used by `llm`, `agent.params.model` and the `codex`, `claude` and `mwm` Evolvers' `evolver.params.model` when they name none |
| `backends` | mapping / {} | Backends declared for this run by name, over `~/.config/harnessadapter/backends.yaml` and the built-ins. Fields: `base_url`, `api_key_env`, `key_required`, `apis`, `messages_url`, `litellm`, `prices`, `extra_body`, `context_window`, `models`, or `from: <other>` to copy one |
| `llm` | reference, mapping / null | The run's chat-completions `llm` tool: a reference, `{model: <reference>, price, extra_body}`, or the endpoint itself (below) |

`agent.params.model` takes the same forms. For a Codex or Claude Code acting agent or Evolver a reference becomes
the CLI's provider settings (`args` for Codex, `env` for Claude Code) and the model's id; a plain id without a
backend goes to the CLI's own provider as before.

### Learner model tool

`llm` defaults to null. When configured, it creates the run's chat-completions tool. It does not configure the acting
model in your pipeline or automatically configure an external learner CLI. The endpoint form:

| Field inside `llm` | Type / default | Meaning |
|---|---|---|
| `base_url` | string; required when configured | Endpoint prefix; the client appends `/chat/completions` |
| `model` | string; required when configured | Learner model identifier (or a `<backend>/<model id>` reference, with no `base_url`) |
| `api_key_env` | string / null | Environment-variable name containing the bearer credential |
| `price` | mapping / null | USD per million tokens: `input`, `cached_input`, `output` |
| `extra_body` | mapping / {} | Fields added to requests, such as endpoint-specific reasoning settings |

Without prices, cost is unknown. With a price mapping, omitted input/output prices default to zero, and omitted
cached-input price falls back to the input price. Supply all applicable prices rather than relying on those defaults.
Per-call arguments override matching `extra_body` fields. The underlying client defaults to a 600-second HTTP timeout and four retries; `llm.timeout` (seconds) is forwarded, `llm.max_retries` is not.

## Trainer and evaluation

| Field | Type / default | Meaning and constraints |
|---|---|---|
| `trainer.run_name` | string / null | Derived from method/template when null; explicit names use letters/digits, underscores, dots, hyphens, starting alphanumeric, at most 81 characters |
| `trainer.output_dir` | path / `runs` | Parent of the run directory and default adapter-tree directory |
| `trainer.tree_dir` | path / null | Overrides default `<output_dir>/trees/<template-id>` |
| `trainer.epochs` | integer / 1 | Positive number of training passes |
| `trainer.allow_early_stop` | boolean / false | Honor an Evolver's `stop` request |
| `trainer.resume_mode` | string / `auto` | Use `auto` for unfinished-run recovery or `disable` for a deliberate restart |
| `trainer.save_freq.steps` | integer / null | Checkpoint-tag frequency by step; null disables that frequency |
| `trainer.save_freq.epochs` | integer / 1 | Checkpoint-tag frequency by epoch; null disables that frequency |
| `trainer.budget.usd` | number / null | Dollar budget; reliable accounting requires known usage and prices |
| `trainer.budget.tokens` | number / null | Token budget |
| `trainer.budget.hours` | number / null | Training wall-time budget |
| `trainer.call_timeout_min` | number / 60 | Per-call timeout in minutes |
| `eval.repeats` | integer / 1 | Attempts per test task; use a positive value |

Use at least one finite budget for a real experiment. Limits are observed at call/rollout boundaries; concurrent
work can overshoot. Separate `check` probes and `eval` calls are outside the training budget.
Checkpoint frequency controls tags; completed steps still have records and accepted adapter history.

## Sandbox

| Field | Type / default | Meaning |
|---|---|---|
| `sandbox.enabled` | boolean / true | Sandbox learner programs launched through the framework |
| `sandbox.network` | string / `restricted` | `none`, `restricted`, or `open` |
| `sandbox.allow` | list / [] | Allowed endpoints for the restricted-mode proxy; the Evolver's own model endpoint is always among them |
| `sandbox.read` | list / [] | Additional readable filesystem paths |
| `sandbox.env` | list or null / null | Passed environment variables; null inherits the environment, [] requests none beyond required runtime settings; the Evolver's model key variable is always passed |

The backend is Linux/bubblewrap. The restricted proxy is not a mandatory network firewall. The sandbox does
not isolate user pipelines or arbitrary in-process Python Evolvers. Read
[the boundary description](../framework/configuration.md#sandbox-network-and-warnings) before granting resources.

## Complete defaults

```{literalinclude} ../../harnessadapter/defaults.yaml
:language: yaml
```
