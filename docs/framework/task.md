# Task and acting pipeline

A task integration connects the framework to the problems and acting system you want to improve. You supply data,
a description, a pipeline, and feedback. Read this page to bring your own agent or benchmark.

## Responsibility and boundaries

The pipeline owns the acting model or agents, their execution loop, environment access, and runtime state.
It decides how to deploy the adapter. The Trainer chooses tasks and collects results; the Evolver consumes those
results to improve the adapter.

```{raw} html
:file: ../_includes/task-contract.html
```

This boundary allows a single agent, a group of agents, or a custom workflow to use the same training interface.

## Interface

The current implementation calls two functions:

```python
run(task: dict, adapter: Path | None) -> RunOutput
score(task: dict, output: RunOutput) -> JudgeResult
```

`run` receives one task and an adapter directory. It must make the required exports available to the acting system
before running the task. `adapter=None` means a run without adapter exports, used for baseline comparison.
The pipeline should preserve its ordinary behavior in either case.

`score` evaluates the returned output. It can instead return a dictionary containing `score` and `feedback`, or a
numeric score. Additional dictionary fields are collected under the normalized result's `extra` field.

Scoring is part of collecting learning material. Although the general architecture can use other feedback sources,
the current `train` and `eval` commands require a configured scoring callable.

### Execution result

Only `output` is required when constructing `RunOutput`:

```{literalinclude} ../../hermit/task.py
:language: python
:pyobject: RunOutput
```

| Field | Meaning for the integration |
|---|---|
| `output` | The value the scoring function receives. Choose a JSON-serializable task-specific representation. |
| `log` | Optional file or directory containing the original execution log. The Trainer copies it into learning material. |
| `messages` | Optional normalized trajectory for methods that use model messages. |
| `cost_usd`, `tokens`, `wall_s` | Usage and timing. Unknown cost/tokens should remain null; zero means known zero. |
| `api_speed`, `extra` | Optional measurements and task-specific metadata. |

Concurrent tasks may run against the same adapter path. Treat that adapter as input and keep execution state and
logs outside it. Give each execution its own working paths unless sharing is part of the task design. Keep returned
log paths available until the Trainer has collected them.

### Feedback result

```{literalinclude} ../../hermit/task.py
:language: python
:pyobject: JudgeResult
```

The scoring function decides what feedback to reveal. Reference answers placed in feedback become visible to the
learner. The numeric score can be task-specific; the current `eval` report counts a task as solved at least once
when any repeated score is at least `1.0`.

## Built-in agents

A task whose acting agent is a tool-calling model, or a coding-agent CLI, names a built-in agent as its pipeline
and writes no loop:

```yaml
agent:
  name: react
  params:
    model: fireworks/accounts/fireworks/models/glm-5p3-flash   # <backend>/<model id>; or {base_url, model, api_key_env, price}
    prompt: utils/agent/prompt.md      # system prompt; `{context}` marks where ADAPTER.md goes (else appended)
    tools: utils/tools.py              # the task's tools: functions marked with @hermit.agent.acting.tool
    user: "{question}"                 # the first user message from the task's fields
    max_turns: 24
    answer_tag: FINAL_ANSWER           # optional: one more call without tools when the tag is missing
```

| `agent.name` | Agent | Settings |
|---|---|---|
| `react` | a chat-completions tool-calling loop over the task's tools and the adapter's tools | `model`, `prompt`, `tools`, `user`, `max_turns`, `max_output_tokens`, `max_tokens`, `temperature`, `answer_tag`, `tool_result_chars`, `logs` |
| `codex`, `claude-code` | Codex or Claude Code run in a fresh workspace the adapter is deployed into | `model`, `login`, `effort`, `prompt`, `files`, `mounts`, `prepare`, `answer`, `collect`, `context_query`, `isolation`, `container`, `project_doc_max_bytes`, `args`, `env`, `price`, `timeout_s`, `retries`, `retry_wait_s`, `workspaces` |

### A coding-agent CLI as the acting agent

