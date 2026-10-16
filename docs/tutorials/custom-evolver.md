# 5. Write your own Evolver

You want a different learning method: what is read from the task feedback and how the adapter is changed. The
Trainer, the task pipeline, the specification check and the adapter tree stay; you implement one class with a
`step` method and declare it in a small manifest.

## The smallest learning Evolver

`docs/_examples/notes_evolver/` turns the judge's feedback on failed tasks into notes in `ADAPTER.md`. It makes no
model call, so it runs offline:

```{literalinclude} ../_examples/notes_evolver/evolver.py
:language: python
```

What a step gets (`ctx`, a `StepContext`):

| Field | What it is |
|---|---|
| `ctx.workspace.adapter` | the working copy of the adapter (read/write): change it, or leave it |
| `ctx.workspace.learning_material` | one directory per step; `current/` is this step's: `summary.json`, `tasks/<id>.json` (input, output, judge, trajectory) |
| `ctx.workspace.task_info` | the task description |
| `ctx.workspace.state` | your own files across steps |
| `ctx.workspace.output` | what you hand back besides the adapter (`report.md` is shown with the commit) |
| `ctx.tools` | `check_spec` (always), the run's `llm` and `budget`, `run_agent`, and the task's tools; `ctx.tool_url` serves the same over HTTP |
| `ctx.progress` | epoch, step, whether this is the epoch's last step |

What it hands back: a `StepResult` with a status (`done`, `failed`, `out_of_budget`, `stop`), a one-line summary,
and the model usage it made outside the framework's metered tools. The Trainer then runs the specification check
on the working copy, repairs or rejects what fails it, and commits.

The manifest beside it:

```{literalinclude} ../_examples/notes_evolver/evolver.yaml
:language: yaml
```

## Run it

With the offline echo task:

```bash
harnessadapter train --config docs/_examples/interface_task/run.yaml evolver.name=../notes_evolver trainer.run_name=notes-demo
cat .demo/interface-example/notes-demo/checkout/ADAPTER.md
```

The echo agent answers every task correctly, so the notes stay empty; make a task fail to see a note appear:

```bash
sed -i 's/"expected":"first input"/"expected":"FIRST"/' docs/_examples/interface_task/tasks.jsonl
harnessadapter train --config docs/_examples/interface_task/run.yaml evolver.name=../notes_evolver trainer.run_name=notes-demo-02
cat .demo/interface-example/notes-demo-02/checkout/ADAPTER.md
git checkout docs/_examples/interface_task/tasks.jsonl
```

## Calling a model

A real method asks a model what to write. Use the run's metered `llm` tool, so the call counts toward the budget
and is logged:

```python
reply = ctx.tools["llm"](messages=[{"role": "user", "content": prompt}])
```

with `llm:` configured in the run config. Calls made another way are reported through `StepResult.usage`.

## Three other ways to get a learner

| Way | When |
|---|---|
| `AgentEvolver` (`harnessadapter.evolver.agent`) | the learner is a coding agent (Codex or Claude Code) editing the adapter directory under a prompt of yours; `harnessadapter/algorithms/evolvers/codex/` is one |
| `CommandEvolver` | the learner is an external program; it gets the workspace as JSON (`HA_CONTEXT`) and edits the directory |
| a subclass of a built-in Evolver | one behaviour changes, the rest stays: override `step`, adjust the context, call the parent |

The contract in full, resume, bootstrap, cancellation and the Python types are in
[Custom Evolver integration](../framework/custom-evolver.md) and [Evolver](../framework/evolver.md).
