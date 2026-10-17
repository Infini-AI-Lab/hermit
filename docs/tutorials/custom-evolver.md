# Write your own Evolver

Implement a class with a `step` method when you want a different way to turn task evidence into adapter edits.
The task pipeline, Trainer checks, and version history remain the framework's responsibility.

## A complete offline learner

The Quick Start's learner reads the current batch's judge feedback and writes one supported formatting rule:

```{literalinclude} ../_examples/first_training/learner/evolver.py
:language: python
```

It is deliberately narrow: it can extract only one rule. A real method can analyze trajectories, generate and
test candidates, and select which files to return. The adapter result is the directory left in
`ctx.workspace.adapter`; `StepResult` reports the call's status, summary, and externally incurred usage.

Its manifest declares the class and defaults:

```{literalinclude} ../_examples/first_training/learner/evolver.yaml
:language: yaml
```

## Run and modify a copy

```bash
mkdir -p .demo
cp -R docs/_examples/first_training .demo/my-evolver
```

In the copied `run.yaml`, set `trainer.output_dir: runs` and `trainer.run_name: learner-demo`. Leave
`evolver.name: learner` pointing to the copied definition, then run:

```bash
harnessadapter check --config .demo/my-evolver/run.yaml
harnessadapter train --config .demo/my-evolver/run.yaml
harnessadapter eval --config .demo/my-evolver/run.yaml --adapter final
cat .demo/my-evolver/runs/learner-demo/checkout/ADAPTER.md
```

Change the learner implementation in that copy and use a new run name for each completed experiment.
No tracked task file needs to be edited or reset.

## Understand the workspace

| Path | Purpose |
|---|---|
| `adapter` | Working adapter files to edit |
| `learning_material/current/` | Current batch's task records and summary |
| `task_info` | Learner-facing task contract |
| `state` | Persistent method-owned state, saved at completed steps |
| `scratch` | Temporary per-step work |
| `output` | Reports and artifacts collected with the step |
| `history` | Earlier adapter versions and diffs |

Store search state in `state`, not only in Python attributes. Crash recovery constructs a new process.
An unchanged adapter is a valid outcome. A failed call keeps the previous adapter; it does not imply rolling
back every learner-state update.

## Use metered tools

With a complete `llm` section in the run config, a learner can call:

```python
reply = ctx.tools["llm"](messages=[{"role": "user", "content": prompt}])
```

The framework accounts for this tool's usage. Report calls made outside metered tools through
`StepResult.usage`, and avoid double counting.

For candidate tests:

```python
result = ctx.tools["run_agent"](task_ids=["train-1"], adapter=str(candidate_directory))
```

Candidate directories must be inside the run directory; only training/validation task ids are accepted.
Those calls count toward learner usage. They do not expose test evaluation to the learner.

## Choose a different execution form

| Form | Use when |
|---|---|
| `Evolver` subclass | Your method runs in Python |
| `AgentEvolver` | A coding-agent host edits files under your prompts |
| `CommandEvolver` | An external program receives workspace context through `HA_CONTEXT` |
| Subclass of a built-in method | You need to modify a small piece of an existing implementation |

The full [extension contract](../framework/custom-evolver.md) covers setup, bootstrap, finalize, types, session
resume, and cancellation. The [Evolver component](../framework/evolver.md) covers status and state semantics.
Verify failure handling, valid/invalid edits, persisted state, and usage accounting before using paid model calls.
