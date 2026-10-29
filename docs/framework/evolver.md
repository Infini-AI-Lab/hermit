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

`step` hands back the adapter **through the files it leaves in `ctx.workspace.adapter`**.
Its return value describes the call; it does not contain the adapter files.
`finalize` may return a different adapter directory for the Trainer to collect.

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
| `sandbox`, `cancel`, `tool_url` | Program execution helper, cancellation signal, and tool endpoint. |
| `progress` | On step calls: epoch, step, and whether the epoch ends here. |
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

```{literalinclude} ../../hermit/evolver/base.py
:language: python
:pyobject: StepResult
```

`Usage` reports model calls, token counts, and cost when known. Calls through the run's metered `llm` tool are already
accounted for; do not report the same usage again in `StepResult`.

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
| `codex`, `claude` | A Codex or Claude Code agent edits the adapter from the learning material; `codex` on the `prompt-only@1.0` template is the [prompt-only](../methods/prompt-only.md) method. | The agent CLI |
| `gepa` | [GEPA](https://github.com/gepa-ai/gepa): one iteration per step. It reflects on the step's trajectories, keeps a child that does better on the same tasks, scores it on validation, and hands back the next parent from its Pareto front. | `llm`, `data.val`, `python -m pip install -e . --group gepa` |
| `skillopt` | [SkillOpt](https://github.com/microsoft/SkillOpt): one SkillOpt step per training step. It turns the step's trajectories into a few bounded edits under its learning rate (decaying when `total_steps` is set) and keeps them only if the validation score improves; the slow update and meta skill run at each epoch's end. | `llm`, `data.val`, `python -m pip install -e . --group skillopt` |
| `ace` | [ACE](https://github.com/ace-agent/ace): per rollout, a Reflector tags the playbook bullets the agent named and diagnoses the attempt (a wrong answer is run again with the reflection, up to `max_num_rounds`), and a Curator adds bullets; the playbook grows by deltas. See `docs/methods/ace.md`. | `llm`; `data.val` to keep the best validation playbook |
| `mwm` | MetaWorld Model, with `template: mwm`: one native MWM learner session per block (the step), under the MWM contract, with the task's evidence store mounted at `/evidence`, in its own network namespace. See `docs/methods/mwm.md`. | The Codex CLI, a task that keeps an MWM evidence store (`evolver_access.environment`), `task-info/mwm/`, `data.shuffle: false` |
| `reharness` | [ReHarness](../methods/reharness.md), with `template: reharness`: one instance of the stream per step. It revises each reflection the acting agent retrieved, from that reflection's own source trajectory with the new trajectory as feedback, then writes the instance's reflection with the revised ones as demonstrations, through the run's `llm` tool with the paper's prompts; no validation data. | `llm`, `data.step_size: 1` and `data.shuffle: false`, an embedding endpoint (`evolver.params.embedding`, and `REHARNESS_EMBED_URL` for the acting side's reader; BM25 without) |
| `meta_harness_adapter` | [Meta-Harness Adapter](../methods/meta_harness_adapter.md), with `template: meta-harness-adapter`: one candidate per step. Each step's rollouts evaluate the candidate handed back last; when none is waiting, a Codex or Claude Code proposer reads every earlier candidate's files, scores and trajectories and writes k new adapters, which pass `check_spec` before they are queued; no validation data. | The proposer CLI, `data.step_size` covering the training split (the search set) |

Change a parameter with `evolver.params` in the run config; each Evolver's `evolver.yaml` under
`hermit/algorithms/evolvers/` lists them with their defaults. For `gepa`, `data.step_size` is the reflection
minibatch; for `skillopt`, it is the batch size and `trainer.epochs` the number of epochs. Both select from candidates
they keep in `state/`, and `finalize` hands back the one with the best validation score. For `reharness` it is 1, one
instance of the stream per step, and `finalize` hands back the bank as it stands. For `meta_harness_adapter` it covers
the search set, so a step is an epoch, and `finalize` hands back the candidate with the best mean score there.

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

Only training or validation IDs are accepted, and the candidate directory must be inside the run directory.
Omitting `adapter` tests the current working copy. The response contains per-task status, output, judge results,
cost, and time, and for training tasks the path of the run's log; it does not return the trajectories themselves. Its usage counts toward the run's Evolver budget.

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
