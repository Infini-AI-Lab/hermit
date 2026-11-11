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

`extra["unscored"]`, a cause, says that the run is not the agent's result (the host or the model provider failed, as
in a Harbor trial with status `infra`): the scoring function is not called, the record has status `infra`, and the
Trainer's and `hermit eval`'s means leave it out and count it ([run modes](configuration.md#how-the-run-is-done-run)).

Concurrent tasks may run against the same adapter path. Treat that adapter as input and keep execution state and
logs outside it. Give each execution its own working paths unless sharing is part of the task design. Keep returned
log paths available until the Trainer has collected them.

### Feedback result

```{literalinclude} ../../hermit/task.py
:language: python
:pyobject: JudgeResult
```

The scoring function decides what feedback to reveal. Reference answers placed in feedback become visible to the
learner. The numeric score can be task-specific; the `eval` report counts a test row (`solved_once`) or a task
(`pass_at_k`) as solved at least once when an attempt solved it: the judge's `passed` when it returns one as a bool (the
Harbor judge does, at `pass_reward`), else a score of at least `1.0`; a judge that returns `passed: None`, a score scale
with no pass (the Harbor judge with `pass_reward: null`), gives the report no solved share.

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

### A coding-agent CLI as the acting agent

`codex`, `claude-code`, `pi`, `mini-swe-agent`, `opencode` and `dsh` act with a coding-agent CLI in a workspace of
Hermit's (the other way is `agent.name: harbor` with `acting_agent.cli`: the CLI in a Harbor task's container,
[Benchmarks](#benchmarks)): a task gives what is its own, in configuration and, where its workspace is built in code, in
functions it names (`file.py:function`); the agent does the rest. OfficeQA's Codex runs are one such configuration
(`examples/officeqa/utils/task.py`):

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
  the container reaches, and so does an OpenCode session (OpenCode waits only 2 s for a server it starts). An
  adapter's [`context_tool`](adapter-manifest.md#per-task-context) is rendered for each
  task (the field `context_query`, else the task's question or prompt), deployed into the context and recorded in
  `RunOutput.extra["context_tool"]`; so is the context of any adapter whose template is not
  [`plugin-portable`](adapter-manifest.md#plugin-portable), which `hermit deploy` alone refuses. The session
  calls the adapter's tools without approvals and may start its sub-agents. An adapter with `memory/NOTES.md` (the
  continual-harness template) is told so in the prompt, and the notes the session leaves come back in the log
  (`log/memory/`). The context must fit `project_doc_max_bytes` (Codex reads no further): a longer one is refused. So
  is one too long for DeepSeek Harness, which gives the model at most 65,536 bytes of a project's instructions.
- **Isolate.** With `environment.image`, the CLI runs in a container of its own, started from a worker on the host
  that runs the adapter's code the deployment renders or serves in the strict sandbox: the workspace and the session's
  home read-write, the mounts read-only at their targets, an internal network whose only way out is an egress proxy to
  the provider's hosts, and the CLI's own sandbox inside for the commands it runs (Codex's workspace-write; Claude
  Code's, with no network and writes to the workspace and `/tmp` only). `docker/codex/` builds Codex's image and the
  proxy, `docker/claude/` Claude Code's image; `environment.container` (or `HERMIT_CODEX_NETWORK`, `HERMIT_CODEX_PROXY`,
  and `HERMIT_CLAUDE_NETWORK`, `HERMIT_CLAUDE_PROXY` for Claude Code) names the network and the proxy. The agent checks
  the image and the network before its first session (for Claude Code, also the CLI in the image and that its sandbox
  starts there). Without an image the agent runs in the task sandbox (bubblewrap), where a mount inside the workspace
  is a link for Codex; for Claude Code and DeepSeek Harness it is bound there by a nested bubblewrap, a real read-only
  directory as in a container (a search that walks the tree, `grep -r`, `find`, Claude Code's Glob and Grep or
  DeepSeek Harness's glob and grep, does not follow a link), and linked with a warning where bubblewrap cannot nest.
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
  where the pipeline runs on the host). Each session has a home of its own: none of the user's Codex, Claude Code or
  DeepSeek Harness settings apply, and a Codex session's commands see only Codex's core variables (`shell_env: all`
  passes every variable).
  A DeepSeek Harness session never gets the caller's `DSH_*`, `DEEPSEEK_BASE_URL` or `DEEPSEEK_SEARCH_BASE_URL`,
  even through `environment.env`: give them in the agent's `env` setting.
- **Web search.** `web_search` must be set, `true` or `false`: Codex's hosted search runs at the provider, past any
  sandbox or proxy, so it is never left to a default. Claude Code's `WebSearch` and `WebFetch` follow it too, and every
  Claude Code session is denied the tools whose work outlives it or reaches past it: `RemoteTrigger`, `CronCreate`,
  `CronDelete`, `CronList`, `ScheduleWakeup`, `ListAgents` and `SendMessage`
  ([Claude subscription](../guides/claude-subscription.md#execution-boundaries)).
  DeepSeek Harness's web search and fetch follow `web_search` too; its search is DeepSeek's own service, so unless the
  model is reached through DeepSeek's API on `DEEPSEEK_API_KEY`, `true` gives the session web fetch only. Its fetch
  runs in the session, not at the provider: in the task sandbox it reaches only what `environment.network` allows.
- **Stop and retry.** One session per run, stopped at `timeout_s`. A session the provider could not serve (429, 5xx,
  a container that did not start; on Codex read from its last failure, the error its turn failed with, else its last
  error or stderr line, or any line when no session began, as for a container docker could not start, whose error ends
  with docker's `--help` hint; never a notice of an error it recovered from, nor what Codex's log quotes of the
  session's work, the model's own text: its records of the session's tool calls, as of a rejected command or a patch
  whose expected lines it did not find, and every record's further lines) runs again after `retry_wait_s`, up to
  `retries`; then the run is an error, not a wrong answer. On Claude Code and DeepSeek Harness any failed session is
  an error, with its tokens and log kept and no judge call. A lost sign-in ends the run, which `hermit train`
  resumes: on Claude Code a login's, on Codex an authentication failure (a 401, an invalid key, not logged in, a
  refused proxy token, or a 403 its turn failed with; never one in a notice of an error Codex recovers from and goes
  on: that it is reconnecting, `Reconnecting...`, or falling back from WebSockets to HTTPS; nor in what its log quotes
  of the session's work). A session past its clock is an attempt with no answer.
- **Record.** The run's directory holds `work/` (the workspace, kept for inspection without its links out),
  `home/` and `log/`. `RunOutput.log` is `log/`: the host's event stream (`agent-codex.jsonl`, `agent-claude.jsonl`,
  `agent-dsh.jsonl`), `prompt.txt`, `last_message.txt`, `deployment.json`, and what `collect` added. The output
  is what `answer` returned, with `fail_reason` when there is no answer; without `answer`, the last message. In a
  run, a model key's value in what the call leaves is scrubbed (`<VARIABLE>`; the login's token too, as
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
launch plan, `<job>/launch/plan.json`), against the adapter deployed by the CLI's host exactly as `hermit deploy` lays
it out (`hermit.harbor.staging`: the context file, skills, roles and hooks, the whole adapter in `hermit/<adapter id>/`
of the task's workdir). harborside uploads the deployment and the home and runs the command as the task's agent user,
the prompt (Harbor's instruction) on its stdin and the session's stream in `/logs/agent/agent-<backend>.jsonl`; a
session still running when Harbor's timeout stops the agent is killed with its process group, so that it edits
nothing under the verifier. That kill and the steps after the session (the adapter's notes copied out, the collected
files, the files returned to this machine) run from `/`, so a session that removed its workdir is still stopped and
what it left still comes back; a step that fails is recorded in the trial's `result.json` (`after_session_errors` in
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
Code's on its command line; mini-swe-agent's shell calls them as the `hermit-tool` command); each call is logged
(`tool-calls.jsonl`). An adapter whose context is per task (a `context_tool`, or one only Hermit runs) gets the
task's context, computed before the trial in the adapter sandbox (`hermit.harbor.context`); the notes an adapter keeps
(`hermit/<id>/memory/`) come back after the session (`memory/` in the log). A key or a token reaches the container by
its variable's name only (`docker compose exec -e NAME`, Claude Code's subscription token on a descriptor): never in a
command line, a file, the plan or a log. Codex's sign-in on `login: chatgpt` (`auth.json`) is a file of the CLI's home
in the container (mode 600, the agent user's), as in Hermit's own Codex sessions, so the session's commands can read it;
it stays there until the trial's container is removed, through a verifier that shares the container (Harbor's shared
mode) and, with `environment.kwargs.keep_containers: true`, in the stopped container Harbor keeps. The host's copy for
the trial is removed after the trial. With `login: chatgpt` (Codex) or `login: claude` (Claude Code) a trial runs on the
user's subscription and shares its plan with Hermit's other sessions on it (hermit.agent.base.login): it waits while the
plan holds sessions back, its session notes the plan's usage (Codex's as Claude Code's), so `MAX_USED` holds trials back
as it holds Hermit's own sessions, a session the plan refused runs again once its window allows (on Codex the refusal is
read from Codex's own notices, never from what its log quotes of the session's tool calls), one the provider could not
serve runs again after a pause (`busy_retries` times), and none starts or runs again once another trial halted the run
while it waited; the trial's usage is every run's (the tokens they reported, unknown only when none did, and their model
calls; the cost unknown when one run's is; each run's own in `extra.attempts` when the trial ran more than once or a
halt ended it after a run). Codex's refreshed sign-in comes back from the container, even when Harbor's timeout lands
while it is being copied out (that step, and no other after-step, runs once more). The answer-only agent (`cli: answer`,
`harborside.answer:HermitAnswer`) makes one model call from Harbor's process, through an endpoint the runner serves on
this machine (the adapter's context as its system text), and writes the answer into the container, for a task whose work
is a reply (LAB-Bench's questions); a request still in flight when the trial ends (Harbor's timeout stopped the agent)
is given up then, before the trial's usage is read, and leaves the trial's cost unknown. On a model a CLI reaches on a
login (`model: {cli, model, login}`), whose request is a session that waits inside it while the login's plan holds
sessions back, the trial waits for that plan before it starts (and does not start once another trial halted the run
meanwhile), and one whose request was still unanswered when the agent's time ran out, after its session had itself
waited for that plan, as that session records its waits, however the plan stands when the trial is read (not a hold that
began once it ran), is infra (`usage_limit`, no halt), not the agent's timeout. Neither covers a wait for one of the
plan's session slots (`SLOTS`): a request that finds every slot taken (`SLOTS` below `agent.concurrency`, or the login's
other sessions in them) waits for one inside the agent's time, and a trial whose time ran out there is the agent's
timeout, scored 0, so keep `agent.concurrency` at most the plan's `SLOTS` less the login's other sessions. A task whose
agent phase has no public network (task.toml, as Harbor reads it) gets the model's and the tools' hosts named to Harbor
(`extra_allowed_hosts`) and the CLI's web search off.

A task row (`tasks.jsonl`, written by `python -m hermit.harbor.dataset index <tasks dir> --out tasks.jsonl
[--rollouts k]`) names its directory as `harbor_task` and is the task's name, so a benchmark's existing split files
apply; `k` rollouts of a task are `k` rows (`<task>#<k>`), which `hermit eval` counts as the task's attempts
(`pass_at_k`; a multi-step task's steps are counted step by step, each attempted once per rollout and repeat), while
its `tasks` and `solved_once` count rows (with one repeat, `solved_once` is a pass@1 rate). Harbor lives in its own
venv (`~/.cache/hermit/harbor-venv`, `agent.params.harbor` names its command): Hermit's side writes one job config per
rollout and runs `harbor run` as a subprocess, killed with its process group past the trial's wait budget, and the
trial's compose projects then taken down by name (from an empty directory, `/` when none can be made, with none of this
process's `COMPOSE_*` variables: no compose file or `.env` is read, whose volumes would go too), and the half that runs
in Harbor's process, `hermit/harbor/harborside/`, imports only harbor, two of Harbor's own dependencies (pydantic, and
certifi where it is installed: Codex's agent's CA bundle of last resort) and the standard library. Startup refuses what
every trial would refuse: a login for another CLI, a login and a binding at once, no `web_search`, a model on this
machine's loopback, a sign-in or a key that is not there, a plan the CLI's Base Agent cannot render, mini-swe-agent
without its package where the adapter is deployed (the `mini-swe-agent` extra, in `agent.params.python`, else Hermit's
own interpreter), for a run that deploys adapters (not `hermit eval --adapter none`, whose trials deploy none), the
answer agent's `attachments` with a model a CLI reaches (a session takes text only), a `harbor` that is no executable
file and, for a task set with a multi-step task, one with no interpreter beside it (or beside the file a link to it
names) for the stream's driver; and an acting agent of Harbor's own (`acting_agent: {name}`, or a class that is no
HermitXXX), which takes no adapter, is refused by `hermit train`, by `hermit eval` with any adapter but `none` and by
`hermit eval --checkpoints` (each checkpoint test takes an adapter), and `hermit check` reports it for a training
config.

| `agent.params` | Meaning |
|---|---|
| `tasks_dir`, `jobs_dir`, `tmpdir`, `harbor` | the task directories; one Harbor job per rollout (outside the checkout); TMPDIR for harbor; Harbor's command, a name on PATH or a path (a relative one is the working directory's; default: `~/.cache/hermit/harbor-venv/bin/harbor`), beside which, or beside the file a link to it names, is the interpreter a stream's driver runs under |
| `model`, `login`, `effort`, `web_search`, `price` | the acting model: a `<backend>/<model>` reference (its endpoint and key), or with `login` (`chatgpt`, `claude`) the CLI's model name on the user's subscription; the answer agent's may be a model a CLI reaches (`{cli, model, login}`, as `llm`'s: each request one session of that CLI, which takes text only, served by an endpoint whose timeout outlasts the session and a wait for the login's plan, and each trial's relay to it keeps that timeout, never cutting a request at its client's 600 s to send it again; on a login each trial first waits for that plan, the mapping's `plan_dir` and `usage_credits`), and its relay takes the client `timeout` and `max_retries` a model mapping gives beside a reference; the reasoning effort; the CLI's web search, required (not for the answer agent); a reference's price (USD per million tokens: `input`, `cached_input`, `output`; in the config, or in the benchmark file (its price taken only with the file's own model); default: the backend's), which prices the HermitXXX trials' usage and the answer agent's endpoint (one of Harbor's own agents keeps Harbor's cost) |
| `plan_dir`, `usage_credits`, `busy_retries`, `busy_wait_s`, `login_root` | on a login, as a CLI session's: the plan's directory (default: the login's own under `~/.cache/hermit/`); whether a session may draw the account's usage credits (default: no, and one that drew them holds the plan's sessions until its window resets); how many times a session the provider could not serve runs again (3, also when null), the n-th after n × `busy_wait_s` (120 s, also when null); the shared copy of the ChatGPT sign-in (`~/.cache/hermit/codex-login/`) |
| `acting_agent` | `{cli: codex}` (the default) or another CLI of the table, with `settings` (the CLI Base Agent's own, over the table's), `kwargs` (the HermitXXX class's), `env` (no secrets), `pythonpath`; or `{name}` / `{import_path}`: a Harbor built-in or another class, run as Harbor runs it, which takes no adapter |
| `<cli>_bin`, `<cli>_version`, `setup_timeout_sec` | a CLI binary of this machine (`codex_bin`, `claude_bin`, `pi_bin`, `opencode_bin`, `dsh_bin`; mini-swe-agent takes none), its folder mounted read-only at `/opt/hermit-<cli>`, first on PATH (for Codex's standalone release, a `codex-package.json` in the binary's grandparent, the release's folder, with its `bin/` first on PATH), used only when it answers `--version` (asked as root) as the asked-for version (any, when none is) and the task's agent user may execute it (`test -x`: a binary or folder of mode 700 of another uid is not, unless that user is root), else the container installs the CLI; the version the container installs, the asked-for one (`codex_version`, ..., `mini_swe_agent_version`; default: the `hermit.harbor.clis` table's; its `codex` and `claude` rows give none, so Harbor's own install's); Harbor's agent setup limit |
| `answer` | the answer agent's `instruction_file`, `attachments` (globs of the images sent with the question: one over 20 MiB is left out, as a file that is no image is, and a figure a wildcard would match in a directory the agent user may not list is not found, as by a glob that matches nothing (no error: the model is asked without it; one named outright is found wherever the agent user may enter its directory); attachments whose listing exec failed, and an image that cannot be measured or read, are `launch_failed`: the model is never asked without them), `answer_file`, `answer_pattern`, `answer_format`, `wait_s`, `max_tokens` |
| `pass_reward`, `verifier_redact`, `reward_key` | the reward that is a pass (default 1.0; null: a score scale, where none is); regexes removed from the curated verifier output (a test's expected answer); the verifier's reward key |
| `agent_timeout_sec`, `environment`, `verifier`, `retry`, `verifier_timeout` | the agent's budget; Harbor's environment (`type`, `import_path`, `delete`, `force_build`, `mounts`, `extra_docker_compose`, `kwargs`), verifier and retry configs, passed through; a verifier that ran out of its time: `infra` (the default: not the agent's result) or `agent` (it timed the agent's solution, as AlgoTune's does: the agent's result) |
| `verifier_timeout_multiplier`, `environment_build_timeout_multiplier` | Harbor's: each task's verifier limit, and its environment start (pull, build) limit, times this (default 1, never below 1: a slow host); the agent's budget is never scaled |
| `docker_cleanup`, `rmi_threshold_gb`, `disk_gate_gb`, `disk_gate_wait_s`, `tmp_gate_gb`, `image_sizes` | `images_keep`, `rmi_task_image` or `rmi_over_gb` (the task's prebuilt image removed after the trial, only when larger than the threshold); no trial starts while Docker's data root (or the host's /tmp) has less than the gate free, room kept for the images still to be pulled |
| `trial_cache` | a finished trial of the same row (the whole row), task directory (its contents), model (its price included; the answer agent's with its relay's client `timeout` and `max_retries`), adapter (each of its files, git's and Python's caches aside: its path, and the bytes and permission bits a deployment copies; a link's those of the file it leads to inside the adapter, a link the deployment refuses by its own text), `context_query` and launch is not run again: a resumed run or eval takes it and spends nothing (its first cost is kept as provenance); each of `hermit eval`'s repeats is a trial of its own |
| `tools_host`, `sandbox_enforce`, `context_query`, `python` | where the adapter's tools are served (default: the Docker bridge's gateway); the adapter sandbox's enforce; the row's field a per-task context retrieves for; the interpreter the hosts' deployment and the per-task context run with (default: Hermit's own) |
| `benchmark` | a YAML file holding these same keys, shared by every method's config of the example (the config's params override it, and a relative path that is the config's folder's (below) is then the benchmark file's folder's, the config's own too; the sign-in it gives (`login`, `login_root`, `claude_bin`, each where the config does not give it) is checked at startup as the config's is, its `login_root` and `claude_bin` resolving against the working directory, as the config's do; its `dataset:` block is `dataset prepare`'s) |
| `stream` | a multi-step task's driver: `idle_s` (the environment stops after that long without a step; 4 h, also when null), `start_timeout_s` (1 h, also when null), `turn_wait_s` |

A relative `tasks_dir`, `image_sizes`, `acting_agent.pythonpath` or `environment.extra_docker_compose` is the config's
folder's, or, with `benchmark` set, the benchmark file's folder's (the config's own values too); `<cli>_bin`,
`jobs_dir`, `tmpdir`, `login_root`, `plan_dir` and a `harbor` that is a path resolve against the working directory,
with `~` expanded.

The rollout's output is `{harbor_task, rollout, step, reward, rewards, status, failure_cause, exception_type, trial,
hooks_active}` and the CLI's last message (`reply`; the answer agent's `answer`), with `status` one of `ok` (a
verifier reward), `timeout` (the agent's time ran out, and the verifier still scored its work: even after a provider
notice in its stream that the session recovered from, and for the answer agent with its model request still in flight,
unless that request's session had itself waited for its login's plan, as the session records its waits,
however the plan stands when the trial is read: `infra`, `usage_limit`; or, with `verifier_timeout: agent`,
the verifier's own time ran out on the agent's solution), `agent_error` (the CLI exited with an error, its
work still scored; the model's safeguards ended the session; the adapter's context over the CLI's limit
beside the image's own) or `infra`: not the agent's result (a sandbox, model-provider, verifier or Harbor
failure, a killed `harbor run`, the disk gate, a lost stream, a session that never started or whose tools
never came up; a Claude Code, Pi or OpenCode session, or the answer agent's request, that the provider refused
before serving it for the run's configuration, `model_rejected`, with no halt: its key, its permission,
its model (not found, not existing, unknown or invalid), its account's bill (a 401, a 402, a 403 or a 404 where the
failure writes it as a status, never a number among its words, or the CLI's words for one) or a request setting it does
not take (an unsupported parameter or value, an unrecognized request argument, an unsupported operation: `max_tokens`
where the model takes `max_completion_tokens`, an effort it has not); the answer agent's by its endpoint's status, or by
a 400 whose message says one of these (the last 1,000 characters of the error's `message`, all that Hermit's endpoint
says of a failed CLI session, of an error that is itself a string, else of the body's own `message` or `detail`) or
whose code says that Hermit's endpoint refused what a CLI's session cannot take or start with (`unsupported_request`);
harborside's own setup failing: no launch plan (or a stream step's plan the agent cannot take), the CLI's install (a
package, an npm or GitHub download, the mounted binary's probe), the workdir or its owner, the run directory, the upload
of the deployment, the home, the bundle or the launch script, the `hermit-tool` script's install, Hermit's
mini-swe-agent runner that cannot run in the container, a workdir it cannot enter, the image's own context file that
cannot be read or captured whole (its copy failed part-way), a secret the plan passes by name that is not in Harbor's
environment (or an environment other than Harbor's local `docker` one, which cannot pass it by name), a launch that
exited non-zero and left no stream (the CLI never started, as when Docker could not run its exec; the stream an earlier
step of a stream left is removed before the launch), a launch whose exec this machine could not run (an
OSError: the compose client not spawned, EMFILE or EAGAIN), or, after its non-zero exit, whose stream it
could not look for (whether the CLI started cannot be told), the answer agent's question (its instruction
file not there in time) or figure that cannot be read in the container, its attachments whose listing exec
failed, or its answer that could not be kept in `answer.json` (an OSError on this machine) or written to `answer_file`
(its exec failed, or this machine could not run it: an OSError), `launch_failed`; a plan's usage limit, a lost
sign-in). On Codex a refused sign-in or key (a 401, an invalid key, not logged in, a refused proxy token, a 403 its turn
failed with; never one in a notice of an error Codex recovers from and goes on: that it is reconnecting, or falling back
from WebSockets to HTTPS; nor one in what Codex's log quotes of the session's work, the model's own text: its records of
the session's tool calls, as of a rejected command or a patch whose expected lines it did not find, and every record's
further lines) is a lost sign-in, as a refused login is on Claude Code, and any other refusal of Codex's requests by the
provider is the agent's result, as dsh's and mini-swe-agent's are; whether the provider could not serve a Codex session
is read from its last failure (the error its turn failed with, else its last error or stderr line, never what its log
quotes; any line when no session began, as for a container docker could not start, whose error ends with docker's
`--help` hint), as Claude Code's, Pi's, OpenCode's and dsh's is: a notice it recovered from leaves a later failure of
its own the agent's result. A session the provider refused for what its input made the request (a prompt too long, an
invalid request, a content filter) keeps the trial's own status, `agent_error` for a CLI that exited with the error: the
agent's result, as Harbor scores it. An infra rollout is `unscored`: the Trainer never judges it, its means and
`hermit eval`'s leave it out and count it (an Evolver that averages the records itself sees a 0 with status `infra`),
and `hermit eval` runs it again with `eval.infra_retries`. The judge scores the reward whenever the verifier gave one, a
`timeout` or an `agent_error` included (Harbor's own job mean does), and says the trial passed at `pass_reward`; a
`timeout` or an `agent_error` whose verifier then failed scores 0, Harbor keeping the agent's exception as the trial's.
The log is a curated copy of the trial (`result.json`, the CLI's stream and the files it brought out, the hook audit,
the tools' calls, the adapter's notes, the verifier's output and reward; never the CLI's home, a sign-in or the agent's
session transcripts). A lost sign-in, or an API account's usage limit, writes `<jobs_dir>/HALT`: every rollout returns
at once as `infra`/`halted` while it exists; a lost sign-in is never run again, on a login neither as a session the plan
refused nor as one the provider could not serve.

**Streams.** A task with `[[steps]]` in its task.toml (`steps/<name>/instruction.md`, `steps/<name>/tests/`) is one
environment that lives through every step: a benchmark whose next question depends on what the earlier ones left in the
environment. `dataset index` writes one row per step (`<task>:<step>`, with `step`, `step_index` and `steps`), so the
Trainer sees the stream as consecutive tasks and the learner acts between them; the `harbor` agent starts a driver with
the first step (`harborside/driver.py`, Harbor's `MultiStepTrial` fed one step at a time over a socket in the stream's
directory, `jobs_dir/streams/<task>__s<rollout>`, then, with `eval.repeats` above 1, `__r<repeat>` for each repeat, and
`__<8 hex>`, a digest of the run's identity: for a training run its record directory, by its path and inode, so that a
resume of the same run directory meets its streams again while their drivers wait for a next step and a run started over
(`trainer.resume_mode: disable`) gets new ones; for every other run (`hermit eval`, `run.mode: split`'s final test, a
checkpoint test, `hermit check`'s probe) one of its own: each repeat a stream of its own, and two runs never share one),
asks it for each step in order with the step's own launch plan (its adapter deployed over the last step's; the adapter's
tools one server for the stream's life), a step whose turn has not come waiting for the earlier ones, running in other
rollouts of the batch, and stops it after the last step, an aborted step, or `stream.idle_s` without a request, or when
the run ends, fails or is stopped with Ctrl-C (a process killed by a signal leaves it up for `stream.idle_s`): a
training run, whose resume meets its streams again, only once it has finished (its `summary.json` written, all epochs
done or stopped early), as a finished run is not resumed. A step's verifier is read as a trial's (`verifier_timeout`,
the check that its tools downloaded) and takes the timeout multipliers. With a frozen adapter, `harbor run` on the same
task evaluates the whole stream in one trial. `data.shuffle` moves a multi-step task's rows as one, in step order (a
step taken ahead of an earlier one would wait for it in the stream), a seed of `run.orders` keeps a task's rows
together in the split's order, and `data.shuffle: false` keeps the split's order: a split or order file that, so kept,
lists a step ahead of an earlier step of its stream (a test or checkpoint split too) is refused before any run that
takes it (`hermit eval` refuses its split at start), and `hermit check` reports it for the splits the config's own run
takes (a training config's train order, a checkpoint split, the test split under `run.mode` split or eval). A stream's
driver outlives the rollout that started it; it runs on the host with the pipeline.

**Packaging a benchmark.** `python -m hermit.harbor.dataset prepare utils/benchmark.yaml` makes an example's data from
the `dataset:` block of its benchmark file: the registry's tasks (`ref: <name>@<version>`: those already in the data
directory are reused, else they are downloaded into a folder of their own, `.<name>.partial` in the data directory when
it exists (else `<data>.partial` beside it), and moved into place when complete (a second prepare downloading there
meanwhile is refused, on a filesystem that keeps file locks), and a file where the data directory goes, or a `<name>`
already there that holds none of the set's tasks (a file, a link, a directory not empty), is in the download's way:
refused before anything is downloaded, never replaced; the data directory is the ref, every character but a letter, a
digit, `_`, `.`, `@` and `-` made `_` (`org/name@1.0` is `org_name@1.0`), under `~/.cache/hermit/harbor-tasks` or
`$HERMIT_HARBOR_TASKS` (a relative one in the working directory), outside the checkout, unless `data:` names another, a
relative one in the benchmark file's folder, but never the `tasks` link below nor a path in it, refused before anything
is downloaded or reused) and, beside the file, `tasks`, a link to them (it names a path of this machine:
the repository's `.gitignore` ignores it in an example's `utils/`; anything but a link there, as a directory of its own,
is refused before anything is downloaded or reused), `tasks.jsonl`, the splits (task counts, or `in_sample`), smoke
rows, a patch of the download and, with `image_sizes: true`, the images' sizes, checked against the
lock beside the file (which holds no absolute path and is rewritten only when its tasks or their source change; one that
cannot be read, JSON that is no lock included, is a mismatch, refused unless `--force`;
hermit/harbor/dataset.py has the block's keys). A benchmark of your own is written as task directories by your
`utils/prepare.py` (the judge goes into `tests/`, uploaded after the agent ran, so the answer is never where the agent
can read it; see `examples/officeqa/utils/harbor/build_tasks.py`). `dataset lock` records the sha256 of every task
directory (its files' paths, bytes and executable bits, not their modes) and `dataset check` compares; `dataset splits`
writes deterministic train/val/test lists by task. The task set is checked without a model by running it with the
scripted agent (`acting_agent: {import_path: harborside.scripted:ScriptedAgent,
kwargs: {script: "echo 42 > /app/answer.txt"}}`): a script that produces the right answer must score 1 and a wrong one
0 (`tests/core/test_harbor_smoke.py` does this against Docker).

Limits: a stream's steps take neither the trial cache nor a login's holds (they run as their turn comes), and a step
on a login notes nothing of the plan's usage, so what the steps used holds back none of the plan's other sessions; a
resumed `hermit train` meets the streams it left while their drivers wait for a next step (`stream.idle_s`), and a step
a driver already ran is not run again (`infra`, `stream_order`); `hermit check`'s probe of a multi-step task runs one
step only; whether the adapter's tools came up is read from Claude Code's and dsh's streams (Codex, Pi and OpenCode run
on without them, unsaid, as in Hermit's own sessions; mini-swe-agent's runner lists them before the model's first call,
so a tool server it cannot reach then ends the session before it started, `session_failed`, and one lost later makes
each `hermit-tool` call an error the model reads); a process the agent started in a session of its own outlives the
kill until the container stops; Harbor's own built-in agents take no adapter.

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
