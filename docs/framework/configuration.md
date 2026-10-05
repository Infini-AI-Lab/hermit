# Configuration

:::{note}
Agreed 2026-10-05; implemented in 0.2.0. Implementation notes at the end of this page.
:::

One YAML file, `run.yaml`, describes one training run. It is the counterpart of verl's training config and is used by
all three commands (`check`, `train`, `eval`). All manifests and configs are YAML (`adapter.yaml`, `evolver.yaml`,
`run.yaml`); data and records stay JSON / JSONL.

## Defaults and overrides

The framework ships a default config. The user's file and command-line overrides change only what they name:

```bash
harnessadapter train --config run.yaml trainer.epochs=5 evolver.name=gepa
```

## Full example

```yaml
# run.yaml: only what differs from the framework defaults needs to be written

data:                          # verl: data
  tasks: tasks.jsonl
  train: splits/train.txt
  val: splits/val.txt          # optional; given = handed to the Evolver
  test: splits/test.txt        # optional; used only by eval
  step_size: 12                # tasks per step (verl: train_batch_size)
  shuffle_seed: 0

task_info: task-info/

pipeline:                      # the user's acting-agent pipeline; verl: actor_rollout_ref
  run: pipeline.py:run
  concurrency: 8
  params: {}                   # passed to the pipeline unchanged

judge:                         # verl: custom_reward_function
  path: judge.py
  name: score

template: lean-collab@1.0      # the template (the tree's root)
start_from: null               # optional: a tag, commit or local adapter folder; default = the template root

evolver:                       # verl: algorithm
  name: gepa
  resume: false                # continue the Evolver's session between steps
  params: {}                   # empty = the Evolver's defaults

evolver_access:                # the user's resources handed to the Evolver; listed = given
  environment: [/data/mathlib]
  tools: [db_query]            # tools declared by the task
  send_material_out: false     # must be true for an Evolver that sends material to an outside service

sandbox:                       # every sandbox setting is the task user's
  enabled: true                # false: the Evolver runs unconfined
  network: restricted          # restricted | open | none
  allow: [https://api.fireworks.ai]   # reachable endpoints when restricted
  read: []                     # more paths the Evolver may read (an allowlist: nothing else is visible)
  env: [FIREWORKS_API_KEY]     # environment variables passed on (unset: all)

trainer:                       # verl: trainer
  run_name: lean-gepa-01
  output_dir: runs/
  epochs: 3
  allow_early_stop: false
  resume_mode: auto            # auto | disable | a step number or checkpoint tag
  save_freq: {steps: null, epochs: 1}
  budget: {usd: 200, tokens: null, hours: 24}
  call_timeout_min: 60

eval:
  repeats: 5
```

## Sections

| Section | verl counterpart | Who needs to understand it |
|---|---|---|
| `data`, `task_info`, `pipeline`, `judge` | `data`, `actor_rollout_ref`, `custom_reward_function` | The task user: all of it is their own |
| `template`, `start_from`, `evolver` | initial model, `algorithm` | Naming them is enough; parameters default |
| `evolver_access`, `sandbox` | — | The task user: which of their resources the Evolver may use, and how it is confined |
| `trainer`, `eval` | `trainer` | The task user: how long, how much money, how many repeats |

There are no on/off switches for resources: a resource that is given (a path, a list entry) is available to the
Evolver, and one that is not given is not. The one explicit permission is `send_material_out`, because a user who does
not know an Evolver's internals cannot know that it sends data out.

## Sandbox, network and warnings

The framework does not force restrictions. Some users have no test set, or their own strategy for evolving; for them
restrictions are unnecessary. Risky settings produce **warnings** at `check` and at the start of `train`, naming the
guarantees that no longer hold. They are recorded in `config.yaml` and in the final adapter's `CARD.md`.

| Setting | Warning |
|---|---|
| `sandbox.enabled: false` | The Evolver can read anything on the machine, including the test split; paths and network are not confined |
| `sandbox.network: open` | Learning material and other data can leave the machine |
| Validation data containing answers | The Evolver may overfit to the validation split |
| `evolver_access.send_material_out: true` | Learning material is sent to an outside service |

Unaffected by any setting: the binding `check_spec`, budgets, and the run records.

## Startup checks of `train`

`train` does not run the settings checks of `check`. It does one check, because it decides whether the Evolver can run
at all: every entry in the Evolver's `needs` must be given by the run config, and `evolver.resume: true` requires an
Evolver that supports resume. Otherwise `train` stops with an error naming what is missing.

## Implementation notes

- Extra keys: `llm` ({base_url, model, api_key_env, price: {input, cached_input, output} per million tokens}; enables
  the `llm` tool), `sandbox.read`, `sandbox.env`, `trainer.tree_dir`.
- Paths are relative to the config's directory; `${VAR}` is expanded.
- The sandbox is an allowlist, and every sandbox setting is the task user's. Inside it the Evolver's programs see
  only the system, the Python installation and the framework, their own installation, the workspace (read-write),
  their granted accesses and Evolver directory, and the paths in `sandbox.read`. The task, the answers, the run
  records and the home directory do not exist inside. `sandbox.env` lists the environment variables passed on
  (besides `PATH`, `HOME`, the locale and the framework's own); unset, all pass and a warning says so.
- `restricted` network is enforced through an allowlist proxy and proxy variables: a program that ignores those
  variables is not stopped (a warning says so).
