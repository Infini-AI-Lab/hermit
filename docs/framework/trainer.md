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
    for tasks in batches(shuffle(train), step_size):
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

The current implementation scores every rollout through the configured `judge.path`. In the component model,
scoring feedback is one part of the Agent → Learning Material path.

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
collected and checked. `final` means this selection, not an automatically chosen highest-scoring checkpoint.
Final evaluation is a separate command.

An epoch is one pass over the training split. Each step takes up to `data.step_size` tasks; the order is reshuffled
deterministically for each epoch, or kept as the split file gives it when `data.shuffle` is false (an ordered
stream, such as a continual-learning sequence). The last batch can be smaller. Validation tasks are offered to the Evolver rather
than included in the Trainer's training batches. Test tasks are reserved for `eval`.

### Resuming after a crash

With `trainer.resume_mode: auto`, an unfinished run resumes from its last completed step. The Trainer restores the
adapter commit and learner-state snapshot, drops later incomplete material, and recomputes the next batch from the
seed and progress.

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
outside the training budget. See [Configuration](configuration.md#budgets-and-checkpoint-frequency).

## Minimal example

After installation, run the bundled offline interface example from the repository root:

```bash
hermit check --config docs/_examples/interface_task/run.yaml
hermit train --config docs/_examples/interface_task/run.yaml
hermit eval --config docs/_examples/interface_task/run.yaml --adapter final
```

The pipeline echoes input text and the Evolver records scores without changing the adapter. It exercises two
training steps, state snapshots, finalization, and test evaluation without model calls. The config disables the
learner sandbox for this trusted local example.

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
| `eval/<adapter>/` | Separate evaluation results. |

The adapter tree defaults to `<output_dir>/trees/<template id>`. Runs use branches; checkpoints and finals use tags
such as `<run>/ckpt-step-5`, `<run>/ckpt-epoch-1`, and `<run>/final`. Checkpoint frequency is controlled by
`trainer.save_freq`. The final adapter receives a `.hermit/CARD.md` describing the experiment.
