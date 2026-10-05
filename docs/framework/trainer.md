# Trainer

:::{note}
Agreed 2026-10-05; implemented in 0.2.0. Implementation notes at the end of this page.
:::

The Trainer is the framework's outer loop. It handles everything around the Evolver: data, training rollouts,
learning material, the workspace, the binding check, git, records, budgets, checkpoints and resume. It is started
with `harnessadapter train --config run.yaml`.

## One step

```text
1. Data          take this step's tasks from the training split
2. Rollout       run the current adapter on them through the pipeline; score them with the judge
3. Material      write the results to learning_material/step_NNNN/ and point `current` at it
4. Workspace     copy the current adapter into adapter/; mount task-info/, history/, state/; assemble the tools
5. Evolver       call step(ctx) and receive a StepResult
6. Check         run check_spec on the adapter handed back; repair it if it fails
7. Commit        if the adapter differs from the previous commit, commit it as a new node
8. Record        store usage, time, check results, StepResult, output/ and a snapshot of state/
```

`setup` is called once before the first step and `finalize` once after the last.

## Data and batching

```yaml
data:
  tasks: tasks.jsonl
  train: splits/train.txt
  val: splits/val.txt        # optional; given to the Evolver only
  test: splits/test.txt      # optional; used only by eval
  step_size: 12              # tasks per step (verl: train_batch_size)
  shuffle_seed: 0
trainer:
  epochs: 3
```

| Concept | Meaning |
|---|---|
| Epoch | One full pass over the training split |
| Step | The next `step_size` training tasks; the order is reshuffled with `shuffle_seed` at the start of every epoch |
| Last step of an epoch | Takes whatever remains when the split is not a multiple of `step_size` |
| Validation split | Never used by the Trainer; given to the Evolver when set |
| Test split | Used only by `eval` |

## Learning material

```text
learning_material/
  README.md        the format
  index.jsonl      one line per step: step, epoch, number of tasks, mean score, cost, whether the adapter changed
  step_0001/
  step_0002/
  ...
  current -> step_0007
```

One directory per step, so an Evolver sees at a glance how many steps there have been; `index.jsonl` gives the trend,
each step directory the detail.

The Trainer produces one standard format, as complete as possible. For each task: its input; the full trajectory (the
host's original log and a normalised message list); the final output; everything the judge returned (score and
feedback, unchanged); cost, time and API speed. When the previous step's adapter was repaired, the step directory also
holds `check_feedback.json`.

Different Evolvers want different views of the same results (short per-task records for GEPA, full trajectories for
SkillOpt, citable evidence for MWM). Conversion is the Evolver's own business; the framework offers a library of
converters for common views, which an Evolver may use or replace.

## When the binding check fails

The rule: **the adapter that goes into the next step is always valid.** The Trainer repairs, tells the Evolver what it
repaired, and leaves every judgement about the repaired adapter to the Evolver.

| Step | What the Trainer does |
|---|---|
| 1. Commit as handed back | The Evolver's version is committed unchanged, its message marking it invalid |
| 2. Repair | Only the affected files: a changed fixed file returns to the root's version; a learnable file in a wrong format returns to the parent's version (a new file is removed); a file outside the learnable paths is removed; a part that fails to deploy returns to the parent's version |
| 3. Check again | Repeat until the adapter passes (files depend on each other, for example `ADAPTER.md` naming a removed tool). The worst case is the parent unchanged. |
| 4. Commit the repair | A second commit; its message lists the repaired files and the reasons |
| 5. Feed back | `check_feedback.json` in the next step's material: each repaired file and why |

The Trainer does not re-validate the repaired adapter; whether it is still good is for the Evolver to judge in the next
step.

## Checkpoints and the final adapter

```yaml
trainer:
  save_freq: {steps: 5, epochs: 1}
```

