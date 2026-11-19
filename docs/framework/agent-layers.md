# Agent layers

Every agent in Hermit, the one that answers a task and the one that learns an adapter alike, is built from three
layers. Each layer uses the one below it through a single API and treats it as a black box.

```{raw} html
:file: ../_includes/agent-layers-interactive.html
```

The code follows the layers:

```text
hermit/agent/
├── model/        Layer 1: model references, backends, the chat client, a single model call (Direct)
├── environment/  where an agent works: the Environment API and its implementations (Local, Sandboxed, Container)
├── base/         Layer 2: the Base Agent API and its implementations, tools, the tool server, login
├── acting/       Layer 3: the Acting Agent API and the built-in Acting Agents
└── evolver/    Layer 3: the Evolver API and the agent Evolver
    └── methods/    the learning methods (gepa, skillopt, meta_harness_adapter, ...)
```

Imports go one way only: Layer 3 imports Layer 2, Layer 2 imports Layer 1 and the environments, and never the
reverse (`tests/core/test_layers.py` checks it). The rest of `hermit/` (the Trainer, adapters and their templates, `hermit
deploy`, the command line) uses the layers and is not part of them; Layer 2 isolates a CLI with the framework's
sandbox (`hermit/sandbox.py`).

## Layer 1: Model

A model and the way to reach it: a `<backend>/<model>` reference (see [Model backends](../guides/model-backends.md))
or a model name with a login (`login: chatgpt` for Codex, `login: claude` for Claude Code), the reasoning effort and the
price.

API: `chat(messages, **request) → (message, usage)`, the chat-completions call of `hermit.agent.model.llm.ChatClient`
(`request` holds the sampling and the tools). The Codex and Claude Code CLIs reach their model themselves; for them
Layer 1 is the settings they are given (the binding, the login, the effort). Every Codex session on a ChatGPT login
draws on the login's plan: it waits while the files under `~/.cache/hermit/codex-login/plan/` hold sessions back
(`PAUSE`, a usage threshold in `MAX_USED`), takes one of the machine's session slots (`SLOTS`), keeps the plan's
usage report, and is run again when the plan refused it (`hermit/agent/base/login.py`). A Claude Code session on
`login: claude` does the same under `~/.cache/hermit/claude-login/plan/`, signed in with the token the user exported
([Claude subscription](../guides/claude-subscription.md)).

