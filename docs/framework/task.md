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

```{literalinclude} ../../hermit/common.py
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

```{literalinclude} ../../hermit/task/data.py
:language: python
:pyobject: JudgeResult
```

Inside the framework each row becomes a `Task` (`hermit/task/task.py`): its public half is the row, what the acting
agent is handed and what a run records as its input; its private half, a row's `_private` mapping when it has one,
only the judge reads. The run (`hermit/trainer/rollout.py`) gives the agent the public half and the judge the whole
task; the agent and the task set never import each other.

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
| `codex`, `claude-code`, `pi` | Codex, Claude Code or Pi run in a fresh workspace the adapter is deployed into (Pi: no `login`, no roles; its project's `.pi/` is trusted for the session) | `model`, `login`, `effort`, `prompt`, `files`, `mounts`, `prepare`, `answer`, `collect`, `context_query`, `project_doc_max_bytes`, `args`, `env`, `price`, `timeout_s`, `retries`, `retry_wait_s`, `workspaces` |
| `mini-swe-agent` | mini-swe-agent run in a fresh workspace the adapter is deployed into (no `login`, no roles; [tutorial](../tutorials/mini-swe-agent.md)) | `model`, `model_class`, `config`, `step_limit`, `effort`, `prompt`, `files`, `mounts`, `prepare`, `answer`, `collect`, `context_query`, `env`, `price`, `timeout_s`, `retries`, `retry_wait_s`, `workspaces` |
| `opencode` | OpenCode run in a fresh workspace the adapter is deployed into (no `login`, no roles; a model reference only; the deployment's skills only) | `model`, `effort`, `prompt`, `files`, `mounts`, `prepare`, `answer`, `collect`, `context_query`, `args`, `env`, `price`, `timeout_s`, `retries`, `retry_wait_s`, `workspaces` |
| `dsh` | DeepSeek Harness run in a fresh workspace the adapter is deployed into (no `login`, no roles; `permission_mode` sets its own sandbox: `read-only`, `workspace-write`, the default, or `danger-full-access`) | `model`, `provider`, `permission_mode`, `image_input`, `effort`, `prompt`, `files`, `mounts`, `prepare`, `answer`, `collect`, `context_query`, `args`, `env`, `price`, `timeout_s`, `retries`, `retry_wait_s`, `workspaces` |
| `answer` | one model call, the answer written where the task's verifier reads it ([Benchmarks](#benchmarks)) | `model`, `instruction_file`, `attachments`, `answer_file`, `answer_pattern`, `answer_format`, `max_tokens`, `wait_s`, `logs` |
| `hermes` | Hermes Agent run in a fresh workspace the adapter is deployed into (no `login`, no roles, no web search; a model reference only; `max_turns` caps its model requests a turn, Hermes's 90 by default) | `model`, `provider`, `max_turns`, `effort`, `prompt`, `files`, `mounts`, `prepare`, `answer`, `collect`, `context_query`, `args`, `env`, `price`, `timeout_s`, `retries`, `retry_wait_s`, `workspaces` |

### A coding-agent CLI as the acting agent

`codex`, `claude-code`, `pi`, `mini-swe-agent`, `opencode`, `dsh` and `hermes` act with a coding-agent CLI in a
workspace of Hermit's (or, for Codex and Claude Code, in a task's own Harbor environment: [Benchmarks](#benchmarks)):
a task gives what is its own, in configuration and, where its workspace is built in code, in functions it names
(`file.py:function`, or `package.module:function` for a method that brings its own session, as ASPIRE's
`hermit.agent.evolver.methods.aspire.actor`); the agent does the rest. OfficeQA's Codex runs are one such
configuration (`examples/officeqa/utils/task.py`):

```yaml
agent:
  name: codex
  params:
    model: fireworks/accounts/fireworks/models/glm-5p3-flash   # or the CLI's model name with `login: chatgpt` (Codex)
                                                               # or `login: claude` (Claude Code)
    effort: medium
    prompt: ../utils/prompt.md            # a template of the task's fields, or file.py:function (task) -> prompt
    prepare: ../utils/task.py:prepare     # (task, workspace[, params, base]) -> mounts: writes the task's files
    answer: ../utils/task.py:answer       # (last message, task) -> the output; or a pattern, the last match
    collect: ../utils/task.py:collect     # (task, workspace, log) -> facts: the task's own artifacts into the log
    project_doc_max_bytes: 65536          # Codex's cap on AGENTS.md; a deployment past it is refused
    timeout_s: 3600
    workspaces: ~/.cache/hermit/officeqa-codex     # outside any checkout: Codex reads a repository's instructions
