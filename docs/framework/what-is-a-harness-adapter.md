# What is a harness adapter?

A harness adapter is **a plugin for an AI agent that a program, not a person, keeps improving.** It is a directory
of files. At the consumption end it behaves like an agent plugin: context, skills, roles and tools that a host
installs. At the training end it carries what a plugin never has: a specification of which files may change, a
checker, and a Git history of every version a learner produced.

```
adapter/                      one directory, one manifest
│
├── consumption end           what an acting agent receives — the shape of a plugin
│   ├── ADAPTER.md            context
│   ├── skills/               skills
│   ├── agents/               roles (subagents)
│   └── tools.py  + MCP entry callable tools
│       └── optional: a standalone service behind those tools
│
└── training end              what a learner works within — absent from any plugin
    ├── adapter.yaml          structure (learnable / fixed files), check, evolver_tools
    ├── EVOLVER.md            how to edit within that structure
    └── the adapter tree      one Git commit per accepted step, origin and CARD.md
```

## The consumption end: a plugin definition, plus an optional service

### The integration protocol is a plugin's

Everything an adapter exports has a counterpart in the plugin formats of today's agents, and the mapping is
mechanical:

| Adapter export | Plugin concept | Codex / Claude Code (`deploy`) |
|---|---|---|
| `ADAPTER.md` | instructions | managed block in `AGENTS.md` / `CLAUDE.md` |
| `skills/<name>/SKILL.md` | skills | copied under `.codex/skills/` / `.claude/skills/` |
| `agents/<role>.md` | subagents | `.codex/agents/*.toml` / `.claude/agents/*.md` |
| `tools` in `adapter.yaml` | an MCP server entry | `.codex/config.toml` / `.mcp.json`, pointing at the framework's wrapper |

A host that can read an instruction file and start an MCP server can use an adapter; it does not need the
framework's Trainer, Evolver, or Git tree. What a given host *cannot* carry (Codex and Pi take no instructions
from a plugin, Hermes caps a plugin's prompt section) is a limit of that host's format, not of the adapter: the
adapter's manifest is the lossless form, and each host gets a projection of it.

### Tools run the adapter's own code

A plugin's MCP entry may point anywhere. An adapter's tool entry points at Python inside the adapter directory,
run by the framework's MCP wrapper (one process per host session, started and stopped by the host). The host sees
an ordinary MCP server; the tool schemas in `adapter.yaml` are what it is offered. This is why a consumer of an
adapter with tools needs `harnessadapter` installed, while a context-only adapter needs nothing.

### When a standalone service is needed

Some adapters have state that no single host session can hold: a shared plan and lemma library that five agents
edit at once, calls that take minutes and must not block each other, decisions to interrupt or resume an agent.
For these the adapter brings its **own service**: one process per run, started before the agents, serving each
agent over its own socket, alive until the run ends (`harnessadapter.runtime.service`; the Lean collaboration
recipe is the example).

This is the one conceptual difference from a plugin at the consumption end. A plugin can *name* a running
server, and a host can start a per-session process for it, but a plugin cannot own a lifecycle: nothing in a
plugin starts a service before the first agent, shares it between agents, or stops it after the last one. An
adapter can, because the service is part of the adapter.

To the integration protocol the service changes nothing: the tool entry simply points at the service instead of
at a per-session wrapper. So:

```
adapter (consumption end)  =  plugin definition  +  optional standalone service with its launcher
```

