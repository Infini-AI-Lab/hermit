# Trainer

The Trainer connects task execution to adapter learning. It calls the task pipeline and Evolver, checks the returned
files, and saves enough progress to continue the run. Read this page to understand what the framework controls and
what it expects from the components you provide.

## Responsibility and boundaries

The Trainer owns batching, learning-material records, workspace preparation, specification checks, version history,
budgets, and recovery. The pipeline owns how the acting agent runs. The Evolver owns how an adapter is improved and
which candidate it returns.

```{raw} html
:file: ../_includes/trainer-contract.html
```

The Trainer uses those interfaces without needing to understand a task's answer format or an Evolver's search
algorithm.

## Interface

Start a run with `hermit train --config run.yaml`. The Python entry point is `Trainer(config).train()`.

| Input | How the Trainer uses it |
|---|---|
| Task data and splits | Select the tasks for each batch. |
| Pipeline and scoring function | Execute and score each task. |
| Template and optional starting adapter | Initialize the version history and reference specification. |
| Evolver definition and parameters | Create the learning method and call its lifecycle methods. |
| Access, budgets, and checkpoint settings | Prepare resources and control the run. |

The returned summary identifies the run, stop reason, final commit/tag, checkpoints, and recorded spending.
The deployable output is the accepted adapter directory, also stored in Git. Detailed records remain in the run
directory; they are not added to the deployed package.

## The training loop

A simple example of the loop, in pseudocode. It leaves out resuming, the sandbox, timeouts, and records; the
sections below describe them.

```python
adapter = start_from or template
evolver.setup()
adapter = commit(evolver.bootstrap(adapter))                          # optional: step 0, before any rollout

for epoch in range(epochs):
    for tasks in batches(shuffle(train), step_size):                  # or the tasks the Evolver named
        results  = [judge(t, pipeline(t, adapter)) for t in tasks]   # rollouts
        material = learning_material(results)

        new = evolver.step(adapter, material)                         # the Evolver edits a copy
        if check_spec(new).passed:
            adapter = commit(new)
        else:
            adapter = commit(repair(new, from_=adapter))              # broken files restored from the parent

        checkpoint(adapter)                                           # per save_freq
        if over_budget(): break

adapter = evolver.finalize(adapter)
tag(adapter, "final")
```

## One step

| Handoff | Input | Output |
|---|---|---|
| Trainer → pipeline | One task and the current adapter path. | `RunOutput`: answer and available execution evidence. |
| Trainer → scoring function | Task and `RunOutput`. | Score, feedback, and additional fields. |
| Trainer → learning-material writer | Per-task records and any prior repair feedback. | Material for this batch. |
| Trainer → Evolver | `StepContext`, including an editable copy of the same adapter. | Files left in `adapter/`, updated state, `StepResult`, and reports. |
| Trainer → specification check | Changed candidate and the template reference. | Accepted files, or a repair followed by another check. |
| Trainer → storage | Accepted files and step results. | Git changes, state snapshot, records, and any checkpoint tag. |

The current implementation scores every rollout through the configured `judge.path`, but three it never judges: one the
pipeline says is not the agent's result (`RunOutput.extra["unscored"]`, [Execution result](task.md#execution-result)),
recorded with status `infra` and left out of the means; and one whose agent session failed
(`RunOutput.extra["session_failed"]`: no answer to judge) and one whose pipeline raised (a lost sign-in stops the run
instead), both recorded with status `error` and score 0. In the component model, scoring feedback is one part of the
Agent → Learning Material path.

