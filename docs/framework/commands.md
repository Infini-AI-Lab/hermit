# Commands

| Command | Purpose |
|---|---|
| `harnessadapter check --config run.yaml` | Check the settings before training |
| `harnessadapter train --config run.yaml` | Run the Trainer |
| `harnessadapter eval --config run.yaml --adapter ...` | Evaluate one adapter on the test split |
| `harnessadapter deploy --adapter DIR --output WORKSPACE --host codex` | Generate a host workspace from an adapter |
| `harnessadapter convert --adapter DIR --output ART --target claude-code` | Emit the installable form of a host |

The first three accept `key=value` overrides after the config (see [Configuration](configuration.md)).

## Typical workflow

```bash
harnessadapter check --config run.yaml                      # optional: catch setting mistakes before spending
harnessadapter train --config run.yaml                      # train; resumes automatically after a crash
harnessadapter eval  --config run.yaml --adapter final      # the official result
harnessadapter eval  --config run.yaml --adapter none       # optional: the agent without any adapter
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
harnessadapter eval --config run.yaml --adapter final         # a run's final adapter
harnessadapter eval --config run.yaml --adapter <tag|commit|path>
harnessadapter eval --config run.yaml --adapter root          # the template's initial content
harnessadapter eval --config run.yaml --adapter none          # no harness adapter: a pure baseline
```

| Item | Rule |
|---|---|
| Data | The test split only |
| Deployment and scoring | The same pipeline and judge as the training rollouts |
| Repeats | `eval.repeats`; reports the mean and the share of tasks solved at least once |
| Records | Per-task score, cost, time and API speed under `<run>/eval/<adapter>/`; the summary counts runs that errored |
| `CARD.md` | With the literal `--adapter final` selector, the results are written into the run's final card |

`eval` evaluates exactly what it is given and runs no baseline by itself. A baseline is one more `eval`, run when the
user chooses: `--adapter none` runs the acting agent with no harness adapter at all (the pipeline receives
`adapter=None`); `--adapter root` runs the template's initial content, which may already deploy tools or context.

## deploy

```bash
harnessadapter deploy --adapter DIR --output WORKSPACE --host codex
harnessadapter deploy --adapter DIR --output WORKSPACE --host claude-code
```

This generates host instruction files, role/skill files, and tool configuration. It does not launch the acting
agent. It is the per-project way to use an adapter; the per-agent way, `convert`, and how the two compare are in
[What is a harness adapter?](what-is-a-harness-adapter.md#two-ways-to-use-an-adapter). The output must be an empty directory or a workspace previously generated for the same host, separate from
the adapter directory. See the [file mapping](adapter.md#acting-interface) for exact paths and update behavior.

## convert

```bash
harnessadapter convert --adapter DIR --output ART --target claude-code   # same artifact installs in Codex
harnessadapter convert --adapter DIR --output ART --target codex
harnessadapter convert --adapter DIR --output ART --target pi
harnessadapter convert --adapter DIR --output ART --target hermes
```

`deploy` prepares a workspace you work in; `convert` emits an artifact you install, so it is available in every
project rather than one directory. The adapter is copied inside the artifact, and the MCP server is named as the
`harnessadapter-mcp` console script rather than an interpreter path, so nothing points at the machine that
produced it.

### What each target gets

| Target | Form | Install |
|---|---|---|
| Claude Code | `.claude-plugin/` marketplace + plugin | `claude plugin marketplace add ART` then `claude plugin install ID@ID-adapters` |
| Codex | the same directory | `codex plugin marketplace add ART` then `codex plugin add ID@ID-adapters` |
| Pi | `package.json` + conventional directories | `pi install ART` |
| Hermes | a `.claude` tree for the importer | `hermes import-agent claude-code --source ART/.claude` |

### What does not survive, and why

- **The context is not a context.** A plugin's component types are skills, agents, hooks, MCP and LSP servers;
  none of them add to `CLAUDE.md` or `AGENTS.md`. The adapter's context is therefore shipped as the skill
  `<id>-context`, which is loaded when its description matches rather than being present in every turn. The
  result reports this as `context_as`. Hermes is the exception: its importer turns `CLAUDE.md` into memory
  entries, which are always present, so there `context_as` is `memory:CLAUDE.md`.
- **Roles reach Claude Code only.** A Codex plugin carries skills and MCP servers; Pi's subagents are a
  third-party extension; `hermes import-agent` ignores `agents/` without reporting it as skipped. Converting an
  adapter that exports roles to those targets fails rather than shipping silently without them. `--allow-lossy`
  emits the artifact anyway, and the result lists what was left out under `omitted`.
- **Only the Claude Code entry is relocatable.** Claude Code expands `${CLAUDE_PLUGIN_ROOT}` in the MCP config
  and exports it to the server it spawns. Codex expands nothing and resolves nothing -- a relative command,
  `./cmd` and `${CODEX_PLUGIN_ROOT}` are all passed through literally, and the server is spawned with the user's
  working directory -- so its entry holds an absolute path and moving the artifact requires converting again.
  The result reports this under `relocatable`.
- **Pi installs the tools separately.** Pi reads MCP servers from `~/.pi/agent/mcp.json` or a project's
  `.pi/mcp.json`, never from a package. The entry is written to `mcp.json` in the artifact and the exact
  `pi mcp add` command is in its README.

### Checking the result

`claude mcp list`, `codex mcp list` and `pi mcp list` each show whether the server starts; `pi mcp list` exits
non-zero when it fails. `hermes import-agent --dry-run` prints what the import would write without writing it.

## Implementation notes

- `check` also verifies that every `sandbox.read` path exists. It does not check the pipeline's host: adapters are meant to
  be usable by any acting agent, and each pipeline deploys the exports (context, tools, roles, skills) in its own way.
- `harnessadapter deploy --adapter DIR --output WORKSPACE --host codex|claude-code` deploys an adapter's exports into
  a host workspace (context block, MCP tools, native roles, skills).
- `harnessadapter convert --adapter DIR --output ART --target ...` emits the installable form instead, and exits 2
  with the reason when the target cannot represent an export.