Two things stay outside this equation and need the host's cooperation: context that is re-rendered every call
(a plugin's instructions are fixed at install time; a host would have to fetch them through a tool), and acting
on the agent from outside (a service can decide to interrupt an agent; only a host with a control channel can do
it).

### Two ways to use an adapter

An adapter reaches an agent either **through a project** or **through the agent itself**.

**Into a project: `deploy`**

```bash
harnessadapter deploy --adapter DIR --output WS --host codex      # or claude-code
cd WS && codex                                                     # or claude
```

- Output: a project folder. The context goes into `AGENTS.md` / `CLAUDE.md` inside a managed block, skills and
  roles under `.codex/` or `.claude/`, and an MCP entry that starts the wrapper
  (`python -m harnessadapter.runtime.mcp_server --adapter DIR`) with this machine's absolute paths.
- Scope: the adapter is active only when the agent runs in that folder; nothing global changes.
- Carries everything the adapter exports, for both supported hosts.
- Re-running it on the same folder replaces the managed block and the config; the project's own files are kept.

**Into the agent: `convert`** (in review)

```bash
harnessadapter convert --adapter DIR --output OUT --target claude-code   # or codex | pi | hermes
# then install OUT with the host's own plugin mechanism
```

- Output: a plugin package in the host's native format, self-contained (a copy of the adapter inside), with no
  machine-specific paths; its MCP entry names `harnessadapter-mcp` on the PATH.
- Scope: installed once, active in every project the agent opens.
- Carries only what the host's plugin format has a slot for. Otherwise it refuses (exit 2, naming the parts) or,
  with `--allow-lossy`, drops them and lists them as `omitted`. Claude Code takes everything; Codex and Pi refuse
  the context and the roles; Hermes refuses the roles and caps the context.

Common to both: the tools are the adapter's own Python, served by a framework process the host starts per
session, so the consumer's machine needs `harnessadapter` installed. These two commands are the whole
consumption interface; whether the process behind the tools is a per-session server or a relay to a standalone
service is the adapter's business, decided by the proposed `serve` section of its manifest, and costs the user
no extra step.

## The training end: what a plugin never has

A plugin is finished when its author ships it. An adapter is produced by a loop: the Trainer runs the agent with
the current adapter, collects what happened, lets an Evolver edit a working copy, checks the result against the
template's specification, and commits it. The manifest carries the parts that make this possible:

| Section of `adapter.yaml` | Role in training |
|---|---|
| `structure.learnable` / `fixed` | which files the learner may change, in what format and size; which must stay as the template ships them |
| `check` | the template's own checker, run after the built-in checks |
| `evolver_tools` | tools the learner (not the agent) gets during training |
| `origin` | the template id and version the tree started from, written by the Trainer |

Beside the manifest: `EVOLVER.md`, the learner's guide to the editable structure (never shown to the acting
agent); `CARD.md`, provenance and evaluation results written at the end of a run; and the **adapter tree**, the
Git repository in which every accepted version is a commit and every run a branch, so any version can be
deployed, compared, or resumed from.

The consumption end never reads these. `deploy` and `convert` take `template` and `exports` and leave the rest
behind, which is exactly the difference between an adapter and the plugin made from it.

## One table

| | Agent plugin | Harness adapter |
|---|---|---|
| Form | files in one host's format | one directory, host-neutral manifest, projected to each host |
| Context, skills, roles, tools | yes | yes, the same protocol |
| Tool code | any server the entry names | the adapter's own, run by the framework wrapper or the adapter's service |
| Standalone service with its own lifecycle | can only point at one | can bring and launch one |
| Produced by | a person | a Trainer and an Evolver, from task feedback |
| Specification of what may change | none | `structure`, `check` |
| History | the author's releases | one commit per accepted step |

## Where this stands

- `deploy` (project workspace) is implemented for Codex and Claude Code; `convert` (native plugin package for
  Claude Code, Codex, Pi, Hermes) is in review.
- The standalone service exists and serves the Lean collaboration recipe; a general `serve` command that starts it
  for any adapter and points the plugin's tool entries at it does not exist yet. The direction is settled and
  written up as a design in the [adapter.yaml reference](adapter-manifest.md#serve-design-not-implemented): the
  manifest states the MCP tools, the adapter starts a healthy MCP server for them, and a host only needs the
  start command and the health rule.
- How an adapter whose context is rendered per call is consumed outside a custom pipeline is not decided.

Next: [Using and training adapters](adapter-lifecycle.md) for the files and handoffs, [Adapter](adapter.md) for the
acting and evolution interfaces, [adapter.yaml reference](adapter-manifest.md) for the manifest (start with its
[Customizing an adapter](adapter-manifest.md#customizing-an-adapter) section).
