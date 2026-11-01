# Agent layers

Every agent in Hermit, the one that answers a task and the one that learns an adapter alike, is built from three
layers. Each layer uses the one below it through a single API and treats it as a black box.

```text
Layer 3  Agent         Acting Agent: answers a task        Evolver Agent: learns a new adapter
            │  run(prompt, workspace, tools, limits, session, roles) → reply + trajectory + usage + session
Layer 2  Base Agent    direct · react · codex · claude
            │  chat(messages, **request) → message + usage
Layer 1  Model         a model and how to reach it
```

The code follows the layers:

```text
hermit/agent/
├── model/      Layer 1: model references, backends, the chat client
├── base/       Layer 2: the Base Agent API and its implementations, tools, the tool server, isolation, login
├── acting/     Layer 3: the Acting Agent API and the built-in Acting Agents
└── evolver/    Layer 3: the Evolver API and the agent Evolver
    └── methods/    the learning methods (gepa, skillopt, meta_harness_adapter, ...)
```

Imports go one way only: Layer 3 imports Layer 2, Layer 2 imports Layer 1, and never the reverse
(`tests/test_layers.py` checks it). The rest of `hermit/` (the Trainer, adapters and their templates, `hermit
deploy`, the command line) uses the layers and is not part of them; Layer 2 isolates a CLI with the framework's
sandbox (`hermit/sandbox.py`).

## Layer 1: Model

A model and the way to reach it: a `<backend>/<model>` reference (see [Model backends](../guides/model-backends.md))
or a model name with a login (`login: chatgpt`), the reasoning effort and the price.

API: `chat(messages, **request) → (message, usage)`, the chat-completions call of `hermit.agent.model.llm.ChatClient`
(`request` holds the sampling and the tools). The Codex and Claude Code CLIs reach their model themselves; for them
Layer 1 is the settings they are given (the binding, the login, the effort).

## Layer 2: Base Agent

How a model takes steps. Layer 2 holds only the framework's agents:

| Base Agent | What it does |
|---|---|
| `direct` | One model call. Tools in the request come back as tool calls; direct never runs them |
| `react` | The framework's tool loop: call the model, run the tools it asks for, call it again |
| `codex` | The Codex CLI, working in a workspace |
| `claude` | The Claude Code CLI, working in a workspace |

API, the only one Layer 3 sees:

```text
agent = make("codex", settings, model)        # hermit.agent.base.interface
agent.run(prompt, workspace=None, tools=(), limits=Limits(), session=None, roles=())
    → Result(reply, trajectory, usage, session, tool_calls, finish_reason, exit_code, timed_out, log, ...)
agent.configured(**settings)                  # a copy with some settings changed, for one call
```

