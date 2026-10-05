# Commands

:::{note}
Agreed 2026-10-05; implemented in 0.2.0. Implementation notes at the end of this page.
:::

| Command | Purpose |
|---|---|
| `harnessadapter check --config run.yaml` | Check the settings before training |
| `harnessadapter train --config run.yaml` | Run the Trainer |
| `harnessadapter eval --config run.yaml --adapter ...` | Evaluate one adapter on the test split |

All three accept `key=value` overrides after the config (see [Configuration](configuration.md)).

## Typical workflow

```bash
harnessadapter check --config run.yaml                      # optional: catch setting mistakes before spending
harnessadapter train --config run.yaml                      # train; resumes automatically after a crash
harnessadapter eval  --config run.yaml --adapter final      # the official result
harnessadapter eval  --config run.yaml --adapter none       # optional: the agent without any adapter
```

## check

`check` is a separate command, outside the run. The user runs it when they want and starts training once satisfied;
`train` does not run it for them. As with model training, a wrong setting can waste a lot of money, and making sure
the settings are right is the user's responsibility.

| What `check` looks at | Example of a problem it finds |
|---|---|
| Template | The starting adapter fails `check_spec`; the template does not support the pipeline's host |
| Evolver | The class does not load; a need is not given; resume is requested but not supported |
| Task | `task-info/README.md` missing; splits overlap; the judge cannot score a sample task |
| Budgets | No hard budget or no iteration budget |
| API speed | One short probe through the pipeline; the measured speed is reported |
| Risky settings | The warnings listed in [Configuration](configuration.md#sandbox-network-and-warnings) |

`check` prints a disclaimer: it checks only these basic items. Passing `check` does not guarantee that the settings are
free of problems or that the run will succeed.

## train

Runs the loop described in [Trainer](trainer.md). At start it checks only that the Evolver's needs are given (see
[Configuration](configuration.md#startup-checks-of-train)) and prints the warnings for risky settings. If a run with
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
| `CARD.md` | When the evaluated adapter is a run's final adapter, the results are written into its card |

`eval` evaluates exactly what it is given and runs no baseline by itself. A baseline is one more `eval`, run when the
user chooses: `--adapter none` runs the acting agent with no harness adapter at all (the pipeline receives
`adapter=None`); `--adapter root` runs the template's initial content, which may already deploy tools or context.

## Implementation notes

- `check` also verifies that every `sandbox.read` path exists. It does not check the pipeline's host: adapters are meant to
  be usable by any acting agent, and each pipeline deploys the exports (context, tools, roles, skills) in its own way.
- `harnessadapter deploy --adapter DIR --output WORKSPACE --host codex|claude-code` deploys an adapter's exports into
  a host workspace (context block, MCP tools, native roles, skills).