A single model call is Layer 1 too, not an agent: `Direct(settings, model).run(messages, tools=..., session=...)`
(`hermit/agent/model/direct.py`) sends one request and returns `Reply(reply, tool_calls, usage, session,
finish_reason, reasoning)`; the tool calls the model asks for come back unrun. It works in no environment. Code that
decides every step itself uses it: an Evolver whose method is its own code (the run's model reaches it as `ctx.agent`),
a benchmark's own loop, the local endpoint, and the ReAct agent's loop.

## Layer 2: Base Agent

How a model takes steps. Layer 2 holds only the framework's agents:

| Base Agent | What it does |
|---|---|
| `react` | The framework's tool loop: call the model, run the tools it asks for, call it again |
| `codex` | The Codex CLI, working in a workspace |
| `claude` | The Claude Code CLI, working in a workspace |
| `pi` | The Pi CLI, working in a workspace (no roles, no web search) |
| `mini-swe-agent` | mini-swe-agent's bash loop, working in a workspace: one task through its Python API, in a process of its own (no roles, no web search) |
| `opencode` | The OpenCode CLI, working in a workspace (a model reference only; no roles, no web search) |
| `dsh` | The DeepSeek Harness CLI, working in a workspace (no roles; web search only with a model on DeepSeek's own API) |
| `hermes` | The Hermes Agent CLI, working in a workspace (a model reference only; no roles, no web search) |

API, the only one Layer 3 sees:

```text
agent = make("codex", settings, model)        # hermit.agent.base.interface
agent.run(prompt, environment=Local(path, Limits(...)), tools=(), session=None, roles=())
    → Result(reply, trajectory, usage, session, tool_calls, finish_reason, exit_code, timed_out, log, ...)
agent.configured(**settings)                  # a copy with some settings changed, for one call
agent, prompt = agent.instructed(system, prompt)   # a copy that takes `system` as its instructions (a CLI's own place)
```

- `prompt`: what to do.
- `environment`: where the agent works and what it may reach (see [Environments](#environments)): its workspace
  (read-write), its limits (what it may read, what it must not see, where it may connect, how long it may run: see
  [Limits](#limits)) and how they are enforced (its isolation). A path is a `Local` environment there.
- `tools`: what the agent may call (see [Tools](#tools)).
- `session`: the session a previous call returned, to continue the same agent: turn by turn in an interactive
  environment, step by step in a resumed learning session. Codex and Claude Code resume their thread; react carries
  the message history.
- `roles`: named sub-agents (name, description, instructions) the agent may delegate to: Codex's roles, Claude
  Code's agents. react, pi, mini-swe-agent, opencode, DeepSeek Harness and Hermes Agent refuse them.
- `trajectory`: the run as standard steps: messages, reasoning, commands with their exit codes and output, tool
  calls, file changes, errors. Layer 3 reads only these; a CLI's raw log (`agent-codex.jsonl`) is kept beside them
  as an archive.

A Base Agent's settings are given when it is made: for `react` the sampling and the loop's limits; for Codex and
Claude Code web search (set explicitly, on or off), Codex's own sandbox mode, the CLI's home, the time limit (the full
list is in `hermit/agent/base/cli.py`). Where it runs is never a setting: the isolation is the call's environment's,
and a CLI given `isolation`, `sandbox` or `container` as settings refuses the call.
`configured(...)` changes some of them for one call (a method's `max_tokens`, the home and log of one task) and
leaves the agent as it was. Running a task again when the provider could not serve it is Layer 3's: the workspace
is placed anew first.

Codex, Claude Code, Pi, mini-swe-agent, OpenCode, DeepSeek Harness and Hermes Agent share one runner, `CliAgent`: the
isolation, the home, the sign-in, the tool server and the process are the same for every CLI. Each CLI is a subclass
whose hooks give what is its own: its command, its home, and how its event stream is read (the reply, the trajectory,
the usage, whether the provider served it, and what a session that was not served met: `outcome`). mini-swe-agent's
process is the framework's own (`hermit/agent/base/mini_swe_agent_run.py`): it runs one task through mini-swe-agent's
Python API and writes the session as JSON events, the stream the runner reads. Hermes Agent prints only its reply, so
hermit enables a plugin of its own in the session's home (`hermit/agent/base/hermes_plugin`) that writes every hook
Hermes fires as a JSON line, the stream the runner reads (`agent-hermes.jsonl`). A new CLI is a subclass listed in
`CLIS`, a reader of its stream in `hermit/agent/base/trajectory.py`, and its model binding in Layer 1.

`react` is the framework's one tool loop (`hermit/agent/base/react.py`). A loop that needs more is a subclass that
overrides its hooks, never a copy: `call_model` (one request), `before_tool` (an answer instead of running a call: a
cap reached, a call refused), `after_tool` (a call's result as the model reads it) and `stopped` (an end besides an
answer). A benchmark whose own program runs some calls drives it step by step: `loop = agent.start(prompt, tools=...,
external=[names])`, then `agent.step(loop)`; the calls of an external tool wait in `loop.pending` until the driver
gives their results (`agent.give(loop, call_id, content)`, which passes them through `after_tool`; `before_tool`
runs before a call waits).

## Environments

Where an agent works, and what it may reach from there (`hermit/environment/`, beside the agents: a task declares
what it needs of one and a judge may run in one too, so it belongs to no layer of the agents). Every Base Agent takes its
environment from the call; nothing about where it runs is a setting.

| Environment | Isolation | What it is |
|---|---|---|
| `Local(path, limits)` | none | A directory of this machine, as it is: no path can be hidden, the network is not limited |
| `Sandboxed(path, limits, sandbox=...)` | sandbox | The directory inside the run's sandbox (`hermit.sandbox.Sandbox`), which decides what the agent reads, writes and reaches |
| `Container(path, limits, settings=...)` | container | A Docker container of the session's own (`hermit/environment/docker.py`: image, network, proxy); a CLI's session runs there |

API: `exec(command, cwd=, env=, timeout=, stdin=) → ExecResult(code, output, timed_out)` runs a command there (a
tool of the agent's, a check) within the environment's limits: its readable mounts in place, its `timeout_s` unless
the call gives a timeout (the command's whole process group is killed when it runs out), and a limit it cannot keep
refused (`Unsupported`: a `Local` one hides no path and limits no network). `upload(source, target)` and
`download(source, target)` move files in and out of the workspace (a link is copied as the link it is),
`path(relative)` names a path of the workspace and refuses one outside it, and `with_limits(limits)` is a copy with
other limits. `placed(path, sandbox)` is the environment an Evolver's own sessions work in: `Sandboxed` when the run's
sandbox is on, `Local` otherwise. The operations follow Harbor's environments (`exec`, `upload`, `download`), so that
a later adapter can give an agent a Harbor environment the same way; that adapter is not in Hermit yet, and a
`Container` runs a CLI's session but no other command yet (its `exec` is not implemented).

## Layer 3: Agent

What the Base Agent is used for. Two kinds:

- The **Acting Agent** answers a task. Its API to the Trainer is `run(task, adapter) → output + trajectory`. It
  places the task and the adapter in the workspace, calls a Base Agent, and reads the answer from the reply. A task
  supplies what is its own as **task functions** named in the configuration: `prepare` (the task's files and
  mounts), `prompt`, `answer` (the output from the last message) and `collect` (the task's artifacts into the
  record). A benchmark whose agent needs more than params is a subclass of a core agent, kept in its example folder:
  a `ReactAgent` subclass (`hermit/agent/acting/react.py`) gives its tools as methods marked `@tool` (their schema
  read from the signature), its environment (`environment_for`), and overrides only the hooks that are its own:
  `system_prompt`, `user_message`, `tools`, `answer`, and the loop's `call_model`, `before_tool`, `after_tool`,
  `stopped`. Changing a prompt or a model is configuration, never a new agent.
- An Acting Agent says what it can take of an adapter, its `supports`: the exports it gives its session (`context`,
  `context_tool`, `tools`, `skills`, `agents`, `hooks`). A CLI takes them all (a deployment connects each where the
  CLI has a place and names the rest in the context); `react` takes the context, the context tool and tools. A method
  says what its adapter needs the acting agent to take (`needs` in its `evolver.yaml`); a run whose agent does not
  take it is refused before it starts, never run with the export silently dropped. What is checked: the built-in
  agents (Harbor's by its acting agent) and a task's own class or function that runs on this machine; a task's class
  that runs only inside its sandbox is not imported to check it, and an agent that declares no `supports` is not
  checked.
- The **Evolver Agent** learns. Its API to the Trainer is `step(ctx)` and `finalize(ctx)`. It reads the acting runs'
  trajectories and writes a new adapter: methods that decide every step in their own code (GEPA, SkillOpt, ReHarness,
  Continual Harness) make single model calls (`Direct`, Layer 1); methods whose learner is an agent call a Base Agent,
  `codex`, `claude`, `pi`, `opencode`, `dsh` or `hermes` (Meta-Harness Adapter, the `codex`, `claude`, `pi`,
  `opencode`, `dsh` and `hermes` Evolvers). With `evolver.base: pi`, a method that decides every step in its own code
  calls Pi as its model ([every method on Pi](../methods/agent.md#every-method-on-pi)). The run's model reaches an
  Evolver as `ctx.agent`, a single model call (`Direct`, Layer 1) metered against the budget; `ask(ctx.agent,
  messages, max_tokens=...)` returns the reply as the `llm` tool does.

What is specific to a task belongs to Layer 3: its environment, rules, turns, scoring, workspace and tools. A
benchmark that defines its own steps keeps its loop in Layer 3 unchanged and makes every model call through Layer
1's `Direct`: the SkillOpt paper's chat loop for OfficeQA calls it in the process
(`examples/officeqa/utils/agent/agent.py`). Code that only takes a base URL, a benchmark's own client in this process
or another, is given a local endpoint instead (`hermit/agent/base/endpoint.py`): each request is one `Direct` call,
relayed to the URL it would have reached with its body, key and reply unchanged (a streamed request is answered as a
stream). CL-Bench's runner and ServeLearnBench's agent reach their model this way, through the launcher's meter as
before. The same endpoint over a CLI Base Agent answers each request with one session of it (the system messages
as its instructions, the rest as its prompt, its reply as the reply): `llm: {cli: claude, model: ..., login: claude,
...}` (or `cli: codex`, `login: chatgpt`, or `cli: pi` with a `<backend>/<model>` reference) makes the run's model one only the CLI reaches, such as a model on a
subscription, for the Evolvers that call `ctx.agent` and the `llm` tool, and `agent.model: {cli: ...}` the same for an
acting pipeline, which sees a local URL. Such a session works in an empty directory of its own, in a bubblewrap
sandbox of its own (`isolation: sandbox`, the default) or a container (`isolation: container`), and is given no tools
(`text_only`): a Claude Code or Pi session can only reply, a Codex session can still run commands inside its sandbox. A chat
call sees no files, so a role that is to read files runs a session itself (`make(...).run(prompt, environment)`). What a
session cannot do is refused, not dropped ([the model contract](../guides/claude-subscription.md#the-model-contract)).
Each call's event stream is kept under the run's `llm_sessions/`.

## Tools

A tool is defined once, in Layer 3, as a `Tool`: a name, a description, an input schema and the function that runs
it. Layer 3 gathers tools from three sources and passes only the allowed ones to `run()`:

| Source | Examples |
|---|---|
| The task | OfficeQA's file tools in direct chat, CL-Bench's game actions |
| The adapter | MWM's `wm`, lean_collab's team tools |
| The Trainer | `check_spec`, `run_agent`, `budget`, and `llm` (the Evolver's model calls, `Direct`, for programs outside the process) |

A tool always runs on the framework's side, with Layer 3's permissions; a declared tool runs in a sandbox of its own
(a template's strict, a task's built from `environment`).
Each Base Agent offers the tools to the model in its own way (and a single model call, `Direct`, which is no agent):

| Base Agent | How the model gets the tools | Who runs a call |
|---|---|---|
| `Direct` (Layer 1, no agent) | Their schemas in the API request | Layer 3, which then calls `Direct` again |
| `react` | Their schemas in the API request | `react`, in its loop |
| `codex`, `claude`, `pi`, `opencode`, `dsh`, `hermes` | The framework's tool server, over MCP (streamable HTTP) | The tool server |
| `mini-swe-agent` | One bash command, `hermit-tool <name> '<JSON arguments>'`, listed in its system message | The tool server, through its JSON entrance |

The tool server (`hermit/agent/base/tool_server.py`) is started for the one call and stopped after it. It carries a
token in its URL, so that only the session it was started for can call it, and tells Codex to approve its tools
without asking (`default_tools_approval_mode = "approve"`). A session in a container reaches it at the container
network's gateway address, a session in the run's sandbox through a forwarded loopback port. It also answers plain
HTTP and JSON (`GET <url>/tools`, `POST <url>/call/<name>`): the Trainer serves its tools this way to an Evolver
written as an external program.

Files an agent runs itself, such as a skill's scripts in the workspace, are adapter content, not tools: they run
inside the agent's sandbox with the agent's permissions.

## Limits

An environment's `limits` state what the agent may touch. Each Base Agent enforces them with the environment's
isolation, and refuses a limit it cannot enforce (`Unsupported`) rather than ignore it. For Codex, Claude Code, Pi,
mini-swe-agent, OpenCode, DeepSeek Harness and Hermes Agent (Pi, mini-swe-agent, OpenCode, DeepSeek Harness and Hermes
Agent run in the run's sandbox or with no isolation, not in a container):

| Limit | In a container | In the run's sandbox | No isolation |
|---|---|---|---|
| `workspace` (read-write) | mounted read-write | as the sandbox shows it | the working directory |
| `readable` (data, at its target; a relative target is in the workspace) | mounted read-only | shown read-only at its target (a bind) | Codex: linked into the workspace (not made read-only); Claude Code, DeepSeek Harness and Hermes Agent: bound there read-only by a nested bubblewrap, linked where bubblewrap cannot nest |
| `hidden` (paths it must not see) | not mounted (refused inside the workspace or the data) | the sandbox is an allowlist: refused when the path lies in what the sandbox or the call shows | refused |
| `network` (hosts besides the model; `None`: not limited) | `[]` only: the egress proxy's allowlist | `[]` only; refused when the network is open or the sandbox lets other hosts through | refused |
| `timeout_s` | the container is removed | the process group is killed | the process group is killed |

A session is stopped the same way when the caller cancels it (the `cancel` setting) or is interrupted. `react`
reads and writes nothing itself and reaches only its model (its tools may use the environment), so `readable`,
`hidden` and `network` hold for it; it refuses `timeout_s` (it cannot stop a model call midway: limit it by
`max_turns`) and roles. Codex's own sandbox mode is a setting: the Acting Agent keeps `workspace-write` (no network for
the commands it runs), and the agent Evolver turns it off inside the run's sandbox, which is its boundary. In a
container Claude Code's own sandbox runs too (`CLAUDE_SANDBOX` in `hermit/agent/base/cli.py`): the commands it runs
have no network and write only the workspace and `/tmp`.
DeepSeek Harness's own sandbox is a setting too (`agent.dsh.permission_mode`, `workspace-write` by default): it limits
what a session writes, not the network, and the agent Evolver turns it off. Hermes Agent has no sandbox of its own: it
never asks for an approval (`--yolo`; its hardline command blocks still apply).

## Settings by layer

Each layer's settings are written apart: `params` is Layer 3's, `base` and the section named after it Layer 2's,
`model` Layer 1's. Acting and learning share the form.

```yaml
agent:
  base: codex                                          # Layer 2
  params:                                              # Layer 3: the Acting Agent with the task's functions
    prompt: ../utils/prompt.md
    prepare: ../utils/task.py:prepare
    answer: ../utils/task.py:answer
  model: {name: gpt-6-luna, login: chatgpt, effort: medium}           # Layer 1
  codex: {web_search: false, timeout_s: 3600}         # Layer 2
environment: {image: "officeqa-codex:0.164.0"}         # where it runs: a container of its own

evolver:
  name: meta_harness_adapter                           # Layer 3
  params: {k: 2, max_rounds: 6}
  base: codex                                          # Layer 2
  model: {ref: fireworks/accounts/fireworks/models/glm-5p3-flash, effort: medium}   # Layer 1
  codex: {web_search: false}
```

`model` is a `<backend>/<model>` reference, or a mapping with `ref` (the reference) or `name` (the CLI's own model
name, with `login`), and `effort` and `price` beside it. With `evolver.base: direct`, `evolver.model` is the run's
`llm`. The form is read into the flat one (`agent.name: codex` with everything in `agent.params`), which is still
accepted; a key given in two layers is refused. On the command line a layer's setting is changed by its own path:
`agent.codex.timeout_s=600`, `agent.model.effort=high`.

## Rules

1. One API per layer, used as a black box. A layer calls only the API of the layer below and never reaches into
   it.
2. What is specific to a task belongs to Layer 3; Layer 2 holds only the framework's agents.
3. A single model call is Layer 1 (`Direct`), not an agent: code that decides every step itself calls it.
4. Settings are per layer and given when the layer is made: changing the model changes only Layer 1's settings,
   changing the agent program only Layer 2's, changing the task or the method only Layer 3's.
5. The workspace is an input. Layer 3 places the adapter's files in it; Codex loads some of them as its own
   extensions (instructions, skills, automatic checks), and how they are used is the Base Agent's business.
6. One trajectory format. A Base Agent returns standard steps, and Evolvers read only those.
7. Where an agent runs is its environment, given with each call; a Base Agent never takes its isolation from its
   settings.
8. A benchmark's own agent is a subclass of a core agent in its example folder, overriding hooks; a loop is never
   copied. An agent declares what it takes of an adapter (`supports`), a method what it needs (`needs`).

## Where the tasks and methods fit

| Task or method | Layer 3 | Layer 2 |
|---|---|---|
| OfficeQA in Codex, every method | Acting Agent with OfficeQA's task functions (`utils/task.py`) | `codex` |
| OfficeQA in direct chat | Acting Agent running SkillOpt's chat loop | none: `Direct` (Layer 1), per turn |
| GEPA, SkillOpt, ReHarness, Continual Harness, OPRO | Evolver Agent | none: `Direct` (Layer 1, `ctx.agent`); OPRO in OfficeQA: `codex` through `llm: {cli: codex}`; with `evolver.base: pi`, `pi` through `llm: {cli: pi}` |
| Meta-Harness Adapter, ASPIRE, `codex` and `claude` Evolvers | Evolver Agent | `codex`, `claude`, `pi` or `opencode` |
| `pi` Evolver | Evolver Agent | `pi` |
| `opencode` Evolver | Evolver Agent | `opencode` |
| `dsh` Evolver | Evolver Agent | `dsh` |
| `hermes` Evolver | Evolver Agent | `hermes` |
| RRSI Adapter | Evolver Agent: upstream's rules and roles, the proposer's prompt | `codex`, `claude` or `pi` for the proposer, and the same, one call per upstream `generate`, for the analyst, digesters and critic |
| AHE Adapter | Evolver Agent: the Evolve Agent and the Agent Debugger | `codex`, `claude` or `pi`, one session per step and per task |
| CL-Bench, ServeLearnBench | Acting Agent running the benchmark's loop | none: `Direct` (Layer 1), per call, through the local endpoint |
| MWM | Evolver Agent | `codex`, `claude` or `pi`, in a view of the run's sandbox with MWM's native mounts (`/w`, `/evidence`, ...) |
| Harness² | Evolver Agent: upstream's proposer | `claude`, one call per upstream session (or `codex` or `pi`) |
| SHE | Evolver Agent: upstream's debugger and Evolve Agent | `Direct` (Layer 1, `ctx.agent`), or `codex`, `claude` or `pi`, one session per call |

## Outside the layers

- Learning a Base Agent itself: full Meta-Harness rewrites the agent's own loop; here only the adapter is learned.
- Methods that need a model's internals, such as log-probabilities, several samples per call or training the
  weights: Base Agents such as Codex do not expose them.

## From Hermit 0.1

The layers moved `hermit/backends.py`, `hermit/llm.py`, `hermit/trajectory.py`, `hermit/agents/` and
`hermit/evolver/` into `hermit/agent/`, the learning methods from `hermit/algorithms/evolvers/` to
`hermit/agent/evolver/methods/`, and the adapter templates from `hermit/algorithms/templates/` to
`hermit/adapter/templates/`. The old import paths (`hermit.backends`, `hermit.llm`, `hermit.trajectory`,
`hermit.agents`, `hermit.evolver`) forward to the new ones for one release and warn when used; `hermit.algorithms`
is gone, and so are `hermit.tools_mcp` and the tool gate `hermit.agent.evolver.gate` (an agent Evolver's tools are
served over MCP and JSON by the call's tool server).
Configurations that name an agent, an Evolver or a template (`agent.name: codex`, `evolver.name: gepa`,
`template: prompt-only@1.0`) are unchanged.

## From the one-agent refactor

| Before | Now |
|---|---|
| `agent.run(prompt, workspace=path, limits=Limits(...))` | `agent.run(prompt, environment=Local(path, Limits(...)))` (a path alone is a `Local`) |
| `isolation`, `sandbox`, `container` in a Base Agent's settings | the environment: `Sandboxed(path, sandbox=box)`, `Container(path, settings={...})`; given as settings, refused |
| `make("direct", settings, model)`, `hermit.agent.base.direct` | `Direct(settings, model)`, `hermit.agent.model.direct` (Layer 1); `make("direct")` is refused; the old module path forwards for one release |
| `hermit.agent.base.container` | `hermit.environment.docker` (the old path forwards for one release) |
| `Mount`, `Limits` from `hermit.agent.base.interface` | `hermit.environment` (still importable from the interface) |
| a benchmark's own copy of the tool loop | a `ReactAgent` subclass with `@tool` methods and hooks |
| `@tool(description, parameters)` | also `@tool` alone: the description from the docstring, the schema from the signature |