- `prompt`: what to do.
- `workspace`: the directory the agent works in, read-write.
- `tools`: what the agent may call (see [Tools](#tools)).
- `limits`: what it may read, what it must not see, where it may connect, how long it may run (see
  [Limits](#limits)).
- `session`: the session a previous call returned, to continue the same agent: turn by turn in an interactive
  environment, step by step in a resumed learning session. Codex and Claude Code resume their thread; direct and
  react carry the message history.
- `roles`: named sub-agents (name, description, instructions) the agent may delegate to: Codex's roles, Claude
  Code's agents. direct and react refuse them.
- `trajectory`: the run as standard steps: messages, reasoning, commands with their exit codes and output, tool
  calls, file changes, errors. Layer 3 reads only these; a CLI's raw log (`agent-codex.jsonl`) is kept beside them
  as an archive.

A Base Agent's settings are given when it is made: for `direct` and `react` the sampling and the loop's limits; for
Codex and Claude Code web search (set explicitly, on or off), the isolation (a container, the run's sandbox, or
none), Codex's own sandbox mode, the CLI's home, the time limit (the full list is in `hermit/agent/base/cli.py`).
`configured(...)` changes some of them for one call (a method's `max_tokens`, the home and log of one task) and
leaves the agent as it was. Running a task again when the provider could not serve it is Layer 3's: the workspace
is placed anew first.

## Layer 3: Agent

What the Base Agent is used for. Two kinds:

- The **Acting Agent** answers a task. Its API to the Trainer is `run(task, adapter) → output + trajectory`. It
  places the task and the adapter in the workspace, calls a Base Agent, and reads the answer from the reply. A task
  supplies what is its own as **task functions** named in the configuration: `prepare` (the task's files and
  mounts), `prompt`, `answer` (the output from the last message) and `collect` (the task's artifacts into the
  record).
- The **Evolver Agent** learns. Its API to the Trainer is `step(ctx)` and `finalize(ctx)`. It reads the acting
  runs' trajectories and writes a new adapter, calling a Base Agent: `direct` for methods that decide every step in
  their own code (GEPA, SkillOpt, ReHarness, Continual Harness), `codex` or `claude` for methods whose learner is an
  agent (Meta-Harness Adapter, the `codex` and `claude` Evolvers). The run's model reaches an Evolver as
  `ctx.agent`, a `direct` Base Agent metered against the budget; `ask(ctx.agent, messages, max_tokens=...)` returns
  the reply as the `llm` tool does.

What is specific to a task belongs to Layer 3: its environment, rules, turns, scoring, workspace and tools. A
benchmark that defines its own steps keeps its loop in Layer 3 unchanged and makes every model call through Layer
2's `direct`: the SkillOpt paper's chat loop for OfficeQA calls it in the process
(`examples/officeqa/utils/agent/agent.py`). Code that only takes a base URL, a benchmark's own client in this process
or another, is given a local endpoint instead (`hermit/agent/base/endpoint.py`): each request is one `direct` call,
relayed to the URL it would have reached with its body, key and reply unchanged (a streamed request is answered as a
stream). CL-Bench's runner and ServeLearnBench's agent reach their model this way, through the launcher's meter as
before.

## Tools

A tool is defined once, in Layer 3, as a `Tool`: a name, a description, an input schema and the function that runs
it. Layer 3 gathers tools from three sources and passes only the allowed ones to `run()`:

| Source | Examples |
|---|---|
| The task | OfficeQA's file tools in direct chat, CL-Bench's game actions |
| The adapter | MWM's `wm`, lean_collab's team tools |
| The Trainer | `check_spec`, `run_agent`, `budget`, and `llm` (the Evolver's `direct`, for programs outside the process) |

A tool always runs on the framework's side, with Layer 3's permissions; an adapter's tools run in the tool sandbox.
Each Base Agent offers the tools to the model in its own way:

| Base Agent | How the model gets the tools | Who runs a call |
|---|---|---|
| `direct` | Their schemas in the API request | Layer 3, which then calls `direct` again |
| `react` | Their schemas in the API request | `react`, in its loop |
| `codex`, `claude` | The framework's tool server, over MCP (streamable HTTP) | The tool server |

The tool server (`hermit/agent/base/tool_server.py`) is started for the one call and stopped after it. It carries a
token in its URL, so that only the session it was started for can call it, and tells Codex to approve its tools
without asking (`default_tools_approval_mode = "approve"`). A session in a container reaches it at the container
network's gateway address, a session in the run's sandbox through a forwarded loopback port. It also answers plain
HTTP and JSON (`GET <url>/tools`, `POST <url>/call/<name>`): the Trainer serves its tools this way to an Evolver
written as an external program.

Files an agent runs itself, such as a skill's scripts in the workspace, are adapter content, not tools: they run
inside the agent's sandbox with the agent's permissions.

## Limits

`limits` states what the agent may touch. Each Base Agent enforces it with its own isolation, and refuses a limit it
cannot enforce (`Unsupported`) rather than ignore it. For Codex and Claude Code:

| Limit | In a container | In the run's sandbox | No isolation |
|---|---|---|---|
| `workspace` (read-write) | mounted read-write | as the sandbox shows it | the working directory |
| `readable` (data, at its target; a relative target is in the workspace) | mounted read-only | granted read-only for the call, at its own path only (another target is refused); kept visible past `hidden` | linked into the workspace (not made read-only) |
| `hidden` (paths it must not see) | not mounted (refused inside the workspace or the data) | covered; refused when the sandbox does not isolate the call (off, or no bubblewrap) | refused |
| `network` (hosts besides the model; `None`: not limited) | `[]` only: the egress proxy's allowlist | `[]` only; refused when the network is open, `sandbox.allow` lets other hosts through, or the sandbox does not isolate the call | refused |
| `timeout_s` | the container is removed | the process group is killed | the process group is killed |

A session is stopped the same way when the caller cancels it (the `cancel` setting) or is interrupted. `direct` and
`react` read and write nothing and reach only their model, so `readable`, `hidden` and `network` hold for them; they
refuse `timeout_s` (they cannot stop a model call midway: the client's own timeout applies), a workspace and roles. Codex's own sandbox mode is a setting: the Acting Agent keeps `workspace-write` (no network for
the commands it runs), and the agent Evolver turns it off inside the run's sandbox, which is its boundary.

## Settings by layer

Each layer's settings are written apart: `params` is Layer 3's, `base` and the section named after it Layer 2's,
`model` Layer 1's. Acting and learning share the form.

```yaml
agent:
  base: codex                                          # Layer 2
  params:                                              # Layer 3: the Acting Agent with the task's functions
    deploy: tree
    prompt: ../utils/prompt.md
    prepare: ../utils/task.py:prepare
    answer: ../utils/task.py:answer
  model: {name: gpt-6-luna, login: chatgpt, effort: medium}           # Layer 1
  codex: {web_search: false, isolation: container, timeout_s: 3600}  # Layer 2

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
3. Layer 2 is always present. When no agent program is needed, `direct` passes the input to the model and returns
   its reply.
4. Settings are per layer and given when the layer is made: changing the model changes only Layer 1's settings,
   changing the agent program only Layer 2's, changing the task or the method only Layer 3's.
5. The workspace is an input. Layer 3 places the adapter's files in it; Codex loads some of them as its own
   extensions (instructions, skills, automatic checks), and how they are used is the Base Agent's business.
6. One trajectory format. A Base Agent returns standard steps, and Evolvers read only those.

## Where the tasks and methods fit

| Task or method | Layer 3 | Layer 2 |
|---|---|---|
| OfficeQA in Codex, every method | Acting Agent with OfficeQA's task functions (`utils/task.py`) | `codex` |
| OfficeQA in direct chat | Acting Agent running SkillOpt's chat loop | `direct`, per turn |
| GEPA, SkillOpt, ReHarness, Continual Harness | Evolver Agent | `direct` (`ctx.agent`) |
| Meta-Harness Adapter, `codex` and `claude` Evolvers | Evolver Agent | `codex` or `claude` |

| CL-Bench, ServeLearnBench | Acting Agent running the benchmark's loop | `direct`, per call, through the local endpoint |
| MWM | Evolver Agent | `codex`, in MWM's native mounts (`NativeMounts` as the sandbox, told `/w`) |
| Harness² | Evolver Agent: upstream's proposer | `claude`, one call per upstream session |

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