environment:                              # the acting agent's sandbox: here a Docker container per session
  image: "officeqa-codex:0.162.0"
  container: {network: oq-sandbox, proxy: "http://oq-egress:3128"}
```

What the agent does for every task:

- **Deploy.** The adapter is deployed as `hermit deploy` deploys it ([Acting interface](adapter.md#acting-interface)):
  the whole adapter under `hermit/<adapter id>/` but its [`internal`](adapter-manifest.md#internal) files, each
  export connected to the CLI's own place, and the context naming the rest. Its tools are served by an MCP server
  on this machine; a container session, which cannot start one, gets them from the session's own tool server, which
  the container reaches, and so does an OpenCode session (OpenCode waits only 2 s for a server it starts) and a Hermes
  Agent session (Hermes reads MCP servers from its home's config only, and is given the deployment's skills folder). An
  adapter's [`context_tool`](adapter-manifest.md#per-task-context) is rendered for each
  task (the field `context_query`, else the task's question or prompt), deployed into the context and recorded in
  `RunOutput.extra["context_tool"]`; so is the context of any adapter whose template is not
  [`plugin-portable`](adapter-manifest.md#plugin-portable), which `hermit deploy` alone refuses. The session
  calls the adapter's tools without approvals and may start its sub-agents. An adapter with `memory/NOTES.md` (the
  continual-harness template) is told so in the prompt, and the notes the session leaves come back in the log
  (`log/memory/`). The context must fit `project_doc_max_bytes` (Codex reads no further): a longer one is refused. So
  is one too long for DeepSeek Harness, which gives the model at most 65,536 bytes of a project's instructions.
  Hermes Agent reads the context from `.hermes.md` and takes the prompt as one command-line argument, under 128 KiB
  (120,000 bytes): a longer prompt is refused, not cut. Hermes's prompt-injection scan replaces a context file it
  matches (benign rules can match) with a notice only the model sees: that session is an error, naming the file.
- **Isolate.** With `environment.image`, the CLI runs in a container of its own, started from a worker on the host
  that runs the adapter's code the deployment renders or serves in the strict sandbox: the workspace and the session's
  home read-write, the mounts read-only at their targets, an internal network whose only way out is an egress proxy to
  the provider's hosts, and the CLI's own sandbox inside for the commands it runs (Codex's workspace-write; Claude
  Code's, with no network and writes to the workspace and `/tmp` only). `docker/codex/` builds Codex's image and the
  proxy, `docker/claude/` Claude Code's image; `environment.container` (or `HERMIT_CODEX_NETWORK`, `HERMIT_CODEX_PROXY`,
  and `HERMIT_CLAUDE_NETWORK`, `HERMIT_CLAUDE_PROXY` for Claude Code) names the network and the proxy. The agent checks
  the image and the network before its first session (for Claude Code, also the CLI in the image and that its sandbox
  starts there). Without an image the agent runs in the task sandbox (bubblewrap), where a mount inside the workspace
  is a link for Codex; for Claude Code, DeepSeek Harness and Hermes Agent it is bound there by a nested bubblewrap, a
  real read-only directory as in a container (a search that walks the tree, `grep -r`, `find`, Claude Code's Glob and
  Grep, DeepSeek Harness's glob and grep or Hermes's `search_files`, does not follow a link), and linked with a warning
  where bubblewrap cannot nest. Hermes Agent runs in the task sandbox or with no isolation, not in a container.
  DeepSeek Harness also confines its commands and file tools in a sandbox of its own, inside the task sandbox
  (`permission_mode`). Its default, `workspace-write`, lets them write only the workspace and `/tmp`, so not
  `environment.write`. Where neither bubblewrap nor Landlock (the Linux kernel's own way of confining a process) can
  start there, it refuses every command and the session goes on. `danger-full-access` turns it off and leaves the task
  sandbox as the boundary.
- **Sign in.** A `<backend>/<model>` reference binds Codex to that backend's key; `login: chatgpt` uses the user's
  own ChatGPT login (`codex login`; `model` is then a Codex model name, the cost unknown), from one shared copy
  (`~/.cache/hermit/codex-login/`, outside every run's directory) that each session's refreshed token is written back
  to, the task sandbox's sessions included. On Claude Code `login: claude` signs in with the token of
  `claude setup-token` in `CLAUDE_CODE_OAUTH_TOKEN`, passed by name (nothing is copied), and nothing else may pay
  ([Claude subscription](../guides/claude-subscription.md)). In the task sandbox a reference or the CLI's own login is
  required: the CLI's own sign-in from the user's home is not copied in (a model that is no reference uses it only
  where the pipeline runs on the host). Each session has a home of its own: none of the user's Codex, Claude Code,
  DeepSeek Harness or Hermes Agent settings apply, and a Codex session's commands see only Codex's core variables
  (`shell_env: all` passes every variable).
  A DeepSeek Harness session never gets the caller's `DSH_*`, `DEEPSEEK_BASE_URL` or `DEEPSEEK_SEARCH_BASE_URL`,
  even through `environment.env`: give them in the agent's `env` setting.
  Nor does a Hermes Agent session get the caller's `HERMES_*`, `TERMINAL_*` or `TIRITH_*`, or another provider's key
  (`OPENROUTER_API_KEY`, ...): give them in `env`. Its auxiliary tasks go to its own model, and it has no fallback
  provider, no memory, no skill-creation nudges, no title request, no update check and no tirith download. Hermes
  itself still loads the `.env` beside its own source (a checkout installed from source) and a machine-wide managed
  `.env`, over what Hermit passed: keep keys out of those files for a Hermes that Hermit runs.
- **Web search.** `web_search` must be set, `true` or `false`: Codex's hosted search runs at the provider, past any
  sandbox or proxy, so it is never left to a default. Claude Code's `WebSearch` and `WebFetch` follow it too, and every
  Claude Code session is denied the tools whose work outlives it or reaches past it: `RemoteTrigger`, `CronCreate`,
  `CronDelete`, `CronList`, `ScheduleWakeup`, `ListAgents` and `SendMessage`
  ([Claude subscription](../guides/claude-subscription.md#execution-boundaries)).
  DeepSeek Harness's web search and fetch follow `web_search` too; its search is DeepSeek's own service, so unless the
  model is reached through DeepSeek's API on `DEEPSEEK_API_KEY`, `true` gives the session web fetch only. Its fetch
  runs in the session, not at the provider: in the task sandbox it reaches only what `environment.network` allows.
  Hermes Agent has none: `web_search` must be `false` (its search and extraction are third-party services that reach
  past the sandbox).
- **Stop and retry.** One session per run, stopped at `timeout_s`. A session the provider could not serve (429, 5xx,
  a container that did not start) runs again after `retry_wait_s`, up to `retries`; then the run is an error, not a
  wrong answer. On Claude Code, DeepSeek Harness and Hermes Agent any failed session is an error, with its tokens and
  log kept and no judge call, and on Claude Code a lost sign-in on a login ends the run, which `hermit train` resumes. A
  session past its clock is an attempt with no answer. Hermes Agent runs again only when Hermes classed its last failed
  request as a provider that could not serve (`rate_limit`, `upstream_rate_limit`, `overloaded`, `server_error`,
  `timeout`, or a 429 or 5xx); at `max_turns` it asks for a summary and exits 0, and that session is failed
  (`finish_reason` `length`), the summary its reply.
- **Record.** The run's directory holds `work/` (the workspace, kept for inspection without its links out),
  `home/` and `log/`. `RunOutput.log` is `log/`: the host's event stream (`agent-codex.jsonl`, `agent-claude.jsonl`,
  `agent-dsh.jsonl`, `agent-hermes.jsonl`), `prompt.txt`, `last_message.txt`, `deployment.json`, and what `collect`
  added. The output is what `answer` returned, with `fail_reason` when there is no answer; without `answer`, the last
  message. In a run, a model key's value in what the call leaves is scrubbed (`<VARIABLE>`; the login's token too, as
  `<CLAUDE_CODE_OAUTH_TOKEN>`).
  `RunOutput.extra` has the same keys for every task: `backend`, `model`, `effort`, `web_search`, `deploy`,
  `isolation`, `deployed`, `exit_code`, `timed_out`, `attempts`, `workspace`, `mounts` (each mount's target inside
  the workspace, `bound` or `linked`), and `context_tool` and `task` when there are any; `session_failed` says why a
  session failed, which makes the run an error record with no judge call.

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

## Task directories

`data.tasks` may name a directory of task directories in [Harbor's format](https://harborframework.com/docs/tasks)
instead of a `tasks.jsonl` (`hermit/task/directory.py`; Harbor is not needed to read them):

```text
tasks/<task>/
  instruction.md      what the agent is asked
  task.toml           [metadata]; [agent] timeout_sec; [verifier] timeout_sec, environment_mode;
                      [environment] network_mode, allowed_hosts, cpus, memory_mb
  environment/        what the task needs of its environment (a Dockerfile for a container)
  tests/test.sh       the verifier; a task without tests/ is judged by the run's judge (judge.mode: host)