A step's rollout scores describe its **input** adapter. The returned adapter first affects a later execution.
Successive steps can use different tasks, so their scores alone are not a controlled comparison of adapter versions.
Use [evaluation](commands.md#eval) for comparisons on a common test split.

## Learning material

The material directory is the interface between execution and learning:

```text
learning_material/
  README.md
  index.jsonl
  current -> step_0002
  step_0001/
  step_0002/
    summary.json
    tasks/<id>.json
    trajectories/<id>/       when a log was supplied
    check_feedback.json      when the preceding update needed repair
```

| Record | Contents |
|---|---|
| `summary.json` | Step, epoch, adapter commit used, task IDs, mean score, and timing/cost summaries. |
| `tasks/<id>.json` | Input, output, status/errors, normalized judge result, usage, messages when supplied, and a relative trajectory path or null. |
| `trajectories/<id>/` | Original pipeline log copied as a file or directory. |
| `check_feedback.json` | Repaired paths, restoration source, and reason. |
| `index.jsonl` | One summary row per step for scanning previous results. |

That is the material under the default [`run.feedback`](configuration.md#how-the-run-is-done-run), `full`. Under
`score` a task's judge result is its score alone, and under `none` the material holds no verdict at all; under both,
the verdict a Harbor trial carries beside the judge (its reward fields, its verifier status and files, and the timeout
of a solution its verifier ran out of time on, whose `timed_out` reads false) is left out.

The Trainer can only preserve evidence the pipeline returns. It does not manufacture a missing transcript.
The [task interface](task.md#execution-result) defines those fields; the Evolver chooses which evidence to read.

## Failure handling

Failures affect different boundaries:

| Event | Behavior |
|---|---|
| Pipeline raises or returns the wrong type | Record a pipeline error with score zero; the batch can still supply evidence. |
| Scoring raises | Record a judge error with score zero. |
| Evolver reports `failed` / `out_of_budget`, raises, or returns the wrong type | Keep the previous adapter and record the step. |
| Returned files violate the specification | Repair as described below. |
| `setup` fails | Stop before training steps begin. |
| `finalize` fails or returns the wrong type | Use the current accepted adapter as final. |
| Unhandled crash | Resume from the last completed step, subject to the saved records. |

### When the binding check fails

The Trainer records the invalid candidate in Git, restores offending files, checks again, and records the repair.
Files under `.hermit/` and fixed files return to the template version (`.hermit/CARD.md` to the previous adapter's);
an `exports.yaml` that does not parse and invalid learnable files return to the previous adapter's version.
New disallowed files are removed. If a failure cannot be attributed to particular files, repair can revert all
changes. The previous accepted adapter is the fallback.

The learner receives the repair report in the next step's material. Invalid commits remain inspectable but cannot
be selected as a starting adapter. Repair establishes specification conformance; it does not evaluate task quality.

## Lifecycle and state

On a fresh run the Trainer calls `setup`, then `bootstrap` when the Evolver defines it (recorded as step 0 and
tagged `<run>/bootstrap`; a resumed run does not repeat it), then processes batches through `step`. At normal completion, allowed
early stop, or a handled budget stop, it calls `finalize`. An unhandled process crash does not guarantee finalization.

The default finalization returns the current adapter. A custom Evolver can select another candidate, which is
collected and checked. `final` means this selection, not an automatically chosen highest-scoring checkpoint; with
`run.mode: stream` there is no selection: `final` is the adapter the last step left. Final evaluation is a separate
command, except with `run.mode: split`: `hermit train` then tests the final adapter on `data.test` when training
ends (`eval/final/`).

An epoch is one pass over the training split. Each step takes up to `data.step_size` tasks; the order is reshuffled
deterministically for each epoch (a multi-step Harbor task's rows moved as one, in step order: a step ahead of an
earlier one would wait for it in the task's stream), or kept as the split file gives it when `data.shuffle` is false
(an ordered stream, such as a continual-learning sequence). An order taken as given (the split's then, an order
file's, or a seed's, which keeps each task's rows in the split's order) that lists a step of a multi-step Harbor task
ahead of an earlier step of its stream, and any order that lists a step but not every earlier step of its stream, is
refused before training, and `hermit check` reports it. The last batch can be smaller. An Evolver that declares
`chooses_tasks` may instead name the next step's tasks from those the epoch has still to run, a step of a multi-step
Harbor task only once each earlier step of its stream has run in the epoch or comes before it in the request (one the
training split leaves out never runs: [Choosing the next tasks](evolver.md#choosing-the-next-tasks)); the epoch still
runs every training task once.
Validation tasks are offered to the Evolver rather than included in the Trainer's training batches. Test tasks are
reserved for `eval`.

### Resuming after a crash

With `trainer.resume_mode: auto`, an unfinished run resumes from its last completed step. The Trainer restores the
adapter commit and learner-state snapshot, drops later incomplete material, and recomputes the next batch from the
seed and the tasks the epoch's recorded steps already ran, honouring the last step's `next_tasks`.

Budget accounting also resumes from that step's saved record. Work done after it and before the crash may therefore
be absent from restored spending. Resuming a learner's external host session additionally depends on that host and
Evolver.

Use `start_from` and a new run name to branch from an adapter without its old learner state. `resume_mode: disable`
starts over, renaming the previous run directory and branch. Step/tag selection through `resume_mode` is not
implemented.

## Budgets

The Trainer supports USD, token, and wall-time budgets plus a per-call timeout. Limits are observed after rollout
tasks, through metered tools during learner calls, and from reported usage when a call returns.

A stop is cooperative at points the framework controls. It refuses further tool calls, signals cancellation, and
stops programs launched through its runner. Arbitrary in-process code that ignores cancellation cannot be forcibly
stopped by this mechanism. Calls already in flight can exceed a limit; agent-backed model usage may become visible
only after the host session ends.

Recorded dollar cost depends on known usage and applicable prices. Separate `check` probes and `eval` commands are
outside the training budget, and so are the tests `hermit train` runs itself (`run.mode: split`'s test of the final
adapter, the checkpoint tests, during training too: their time is not counted against `hours`). See
[Configuration](configuration.md#budgets-and-checkpoint-frequency).

## Minimal example

After installation, run the bundled offline interface example from the repository root:

```bash
hermit check --config docs/_examples/interface_task/run.yaml
hermit train --config docs/_examples/interface_task/run.yaml
hermit eval --config docs/_examples/interface_task/run.yaml --adapter final
```

The pipeline echoes input text and the Evolver records scores without changing the adapter. It exercises two
training steps, state snapshots, finalization, and test evaluation without model calls. The Evolver is trusted
Python in the Trainer's process; the pipeline runs in the task sandbox (`enforce: best_effort`: unconfined and
recorded where bubblewrap is missing).

Outputs go under `.demo/interface-example/interface-demo/`. A completed run is not resumed; use a new
`trainer.run_name` override for another experiment and the same override when evaluating it.

## Records and versions

| Location | Purpose |
|---|---|
| `checkout/` | Current adapter worktree, including the final card. |
| `steps.jsonl` | Completed steps, adapter commit IDs, scores, statuses, usage, and check results. |
| `step_NNNN/state/`, `step_NNNN/output/` | Saved learner state and reports. |
| `learning_material/` | Evidence supplied to the learner. |
| `config.yaml`, `summary.json` | Resolved settings and final run summary. |
| `stream_summary.json` | With `run.mode: stream`: the stream's prequential score, overall, by halves and by step. |
| `checkpoints.json`, `checkpoints/<name>/<adapter>/` | With `run.checkpoints`: each checkpoint's commit and test results; each tested adapter's summary and per-attempt results. |
| `eval/<adapter>/` | Evaluation results: `hermit eval`'s, and with `run.mode: split` the final adapter's on `data.test`. |

With `run.orders`, each order is a run of its own, `<run_name>-o<i>`, beside `<run_name>/streams.json`, which holds
each order's stream score and the means over the orders.

The adapter tree defaults to `<output_dir>/trees/<template id>`. Runs use branches; checkpoints and finals use tags
such as `<run>/ckpt-step-5`, `<run>/ckpt-epoch-1`, and `<run>/final`. Checkpoint frequency is controlled by
`trainer.save_freq`. The final adapter receives a `.hermit/CARD.md` describing the experiment.