`codex` and `claude-code` are the framework's one way to act with a coding-agent CLI: a task gives what is its own,
in configuration and, where its workspace is built in code, in functions it names (`file.py:function`); the agent
does the rest. OfficeQA's Codex runs are one such configuration (`examples/officeqa/utils/task.py`):

```yaml
agent:
  name: codex
  params:
    model: fireworks/accounts/fireworks/models/glm-5p3-flash   # or a Codex model name with `login: chatgpt`
    effort: medium
    prompt: ../utils/prompt.md            # a template of the task's fields, or file.py:function (task) -> prompt
    prepare: ../utils/task.py:prepare     # (task, workspace[, params, base]) -> mounts: writes the task's files
    answer: ../utils/task.py:answer       # (last message, task) -> the output; or a pattern, the last match
    collect: ../utils/task.py:collect     # (task, workspace, log) -> facts: the task's own artifacts into the log
    project_doc_max_bytes: 65536          # Codex's cap on AGENTS.md; a deployment past it is refused
    isolation: container                  # none | container: a Docker container per session
    container: {image: "officeqa-codex:0.162.0", network: oq-sandbox, proxy: "http://oq-egress:3128"}
    timeout_s: 3600
    workspaces: ~/.cache/hermit/officeqa-codex     # outside any checkout: Codex reads a repository's instructions
```

What the agent does for every task:

- **Deploy.** The adapter is deployed as `hermit deploy` deploys it ([Acting interface](adapter.md#acting-interface)):
  the whole adapter under `hermit/<adapter id>/` but its [`internal`](adapter-manifest.md#internal) files, each
  export connected to the CLI's own place, and the context naming the rest. Its tools are served by an MCP server
  on this machine; a container session, which cannot start one, gets them from the session's own tool server, which
  the container reaches. An adapter's [`context_tool`](adapter-manifest.md#per-task-context) is rendered for each
  task (the field `context_query`, else the task's question or prompt), deployed into the context and recorded in
  `RunOutput.extra["context_tool"]`; so is the context of any adapter whose template is not
  [`plugin-portable`](adapter-manifest.md#plugin-portable), which `hermit deploy` alone refuses. The session
  calls the adapter's tools without approvals and may start its sub-agents. An adapter with `memory/NOTES.md` (the
  continual-harness template) is told so in the prompt, and the notes the session leaves come back in the log
  (`log/memory/`). The context must fit `project_doc_max_bytes` (Codex reads no further): a longer one is refused.
- **Isolate.** With `isolation: container`, Codex runs in a container of its own: the workspace and the session's home
  read-write, the mounts read-only at their targets, an internal network whose only way out is an egress proxy to
  the provider's hosts, and Codex's workspace-write sandbox inside. `docker/codex/` builds the image and the proxy;
  `container` (or `HERMIT_CODEX_IMAGE`, `HERMIT_CODEX_NETWORK`, `HERMIT_CODEX_PROXY`) names them. The agent checks the
  image and the network before its first session. Without a container a mount is a link.
- **Sign in.** A `<backend>/<model>` reference binds Codex to that backend's key; `login: chatgpt` uses the user's
  own ChatGPT login (`codex login`; `model` is then a Codex model name, the cost unknown), from one shared copy
  (`~/.cache/hermit/codex-login/`, outside every run's directory) that each session's refreshed token is written back
  to, the rollout sandbox's sessions included; a model that is no reference uses the user's own Codex sign-in as it
  is. Each session has a home of its own: none of the user's Codex or Claude Code settings apply, and the session's
  commands see only Codex's core variables (`shell_env: all` passes every variable).
- **Web search.** `web_search` must be set, `true` or `false`: Codex's hosted search runs at the provider, past any
  sandbox or proxy, so it is never left to a default. Claude Code's `WebSearch` and `WebFetch` follow it too.
- **Stop and retry.** One session per run, stopped at `timeout_s`. A session the provider could not serve (429, 5xx,
  a container that did not start) runs again after `retry_wait_s`, up to `retries`; then the run is an error, not a
  wrong answer.
- **Record.** The run's directory holds `work/` (the workspace, kept for inspection without its links out),
  `home/` and `log/`. `RunOutput.log` is `log/`: the host's event stream unchanged (`agent-codex.jsonl`,
  `agent-claude.jsonl`), `prompt.txt`, `last_message.txt`, `deployment.json`, and what `collect` added. The output
  is what `answer` returned, with `fail_reason` when there is no answer; without `answer`, the last message.
  `RunOutput.extra` has the same keys for every task: `backend`, `model`, `effort`, `deploy`, `isolation`,
  `deployed`, `exit_code`, `timed_out`, `attempts`, `workspace`, and `context_tool` and `task` when there are any.

The agent records; the Evolver interprets. Every Evolver reads a host's stream through `hermit.agent.base.trajectory`
(`read(record, step_dir)` gives the run's steps: messages, reasoning, commands with their exit codes and output,
file changes, tool calls, errors) and renders what its learner needs: SkillOpt rebuilds its Codex trace summary,
GEPA a text of the steps. A hook adds to the record, never replaces it, so every Evolver sees the same run.

Every agent has the same interface, the task's own included:

```python
from hermit.agent.acting import Agent

class MyAgent(Agent):                     # agent.name: agent/agent.py:MyAgent
    def run(self, task, adapter):         # self.params, self.base (the config's directory), self.session(adapter)
        ...
        return RunOutput(output=...)
```

A plain `run(task, adapter)` function is accepted too (`agent.name: agent/pipeline.py:run`); it receives
`params` and `base` when its signature names them. A task tool is a plain function:

```python
from hermit.agent.acting import tool

@tool("Read lines of one bulletin file.", {"type": "object", "properties": {
      "filename": {"type": "string"}, "start_line": {"type": "integer"}}, "required": ["filename"]})
def read(filename: str, start_line: int = 1) -> dict:
    ...
```

A function that declares `_task` receives the task row. The result is JSON; an exception becomes an error
message the model reads. The adapter's own tools are offered beside these and answered through
[`Session`](adapter.md#custom-acting-pipelines). A benchmark that owns its agent loop is an `Agent` subclass
that runs that loop with `Session` plugged in; `examples/clbench_poker/utils/agent/agent.py` is one. For an adapter
whose manifest names a [`context_tool`](adapter.md#per-task-context), such a loop reads the context with
`session.context(query=<the instance's input>, task=<the row>)` and records `session.last_context` in
`RunOutput.extra["context_tool"]`, as `react` does, so the Evolver knows what the agent read.

## Harbor benchmarks

A benchmark whose agent works in a container (a coding or terminal benchmark, a corpus the agent searches with shell
tools) is run through [Harbor](https://github.com/laude-institute/harbor): its task format (`task.toml`,
`instruction.md`, `environment/` with a Dockerfile or a prebuilt `docker_image`, `tests/test.sh` writing
`/logs/verifier/reward.txt`) is what Terminal-Bench, SWE-bench and the datasets on the Harbor hub share, and the
built-in `harbor` agent makes one rollout one Harbor trial:

```yaml
agent:
  name: harbor
  concurrency: 8
  params:
    tasks_dir: ../utils/tasks            # the task directories (python -m hermit.harbor.dataset downloads or indexes)
    model: gpt-5.6-luna                  # the acting model; a `<backend>/<model>` reference binds an endpoint and key
    agent_timeout_sec: 3600              # a uniform agent budget; null: each task's own
    jobs_dir: ~/.cache/hermit/harbor/jobs
judge:
  path: hermit.harbor.judge              # the reward the container's verifier wrote
sandbox:
  profiles: {rollout: {enabled: false}}  # the rollout drives Docker through Harbor: unconfined, as every such agent
```

Harbor starts the task's container, installs the acting agent in it (Codex by default: `harborside.codex:HermitCodex`,
Harbor's Codex with the adapter deployed into the container as `hermit deploy` lays it out, the AGENTS.md context,
skills, roles and hooks, with the whole adapter copied to `hermit/<adapter id>/` in the task's workdir), runs the
instruction and then the task's own verifier; the trial's reward is the rollout's output and the judge's score. A
task row (`tasks.jsonl`, written by `python -m hermit.harbor.dataset index <tasks dir> --out tasks.jsonl
[--rollouts k]`) names its directory as `harbor_task` and is the task's name, so a benchmark's existing split files
apply; `k` rollouts of a task are `k` rows (`<task>#<k>`). Harbor
lives in its own venv (`~/.cache/hermit/harbor-venv`, `agent.params.harbor` names its command): Hermit's side writes
one job config per rollout and runs `harbor run` as a subprocess, and the container-side half under
`hermit/harbor/harborside/` imports harbor and the standard library only.

| `agent.params` | Meaning |
|---|---|
| `tasks_dir`, `jobs_dir`, `tmpdir`, `harbor` | the task directories; one Harbor job per rollout (outside the checkout); TMPDIR for harbor; Harbor's command |
| `model`, `effort`, `codex_version`, `codex_login` | the acting model (a name, or a backend reference: its endpoint and key variable go to Harbor); Codex's reasoning effort, npm pin, and whether the ChatGPT login (`~/.codex/auth.json`) is uploaded when no endpoint is bound |
| `acting_agent` | `{import_path}` or `{name}` (Harbor's built-ins: `codex`, `mini-swe-agent`, ...; they take no adapter), `kwargs`, `env`, `pythonpath` (directories of an example's own Harbor agents) |
| `agent_timeout_sec`, `environment`, `verifier`, `retry` | the agent's budget; Harbor's environment (`type`, `import_path`, `delete`, `force_build`, `mounts`, `kwargs`), verifier and retry configs, passed through |
| `docker_cleanup`, `rmi_threshold_gb`, `disk_gate_gb`, `disk_gate_wait_s` | `images_keep`, `rmi_task_image` or `rmi_over_gb` (the task's prebuilt image removed after the trial, only when larger than the threshold); no trial starts while Docker's data root has less than the gate free |
| `benchmark` | a YAML file holding these same keys, shared by every method's config of the example (the config's params override it) |
| `stream` | a multi-step task's driver: `idle_s` (the environment stops after that long without a step), `start_timeout_s`, `turn_wait_s` |

The rollout's output is `{harbor_task, rollout, step, reward, rewards, status, failure_cause, exception_type, trial,
hooks_active}`, with `status` one of `ok` (a verifier reward), `timeout` (Harbor's `AgentTimeoutError`; the verifier
may still have scored), `infra` (a sandbox, model-provider, verifier or Harbor failure, a killed `harbor run`, the
disk gate, a lost stream) or `agent_error`; `tokens` is known for an `ok` trial only. The log is a curated copy of
the trial (`result.json`, the agent's outputs and ATIF `trajectory.json`, the verifier's output and reward; never the
agent's session transcripts). A usage-limit or authentication failure writes `<jobs_dir>/HALT`: every rollout returns
at once as `infra`/`halted` while it exists.

**Streams.** A task with `[[steps]]` in its task.toml (`steps/<name>/instruction.md`, `steps/<name>/tests/`) is one
environment that lives through every step: a benchmark whose next question depends on what the earlier ones left
in the environment. `dataset index` writes one row per step (`<task>:<step>`, with `step`, `step_index` and
`steps`), so the Trainer sees the stream as consecutive tasks and the learner acts between them; the `harbor` agent
starts a driver with the first step (`harborside/driver.py`, Harbor's `MultiStepTrial` fed one step at a time over a
socket in the stream's directory under `jobs_dir/streams/`), asks it for each step in order (a step whose turn has
not come waits for the earlier ones, running in other rollouts of the batch), and stops it after the last step, an
aborted step, or `stream.idle_s` without a request. With a frozen adapter, `harbor run` on the same task evaluates
the whole stream in one trial. Put the stream's rows in file order with `data.shuffle: false`. A stream's driver
must outlive the rollout that started it, so streams need `sandbox.profiles.rollout.enabled: false` (as every agent
that drives Docker does today).

**Packaging a benchmark.** `python -m hermit.harbor.dataset download terminal-bench@2.0 --output <dir>` fetches a
registry dataset; a benchmark of your own is written as task directories by your `utils/prepare.py` (the judge goes
into `tests/`, uploaded after the agent ran, so the answer is never where the agent can read it; see
`examples/officeqa/utils/harbor/build_tasks.py`). `dataset lock` records the sha256 of every task directory and
`dataset check` compares; `dataset splits` writes deterministic train/val/test lists by task. The task set is
checked without a model by running it with the scripted agent (`acting_agent: {import_path:
harborside.scripted:ScriptedAgent, kwargs: {script: "echo 42 > /app/answer.txt"}}`): a script that produces the
right answer must score 1 and a wrong one 0 (`tests/test_harbor_smoke.py` does this against Docker).

Limits: an adapter whose context comes from a `context_tool` is refused, as the CLI agents refuse it (its per-task
context would never reach the agent in the container); the adapter's tools are not served in the container (no
Hermit there; the config's `mcp_servers` are dropped with a log line); `hermit eval` on a Harbor dataset still
needs the training-only config keys.

## What the task owner provides

| Input | Purpose | Used by |
|---|---|---|
| `tasks.jsonl` | Task dictionaries with unique IDs. | Pipeline and scoring function. |
| Split files | One task ID per line for train, optional validation, and test. | Trainer and evaluation. |
| `task-info/` | Description of the task, objective, feedback, and execution. | Evolver. |
| Pipeline callable | Adapter integration and acting execution. | Training, evaluation, and candidate experiments. |
| Scoring callable | Score and feedback for a result. | The same execution paths. |
| Optional resources | Environment paths or task tools granted to the learner. | Evolver. |
| Run configuration | Connect these inputs with a template and Evolver. | Framework commands. |

A task dictionary needs a unique `id` (`task_id` is also accepted); its other fields belong to your integration.
Split IDs must refer to existing tasks. `check` detects overlapping splits. A missing test split permits training,
but `eval` needs test tasks.

### task-info

Provide a `README.md` as the learner's entry point. Explain what an input means, what a correct result looks like,
the optimization objective, and how to interpret feedback. Larger tasks can split this into `task.md`, `goal.md`,
`judge.md`, and `pipeline.md`; these are organizational conventions.

Written task rules inform the learner. They are not automatically enforced by the Trainer's specification check.
Requirements that must be enforced need an appropriate task judge or template check.

## Interaction with other components

The Trainer calls the pipeline on each training task, passes its result to the scoring function, and writes the
combined record as [learning material](trainer.md#learning-material). The same functions serve test evaluation and
candidate runs requested by the Evolver. They should not rely on one particular caller or call order.

The adapter reaches the acting system through its [exports](adapter.md#acting-interface). A context-only integration
may read text into its prompt. A tool or service adapter needs the corresponding host integration and resources.
`check` does not prove that a host supports every export.

The task environment and learner access are separate. A pipeline may use resources that the Evolver cannot inspect.
`evolver_access.environment` grants the learner additional paths; `evolver_access.tools` declares capabilities.
Neither setting configures the acting pipeline for you.

These choices keep task execution reusable when the template or learning method changes, and keep the pipeline's
original execution available as the `adapter=None` baseline.

## Minimal example

This complete offline stub loads adapter context and echoes the task input. Its messages demonstrate the exchange;
it does not call a model. Replace the echo with your acting system when integrating a real task.

```{literalinclude} ../_examples/interface_task/pipeline.py
:language: python
```

Its scoring function compares the output with the expected string:

```{literalinclude} ../_examples/interface_task/judge.py
:language: python
```

The example config binds these functions as:

```yaml
pipeline:
  name: pipeline.py:run
judge:
  path: judge.py
  name: score
```

Paths are relative to the config file. `agent.name` includes the class or function name; `judge.path` names the file
or module and `judge.name` names the function (default `score`). `agent.params` is the agent's to read.

Run the [offline example](trainer.md#minimal-example) to exercise these interfaces. For a real model and document
environment, see [OfficeQA](../recipes/officeqa.md).
