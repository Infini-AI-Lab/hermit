# Commands

| Command | Purpose |
|---|---|
| `hermit check --config run.yaml` | Check the settings before training |
| `hermit train --config run.yaml` | Run the Trainer |
| `hermit eval --config run.yaml --adapter ...` | Evaluate one adapter on the test split |
| `hermit deploy --adapter DIR --workspace WORKSPACE --host codex` | Generate a host workspace from an adapter |
| `hermit convert --adapter DIR --output OUT --target claude-code` | Emit a plugin in a host's native format |
| `hermit serve DIR` | Serve adapter tools over stdio, normally started by a host |

The first three accept `key=value` overrides after the config (see [Configuration](configuration.md)).

## Typical workflow

```bash
hermit check --config run.yaml                      # optional: catch setting mistakes before spending
hermit train --config run.yaml                      # train; resumes automatically after a crash
hermit eval  --config run.yaml --adapter final      # evaluate the final selection
hermit eval  --config run.yaml --adapter none       # optional: the agent without any adapter
```

## check

`check` is a separate command, outside the run. The user runs it when they want and starts training once satisfied;
`train` does not run it for them. The probe calls the pipeline and can incur model costs. Pass `--no-probe` to skip it.

| What `check` looks at | Example of a problem it finds |
|---|---|
| Template | The starting adapter fails `check_spec` |
| Evolver | The class does not load; a need is not given; resume is requested but not supported |
| Task | `task-info/README.md` missing; splits overlap; the judge cannot score a sample task |
| Budgets | No hard budget or no iteration budget |
| Pipeline probe | One training task without an adapter; score, time, reported API speed, and cost are printed |
| Risky settings | The warnings listed in [Configuration](configuration.md#sandbox-network-and-warnings) |

`check` prints a disclaimer: it checks only these basic items. Passing `check` does not guarantee that the settings are
free of problems or that the run will succeed.

## train

Runs the loop described in [Trainer](trainer.md). At start it loads and validates the configured components and
checks the Evolver's declared needs (see [Configuration](configuration.md#what-is-checked-at-startup)). It does not run
the full standalone `check` command. If a run with
the same `run_name` is unfinished, `resume_mode: auto` continues it.

## eval

```bash
hermit eval --config run.yaml --adapter final         # a run's final adapter
hermit eval --config run.yaml --adapter <tag|commit|path>
hermit eval --config run.yaml --adapter root          # the template's initial content
hermit eval --config run.yaml --adapter none          # no harness adapter: a pure baseline
```

| Item | Rule |
|---|---|
| Data | The test split only |
| Deployment and scoring | The same pipeline and judge as the training rollouts |
| Repeats | `eval.repeats`; reports the mean and the share of tasks solved at least once |
| Records | Per-task score, cost, time and API speed under `<run>/eval/<adapter>/`; the summary counts runs that errored |
| `.hermit/CARD.md` | With the literal `--adapter final` selector, the results are written into the run's final card |

`eval` evaluates exactly what it is given and runs no baseline by itself. A baseline is one more `eval`, run when the
user chooses: `--adapter none` runs the acting agent with no harness adapter at all (the pipeline receives
`adapter=None`); `--adapter root` runs the template's initial content, which may already deploy tools or context.

## deploy

```bash
hermit deploy --adapter DIR --workspace WORKSPACE --host codex
hermit deploy --adapter DIR --workspace WORKSPACE --host claude-code
hermit deploy --adapter DIR --workspace WORKSPACE --host pi
hermit deploy --adapter DIR --workspace WORKSPACE --host mini-swe-agent
```

This generates host instruction files, role/skill files, and tool configuration. It does not launch the acting
agent. Pi has no subagents, so `--host pi` refuses an adapter that exports roles; its `.pi/` files (tools, skills)
load once the user trusts the project in Pi, while `AGENTS.md` needs no trust. `--host mini-swe-agent` writes one
`mini-swe-agent.yaml` with the context in its system template, the tools as the bash command `hermit-tool` and the
skills under `.hermit/skills/`, and refuses roles too ([mini-swe-agent](../tutorials/mini-swe-agent.md)). It is the per-project way to use an adapter; the per-agent way, `convert`, and how the two compare are in
[What is a harness adapter?](what-is-a-harness-adapter.md#two-ways-to-use-an-adapter). The workspace must be an empty directory or one previously generated for the same host, separate from
the adapter directory. See the [file mapping](adapter.md#acting-interface) for exact paths and update behavior.

## convert

```bash
hermit convert --adapter DIR --output OUT --target claude-code
hermit convert --adapter DIR --output OUT --target codex
hermit convert --adapter DIR --output OUT --target pi
hermit convert --adapter DIR --output OUT --target hermes
```

`deploy` prepares a workspace you work in; `convert` emits a plugin in the host's native format, installed once and
available in every project. The adapter is copied into the plugin, and its MCP entry runs `hermit serve` on
that copy, so `hermit` must be installed and on PATH wherever the plugin is used. Nothing in the plugin
names the machine that produced it.

### What each host's plugin carries

| Adapter part | Claude Code | Codex | Pi | Hermes |
|---|---|---|---|---|
| Context (`ADAPTER.md`) | output style, forced while enabled | refused | refused | system prompt section, up to 4,000 characters |
| Skills | `skills/` | `skills/` | `skills/` | `ctx.register_skill` |
| Tools | `.mcp.json` | `mcp.json` | generated extension, `pi.registerMcpServer` | `ctx.register_tool`, forwarding to `hermit serve` |
| Roles | `agents/` | refused | refused | refused |

A part the target's plugin format cannot carry is refused: `convert` exits 2 and names every such part before writing
anything. A non-empty context (`ADAPTER.md`) makes Codex and Pi refuse, and a context over 4,000 characters makes
Hermes refuse. `--allow-lossy` emits the plugin without those parts and lists them under
`omitted` in the result.

Why the refusals:

- **Codex** reads no instruction file and no subagent from a plugin. Its plugin format is Agent Plugins v1: root
  `plugin.json`, `skills/`, `mcp.json`. (The `.codex-plugin/plugin.json` layout is a compatibility fallback that does
  not expand `${PLUGIN_ROOT}`.) `deploy --host codex` does register roles, in a workspace.
- **Pi** builds its system prompt from context files in the agent and working directories, never from a package, and
  has no built-in subagents.
- **Hermes** drops a plugin's system prompt section over 4,000 characters with only a log warning, and has no
  file-defined subagents. It also drops a section that would take all plugins' sections past 8,000 characters
  together, which depends on the user's other plugins and is not checked here.

### Install

| Target | Output | Install |
|---|---|---|
| Claude Code | a marketplace with one plugin | `claude plugin marketplace add OUT`, then `claude plugin install ID@ID-adapters` |
| Codex | a marketplace (`.agents/plugins/marketplace.json`) with one plugin | `codex plugin marketplace add OUT`, then `codex plugin add ID@ID-adapters` |
| Pi | a package | `pi install OUT` |
| Hermes | a plugin directory | copy OUT to `~/.hermes/plugins/ID`, then `hermes plugins enable ID` |

The tool-call audit log goes to each host's per-plugin data directory (`${CLAUDE_PLUGIN_DATA}`, `${PLUGIN_DATA}`, the
Hermes plugin data directory); Pi has none, so it goes to the package directory.

### Checking the result

`claude mcp list` and `codex mcp list` show whether the tool server starts. `hermes plugins doctor OUT` runs Hermes'
discovery, manifest parsing and `register(ctx)` without installing.

`hermit.json` next to the embedded adapter records the adapter's id and version and the framework version
that made the plugin; `hermit serve` warns on stderr when the installed framework is another version.
Converting again into the same output replaces the generated parts, so a skill, a role or a context the adapter no
longer has does not survive from the earlier plugin; the host's audit log there is kept.

## serve

```bash
hermit serve DIR
hermit serve DIR --audit-log tool-calls.jsonl
```

Starts the implemented per-session MCP tool server on stdio. Generated plugin entries normally run it for the host.
It is not an HTTP preview server or the proposed standalone-service launcher. `--standalone` and `--serve-url` are not supported.

## Other command options

`deploy` and `convert` accept `--python` to choose the Python executable used for tools; it defaults to the running interpreter.
`deploy --output` is an alias for `--workspace`. `convert --allow-lossy` allows explicitly reported omitted exports.
`check --no-probe` skips only the acting-task probe. Use `hermit COMMAND --help` for argument syntax.

## Implementation notes

- `check` also verifies that every `sandbox.read` path exists. It does not check the pipeline's host: adapters are meant to
  be usable by any acting agent, and each pipeline deploys the exports (context, tools, roles, skills) in its own way.
- `hermit deploy --adapter DIR --workspace WORKSPACE --host codex|claude-code|pi|mini-swe-agent` deploys an adapter's exports into
  a host workspace (context block, MCP tools, native roles, skills).
- `hermit convert --adapter DIR --output OUT --target ...` emits a plugin in the host's native format instead,
  and exits 2 naming every part the target's plugin cannot carry.
