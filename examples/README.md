# Examples

Each top-level folder is one Evolver, the method that trains the adapter. Its subfolders are the tasks it runs on,
each with its configs. The tasks themselves (data preparation, acting-agent pipeline, judge, task description) are
shared by every method and live in [`tasks/`](tasks/).

```text
examples/
  <method>/
    README.md        how the method works, its knobs, its examples
    <task>/
      smoke.yaml     a few questions, to check the setup end to end
      run.yaml       the full run
      adapter/       an adapter trained with run.yaml, with its CARD.md (where included)
  tasks/<task>/      prepare.py, pipeline.py, judge.py, task-info/
  deploy/<use>/      finished adapters to check and deploy
```

## Evolvers

| Folder | `evolver.name` | Method | Tasks |
|---|---|---|---|
| [`prompt_only/`](prompt_only/) | `prompt_only` | One model call per step rewrites `ADAPTER.md` from the step's results | `officeqa` |
| [`codex/`](codex/) | `codex` | A Codex agent edits the adapter from the step's learning material | `officeqa` |
| [`gepa/`](gepa/) | `gepa` | GEPA: reflective text evolution with Pareto selection | `officeqa`, `officeqa_paper` |
| [`skillopt/`](skillopt/) | `skillopt` | SkillOpt: a few validation-gated skill edits per step, under a learning rate | `officeqa`, `officeqa_paper` |
| [`mwm/`](mwm/) | `mwm` | MetaWorld Model: a Codex learner maintains a world model of the environment after every block, under the native MWM contract | `clbench` (database, poker), `servelearnbench` (retail_l3) |

## Other

| Folder | Contents |
|---|---|
| [`tasks/`](tasks/) | `officeqa`: 133 OfficeQA questions, answered by searching the Treasury Bulletins<br>`officeqa_paper`: OfficeQA in the SkillOpt paper's setting<br>`clbench`: the CL-Bench database and poker streams, played by metaworld's host seat<br>`servelearnbench`: a ServeLearnBench scenario (serving stream, per-window tests), played by the benchmark's own agent loop |
| [`deploy/`](deploy/) | `lean_collab`: a finished adapter for five Codex agents proving Lean theorems together |

## Run an example

Run from the repository root. Prepare the task once, then check, train and evaluate with a method's config:

```bash
python examples/tasks/officeqa/prepare.py /path/to/officeqa/prepared
export FIREWORKS_API_KEY=...
harnessadapter check --config examples/gepa/officeqa/smoke.yaml
harnessadapter train --config examples/gepa/officeqa/smoke.yaml
harnessadapter eval  --config examples/gepa/officeqa/smoke.yaml --adapter final
```

Paths inside a config, and a path given to `--adapter`, are relative to the config's folder; a run writes to
`<method>/<task>/runs/`. Run `smoke.yaml` before `run.yaml`: it checks the data, pipeline, judge and Evolver together
on a few questions.

## Add an example

- A task: `tasks/<task>/` with `pipeline.py`, `judge.py`, `task-info/README.md` and a `prepare.py` that writes
  `tasks.jsonl` and the splits (see [Task integration](../docs/framework/task.md)).
- A method on an existing task: `<method>/<task>/smoke.yaml` and `run.yaml`, and a row in `<method>/README.md`.
- A method: `<method>/README.md` in the shape of the others: what it does, one step, knobs, examples, commands.
