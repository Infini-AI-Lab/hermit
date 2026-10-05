# Commands

| Command | Purpose |
|---|---|
| `harnessadapter check --config run.yaml` | Check the settings before training |
| `harnessadapter train --config run.yaml` | Run the Trainer |
| `harnessadapter eval --config run.yaml --adapter ...` | Evaluate one adapter on the test split |
| `harnessadapter deploy --adapter DIR --output WORKSPACE --host codex` | Generate a host workspace from an adapter |

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
| Records | Per-task score, cost, time and API speed under `<run>/eval/<adapter>/` |
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
agent. The output must be an empty directory or a workspace previously generated for the same host, separate from
the adapter directory. See the [file mapping](adapter.md#acting-interface) for exact paths and update behavior.

## Implementation notes

- `check` also verifies that every `sandbox.read` path exists. It does not check the pipeline's host: adapters are meant to
  be usable by any acting agent, and each pipeline deploys the exports (context, tools, roles, skills) in its own way.
- `harnessadapter deploy --adapter DIR --output WORKSPACE --host codex|claude-code` deploys an adapter's exports into
  a host workspace (context block, MCP tools, native roles, skills).
