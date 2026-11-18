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
| Risky settings | The warnings listed in [Configuration](configuration.md#sandboxes) |

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
hermit deploy --adapter DIR --workspace WORKSPACE --host opencode
hermit deploy --adapter DIR --workspace WORKSPACE --host dsh
hermit deploy --adapter DIR --workspace WORKSPACE --host hermes
```

This generates host instruction files, role/skill files, and tool configuration. It does not launch the acting
agent. Pi has no subagents and reads no hook file, so `--host pi` names an adapter's roles and hooks in `AGENTS.md`
instead; its `.pi/` files (tools, skills) load once the user trusts the project in Pi, while `AGENTS.md` needs no
trust. mini-swe-agent reads one config file, so `--host mini-swe-agent` writes `mini-swe-agent.yaml`: Hermit's
prompts over mini-swe-agent's `mini` config, with the context, the names of the adapter's skills, roles, hooks and
other files, and its tools as one bash command (`hermit-tool`) in the system template; run `mini -c
mini-swe-agent.yaml` from the workspace ([tutorial](../tutorials/mini-swe-agent.md)).
`--host opencode` writes `AGENTS.md`, the project's `opencode.json` (the tools, offered directly) and
`.opencode/skills`, which OpenCode loads with no trust step; it names roles and hooks in `AGENTS.md`, as for Pi.
OpenCode 2.2 waits at most 2 s for the tools' server it starts, which takes about 3 s: an `opencode run` given its
prompt at once may send the first request without the adapter's tools (the `opencode` acting agent serves them itself).
DeepSeek Harness has no file of its own for roles, hooks or a project's MCP servers, so `--host dsh` names an adapter's
roles and hooks in `AGENTS.md` and writes its tools into a patch layer (an overlay of settings for a session),
`.dsh/hermit.patch.yml`, which also names the folder of its skills (`.dsh/skills`). The layer loads only when named,
while `AGENTS.md` needs no flag: run `dsh --profile headless --patch .dsh/hermit.patch.yml "TASK"` from the workspace.
Hermes Agent reads one project context file, the first of `.hermes.md`, `AGENTS.md`, `CLAUDE.md` and `.cursorrules`,
so `--host hermes` writes the context to `.hermes.md`, the skills to `.hermes/skills/`, and names roles and hooks in
`.hermes.md`. Hermes has no project file for MCP servers or skill folders, only its home's `config.yaml`, which the
deployment never changes: `.hermes/hermit.config.yaml` is the part of it to merge into `~/.hermes/config.yaml`
(`mcp_servers.<adapter id>`, a stdio server running `python -m hermit.runtime.mcp_server --adapter ...` with a 120 s
timeout; `skills.external_dirs`, the absolute `.hermes/skills`; `context_file_max_chars: 1000000`, the context whole).
Then run `hermes` from the workspace. Hermes scans each context file for prompt injection and replaces one it
matches with a `[BLOCKED: ...]` notice, telling only the model; benign rules can match ("never cat the .env file").
The `hermes` acting agent fails such a session, naming the file and what the scan matched.
`--context-limit BYTES` (Codex) is the `project_doc_max_bytes` your Codex configuration sets, 32768 (Codex's
default) when not given: a longer `AGENTS.md` is refused, since Codex would cut it; `0` skips the check. It is the per-project way to use an adapter; the per-agent way, `convert`, and how the two compare are in
[What is a harness adapter?](what-is-a-harness-adapter.md#two-ways-to-use-an-adapter). The workspace must be an empty directory or one previously generated for the same host, separate from
the adapter directory. See the [file mapping](adapter.md#acting-interface) for exact paths and update behavior.

:::{warning}
**The patch layer is not protected from the agent.** DeepSeek Harness's sandbox leaves `.dsh/hermit.patch.yml` writable,
like any file of the workspace: a session started by hand can edit it, and a stdio tool server it adds there runs
outside that sandbox in the next session started with `--patch`. Start such a session from a fresh deployment (a new
folder), or read the layer first.
:::

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
| Hooks | refused | refused | refused | refused |
| The adapter's folder and its other files | named by a `SessionStart` hook | not named: listed under `unnamed_files` | not named: listed under `unnamed_files` | not named: listed under `unnamed_files` |

A part the target's plugin format cannot carry is refused: `convert` exits 2 and names every such part before writing
anything. A non-empty context (`ADAPTER.md`) makes Codex and Pi refuse, and a context over 4,000 characters makes
Hermes refuse. Hooks make every target refuse: no plugin carries them yet, and `deploy --host codex` or
`--host claude-code` connects them in a workspace. `--allow-lossy` emits the plugin without those parts and lists them
under `omitted` in the result.

In training and in a `deploy` workspace, the agent's context says where the adapter's copy is (paths in its
instructions, skills and roles are relative to that folder) and lists the files no export connects, such as a
README or a tool's source. A plugin carries those files under `adapter/`, but an output style, a skill folder or a
system prompt section cannot say where the plugin is installed. A Claude Code plugin says it with a `SessionStart`
hook: it prints the installed folder and the same list (`hermit-files.md`), and Claude Code adds a SessionStart hook's
output to the context (the hook runs under `sh`, Git Bash on Windows). Codex, Pi and Hermes have no such place, so their
result lists the files under `unnamed_files` and the plugin's README names them; nothing is refused for them, since
the plugin still holds the files.

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

- `check` also verifies that every declared sandbox path exists (`environment.read`, `judge.tests`,
  `evolver_access.environment`) and that no declared path holds the data, the judge or the runs. It does not check
  the pipeline's host: adapters are meant to be usable by any acting agent, and each pipeline deploys the exports
  (context, tools, roles, skills) in its own way.
- `hermit deploy --adapter DIR --workspace WORKSPACE --host codex|claude-code|pi|mini-swe-agent|opencode|dsh|hermes`
  copies an adapter to `hermit/<id>/` in a host workspace and connects its exports there (context block, MCP tools,
  native roles, skills, hooks).
- `hermit convert --adapter DIR --output OUT --target ...` emits a plugin in the host's native format instead,
  and exits 2 naming every part the target's plugin cannot carry.