```

Each task's row (its public half, what the agent is handed) holds the instruction, its settings and its metadata, and
`harbor_task`, the directory's name; where its directory and tests are is its private half, which only the judge
reads. `data.view: <file.py>:<function>` turns each row into the row the task set's agents and learners read instead
(a benchmark moved from a `tasks.jsonl` to task directories keeps its prompts and its learning material so); what a
task asks of its environment is checked either way. A benchmark's `prepare.py` writes each task with
`hermit.task.directory.write_task(<dir>, instruction, metadata={...}, environment={"network_mode": ...})`, whose
`[metadata]` reads back as given, every value in its order (TOML has no null: None is refused). Splits name the directories (`<task>`, `<task>:<step>` for a multi-step task's steps, `<task>#<k>` with
`data.rollouts: k`). The run's `environment` must give each task what it asks: a task whose agent needs the public
network (what a task that says nothing asks, Harbor's default) or more memory than `environment.resources.memory_mb`
is refused, never run with less; so is an allowlist task whose `allowed_hosts` the run's allowlist lacks, and, with
the script verifier below, a task whose verifier needs a wider network. A task's `[agent] timeout_sec` bounds each of
its calls (with a minute for the sandbox to start), unless `agent.timeout_min` is stricter.

A task's own verifier runs as Harbor runs it, in the judge's sandbox:

```yaml
judge:
  path: hermit.task.verify
  name: script
  mode: shared            # over the agent's work; separate: a fresh sandbox with judge.collect's paths
```

`tests/` is shown at `/tests` and a fresh log directory at `/logs/verifier`; `test.sh` runs in the agent's workspace
(`HERMIT_WORKSPACE`, from the output's `extra.workspace`) with the agent's output in the file `HERMIT_OUTPUT` names,
and writes `reward.txt` (one number) or `reward.json` (named numbers: `reward` is the score, else their mean). Beyond
Harbor, `feedback.txt` there is the feedback a learning method reads (else the script's output's tail). A task whose
task.toml asks for `environment_mode = "separate"` is refused under `mode: shared`. A step's own `[steps.verifier]`
settings come before the task's, its mode and environment resolved as Harbor resolves them, and its `tests/` are laid
over the task's (its `test.sh` first); a reward that is not a finite number is an error; `[verifier.env]` is set for the script, with `${NAME}` and `${NAME:-default}` taken
from the judge sandbox's environment (`environment.env`), as Harbor resolves them; the judge's deadline is at least the
verifier's `timeout_sec` and a minute. The workspace is the agent's own
(`HERMIT_WORKSPACE`, its output's `extra.workspace`, else the directory it ran in). Not supported here yet: files a task
places in the agent's workspace before it starts, and a multi-step task (its steps share one environment in order),
which is refused.

## Benchmarks

A benchmark enters Hermit as a task set in [Harbor](https://github.com/laude-institute/harbor)'s format (`task.toml`,
`instruction.md`, `environment/` with a Dockerfile or a prebuilt `docker_image`, `tests/test.sh` writing
`/logs/verifier/reward.txt`): what Terminal-Bench, SWE-bench and the datasets on the Harbor hub share (the design, with
diagrams: [How Harbor runs a task](../developer/harbor-design.md)). A task set that
needs the environments its tasks name (an image of its own each) runs in Harbor's environments: Harbor gives the
task's data, its environment and its verifier, and every agent is Hermit's. The run config says so in
`environment.harbor`:

```yaml
data:
  tasks: ../utils/tasks.jsonl            # the rows `python -m hermit.harbor.dataset prepare` writes (harbor_task)
  test: ../utils/rows/test.txt
agent:
  base: codex                            # any acting agent: Codex or Claude Code in the task's environment, the
  concurrency: 6                         # answer-only agent, a ReactAgent subclass whose tools use its environment
  model: {name: gpt-5.6-luna, login: chatgpt, effort: medium}
  codex: {web_search: false}
environment:
  harbor:
    benchmark: ../utils/benchmark.yaml   # the settings every config of the example shares (below)
    jobs_dir: ~/.cache/hermit/harbor/jobs/tb20-codex
judge:
  path: hermit.task.verify               # the verdict of Harbor's verifier in the task's environment
  name: environment
```

**A rollout.** The run (`hermit/trainer/rollout.py`) starts the task's own Harbor environment
(`hermit/environment/harbor.py`), hands the acting agent that environment with the task's public half
(`run(task, adapter, environment=env)`), runs Harbor's verifier on what the agent left there whenever the session's
end is the agent's result (a timeout, a CLI that exited with an error: Harbor verifies all of them; a session that is
infra, or an adapter that failed before any session, is read without it), and stops the environment. The
verifier's verdict is the output's `extra["verified"]`, which the judge reads: the reward is the score (the reward
under `reward_key`; none there is infra, as the runner read it), `passed` says whether it reached `pass_reward`, the feedback is the verifier's
`feedback.txt` (Hermit's, beyond Harbor) or its output's tail. A verifier that left no reward, or ran out of its time
on the agent's solution, scores 0, as Harbor grades it. A rollout that is not the agent's result (`infra`, read as
Hermit's Harbor runner read a trial: `hermit/environment/harbor_errors.py`, ported from its `errors.py`) is never the
agent's 0: an environment that did not start (its pull or build, Docker, the healthcheck: `sandbox_failed`; the driver:
`harbor_failed`, `harbor_timeout`), one that failed under the verifier, a verifier that left neither an exception nor a
reward, or ran no test (its tools never arrived, the host's disk full: `verifier_failed`), a session that never
started or that the provider could not serve (`session_failed`, `model_provider_failed`), a provider that refused the run's configuration before serving anything
(`model_rejected`, never run again; a prompt too long or the model's own refusal stays the agent's result), and a
provider that does not bill the account (its quota or credit spent: the run ends, as a lost sign-in does; so does a
session the account refused for its usage limit when the run holds no plan to wait for). An adapter that failed
before any session (its deployment, its per-task context, its context over Codex's cap: `adapter_failed`,
`context_over_limit`) is the agent's result, scored 0 with its message, as the runner graded it. It runs
again, the whole task
in a fresh environment, up to `infra_retries` times (default 2), each after `infra_wait_s` (300 s), unless its cause
runs no better again (a disk gate that waited its time out, `disk`; `model_rejected`); every run's usage is the
rollout's (`extra.attempts`). One that stays infra is marked `unscored` and the judge refuses to score it: the run
records a failed run (status `judge_error`, score 0, its usage and log kept), which a learner sees as one and `hermit
eval` counts among its `errors` and as 0 in its `mean_score`; `python -m hermit.harbor.report <eval dir>` gives pass@1
over the runs that are the agent's result ([Evaluation](../guides/evaluation.md)). Each eval row keeps the rollout's
`outcome` (`status`: ok, timeout, agent_error or infra; `failure_cause`, `timed_out`, `exit_code`, `unscored`,
`verifier_reward_raw`), as the runner's output kept it; a setting is overridden on the command line by its path
(`environment.harbor.infra_retries=0`). AlgoTune's judge (`examples/algotune/utils/judge.py`) reads the same verdict
and scores its speedup.

**The environment.** Harbor runs in its own venv (Python 3.13, `harbor` 0.25.0, `~/.cache/hermit/harbor-venv`), so each
environment is a driver process under Harbor's python (`hermit/environment/harbor_driver.py`) that speaks JSON lines
with Hermit. Harbor runs the whole trial with its own `Trial.run()`: Harbor's capability check (a task with MCP servers,
a skills folder or a Windows container is refused: `unsupported_task`, never run again), the prebuilt image pulled or
the Dockerfile built (with the build timeout and its multiplier), the healthcheck, the agent's setup (within its setup
timeout, under the baseline network policy), the agent phase (its network policy, its user, its timeout), the task's
tests uploaded only for the verifier, the verifier's environment (`[verifier] environment_mode`: shared, or a separate
one), its timeout and multiplier, the reward files, the environment's stop, the trial's `result.json` (under
`jobs_dir/<task>__<id>/`) and the scrub of credentials. The agent in that trial is a bridge with no logic of its own
(`HermitBridge`, a Harbor agent in the driver): its setup installs what the acting agent declares it needs
(`Agent.installs`: the CLI unless `bin` names one, python3 for an adapter's hooks) with Harbor's own installers, Debian
bullseye's archived sources repaired first; its run relays the agent's `exec`, `upload` and `download` to the
environment until Hermit's agent is done, or Harbor's agent timeout ends the phase, then, as the runner's agents did:
kills the process group of each command it started that still runs (each runs under `setsid`; a kill that fails is
infra), removes the credential files the agent registered (a sign-in; one not removed is infra), and copies the agent's
logs (a CLI's stream is also written to `/logs/agent` as it runs, and scrubbed there at the phase's end, best effort, in
a local Docker environment; elsewhere a shared verifier gets it as it came); what an ended command left running, a
server the task asked for, stays. Harbor then verifies. The agent's own budget ends 60 s before Harbor's agent timeout
(10% of a short one), so that its session's end and logs reach the trial first. Around it Hermit keeps its warm start
(`hermit/environment/harbor_docker.py`): no environment starts while the host's /tmp or Docker's data root has less than
its gate free (room kept for the task's image when it is not here yet, `image_sizes`, and for the images other
environments of the run are still to pull), the task's prebuilt image is removed after it as `docker_cleanup` says, and
a driver that had to be killed (or the process that ended without stopping it) has its trial's compose projects taken
down by name. Every environment Harbor supports is a `type` (docker by default; e2b, modal, daytona, ... with Harbor's
extra installed in its venv).

An environment's operations are the Environment API's ([Agent layers](agent-layers.md#environments)): `exec` runs a
command in the task's main container as the task's agent user, from its working directory (the workspace), each
variable passed by name (never on a command line of this machine), under a timeout enforced there (its process group
killed); `upload` and `download` move files (a relative path is in the workspace, an absolute one anywhere there);
`install(cli)` reports the version the setup installed (nothing installs during the phase). The task's directory and tests are the task's
private half: the agent never sees them. The agent has no worker in the task sandbox: every command and CLI of its
runs in the task's container, and its loop runs in the run's process with the environment (the environment, its
driver and Docker there too).

**The agents.** A CLI Base Agent (Codex, Claude Code) runs where the environment runs its commands (isolation
`remote`, `hermit/agent/base/cli.py`), as the Harbor runner's launch plan ran a session in a task's container: its home
is rendered here and uploaded to `/installed-agent/hermit-home`, the CLI is the environment's (`agent.<cli>.bin`, a
path there, such as a binary of this machine the environment mounts, `environment.harbor.environment.mounts`, used
when it runs there as `agent.<cli>.version` (any, when none is pinned) and the agent's user may execute it, linked onto
PATH with, for Codex, the release's `rg` and this machine's CA bundle for an image that has none; else Harbor's setup
installs it, `agent.<cli>.version`; nvm's and `~/.local/bin` are on PATH), the command runs
from the workspace with the prompt (Harbor's instruction, unless the agent's `prompt` says otherwise) on its stdin, its
output is the stream Hermit reads as it reads its own sessions (Codex's lost sign-in, provider failure, plan limit and
refused configuration read from its own notices, as the runner read them), and the CLI's last message comes back. Its
clock is the task's agent budget unless the agent's `timeout_s` says less. The model's key reaches the exec by name;
Claude Code's subscription token (`login: claude`) is moved into a pipe there before anything that outlives the move
starts (no process there holds it in its environment), and no variable of the agent's reaches this machine's Docker
client; on a login the run takes its admission to the plan (the wait and one of its session slots) around the whole
of the task's environment, never inside the agent's time, and a session the plan refused runs the task again once the
plan allows (not counted). Codex's own sandbox is off there (the environment is the
boundary, as Harbor's own agents run) unless `codex_sandbox` says otherwise, Claude Code runs as the environment's root
may, and the CLI's web search is off where the task's agent phase has no public network. Every sign-in file the home
took is removed from the environment before the verifier runs (a ChatGPT sign-in's refresh, and the plan's usage
reports, come back first); a removal that fails fails the call. A session is not resumed there. The
acting agent deploys the adapter as `hermit deploy` lays it out for a session there (its hooks run from `hermit/<id>/`
relative to the workspace with `python3`, installed at the setup when missing; a context file the image already has kept after
Hermit's text, the whole within Codex's cap, 262144 bytes there as the runner's unless `project_doc_max_bytes` says)
and uploads it into the workspace; the adapter's notes and the hooks' audit
and state (`hook-calls.jsonl`, `hook-state/`) come back into the log. The answer-only agent (`agent.name: answer`,
`hermit/agent/acting/answer.py`) makes one model call (the adapter's context its system message, Harbor's instruction
the question) and writes the answer where the verifier reads it (`answer_file`, `/app/answer.txt` unless null;
`answer_pattern`, `answer_format`, `instruction_file`, `attachments`: the environment's images sent with the question).
The agent's logs reach the verifier as `/logs/agent` (the stream, the answer agent's `answer.json`) in Harbor's shared
verifier mode only: a separate verifier gets its own mounts and the task's configured artifacts, not the agent logs.

Not supported in a Harbor environment yet, and refused rather than dropped: the adapter's tools (the framework's tool
server runs on this machine), the agent's `mounts`, `prepare` and `collect`, readable mounts, hidden paths or a
network limit of Hermit's (the task's task.toml says what its environment reaches), and a multi-step task
(`[[steps]]`, which needs one environment across the Trainer's steps). The credentials an agent command was given are
scrubbed from the logs Hermit hands the verifier (and, in a local Docker environment, from a CLI's stream there) and
from the trial's files after the trial.

| `environment.harbor` | Meaning |
|---|---|
| `benchmark` | a YAML file holding these same keys, shared by every config of the example (the config's override it; its relative paths are its folder's; its `dataset:` block is `dataset prepare`'s) |
| `tasks_dir` | the task directories (a row's `harbor_task` names one; a directory task's private half names its own) |
| `environment` | Harbor's environment config: `type`, `import_path`, `kwargs`, `delete`, `force_build`, `mounts`, `extra_docker_compose`, `env`, `override_cpus`, `override_memory_mb`, ... |
| `verifier`, `verifier_timeout_multiplier`, `environment_build_timeout_multiplier` | Harbor's verifier config, and its multipliers (at least 1) |
| `agent_timeout_sec`, `setup_timeout_sec` | the agent's budget (Harbor's `override_timeout_sec`; null: each task's own), the agent's setup's, its CLI's install (Harbor's `override_setup_timeout_sec`; default 1200 s, not Harbor's 360 s) |
| `allowed_hosts` | hosts a task whose agent phase has no public network lets the agent reach (Harbor's `extra_allowed_hosts`); the run adds its agent's model hosts |
| `docker_cleanup`, `rmi_threshold_gb`, `disk_gate_gb`, `disk_gate_wait_s`, `tmp_gate_gb`, `image_sizes` | the warm start: `images_keep`, `rmi_task_image` or `rmi_over_gb`; the gates; the images' compressed sizes |
| `jobs_dir`, `harbor` | where each environment's trial goes; Harbor's command, beside which (or beside the file a link to it names) is the python the driver runs under |
| `start_timeout_s`, `verify_timeout_s` | how long Hermit waits for the driver's start and its verifier |
| `busy_retries`, `busy_wait_s` | how many times a session the provider could not serve runs the task again in a fresh environment, over all of its runs (default 3, not counted in `infra_retries`), the n-th after n x `busy_wait_s` (120 s) |
| `infra_retries`, `infra_wait_s` | how many times a rollout that is not the agent's result runs again in a fresh environment (default 2), each after how long (300 s); never one whose cause runs no better again (`disk`, `model_rejected`) |
| `verifier_timeout`, `pass_reward`, `reward_key` | a verifier that ran out of its time: `agent` (scored 0) or `infra`; the reward that passes (1.0; null: a scale); the reward's key (a verifier that left no reward under it is infra) |
| `verifier_redact` | regexes removed from the verifier's output the feedback and an infra verdict quote (a test's expected answer the learner must not read) |

A task row (`tasks.jsonl`, written by `python -m hermit.harbor.dataset index <tasks dir> --out tasks.jsonl
[--rollouts k]`) names its directory as `harbor_task`, so a benchmark's split files apply with one rollout a task;
`k` rollouts of a task are `k` rows (`<task>#<k>`). A directory of task directories as `data.tasks` works too.

**Packaging a benchmark.** `python -m hermit.harbor.dataset prepare utils/benchmark.yaml` makes an example's data from
the `dataset:` block of its benchmark file: the registry's tasks (`ref: <name>@<version>`, downloaded under
`~/.cache/hermit/harbor-tasks` or `$HERMIT_HARBOR_TASKS`, patched when the block names a patch, reused when already
there from the same source), and beside the file `tasks` (a link to them: keep it out of git), `tasks.jsonl`, the
splits (task counts, or `in_sample`), smoke rows and, with `image_sizes: true`, the images' sizes, checked against the
lock beside the file (`hermit/harbor/dataset.py` has the block's keys). `dataset lock` records the sha256 of every task
directory and `dataset check` compares; `dataset splits` writes deterministic train/val/test lists by task. A benchmark
of your own is written as task directories by your `utils/benchmark/prepare.py` (`write_task`; a verifier of its own
goes into `tests/`, uploaded after the agent ran, so the answer is never where the agent can read it; see
`examples/officeqa/utils/harbor/build_tasks.py`). A task set of task directories that Hermit runs in its own sandbox or
on the host, with a judge on the host (`judge.mode: host`, no `tests/`), is what the examples whose agent owns its loop
use (`examples/robosuite/`, `libero/`, `clbench_*`, `servelearnbench/`, `agentdyn/`, ...; [Task directories](#task-directories)).
A task set is checked without a model by running a command of a known answer in each task's environment and verifying
it (`tests/core/test_harbor_environment.py` does this against Docker).

## What the task owner provides

| Input | Purpose | Used by |
|---|---|---|
| `tasks.jsonl`, or task directories | Task dictionaries with unique IDs ([Task directories](#task-directories): one each, read through `data.view`). | Pipeline and scoring function. |
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
