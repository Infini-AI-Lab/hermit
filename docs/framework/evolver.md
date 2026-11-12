# Evolver

An Evolver implements the learning method. It receives evidence from task executions and returns the adapter to use
next. Read this page to choose a method or implement one that works with the Trainer.

## Responsibility and boundaries

The Evolver decides how to analyze evidence, propose changes, test candidates, and select an adapter. Its state can
hold experience, candidate records, or search progress. The Trainer supplies the execution loop, checks the returned
adapter, and records the run.

```{raw} html
:file: ../_includes/evolver-contract.html
```

The same interface supports a learner agent, a scripted procedure, or a search algorithm. A method still needs to
understand the editable content of the selected template.

## Interface

The Trainer creates an Evolver instance and calls these methods:

| Method | When called | Contract |
|---|---|---|
| `setup(ctx: Context) -> None` | Before the first step of a fresh run. | Optional initialization, such as creating learner state. Per-step material is not available yet. |
| `bootstrap(ctx: StepContext) -> StepResult \| None` | Once on a fresh run, after `setup` and before the first rollouts. | Optional: change the starting adapter (for example, from the task's public information); the result is checked and committed as step 0. Not overridden, or returning `None`: no bootstrap. No learning material exists yet. |
| `step(ctx: StepContext) -> StepResult` | Once per training batch, after its execution results have been collected. | Read the evidence, leave one adapter in the working directory, and report status and usage. Required. |
| `finalize(ctx: Context) -> FinalResult` | On normal completion or a controlled stop. | Optional final selection. The default returns the current working adapter. |
| `step_scores(result: StepResult) -> dict \| None` | After each step but the bootstrap, with that step's result. | Optional, for an Evolver whose protocol scores a step itself (Harness²: the deliverable it chose without the labels): its own score of each of the step's tasks, kept in the step's record (`own_scores`), which a stream's score (`run.mode: stream`) takes in place of the rollout's. A task it maps to `None` is a row its protocol leaves unscored (Harness²: a deliverable whose execution was not the agent's result): left out of the stream's means and counted. A task it leaves out, and the default `None`, keep the rollouts' scores. |

`step` hands back the adapter **through the files it leaves in `ctx.workspace.adapter`**.
Its return value describes the call; it does not contain the adapter files.
`finalize` may return a different adapter directory for the Trainer to collect.