| Item | Rule |
|---|---|
| Checkpoints | Set by the user (`trainer.save_freq`); the Trainer tags those commits `<run>/ckpt-step-N` or `<run>/ckpt-epoch-N` |
| Final adapter | The Trainer always calls `finalize`. The default returns the current adapter (the last step's); an Evolver that overrides it may choose another, for example the best candidate in its pool |
| Final tag | The final adapter passes the binding check, is committed if new, tagged `<run>/final`, and gets its `CARD.md` |

## Budgets

| Kind | Config | Enforcement |
|---|---|---|
| Hard budgets | `trainer.budget: {usd, tokens, hours}` | The run stops when one is reached |
| Per-call limit | `trainer.call_timeout_min` | A call that exceeds it counts as `failed` |
| Iteration budget | `trainer.epochs` and `data.step_size` | The run ends after the last step |

Hard budgets are checked whenever the Trainer can observe spending, not only between steps:

| Check point | What is observed |
|---|---|
| After each training-rollout task | Rollout cost and time |
| During an Evolver call | Wall time; usage through metered tools and the `llm` helper; for built-in agents, the session log as it grows |
| When a call returns | The `Usage` in `StepResult` (the only source besides wall time for a black box) |

When a hard budget is reached the Trainer stops at the next point it controls (a tool call, a started program, the
end of the call; spending may slightly exceed the budget): the step in progress is discarded, the adapter of the last
completed step stays current, `finalize` is called, and the records state the reason.

How long a run lasts is the user's decision. An Evolver that believes it has converged hands back the adapter
unchanged for the remaining steps. If the user sets `trainer.allow_early_stop: true`, a `StepResult` with status
`stop` ends the run instead.

## Run records

Records live in `<output_dir>/<run_name>/`, never in the adapter.

| File | Contents |
|---|---|
| `config.yaml` | The full run config as resolved (defaults plus overrides), the template, Evolver and task versions, and any warnings raised at start |
| `steps.jsonl` | One line per step: epoch and step, task list, rollout scores, cost, time, API speed, the Evolver's `Usage`, whether the adapter changed, check and repair results, commit hashes |
| `step_NNNN/` | The Evolver's `output/` unchanged, its `StepResult`, and a snapshot of `state/` |
| `learning_material/` | All the learning material (the training trajectories live here) |
| `summary.json` | Written at the end: total cost and time (rollouts and Evolver separately), the stop reason, the final adapter, the checkpoints |
| `eval/<adapter>/` | Results of `eval` runs (see [Commands](commands.md)) |

## Resuming after a crash

A crashed run resumes from its last completed step, not only from a checkpoint.

| Needed to resume | Where it comes from |
|---|---|
| The adapter | That step's commit |
| The Evolver's `state/` | The snapshot stored with that step |
| Which tasks come next | Recomputed from the seed, the epoch and the step number |
| Learning material and budget spent | `learning_material/` and `steps.jsonl` |

The step that was in progress is discarded, including its finished rollouts; spending keeps counting from where it
was. If the run asks for session resume but the Evolver's session cannot be found, the Evolver gets its fresh-start
prompt and the records say so.

```yaml
trainer:
  resume_mode: auto      # auto: continue an unfinished run with this name; disable: start over;
                         # or a step number / checkpoint tag
```

`resume_mode` continues **the same** run. Starting **a new** run from an existing adapter is `start_from` (see
[Configuration](configuration.md)): like loading pretrained weights, its Evolver state starts empty.

## Implementation notes

- Run directory, besides the records above: `workspace/` (the Evolver's directories and the agent home), `root/` (the
  template's files, the reference for `check_spec`), `checkout/` (the run's git worktree), `access/validation/`
  (validation tasks given to the Evolver), `tools.jsonl` (every tool call).
- The tree lives in `trainer.tree_dir` (default `<output_dir>/trees/<template id>`).
- `resume_mode: disable` starts over: the old run directory and branch are renamed with a `.replaced-<time>` suffix.
  A finished run is never resumed; choose another `run_name`.
- Each step record holds the Evolver's reported usage and, separately, the usage of metered tool calls
  (`metered_tool_usage`).
