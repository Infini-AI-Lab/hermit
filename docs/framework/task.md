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
| `hermes` | Hermes Agent run in a fresh workspace the adapter is deployed into (no `login`, no roles, no web search; a model reference only; `max_turns` caps its model requests a turn, Hermes's 90 by default) | `model`, `provider`, `max_turns`, `effort`, `prompt`, `files`, `mounts`, `prepare`, `answer`, `collect`, `context_query`, `args`, `env`, `price`, `timeout_s`, `retries`, `retry_wait_s`, `workspaces` |

### A coding-agent CLI as the acting agent

`codex`, `claude-code`, `pi`, `mini-swe-agent`, `opencode`, `dsh` and `hermes` act with a coding-agent CLI in a
workspace of Hermit's (the other way is `agent.name: harbor` with `acting_agent.cli`: the CLI in a Harbor task's
container, [Benchmarks](#benchmarks)): a task gives what is its own, in configuration and, where its workspace is
built in code, in functions it names (`file.py:function`); the agent does the rest. OfficeQA's Codex runs are one such
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
task asks of its environment is checked either way. Splits name the directories (`<task>`, `<task>:<step>` for a multi-step task's steps, `<task>#<k>` with
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

A benchmark enters Hermit as a task set in [Harbor](https://github.com/laude-institute/harbor)'s format: that is the
one protocol for every task, whether its agent works inside the container (a coding or terminal benchmark, a corpus
searched with shell tools) or only answers (a question stream whose state the environment keeps), instead of Docker
code or a loop of the benchmark's own. The format (`task.toml`,
`instruction.md`, `environment/` with a Dockerfile or a prebuilt `docker_image`, `tests/test.sh` writing
`/logs/verifier/reward.txt`) is what Terminal-Bench, SWE-bench and the datasets on the Harbor hub share, and the
built-in `harbor` agent makes one rollout one Harbor trial:

```yaml
agent:
  name: harbor
  concurrency: 8
  params:
    benchmark: ../utils/benchmark.yaml   # the keys every method's config of the example shares (the config overrides)
    tasks_dir: tasks                     # the task directories (python -m hermit.harbor.dataset prepare writes them),
                                         # relative to the benchmark file's folder once `benchmark` is set
    model: fireworks/accounts/fireworks/models/glm-5p3   # a <backend>/<model> reference: its endpoint and key
    acting_agent: {cli: codex}           # the CLI in the task's container: codex | claude | pi | opencode | dsh |
                                         # mini-swe-agent; or answer: one model call from Harbor's process
    web_search: false                    # required, either way (but for cli: answer)
    agent_timeout_sec: 3600              # a uniform agent budget; null: each task's own
judge:
  path: hermit.harbor.judge              # the reward the container's verifier wrote
```

The pipeline of a Harbor task set runs on the host: the task's container is its sandbox, and the verifier runs there
after the agent (Harbor's `[verifier] environment_mode`: shared, or a separate container).

**A trial.** Harbor starts the task's container and the acting agent runs in it, as one HermitXXX class per CLI
(`hermit.harbor.clis`: `harborside.codex:HermitCodex`, `harborside.claude:HermitClaude`, `harborside.pi:HermitPi`,
`harborside.opencode:HermitOpenCode`, `harborside.dsh:HermitDsh`, `harborside.mini_swe_agent:HermitMiniSweAgent`):
Harbor's install of the CLI (Codex's, on an image with glibc, under the agent user's nvm, which the launch loads as it
does for Pi and dsh), or a binary of this machine mounted read-only (`<cli>_bin`, first on PATH, ahead of nvm's, used
only when it answers `--version` (asked as root) as the asked-for version and the task's agent user may execute it,
else the install), and one executor for all of them. What the session is, the CLI's home, its command line and its
environment, Hermit's side renders with the CLI Base Agent its own sessions use (`hermit.harbor.launch`: the trial's
launch plan, `<job>/launch/plan.json`; Pi's and dsh's provider settings take the backend's `developer_role` from the
backends registry, the built-ins and the user's backends file, when it has that backend at the trial's URL, whatever
the run config's `backends:` sets for it, and `keep` otherwise, where Hermit's own sessions take the run config's
`backends:` over the registry: `hermit/harbor/FRAMEWORK_SUPPORT.md`, candidate 14), against the adapter deployed by
the CLI's host exactly as `hermit deploy` lays
it out (`hermit.harbor.staging`: the context file, skills, roles and hooks, the whole adapter in `hermit/<adapter id>/`
of the task's workdir; its record, `.hermit-deploy.json`, names the paths of this machine it was staged at, as the
host writes it). harborside uploads the deployment and the home and runs the command as the task's agent user,
the prompt (Harbor's instruction) on its stdin and the session's stream in `/logs/agent/agent-<backend>.jsonl`; a
session still running when Harbor's timeout stops the agent is killed with its process group, so that the CLI edits
nothing under the verifier (a command it ran in a session of its own, as Claude Code runs its shell tool's, is outside
that group and goes on unless the CLI ended it within the plan's `kill_grace_s`). That kill and the steps after the
session (the adapter's notes copied out, the collected files, the files returned to this machine) run from `/`, so a
session that removed its workdir is still stopped and what it left still comes back; the notes and the collected files
go each into a new folder of root's in `/logs/agent` (which every user can write in), whatever was there removed first,
so that no link the agent's user put there is followed; a step that fails is recorded in the trial's `result.json` (`after_session_errors` in
the agent's metadata), never over the session's own outcome; a copy of the notes or of a collected file that failed (a
full disk, a read error) is removed, never kept in part, which Hermit's side would take for the whole (without it the
reply is the stream's, and the adapter's notes stay as they were); and a file to return whose check failed is such a
failure, never taken for one the session did not leave. After the trial the runner reads that stream with the same
Base Agent (the reply, whether the session was served, refused by the plan or not served by the provider, the usage at
`agent.params.price` when one is given, else at the backend's price): a trial's session is read as Hermit reads its
own.

The adapter's tools are served from this machine for the trial (`hermit.harbor.tools`: a ToolServer in the runner's
process, on the Docker bridge's gateway, which a task container reaches, in front of a Session of the adapter in the
strict adapter sandbox, where the adapter's code runs) and named to the CLI as an MCP server, in its home (Claude
Code's on its command line; mini-swe-agent's shell calls them as the `hermit-tool` command), the host's deployment
naming no server of its own (its `prepare(serve_tools=False)`, Pi's and dsh's too); each call is logged
(`tool-calls.jsonl`). An adapter whose context is per task (a `context_tool`, or one only Hermit runs) gets the task's
context, computed before the trial in the adapter sandbox (`hermit.harbor.context`); the notes an adapter keeps
(`hermit/<id>/memory/`) come back after the session (`memory/` in the log). A key or a token reaches the container by
its variable's name only (`docker compose exec -e NAME`, Claude Code's subscription token on a descriptor): never in a
command line, a file, the plan or a log. The task's container is the session's sandbox, though: its commands run as
the CLI's user there, and one that read the CLI's memory could still find the key or the token (`harborside.claude`).
`acting_agent.settings.env` passes a variable given whole (`$NAME`) by its name; another `$NAME` inside a value is
this machine's value, written into the plan (`$PATH` is this machine's PATH), and is refused when its name may be a
secret's (holding `KEY`, `TOKEN`, `$MAX_OUTPUT_TOKENS` too, `SECRET`, `PASSWORD`, `CREDENTIAL` or `AUTH`, or with a
word `PASSWD`), as is a value given as it is to a variable named as a secret, a word of its name (between
underscores) being one of them (`DB_PASSWD: hunter2`, `NPM_AUTH: ...`, `HF_TOKEN_READ: ...`; not `MAX_OUTPUT_TOKENS`
or `GIT_AUTHOR_NAME`). The rules go by names: they refuse a setting that only looks like a secret's (`TOKEN_LIMIT:
4096`, `AUTH_MODE`, `SSH_KEY_PATH`: pass it by name, `NAME: $NAME`, which only Harbor's local Docker environment
takes), and let through a secret whose name says none of them (`PGPASSWORD`, `MYSQL_PWD`, `GITHUB_PAT`), whose value
is then written into each trial's plan: keep secrets out of the settings' values.
The container gets no other variable of this machine's: one that main's Base Agents drop from a
session's environment (`dropped_env`, this process's variables) reaches it only when the settings name it, as in
main, and on `login: claude` an `env` that names what would pay instead (`ANTHROPIC_API_KEY`) is refused, as in
Hermit's own sessions. Codex's sign-in on `login:
chatgpt` (`auth.json`) is a file of the CLI's home in the container (mode 600, the agent user's), as in Hermit's own
Codex sessions, so the session's commands can read it; once the session has ended it is copied back and removed from
the container, before a verifier that shares the container (Harbor's shared mode) runs. One whose copy-back failed
(over 64 KiB, an exec that failed) or was cut short (Harbor's second cancellation) stays until the trial's container
is removed, and with `environment.kwargs.keep_containers: true` in the stopped container Harbor keeps. The host's copy
for the trial is removed after the trial. With `login: chatgpt` (Codex) or `login: claude` (Claude Code) a trial
runs on the user's subscription and shares its plan with Hermit's other sessions on it (hermit.agent.base.login): it
waits while the plan holds sessions back, with no bound (main's `plan_max_wait_s` is no setting of the runner's: the
window's reset, a PAUSE file's removal or the run's end ends the wait), its session notes the plan's usage (Codex's as
Claude Code's), so `MAX_USED`
holds trials back as it holds Hermit's own sessions, a session the plan refused runs again once its window allows (on
Codex the refusal is read from Codex's own notices, never from what its log quotes of the session's tool calls), one
the provider could not serve runs again after a pause (`busy_retries` times over all of the trial's runs:
`infra_retries` adds none to it), and none starts or runs again once
another trial ended the run while it waited; the trial's usage is every run's (the tokens they reported, unknown only
when none did, and their model calls; the cost unknown when one run's is; each run's own in `extra.attempts` when the
trial ran more than once, which a training record keeps in its `pipeline_extra` and `hermit eval`'s rows do not; a
trial a lost sign-in ended records nothing, the usage of its earlier runs included: `LoginFailed` ends the run).
Codex's refreshed sign-in comes back from the container (a regular file of at most 64 KiB when checked: a larger one
is not downloaded, and one a process grew after the check is refused when it is taken back), even when Harbor's
timeout lands while it is being copied out (that step, and no other after-step, runs once more), and becomes the
shared copy as a session's refresh does (a later refresh of
the copy's account) when it holds a refresh and an access token, stamped at most a minute ahead of this machine's
clock in Harbor's local Docker environment, whose containers share it, an hour in another (`hermit.harbor.signin`); a
stream's step gets the shared copy at its turn, never when its rollout began. A copy comes back only when its trial
ends: trials that run at once on one copy whose token is due for its refresh race for the copy's one refresh token,
and a loser's refresh refused as already used is a lost sign-in, which ends the run, though the winner's copy is good
(it becomes the shared copy as the winner's trial ends, and `hermit train`'s resume then runs on it; `hermit eval`
stops). The answer-only agent (`cli: answer`,
`harborside.answer:HermitAnswer`) makes one model call from Harbor's process, through an endpoint the runner serves on
this machine (the adapter's context as its system text), and writes the answer into the container, for a task whose
work is a reply (LAB-Bench's questions); a request still in flight when the trial ends (Harbor's timeout stopped the
agent) is given up then, before the trial's usage is read, and leaves the trial's cost unknown. A model a CLI reaches
on a login (`model: {cli, model, login}`, each request one session on the login, in a sandbox of its own: main's
`cli_sandbox`, strict unless `sandbox_enforce: best_effort`, which `HA_ADAPTER_SANDBOX_ENFORCE`, the adapter sandbox's
variable, does not loosen) is given in
the benchmark file, whose model the runner serves itself: the trial waits for that plan before it starts (and does not
start once another trial ended the run meanwhile), its relay waits for it before each request, and a trial whose one
request was still unanswered when the agent's time ran out, held back by that plan at any time (a hold that lifted
before the time ran out too: the time the plan took was not the agent's), is infra (`usage_limit`; the run goes on),
not the agent's timeout; a session the plan refused comes back at once (the relay runs its sessions with `plan_rerun:
false`), its trial infra (`usage_limit`), run again once the plan allows (not counted in `infra_retries`, as often as
it refuses, each time after the plan's hold: an hour when nothing says when its window resets), never
waiting for the window inside the agent's time; a Codex session of the relay whose sign-in was lost ends the run, as a
trial's does. The same model in the run config's own `agent.params.model`, which the pipeline serves, is refused at
startup: its session would wait for the plan inside the request, which the runner cannot tell from the model's own
time. The runner's waits do not cover one for the plan's session slots (`SLOTS`): a request that finds every slot
taken (`SLOTS` below `agent.concurrency`, or the login's other sessions in them) waits for one inside the agent's
time, and a trial whose time ran out there is the agent's timeout, scored 0, so keep `agent.concurrency` at most the
plan's `SLOTS` less the login's other sessions. A task whose agent phase has no public network (task.toml, as Harbor
reads it) gets the model's and the tools' hosts named to Harbor (`extra_allowed_hosts`) and the CLI's web search off.
`hermit check` and `hermit train` warn of an acting agent on `login: claude` whose pipeline runs on the host, which a
Harbor task set's does, when the run config gives the login (one the benchmark file alone gives is not seen); the
unconfined sessions the warning names are a local pipeline's: a Harbor trial's session runs in the task's container,
its token on a descriptor the CLI reads once, not in the CLI's environment (`harborside.claude`).

A task row (`tasks.jsonl`, written by
`python -m hermit.harbor.dataset index <tasks dir> --out tasks.jsonl [--rollouts k]`) names its directory as
`harbor_task` and is the task's name, so a benchmark's existing split files apply with one rollout a task; `k`
rollouts of a task are `k` rows (`<task>#<k>`), which a split file then names one by one. Harbor lives in its own venv
(`~/.cache/hermit/harbor-venv`, `agent.params.harbor` names its command): Hermit's side writes one job config per
rollout and runs `harbor run` as a subprocess, killed with its process group past the trial's wait budget, and the
trial's compose projects then taken down by name (from an empty directory, `/` when none can be made, with none of
this process's `COMPOSE_*` variables: no compose file or `.env` is read, whose volumes would go too), and the half
that runs in Harbor's process, `hermit/harbor/harborside/`, imports only harbor, two of Harbor's own dependencies
(pydantic, and certifi where it is installed: Codex's agent's CA bundle of last resort) and the standard library.
Startup refuses what every trial would refuse: a login for another CLI, a login and a binding at once, no
`web_search`, a model on this machine's loopback, a sign-in or a key that is not there, Harbor's retry on `login:
chatgpt`, a plan the CLI's Base Agent cannot render (mini-swe-agent's: web search, or a model class whose replies
Hermit does not read), mini-swe-agent without its package where the adapter is deployed (the `mini-swe-agent` extra,
in `agent.params.python`, else Hermit's own interpreter) and no bubblewrap, or one that cannot start the strict
adapter sandbox (no user namespaces), while the adapter sandbox is required (`sandbox_enforce`), both for any run,
`hermit eval --adapter none`'s too, which the runner cannot tell from one that deploys adapters; the answer agent's
`attachments` with a model a CLI reaches (a session takes text only), and such a model in the run config (above); a
benchmark file whose `model` is a `<backend>/<model>` reference or another mapping (below); `trial_cache` (there is no
trial cache: a resumed run runs its trials again); a `harbor` that is no executable file and, for a task set with a
multi-step task, one with no interpreter beside it (or beside the file a link to it names) for the stream's driver.
`hermit check` builds the runner (`agent.name: harbor` runs on the host), so it reports the first of these refusals
before a run; its probe then runs one real trial (the model's cost, the plan's usage) unless `--no-probe`. An acting
agent of Harbor's own (`acting_agent: {name}`, or a class that is no HermitXXX) takes no adapter: a rollout given one
fails before anything is staged, so `hermit eval --adapter none` is the run it takes.

| `agent.params` | Meaning |
|---|---|
| `tasks_dir`, `jobs_dir`, `tmpdir`, `harbor` | the task directories; one Harbor job per rollout (outside the checkout); TMPDIR for harbor; Harbor's command, a name on PATH or a path (a relative one is the working directory's; default: `~/.cache/hermit/harbor-venv/bin/harbor`), beside which, or beside the file a link to it names, is the interpreter a stream's driver runs under |
| `model`, `login`, `effort`, `web_search`, `price` | the acting model: a `<backend>/<model>` reference (its endpoint and key), or with `login` (`chatgpt`, `claude`) the CLI's model name on the user's subscription; the answer agent's may be a model a CLI reaches on its login (`{cli: codex \| claude, model, login}`, in the benchmark file, which the runner serves as written: each request one session of that CLI, which takes text only, each trial first waiting for that login's plan, the mapping's `plan_dir` and `usage_credits`; unlike `llm`'s, no `<backend>/<model>` reference is bound there, so one without `login`, Pi's among them, is refused at startup), and its relay takes the client `timeout` and `max_retries` a model mapping gives beside a reference; the reasoning effort (`off`, YAML's false, gives none, as in main's sessions; the answer agent's relay sends it with each request, so an effort its model does not take is each trial's `model_rejected`, not refused at startup); the CLI's web search, required (not for the answer agent); a reference's price (USD per million tokens: `input`, `cached_input`, `output`; the config's, else the benchmark file's; default: the backend's), which prices the HermitXXX trials' usage (on Anthropic's API Claude Code's own cost is kept, as everywhere in Hermit; `hermit train` and `hermit check` check a reference's dialect as a chat client's, main's rule for an agent it does not know, so they refuse Claude Code on a backend that serves only the Messages API, Anthropic's, which `hermit eval` runs, and do not check Codex's need of the Responses API: `hermit/harbor/FRAMEWORK_SUPPORT.md`, candidate 7) and the answer agent's endpoint (one of Harbor's own agents keeps Harbor's cost) |
| `plan_dir`, `usage_credits`, `busy_retries`, `busy_wait_s`, `login_root` | on a login, as a CLI session's: the plan's directory (default: the login's own under `~/.cache/hermit/`); whether a session may draw the account's usage credits (default: no, and one that drew them holds the plan's sessions until its window resets); how many times a session the provider could not serve runs again over all of the trial's runs (3, also when null; `infra_retries` adds none), the n-th after n × `busy_wait_s` (120 s, also when null); the shared copy of the ChatGPT sign-in (`~/.cache/hermit/codex-login/`) |
| `acting_agent` | `{cli: codex}` (the default) or another CLI of the table (not `hermes`: Hermes Agent runs in Hermit's own workspaces only, and the runner refuses it at startup), with `settings` (the CLI Base Agent's own, over the table's; not `login`: a trial's login is `agent.params.login`, and one there is refused at startup), `kwargs` (the HermitXXX class's), `env` (no secrets: its values are written into each trial's job and onto `docker compose exec`'s command line, so a variable Harbor would take for a secret (a name holding `KEY`, `SECRET`, `TOKEN` but not as `TOKENS`, `PASSWORD`, `CREDENTIAL` or `AUTH`: `GIT_AUTHOR_NAME` and `TOKENIZERS_PARALLELISM` too; or with a word `PASSWD`, `DB_PASSWD_FILE` too), or a value naming one, whole as `${NAME}` or `${NAME:-default}`, which Harbor fills in, or otherwise (`$NAME`, or inside a longer value), which it hands on as that text, is refused at startup), `pythonpath`; or `{name}` / `{import_path}`: a Harbor built-in or another class, run as Harbor runs it, which takes no adapter (Harbor's own `claude-code`: its WebSearch and WebFetch off on every task with `web_search: false`, else Harbor's rule, off where the task's agent network is not public; its sign-in the key this process has, never the subscription, so `CLAUDE_FORCE_OAUTH`, or the subscription's token as its only sign-in of a model at Anthropic's API, is refused at startup: the subscription runs with `cli: claude` and `login: claude`) |
| `<cli>_bin`, `<cli>_version`, `setup_timeout_sec` | a CLI binary of this machine (`codex_bin`, `claude_bin`, `pi_bin`, `opencode_bin`, `dsh_bin`; mini-swe-agent takes none), its folder mounted read-only at `/opt/hermit-<cli>`, first on PATH (for Codex's standalone release, a `codex-package.json` in the binary's grandparent, the release's folder, with its `bin/` first on PATH), used only when it answers `--version` (asked as root) as the asked-for version (any, when none is) and the task's agent user may execute it (`test -x`: a binary or folder of mode 700 of another uid is not, unless that user is root), else the container installs the CLI; the version the container installs, the asked-for one (`codex_version`, ..., `mini_swe_agent_version`; default: the `hermit.harbor.clis` table's; its `codex` and `claude` rows give none, so Harbor's own install's); Harbor's agent setup limit |
| `answer` | the answer agent's `instruction_file`, `attachments` (globs of the images sent with the question: one over 20 MiB is left out, as a file that is no image is, and a figure a wildcard would match in a directory the agent user may not list is not found, as by a glob that matches nothing (no error: the model is asked without it; one named outright is found wherever the agent user may enter its directory); attachments whose listing exec failed, and an image that cannot be measured or read, are `launch_failed`: the model is never asked without them), `answer_file`, `answer_pattern`, `answer_format`, `wait_s`, `max_tokens` |
| `pass_reward`, `verifier_redact`, `reward_key` | the reward that is a pass (default 1.0; null: a score scale, where none is); regexes removed from the curated verifier output (a test's expected answer); the verifier's reward key |
| `agent_timeout_sec`, `environment`, `verifier`, `retry`, `verifier_timeout` | the agent's budget; Harbor's environment (`type`, `import_path`, `delete`, `force_build`, `mounts`, `extra_docker_compose`, `kwargs`), verifier and retry configs, passed through (with Harbor's retry on, `retry.max_retries` above its default 0, a failed attempt's usage is not counted: Harbor removes its trial before the next; refused on `login: chatgpt`, whose every attempt would start from the sign-in copied before the first); a verifier that ran out of its time: `agent` (the default, as Harbor grades it: the agent's result, its solution too slow for the tests, as AlgoTune's verifier times it, or a process it left that hung them) or `infra` (not the agent's result, run again, `infra_retries`: a host whose load makes verifiers time out); any other value is refused at startup |
| `verifier_timeout_multiplier`, `environment_build_timeout_multiplier` | Harbor's: each task's verifier limit, and its environment start (pull, build) limit, times this (default 1, never below 1: a slow host); the agent's budget is never scaled |
| `docker_cleanup`, `rmi_threshold_gb`, `disk_gate_gb`, `disk_gate_wait_s`, `tmp_gate_gb`, `image_sizes` | `images_keep`, `rmi_task_image` or `rmi_over_gb` (the task's prebuilt image removed after the trial, only when larger than the threshold); no trial, nor a stream's environment, starts while Docker's data root (or the host's /tmp) has less than the gate free, room kept for the task's image and for the images still to be pulled |
| `infra_retries`, `infra_wait_s` | a trial that is not the agent's result (`infra`, below) runs again this many times at most (default 2), each after this long (default 300 s): never a stream's step, whose environment went on without it, nor a task Harbor runs for none of Hermit's agents (`unsupported_task`), a step whose turn cannot come (`stream_order`), a disk gate that waited its time out, a provider's refusal of the run's configuration (`model_rejected`) or a host failure (`host_failed`: a jobs directory that cannot be made, or past the trial's staging, where its session may have run); the plan of the answer agent's model that refused its request runs the trial again once it allows, not counted; on a login, `busy_retries` counts a provider that could not serve; there is no trial cache (`trial_cache` is refused): a resumed `hermit train` runs its unfinished step's trials again, and an eval run again runs all of its own |
| `tools_host`, `sandbox_enforce`, `context_query`, `python` | where the adapter's tools are served (default: the Docker bridge's gateway); the adapter sandbox's enforce (`required`, the default unless `HA_ADAPTER_SANDBOX_ENFORCE`, the adapter sandbox's own variable, says otherwise, needs bubblewrap: without it every run of Hermit's agents, a table CLI or the answer agent, is refused at startup, `hermit eval --adapter none`'s too, which the runner cannot tell from one that deploys adapters; Harbor's own agents take no adapter and skip the check; `best_effort` renders adapters unconfined, recorded); the row's field a per-task context retrieves for; the interpreter the hosts' deployment and the per-task context run with (default: Hermit's own) |
| `benchmark` | a YAML file holding these same keys, shared by every method's config of the example (the config's params override it, and a relative path that is the config's folder's (below) is then the benchmark file's folder's, the config's own too; the sign-in it gives (`login`, `login_root`, `claude_bin`, each where the config does not give it) is checked at startup as the config's is, its `login_root` and `claude_bin` resolving against the working directory, as the config's do; its `dataset:` block is `dataset prepare`'s); its `model`, which counts only where the config gives none, is a model's name (the CLI's own with a login, or one of Harbor's own agents') or the answer agent's model a CLI reaches (`{cli: ...}`): a `<backend>/<model>` reference or another model mapping is refused there, as it belongs in the config's own `agent.params.model`, which the config's loading binds and `hermit check` sees |
| `stream` | a multi-step task's driver: `idle_s` (the environment stops after that long without a step; 4 h, also when null), `start_timeout_s` (1 h, also when null), `turn_wait_s` (how long a step waits for its turn; when null, the budgets of the steps before it, each its own, and `start_timeout_s`) |

A relative `tasks_dir`, `image_sizes`, `acting_agent.pythonpath`, `acting_agent.settings.config` (mini-swe-agent's)
or `environment.extra_docker_compose` is the config's
folder's, or, with `benchmark` set, the benchmark file's folder's (the config's own values too); `<cli>_bin`,
`jobs_dir`, `tmpdir`, `login_root`, `plan_dir` and a `harbor` that is a path resolve against the working directory,
with `~` expanded. The runner follows `tasks_dir`'s links once (the `tasks` link prepare writes), so a prepare that
points it at another set mid-run changes no trial's task set.

The rollout's output is
`{harbor_task, rollout, step, reward, rewards, status, failure_cause, exception_type, trial, hooks_active}` and the
CLI's last message (`reply`; the answer agent's `answer`), with `status` one of `ok` (a verifier reward), `timeout`
(the agent's time ran out, and the verifier still scored its work: even after a provider notice in its stream that the
session recovered from, and for the answer agent with its model request still in flight, unless that request's session
had itself waited for its login's plan, as the session records its waits, however the plan stands when the trial is
read: `infra`, `usage_limit`; or the verifier's own time ran out on the agent's solution, `solution_timed_out`, unless
`verifier_timeout: infra`), `agent_error` (the CLI exited with an error, its work still scored; the model's safeguards
ended the session; the adapter's context over the CLI's limit beside the image's own; the adapter's own deployment or
per-task context failing in its child process on this machine, `adapter_failed`, its message the feedback, no
container started, unless this process then sees this machine fail any adapter's staging there (a job folder it
cannot write, less than 64 MB free under it, the strict adapter sandbox not starting: `staging_failed`, infra, which
an adapter's own staging that filled the disk reads as too, the host being checked after it; a
child killed or out of its time stays the adapter's), and a trial's launch plan refused for what its adapter is (its
tools beside `text_only`; dsh's MCP server name, made of the adapter's id: 32 characters at most of
`[A-Za-z0-9_-]`), which the plan rendered at startup, with no adapter, does not meet (a manifest this process cannot
load, hermit.adapter.loader's ValueError, is a failed run of the pipeline instead, as on `main`); a verifier that left
no reward Harbor could read, no reward file, an empty one or one that does not parse, `verifier_failed`, scored 0 as
Harbor grades it) or `infra`: not the agent's result (a sandbox, model-provider or Harbor failure, a killed `harbor
run`, the disk gate, a lost stream, an OSError on this machine while it stages the adapter (its deployment, its
per-task context) or writes the trial's job and launch, `staging_failed` (the adapter's own failing in its child
process is `adapter_failed`, above); an OSError on this machine past those steps (a process it could not start, a file
it could not write or read), `host_failed`, its cost unknown (0 for a jobs directory it could not make: nothing ran),
though a lost sign-in the rollout met before it still ends the run; a verifier whose tools never arrived or that the
host's full disk stopped before its tests wrote a report (with one, the words are a test's output), whatever it then
wrote or raised and whether the agent had finished, run out of its time or exited with an error (Harbor verifies all
three), `verifier_failed`; a trial whose environment or agent setup failed before its agent started, which costs 0; a
session that never started or whose tools never came up; for a CLI of the table and the answer agent, a task Harbor
runs for none of them, one that declares MCP servers or a skills folder or targets a Windows container,
`unsupported_task`, never handed to Harbor (Harbor's own agents run it); a Claude Code, Codex, Pi or OpenCode session,
or the answer agent's request, that the provider refused before serving it for the run's configuration,
`model_rejected`, the run going on and the trial never run again (Codex's by the error its turn failed with: a 400, a
402 or a 404 it does not retry, a provider's error body it gives as it is (a 400 it reads as an invalid request:
`{"error": ...}`, `{"detail": ...}`), its words for an API account's quota, "Quota exceeded. Check your plan and
billing details.", or its words for a plan that refuses every session until someone acts: a workspace's credits spent
or its spend cap reached, a member's limit its owner set, a ChatGPT plan without Codex; a 401 or a 403 there is a
lost sign-in): its key, its permission, its model (not found, not existing, unknown, invalid or not supported), its
account's bill (a 401, a 402, a 403 or a 404 where the failure writes it as a status, never a
number among its words, or the CLI's words for one) or a request setting it does not take (an unsupported parameter or
value, an unrecognized request argument, an unsupported operation: `max_tokens` where the model takes
`max_completion_tokens`, an effort it has not); the answer agent's by its endpoint's status, or by a 400 whose message
says one of these (the last 1,000 characters of the error's `message`, all that Hermit's endpoint says of a failed CLI
session, of an error that is itself a string, else of the body's own `message` or `detail`) or whose code says that
the runner's relay refused what a CLI's session cannot take or start with; of these, a refusal of the account's bill
(a 402 written as a status, payment required, a credit balance too low, an insufficient quota, balance, credits or
funds, Codex's quota and plan words above, billing) is `usage_limit` instead and ends the run, as an API account's
usage limit does: nothing later can be served, and so does a 429 for an API account's spent quota (OpenAI's
`insufficient_quota`, the error's code or type, or a CLI's words for it), which no pause ends, though a 429 is
otherwise the provider's (`model_provider_failed`); harborside's own setup failing: no launch plan (or a stream
step's plan the agent cannot take), the CLI's install (a package, an npm or GitHub download, the mounted binary's
probe), the workdir or its owner, the run directory, the upload of the deployment, the home, the bundle or the launch
script, the `hermit-tool` script's install, Hermit's mini-swe-agent runner that cannot run in the container, a
workdir it cannot enter, the image's own context file that cannot be read or captured whole (its copy failed
part-way), a secret the plan passes by name that is not in Harbor's environment (or an environment other than
Harbor's local `docker` one, which cannot pass it by name), a launch that exited non-zero and left no stream (the CLI
never started, as when Docker could not run its exec; the stream an earlier step of a stream left is removed before
the launch), a launch whose exec this machine could not run (an OSError: the compose client not spawned, EMFILE or
EAGAIN), or, after its non-zero exit, whose stream it could not look for (whether the CLI started cannot be told),
the answer agent's question (its instruction file not there in time) or figure that cannot be read in the container,
its attachments whose listing exec failed, or its answer that could not be kept in `answer.json` (an OSError on this
machine) or written to `answer_file` (its exec failed, or this machine could not run it: an OSError),
`launch_failed`; a plan's usage limit, a lost sign-in). On Codex a refused sign-in or key (a 401, an invalid key, not
logged in, no Codex credentials found, a refused proxy token, the ChatGPT workspace routing's 401, a refresh of the
ChatGPT sign-in the provider refused, a 403 its turn failed with; only since the session last worked, never a refusal
of a side request it worked after; never one in a notice of an error Codex recovers from and goes on: that it is
reconnecting, or falling back from WebSockets to HTTPS; nor one in what Codex's log quotes of the session's work, the
model's own text: its records of the session's tool calls, as of a rejected command or a patch whose expected lines
it did not find, and every record's further lines, a line of a JSON object among them, unless it is in Codex's own
form, an event of one of Codex's types or a log record of one of Codex's targets (`codex_core::...`), which is read
as Codex's: a session's work that prints one, rarely, can read as a lost sign-in or a refusal) is a lost sign-in, as
a refused login is on Claude Code; a refusal of the run's configuration or bill in its last failure is
`model_rejected` (above), the plan's usage limit is read from Codex's own notices alone (never what its log quotes),
and any other refusal of Codex's requests by the provider is the agent's result, as dsh's and mini-swe-agent's are;
whether the provider could not serve a Codex session is read from its last failure (the error its turn failed with,
else its last error or stderr line since the session last worked, never a warning or what its log quotes; and a turn
that failed with the error Codex was retrying, its retries run out, or a stream that ended while it retried, unless
the sign-in was lost, as on a ChatGPT login that cannot reach chatgpt.com, whose turn fails with `workspace routing
discovery failed`; never a session that failed on its own after a retry it went on from, nor a response the
provider's content filter ended, which Codex writes as a disconnected stream: the agent's result; any line when no
session began, as for a container docker could not start, whose error ends with docker's `--help` hint), as Claude
Code's, Pi's, OpenCode's and dsh's is: a notice it recovered from leaves a later failure of its own the agent's
result. A session the provider refused for what its input made the request (a prompt too long, an invalid request, a
content filter) keeps the trial's own status, `agent_error` for a CLI that exited with the error: the agent's result,
as Harbor scores it, and so is such a request of one of Harbor's own agents (litellm's `BadRequestError` or
`UnprocessableEntityError`). Harbor grades a model provider's other failures as the agent's too, a refused sign-in or
model aside (a rate limit, a lost connection or a server error, after its retries; a parameter, a response or a
budget the provider refused), where Hermit keeps them `infra` and runs them again: a provider that could not serve,
or refused the configuration, is not the agent's result. An infra rollout is `unscored` (its cause in `extra`): the
runner runs it again (never a stream's step, nor `unsupported_task`, `stream_order`, a disk gate that waited its time
out, `model_rejected` or `host_failed`), up to `infra_retries` times, each after `infra_wait_s`, every run's usage
the rollout's (`extra.attempts`), and one that ends `infra` all the same is no verdict: the judge refuses it, so the
run records a failed run (status `judge_error`, score 0, its usage and log kept), which a learner sees as one and
`hermit eval` counts among its `errors` and as 0 in its `mean_score`; `python -m hermit.harbor.report <eval dir>`
gives the eval's pass@1 over the trials that are the agent's result ([Evaluation](../guides/evaluation.md)). The
judge scores the reward whenever the verifier gave one, a `timeout` or an `agent_error` included (Harbor's own job
mean does), and says the trial passed at `pass_reward`; a `timeout` or an `agent_error` whose verifier then left no
reward scores 0, Harbor keeping the agent's exception as the trial's. The log is a curated copy of the trial
(`hermit.harbor.curate`): at its top, where the methods' readers look in the log of a session of Hermit's own, the
CLI's stream under the name such a session keeps it (`agent-<backend>.jsonl`, Harbor's own Codex's and Claude Code's
too), `task.md`, `prompt.txt`, `last_message.txt` and the hook audit (`hook-calls.jsonl`, `hook-state/`); beside them
`result.json`, the agent's other outputs (`agent/`, with the ATIF `trajectory.json` of Harbor's own agents), the
tools' calls, the adapter's notes (`memory/`) and the verifier's output and reward; never the CLI's home, a sign-in
or the agent's session transcripts. A lost sign-in, or an API account's usage limit, ends the run: the rollout raises
`LoginFailed`, as a lost sign-in of a session of Hermit's own does (its message names the trial's job and the cause),
and so does every rollout of the runner that starts, or waits for the plan, a slot, the disk or a run again, after
it; nothing is written that a run of another process would meet. A lost sign-in is never run again, on a login
neither as a session the plan refused nor as one the provider could not serve. `hermit train` stops, unfinished,
before the learner is called on the step's rollouts (after the learner's call, for one in `run_agent`'s trials), and
resumes after its last finished step once the sign-in or the account is restored, that step's trials run again;
`hermit eval` stops.

**Streams.** A task with `[[steps]]` in its task.toml (`steps/<name>/instruction.md`, `steps/<name>/tests/`) is one
environment that lives through every step: a benchmark whose next question depends on what the earlier ones left in
the environment. `dataset index` writes one row per step (`<task>:<step>`, with `step`, `step_index` and `steps`), so
the Trainer sees the stream as consecutive tasks and the learner acts between them; the `harbor` agent starts a driver
with the first step (`harborside/driver.py`, Harbor's `MultiStepTrial` fed one step at a time over a socket named by
a digest of the stream's directory, under `$XDG_RUNTIME_DIR` or `~/.cache/hermit/harbor/`; the stream's directory is
`jobs_dir/streams/<task>__s<rollout>__<id>`, the id the runner's own, so that two runs on one jobs
directory, at once or one after the other, a resumed run among them, never share a stream), asks it for each step in
order with the step's own launch plan (its adapter deployed over the last step's; the adapter's tools one server for
the stream's life) and the hosts its agent phase may reach, a step whose turn has not come waiting for the earlier
ones, running in other rollouts of the batch (one whose turn cannot come ends at once: the limits below), and stops it
after the last step, an aborted step, a step whose turn could not come, or `stream.idle_s` without a request. At the
runner's exit, or once the runner is freed, the drivers of the streams that did not finish are stopped, one after
another (no thread may start at exit, so N hung drivers hold it up N × 2 minutes, and up to 40 s more for each kill,
SIGTERM then SIGKILL, and its environment's `docker compose down`), each killed if it has not stopped within 2
minutes (a process killed by a signal leaves them up for `stream.idle_s`): no stream is resumed. A resumed `hermit
train` (a new runner) has no driver for a stream under way, so that stream's remaining steps in the epoch end `infra`
(`stream_order`), counted 0 in the step's mean and given to the learner as such, and the stream starts over at its
first step's next rollout (the next epoch). A stream that finished runs again from its first step in a new
environment in the same directory (an eval's next repeat, a later epoch), the earlier run's trial set aside
(`earlier/<n>/`), once the earlier run's driver has exited (its record says it is ending before its last answers; the
new one waits for it up to 30 s, then kills it). A step's
verifier is read as a trial's (`verifier_timeout`, the check that its tools downloaded) and takes the timeout
multipliers. With a frozen adapter, `harbor run` on the same task evaluates the whole stream in one trial. A stream's
steps run in their order, each once while the stream runs: keep its rows in file order with `data.shuffle: false` (the
Trainer shuffles a split's rows one by one), and list every step of a stream in a split that lists any; a step out of
its turn ends `infra` (`stream_order`). A stream's driver outlives the rollout that started it; it runs on the host
with the pipeline.

**Packaging a benchmark.** `python -m hermit.harbor.dataset prepare utils/benchmark.yaml` makes an example's data from
the `dataset:` block of its benchmark file: the registry's tasks (`ref: <name>@<version>`: those already in the data
directory are reused, unless the `.hermit-source.json` a download leaves there names another source, the ref with
`repo`, `registry_url` and `registry_path`, or another patch (or none where the block has one, or one where it has
none), refused before the patch runs on them, else they are downloaded into a folder of their own, `.<name>.partial`
in the data directory when it exists (else `<data>.partial` beside it), patched there, and moved into place when
complete (a second prepare downloading there meanwhile is refused, on a filesystem that keeps file locks), and a file
where the data directory goes, or a `<name>` already there that holds none of the set's tasks (a file, a link, a
directory not empty), is in the download's way: refused before anything is downloaded, never replaced; the data
directory is the ref, every character but a letter, a digit, `_`, `.`, `@` and `-` made `_` (`org/name@1.0` is
`org_name@1.0`; `+<hash>` of the registry options and the patch, its hook and its file's digest, after it when the
block sets any, so another registry's set of the ref, or one patched otherwise, is never taken for it, and a changed
patch's set is downloaded anew), under `~/.cache/hermit/harbor-tasks` or `$HERMIT_HARBOR_TASKS` (a relative one in the
working directory), outside the checkout, unless `data:` names another, a relative one in the benchmark file's folder,
but never the `tasks` link below nor a path in it, refused before anything is downloaded or reused) and, beside the
file, `tasks`, a link to them (it names a path of this machine: keep it out of git; anything but a link there, as a
directory of its own, is refused before anything is downloaded or reused), `tasks.jsonl`, the splits (task counts, or
`in_sample`), smoke rows, a patch of the download and, with `image_sizes: true`, the images' sizes (an image whose
size cannot be read anew keeps the one written before), all made before any is written (a split asking for more tasks
than there are, or of a fraction, a smoke task named twice, leave every file as it was), checked against the lock
beside the file (which holds no absolute path, a `registry_path` of this machine named by its file name, and is
rewritten only when its tasks or their source change, the source, its ref and registry options, another one's a
mismatch; one that cannot be read, JSON that is no lock included, is a mismatch, refused unless `--force`;
hermit/harbor/dataset.py has the block's keys). A benchmark of your own is written as task directories by your
`utils/prepare.py` (the judge goes into `tests/`, uploaded after the agent ran, so the answer is never where the agent
can read it; see `examples/officeqa/utils/harbor/build_tasks.py`). `dataset lock` records the sha256 of every task
directory (its files' paths, bytes and executable bits, not their modes, and a link by its own text, never by what it
leads to) and `dataset check` compares; `dataset splits` writes deterministic train/val/test lists by task. The task
set is checked without a model by running it with the scripted agent
(`acting_agent: {import_path: harborside.scripted:ScriptedAgent, kwargs: {script: "echo 42 > /app/answer.txt"}}`): a
script that produces the right answer must score 1 and a wrong one 0 (`tests/core/test_harbor_smoke.py` does this
against Docker).

Limits: a stream's steps take no login's holds (they run as their turn comes) and never run again when `infra`, and a
step on a login notes nothing of the plan's usage, so what the steps used holds back none of the plan's other
sessions; a step a driver already ran is refused while its stream goes on (`infra`, `stream_order`), and its first
step run again after the stream's last step starts the stream over in a new environment (its trial directory and
replies set aside in `earlier/<n>/`: a step that leaves no output of its own reads none of the first run's, nor the
tool calls it left, its own read only once its turn came), where a later step run again ends `stream_order` (no
driver, and no rollout of its first step running), so `run_agent` runs a training step in the Trainer's own stream of
its task, out of its turn unless it is the step the stream waits for ([Known
limitations](../reference/compatibility.md#known-limitations)), a first step it runs once the Trainer's pass of the
stream finished starts a new pass that the next epoch inherits (that epoch's first step `stream_order`, its later
steps in the environment the candidate's pass left), and a validation step it runs a second time ends `stream_order`
unless it is the first step of a stream that had finished; a step whose turn cannot come ends `infra` (`stream_order`)
at once, not after `stream.turn_wait_s`, and starts no driver: its driver waits for a step no rollout is running (one
that ended before it reached the driver, as its staging, its tools or its launch plan failed on this machine or the
disk gate held it; or one that ran in an earlier training step), or it has no driver (stopped, or idled out) and no
rollout of its first step is running to start one, and then it ends before anything is staged for it; a step that
ended in any way before its driver was asked for it (those, its own folder not made on this machine, `host_failed`,
its turn's wait running out, its turn's sign-in or tools
not readied, an exception of any of them) gives its stream up, so that the driver is stopped once the stream's
rollouts have ended (that step's own, when it was the last) and every later step of the pass ends so, and the stream's
next pass starts from its first step in a new environment; a lost sign-in in a step ends the run, whose resume loses
the stream's remaining steps in that epoch (above); a driver that answers no stop within 15 minutes (hung in a step)
is killed with its process group and its trial's compose projects are taken down by name, as an expired trial's are
(best effort: `docker compose ls` lists what is left); whether the adapter's tools came up is read from Claude Code's
and dsh's streams (Codex, Pi and OpenCode run on without them, unsaid, as in Hermit's own sessions; mini-swe-agent's
runner lists them before the model's first call, so a tool server it cannot reach then ends the session before it
started, `session_failed`, and one lost later makes each `hermit-tool` call an error the model reads); a process the
agent started in a session of its own outlives the kill until the container stops; Harbor's own built-in agents take
no adapter. A config for `hermit eval` alone, such as a baseline's, still gives the keys every config needs:
`data.train`, which the eval reads and checks, `template`, whose manifest it reads, and `evolver.name`, which with the
template names the run's folder unless `trainer.run_name` does (`<output_dir>/<run>/eval/<adapter>/`: two such configs
of the same evolver and template share it, the second eval's results replacing the first's).

How the Harbor runner fits the framework's interfaces without changing them, what that costs, and the candidates for
framework support are in `hermit/harbor/FRAMEWORK_SUPPORT.md`.

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