The class also declares what the Trainer checks before the run: `supports_resume`
([State and session lifetime](#state-and-session-lifetime)), `chooses_tasks`
([Choosing the next tasks](#choosing-the-next-tasks)), `min_feedback` and `hides_labels`. `min_feedback` is the least
of each verdict the Evolver's code works with, of [`run.feedback`](configuration.md#how-the-run-is-done-run)'s
`none` < `score` < `full`: `score`, the default (it reads each run's score); `none` (it needs no verdict: the CLI
Evolvers and a `CommandEvolver`'s program, which read the material as the run shows it; Harness², MWM, ReHarness,
Continual Harness); `full` (it reads what else the judge returns: AHE Adapter, EvoSafeHarness; the documentation's
example Evolvers that read the judge's feedback, the Quick Start's learner among them). An Evolver whose params decide
what it reads overrides the class method `min_feedback_for(params)` (its `evolver.params` over its manifest's; by
default `min_feedback`): SkillOpt's gives `full` for a soft or mixed `gate_metric`, whose soft score is the judge's
`extra.f1`. `hermit train` refuses a `run.feedback` below it before training, and `hermit check` reports one.
`hides_labels = True` says that the Evolver keeps the labels from its learner sessions itself (Harness²): it is given
the full records, whatever `run.feedback` says.

### Inputs

`ctx.workspace` supplies paths, rather than copies of all the data inside a Python object:

| Path | Contents | Evolver's intended access |
|---|---|---|
| `adapter` | Editable copy of the accepted adapter. | Read/write. |
| `learning_material` | Current and previous training results. | Read-only. |
| `task_info` | Task description and objectives. | Read-only. |
| `history` | Earlier adapter versions and diffs. | Read-only. |
| `state` | Method-specific information kept across calls. | Read/write. |
| `scratch` | Temporary work, cleared for each step. | Read/write. |
| `output` | Reports and other call outputs, collected with the step. | Read/write. |

See the [learning material format](trainer.md#learning-material) for its record fields.
These are the interface's access boundaries; sandbox enforcement applies to programs launched through the sandbox,
not arbitrary Python code in an in-process Evolver.

The rest of the context supplies:

| Field | Meaning |
|---|---|
| `params` | Evolver defaults merged with this run's parameters. |
| `access` | Optional validation data and granted environment paths. |
| `tools` | Run, task, and template capabilities available to the method. |
| `message` | Human-readable description of the call and available resources. |
| `sandbox`, `cancel`, `tool_url` | The learner's sandbox (it wraps the programs an Evolver starts: the workspace under fixed names below `/w`, paths translated; `sandbox.view(...)` gives a method a view of its own), cancellation signal, and tool endpoint. |
| `progress` | On step and bootstrap calls: epoch, step, whether the epoch ends here, and, for an Evolver that chooses its tasks, the epoch's training tasks still to run (`remaining`). |
| `session` | Optional previous learner-session handle when session resume is enabled. |

The learner interface does not supply test tasks. Whether files remain inaccessible also depends on the run's
filesystem grants and execution isolation.

### Outputs

| Output | Where it goes | Consumer |
|---|---|---|
| Selected adapter | `workspace.adapter` | Trainer checks it; the next acting batch uses the accepted version. |
| Updated learner state | `workspace.state` | Later Evolver calls and crash recovery. |
| Status and usage | Returned `StepResult` | Trainer control flow and accounting. |
| Reports | `workspace.output` | Saved run records. |

```{literalinclude} ../../hermit/agent/evolver/base.py
:language: python
:pyobject: StepResult
```

`Usage` reports model calls, token counts, and cost when known. Calls through the run's metered model (`ctx.agent`, or
the `llm` tool) are already accounted for; do not report the same usage again in `StepResult`.

### Choosing the next tasks

By default the Trainer takes each step's tasks in order: the next `data.step_size` of the epoch. An Evolver that
decides what to learn from next (a curriculum) declares it and names the next step's tasks in
`StepResult.next_tasks`:

```python
class Curriculum(Evolver):
    chooses_tasks = True

    def step(self, ctx):
        ...
        remaining = ctx.progress.remaining      # the epoch's training tasks still to run, in the Trainer's order
        return StepResult("done", "...", next_tasks=[pick(remaining)])
```

Only an Evolver with `chooses_tasks = True` is given `progress.remaining` (also listed in the call's message);
for every other Evolver it is empty, so a stream's later tasks stay unseen. The request is checked when it is made
and holds for the next step only. It must be distinct task ids, at most `data.step_size` of them, from
`progress.remaining`; at the last step of an epoch, where nothing remains, from the training split, which the next
epoch runs again; `progress.remaining` is then empty, so an Evolver that chooses at an epoch's end keeps the ids it
was given earlier (the bootstrap is given the first epoch's whole order, and may name the first step's tasks the
same way). An empty list names nothing. A step of a multi-step Harbor task may be named only when each earlier step of
its stream (its task's rollout) has run in the epoch or comes before it in the same request (at an epoch's end only the
latter, since the next epoch runs every step again; one the training split leaves out never runs): any other request
is refused, since a task's steps run in order in its stream. A request is taken from any step result, a failed step's
included; one made at the run's last step is recorded and has no step to apply to.
An epoch still runs every training task once: the tasks the Evolver leaves out come later in the epoch, in the
Trainer's order. The step's record keeps an accepted request as `next_tasks` and a refused one as
`next_tasks_refused` with the reason, and the next step then runs the order's tasks; a step that ran the Evolver's
tasks says `chosen_by: evolver`. A step may hold fewer tasks than `data.step_size`. An external program
(`CommandEvolver`) names them as `next_tasks` in `output/step_result.json`, from a subclass that sets
`chooses_tasks = True`.

### Status handling

| Status | What the Trainer does |
|---|---|
| `done` | Check changed files, repair if necessary, record the step, continue. |
| `failed` | Keep the previous adapter and record the failed step. |
| `out_of_budget` | Keep the previous adapter, as for `failed`; the run's own budget decides whether training stops. |
| `stop` | Process the returned adapter, then stop if `trainer.allow_early_stop` is true; otherwise continue. |

An exception or wrong return type from `step` is recorded as a failure. Learner-state snapshots are saved with
completed step records, including failed steps; keeping the previous adapter does not imply rolling back learner state.

## Built-in Evolvers

For selection guidance, setup, and the complete parameter defaults, use [Choose a learning method](../methods/index.md).

| `evolver.name` | Method | Needs |
|---|---|---|
| `codex`, `claude`, `pi`, `opencode`, `dsh` | A Codex, Claude Code, Pi, OpenCode or DeepSeek Harness agent edits the adapter from the learning material; `codex` on the `prompt-only@1.0` template is the [prompt-only](../methods/prompt-only.md) method. | The agent CLI |
| `gepa` | [GEPA](https://github.com/gepa-ai/gepa): one iteration per step. It reflects on the step's trajectories, keeps a child that does better on the same tasks, scores it on validation, and hands back the next parent from its Pareto front. | `llm`, `data.val`, `python -m pip install -e . --group gepa` |
| `skillopt` | [SkillOpt](https://github.com/microsoft/SkillOpt): one SkillOpt step per training step. It turns the step's trajectories into a few bounded edits under its learning rate (decaying when `total_steps` is set) and keeps them only if the validation score improves; the slow update and meta skill run at each epoch's end. | `llm`, `data.val`, `python -m pip install -e . --group skillopt` |
| `ace` | [ACE](https://github.com/ace-agent/ace): per rollout, a Reflector tags the playbook bullets the agent named and diagnoses the attempt (a wrong answer is run again with the reflection, up to `max_num_rounds`), and a Curator adds bullets; the playbook grows by deltas. See `docs/methods/ace.md`. | `llm`; `data.val` to keep the best validation playbook |
| `opro` | [OPRO](../methods/opro.md), with `template: prompt-only@1.0`: one OPRO step per training step. The optimizer reads the best instructions scored so far, ascending, and a few training problems, and writes new instructions, each scored on every training task; finalize hands back the best on training or the best validation mean of the top `top_k`. | `llm`, `data.step_size` the training split's size; `data.val` for `select: validation` |
| `ahe_adapter` | [AHE Adapter](../methods/ahe_adapter.md), with `template: ahe@1.0`: one AHE iteration per step. It classifies each task by its k rollouts, scores the previous step's change manifest and rolls HARMFUL changes back, then runs an Agent Debugger session per task and an Evolve Agent session that edits the adapter; finalize hands back the best-ever iteration. | The Codex CLI, k rows per task sharing `task_field`, an acting agent that runs the adapter's hooks (Codex, Claude Code) |
| `mwm` | MetaWorld Model, with `template: mwm`: one native MWM learner session per block (the step), under the MWM contract, with the task's evidence store mounted at `/evidence`, in its own network namespace. See `docs/methods/mwm.md`. | The Codex CLI, a task that keeps an MWM evidence store (`evolver_access.environment`), `task-info/mwm/`, `data.shuffle: false` |
| `reharness` | [ReHarness](../methods/reharness.md), with `template: reharness`: one instance of the stream per step. It revises each reflection the acting agent retrieved, from that reflection's own source trajectory with the new trajectory as feedback, then writes the instance's reflection with the revised ones as demonstrations, through the run's `llm` tool with the paper's prompts; no validation data. | `llm`, `data.step_size: 1` and `data.shuffle: false`, an embedding endpoint (`evolver.params.embedding`, and `REHARNESS_EMBED_URL` for the acting side's reader; BM25 without) |
| `meta_harness_adapter` | [Meta-Harness Adapter](../methods/meta_harness_adapter.md), with `template: meta-harness-adapter`: one candidate per step. Each step's rollouts evaluate the candidate handed back last; when none is waiting, a Codex, Claude Code, Pi or OpenCode proposer reads every earlier candidate's files, scores and trajectories and writes k new adapters, which pass `check_spec` before they are queued; no validation data. | The proposer CLI, `data.step_size` covering the training split (the search set) |
| `rrsi_adapter` | [RRSI Adapter](../methods/rrsi_adapter.md), with `template: rrsi-adapter`: one candidate per step. Each step's rollouts evaluate the candidate handed back last; when a round's candidates are all measured, upstream's selection decides it, upstream's analyst reads the incumbent's evaluation, and m Codex, Claude Code or Pi proposer sessions edit the incumbent under RRSI's edit budget, each diff screened by upstream's critic and a smoke before it is queued; no validation data. | The proposer CLI (Codex, Claude Code or Pi), which also runs the analyst, digesters and critic, `data.step_size` covering the training split with k rows per task, `evolver.params.domain`, `python -m pip install -e . --group rrsi` |

Change a parameter with `evolver.params` in the run config; each Evolver's `evolver.yaml` under
`hermit/agent/evolver/methods/` lists them with their defaults. For `gepa`, `data.step_size` is the reflection
minibatch; for `skillopt`, it is the batch size and `trainer.epochs` the number of epochs. Both select from candidates
they keep in `state/`, and `finalize` hands back the one with the best validation score. For `reharness` it is 1, one
instance of the stream per step, and `finalize` hands back the bank as it stands. For `meta_harness_adapter` it covers
the search set, so a step is an epoch, and `finalize` hands back the candidate with the best mean score there. For
`rrsi_adapter` it covers the evolve set with k rows per task, and `finalize` hands back the incumbent, H_T.

## Interaction with the Trainer

The Trainer first runs tasks using the current adapter, writes their results, and prepares a fresh working copy.
Only then does it call `step`. Scores in the current material therefore describe the adapter **before** this update.

The Evolver can keep many candidates internally, but it selects one working adapter for the Trainer. An unchanged
adapter is a valid result. The Trainer owns Git operations; the learner edits files and reads the supplied history.
The working copy is itself a Git repository holding the adapter's ancestry (this run's commits back to the
template's root, nothing else), so `git log` and `git diff` work there; changes to its Git metadata are not
collected. The copy a task's pipeline receives for rollouts has no Git metadata.

For candidate experiments, the run offers:

```python
ctx.tools["run_agent"](task_ids=["task-1"], adapter=str(candidate_directory))
```

Only training or validation IDs are run, and in a stream (`run.mode: stream`) a training ID only once the stream has
reached it (this step's rows included), and never a training ID that is a step of a multi-step Harbor task (it runs
only in the Trainer's own stream of that task, after the steps before it); the candidate directory must be inside the
run directory. An ID outside these raises no error: it comes back as a result with status `error`, the reason in
`error`, and a judge score of 0 (no judge under `run.feedback: none`), which an Evolver that averages the results
counts as a 0. The run's budget does fail the call: once one of its runs ends with the budget reached, the call starts
no other, and when the runs in flight end it raises `BudgetExceeded` (a learner session's tool call gets an error) and
returns nothing, not even the results of the runs it made, whose cost counts all the same. Omitting `adapter` tests
the current working copy. The response contains per-task status, output, judge results, cost, and time, how the
session ended when the pipeline reports it (`exit_code`, `timed_out`: the CLI agents do), and for training tasks the
path of the run's log; it does not return the trajectories themselves. The judge results are what
[`run.feedback`](configuration.md#how-the-run-is-done-run) shows: all the judge returned under `full`, the score alone
under `score` and none under `none`, and under both of these the output and the log lose the verdict's fields and
files, as the learning material does. Its usage counts toward the run's Evolver budget.

The validation steps of a multi-step Harbor task run in one stream (each rollout's own) for the whole run, whatever
adapter each call names: give them all, in step order, in one call. A step runs only after the steps before it, in that
call or an earlier one: one called without them waits for its turn (`stream.turn_wait_s`, by default over an hour) and
comes back `infra` (`stream_order`); one called again while its driver is up (until the last step, an abort,
`stream.idle_s` without a request, or the run's end) is `infra` at once; and one called with another adapter than the
step before it continues the environment that step left.

The Evolver may also call `check_spec` before returning. The Trainer still makes the acceptance decision from the
returned files. Passing specification checks does not establish better performance.

## State and session lifetime

`state/` persists across calls and is snapshotted after completed steps. Its format belongs to the method.
The framework restores those snapshots when resuming an unfinished run. Python instance attributes alone do not
survive a process restart.

A host session handle is separate from this state. Set `supports_resume = True` only when the method can use a
previous session; the user enables it with `evolver.resume`. Agent-backed methods may start a new host process while
resuming its conversation. A new run started from an adapter begins with fresh learner state.

This separation lets a deployed adapter operate independently of the procedure that learned it.

## Minimal example

An Evolver can leave the adapter unchanged while updating its state. This method records observed scores without
calling a model or performing optimization:

```{literalinclude} ../_examples/record_only/evolver.py
:language: python
:pyobject: RecordOnlyEvolver
```

The [custom Evolver guide](custom-evolver.md) provides its imports, manifest, and an offline end-to-end command.

For the exact context and result types, see [Python types](custom-evolver.md#python-types). Execution and budget limits are covered in [Trainer](trainer.md#budgets).
