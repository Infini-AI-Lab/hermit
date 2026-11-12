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
hermit train --config path/to/run.yaml trainer.run_name=trial-02 data.step_size=4
hermit eval --config path/to/run.yaml --adapter final trainer.run_name=trial-02 eval.repeats=3
```

Overrides are YAML values; quote shell-sensitive lists and dictionaries. Unknown framework keys are rejected. A key
of `run` or `eval` left empty (null), in the file or by an override, is its default, and so is one that an override
giving the whole section leaves out (`'eval={repeats: 3}'` sets `eval.infra_retries` and `eval.infra_wait_s` to their
defaults, not the file's, and an override after it may still name them); either section left empty is all its defaults.
`evolver.params`, `agent.params`, `pipeline.params`, `llm` and `backends` are free-form mappings at merge time; this is not a
guarantee that their consumer supports arbitrary fields.

## Data and task

| Field | Type / default | Meaning and constraints |
|---|---|---|
| `data.tasks` | path / null; required | JSONL with unique `id` or `task_id` values |
| `data.train` | path / null; required to train | One training task id per line; order files in `run.orders` replace it |
| `data.val` | path / null | Validation ids; required by methods declaring validation-data needs |
| `data.test` | path / null | Test ids; a nonempty test split is required by `eval`, but for `--split <file>` (that file's ids) and `--checkpoints` (each checkpoint's own `split`; this one for a checkpoint that names none) |
| `data.step_size` | integer / 8 | Positive number of task executions in a main training batch |
| `data.shuffle` | boolean / true | Shuffle training order (a multi-step Harbor task's rows as one, in step order); set false for ordered streams |
| `data.shuffle_seed` | integer / 0 | Training shuffle seed; does not seed every model or method |
| `task_info` | directory / null | Learner-facing task description; provide `README.md` |
| `agent.name` | string / null; required | Built-in `react`, `codex`, `claude-code`, `pi`, `mini-swe-agent`, `opencode`, `dsh`, `harbor` (a benchmark's task set in Harbor's format, see [Benchmarks](../framework/task.md#benchmarks)), an Agent class, or a `file.py:function` / `module:function` |
| `agent.concurrency` | integer / 4 | Maximum concurrent rollout/evaluation task executions; use a positive value |
| `agent.timeout_min` | number / null | A sandboxed task call (its worker) running longer is stopped and recorded as an error; null: no limit |
| `agent.params` | mapping / {} | Passed to Agent constructors; plain functions receive it when they declare a `params` argument |
| `agent.base` | string / null | Layer 2 in the per-layer form ([Agent layers](../framework/agent-layers.md#settings-by-layer)): `codex`, `claude`, `pi`, `mini-swe-agent`, `opencode`, `dsh`, `react`, or `direct` (with `agent.name`, a task's own loop); sets `agent.name` when unset |
| `agent.model` | string / mapping / null | Layer 1: a `<backend>/<model>` reference, or `{ref` or `name, login, effort, price, ...}`; read into `agent.params` |
| `agent.codex`, `agent.claude`, `agent.pi`, `agent.mini-swe-agent`, `agent.opencode`, `agent.dsh`, `agent.react`, `agent.direct` | mapping / null | Layer 2's settings for that Base Agent (the one `agent.base` names); read into `agent.params`. A key set in two layers is refused |
| `pipeline.run` | string / null | Legacy alias for `agent.name`, applied only when `agent.name` is unset |
| `pipeline.concurrency` | integer / null | Legacy concurrency override when the legacy pipeline alias is used |
| `pipeline.params` | mapping / {} | Legacy parameters merged into `agent.params` when the pipeline alias is used |
| `judge.path` | file/module / null; required | Module containing the scoring callable |
| `judge.name` | string / `score` | Callable accepting `(task, RunOutput)` |

Split ids must exist in the task data. Use disjoint train/validation/test splits; `check` reports overlaps.
Task row fields become training material, so private labels require judge-owned storage. `data.train`, `template`
and `evolver.name` are required by `hermit train` (and by `check`, but for a `run.mode: eval` config): `hermit eval`
and an eval-only config (`run.mode: eval`) need none of them, except that an adapter of the run's tree (`--adapter
final`, `root`, a tag or a commit) needs `template` or `trainer.tree_dir`, the tree being the template's (`root` on a
tree that does not exist yet needs `template`, whose adapter begins it); `none` and an adapter directory need neither.

## Adapter and learning method

| Field | Type / default | Meaning and constraints |
|---|---|---|
| `template` | string / null; required to train | Built-in id, `id@version`, or directory containing `.hermit/adapter.yaml` |
| `start_from` | string / null | Initial adapter tag, commit, or compatible directory; does not restore learner state |
| `evolver.name` | string / null; required to train | Built-in method or directory containing its Evolver definition |
| `evolver.resume` | boolean / false | Request learner-session continuation; method must support it |
| `evolver.params` | mapping / {} | Overrides merged with the chosen method's defaults |
| `evolver.base` | string / null | Layer 2 the method runs on: `codex`, `claude`, `pi`, `opencode` or `dsh` (an agent Evolver: sets `evolver.params.backend`) or `direct`. On a method that calls the run's model, `pi` makes that model Pi (`llm: {cli: pi, ...}`) |
| `evolver.model` | string / mapping / null | Layer 1, as `agent.model`; with `evolver.base: direct` it is the run's `llm` |
| `evolver.codex`, `evolver.claude`, `evolver.pi`, `evolver.opencode`, `evolver.dsh`, `evolver.direct` | mapping / null | Layer 2's settings, read into `evolver.params` (`evolver.pi` on a method that calls the run's model: into `llm`) |
| `evolver_access.environment` | list / [] | Granted environment resources; required for MWM's evidence integration |
| `evolver_access.tools` | list / [] | Task tool declarations offered to the learner |
| `evolver_access.send_material_out` | boolean / false | Consent declaration checked for methods requesting that need; not a network filter |

The template defines what is editable. Method selection does not change those rules. See
[adapter metadata reference](../framework/adapter-manifest.md) for exports, learnable files, and checkers.

## Models and backends

A model is named by a reference, `<backend>/<model id>`; a backend is an endpoint (base URL, key variable, the
dialects it serves, prices). See [Model backends](../guides/model-backends.md).

| Field | Type / default | Meaning and constraints |
|---|---|---|
| `model` | string / null | The run's default model, a `<backend>/<model id>` reference; used by `llm`, `agent.params.model` and the `codex`, `claude`, `dsh` and `mwm` Evolvers' `evolver.params.model` when they name none |
| `backends` | mapping / {} | Backends declared for this run by name, over `~/.config/hermit/backends.yaml` and the built-ins. Fields: `base_url`, `api_key_env`, `key_required`, `apis`, `messages_url`, `litellm`, `prices`, `extra_body`, `context_window`, `max_output_tokens`, `models`, or `from: <other>` to copy one |
| `llm` | reference, mapping / null | The run's chat-completions `llm` tool: a reference, `{model: <reference>, price, extra_body}`, the endpoint itself (below), or a CLI's sessions, `{cli: ..., ...}` (below) |

`agent.params.model` takes the same forms. For a Codex or Claude Code acting agent or Evolver a reference becomes
the CLI's provider settings (`args` for Codex, `env` for Claude Code, `provider` for Pi, OpenCode and DeepSeek Harness)
and the model's id; a mini-swe-agent acting agent calls the backend through litellm with the key passed per request. A
plain id without a backend goes to the CLI's own provider as before (for mini-swe-agent: a litellm model id, its key
from litellm's usual variable).

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

A model only a CLI reaches, as `llm` or as an acting pipeline's `agent.model`: each chat call is one session with no
tools, which on Claude Code or Pi can only reply ([Claude subscription](../guides/claude-subscription.md)). Any other key is
the Base Agent's setting (`hermit/agent/base/cli.py`).

| Field | Type / default | Meaning |
|---|---|---|
| `cli` | `codex`, `claude`, `pi` | The Base Agent. Pi takes a `<backend>/<model>` reference, `isolation: sandbox` and no `login` |
| `model` | string | The CLI's model name; required with `login: claude`. A `<backend>/<model id>` reference binds it to that backend's key instead |
| `login` | `chatgpt`, `claude` | The user's subscription on that CLI |
| `isolation` | string / `sandbox` | `sandbox`: a bubblewrap sandbox of its own (role `llm`, an acting pipeline's `agent-model`: its empty workspace and home, its model's or login's hosts through a proxy of its own, its key or token, nothing of the run); `container`: a Docker container of its own. With `sandbox.enabled: false` it runs unconfined |
| `container` | mapping / {} | With `isolation: container`: `image`, `network`, `proxy` (else `HERMIT_CODEX_*` or `HERMIT_CLAUDE_*`) |
| `web_search` | boolean | Required on Codex; on Claude Code false by default, and it cannot be on |
| `effort` | string / null | The default reasoning effort; a request's `reasoning_effort` overrides it |
| `timeout_s` | number / 3600 | One session's clock |
| `timeout` | number / 3600 | The chat client's; give it more than `timeout_s`. With a `login` the client also waits out the plan: `plan_max_wait_s` is added (none: no limit) |
| `unapplied` | list / [] | Request fields accepted without being applied (`temperature`); others the CLI has no setting for are refused |
| `plan_dir`, `plan_max_wait_s`, `plan_rerun`, `usage_credits` | see the guide | The login's plan. For a workspace session they are the Base Agent's settings (`agent.claude`, `evolver.claude`), not Layer 1's (`agent.model`) |

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
| `eval.repeats` | integer / 1 | Attempts per test row (a Harbor task indexed with k rollouts is k rows); use a positive value |
| `eval.infra_retries` | integer / 0 | `hermit eval`, and the tests `hermit train` runs itself (`run.mode: split`'s final test, the checkpoint tests), run the rows that ended `infra` (not the agent's result) again up to this many times, unless one reports a halt; never a multi-step task's step rows, nor a task Harbor runs for none of Hermit's agents (`unsupported_task`) |
| `eval.infra_wait_s` | number / 600 | Seconds before each such pass |
| `run.mode` | string / null | `eval` (`hermit eval` only), `split` (train, then the final adapter on `data.test`), `stream` (each row scored once, before it is learned from); null: train only ([run modes](../framework/configuration.md#how-the-run-is-done-run)) |
| `run.feedback` | string / null | `full` (null), `score` or `none`: what the learner is shown of each verdict; a stream must name it; `hermit train` refuses one below the Evolver's `min_feedback` (under its `evolver.params`, for an Evolver whose params decide it) |
| `run.orders` | list / null | Stream: seeds or order files, one run each (`<run_name>-o<i>`) |
| `run.checkpoints` | list / null | Stream: `{name, after: <task id> \| end, split, adapters: [checkpoint \| final \| root]}`; a task id YAML would read as a number goes in quotes (`after: '42'`) |
| `run.checkpoints_during` | boolean / false | A checkpoint's test right after the step that reaches it, not after training |
| `run.state`, `run.score` | string / null | `file.py:fn` hooks: a checkpoint test's environment, and the benchmark's own scores of it |

Use at least one finite budget for a real experiment. Limits are observed at call/rollout boundaries; concurrent
work can overshoot. Separate `check` probes and `eval` calls are outside the training budget, and so are the tests
`hermit train` runs itself (`run.mode: split`'s final test, the checkpoint tests, those during training included).
Checkpoint frequency controls tags; completed steps still have records and accepted adapter history.

## Sandboxes

| Field | Type / default | Meaning |
|---|---|---|
| `environment.runner` | string / `sandbox` | `sandbox`: the acting agent runs in the task sandbox; `host`: in the Trainer's process (a task whose code isolates what it runs) |
| `environment.read` | list / [] | Task paths shown read-only to the acting agent; never the data, the judge, its tests or the runs |
| `environment.write` | list / [] | Paths it may write besides its own work directory |
| `environment.packages` | list / [] | Python distributions shown besides the framework's environment (an editable install's package, one on `PYTHONPATH`), with their dependencies |
| `environment.env` | list / [] | Variables passed in: names or patterns (`MWM_*`) |
| `environment.network` | string / `no-network` | `no-network`, `allowlist` or `public`; the agent's model provider stays reachable |
| `environment.allowed_hosts` | list / [] | With `allowlist`: hosts reachable through the proxy |
| `environment.gpu` | list / [] | NVIDIA GPUs shown in the task sandbox, by the N of `/dev/nvidia<N>` (CUDA and EGL; inside they count from 0); none: no GPU (EGL renders in software where Mesa is installed). Not with `environment.image` ([sandbox guide](../guides/sandbox.md)) |
| `environment.resources` | mapping / {} | `memory_mb`, `file_mb`, `cpu_s` per process (positive whole numbers; null: no limit); not with `environment.image` (the container keeps its own) |
| `environment.image` | string / null | A Docker image: a Codex or Claude Code acting agent runs in a container of its own |
| `environment.container` | mapping / {} | With `image`: the container's `network` and `proxy` |
| `judge.mode` | string / `host` | `host`, `shared` or `separate` |
| `judge.tests` | list / [] | `shared`, `separate`: paths only the judge sees (its own file is shown; a file beside it is not unless listed here) |
| `judge.collect` | list / [] | `separate`: paths of the agent's work directory carried to the judge |
| `sandbox.enabled` | boolean / true | False runs everything unconfined |
| `sandbox.enforce` | string / `required` | `required` or `best_effort` (run unconfined without bubblewrap, recorded) |
| `sandbox.hide` | list / [] | Modules or packages of the framework's environment covered in every sandbox but the judge's |
| `sandbox.evolver.allowed_hosts` | list / [] | More hosts the learner reaches, through the proxy |
| `sandbox.evolver.env` | list / [] | Variables passed to the learner (names or patterns) |
| `sandbox.evolver.resources` | mapping / {} | `memory_mb`, `file_mb`, `cpu_s` per process (positive whole numbers; null: no limit) |

The backend is Linux/bubblewrap (Docker for `environment.image`). A sandbox holds its models' keys, as Harbor's agents
do; what the run keeps is scrubbed of them. An arbitrary in-process Python Evolver's own code runs in the Trainer; what
it starts runs in the learner's sandbox. Read [the boundary description](../framework/configuration.md#sandboxes) before
granting resources.

## Complete defaults

```{literalinclude} ../../hermit/defaults.yaml
:language: yaml
```
