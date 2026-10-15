# Custom Evolver integration

Implement an Evolver when you want to change the learning method while reusing the task pipeline, adapter
specification, and Trainer. Start with the [Evolver contract](evolver.md) for inputs, outputs, and lifecycle behavior.

## Implement the method

Subclass `Evolver` and implement `step`. The following complete example reads the current batch summary, appends
an observation to persistent state, writes a report, and leaves the adapter unchanged:

```{literalinclude} ../_examples/record_only/evolver.py
:language: python
```

The unchanged adapter is the one this step selects. A method that proposes changes writes them into
`ctx.workspace.adapter`; a method with a candidate pool copies its selected candidate there. `StepResult` reports
the call's status and usage.

This example exercises the contract without model calls or optimization. It uses the default `setup` and
`finalize` methods. Put any state needed after a process restart in `workspace.state`, rather than only in instance
attributes.

## Declare and select it

Place `evolver.yaml` beside `evolver.py`:

```{literalinclude} ../_examples/record_only/evolver.yaml
:language: yaml
```

The `class` value is either `file.py:ClassName`, relative to the manifest directory, or an importable
`package.module:ClassName`. The loader constructs the class without arguments and merges manifest `params` with
run-level `evolver.params`.

Select its directory in the run config:

```yaml
evolver:
  name: /path/to/record_only
  params: {}
```

Use `needs` to declare required resources: `validation_data`, `environment`, or `send_material_out`. The Trainer
checks them against the run settings before starting. Set `supports_resume = True` if the method understands the
previous session handle; this is independent of storing files in `state/`.

## Run the interface example

From the repository root, with the package installed:

```bash
harnessadapter check --config docs/_examples/interface_task/run.yaml
harnessadapter train --config docs/_examples/interface_task/run.yaml
harnessadapter eval --config docs/_examples/interface_task/run.yaml --adapter final
```

The task is a local echo stub. The run has two training steps and one test task; no model is called.
Inspect `.demo/interface-example/interface-demo/` for the learning material, per-step
`state/observations.json` snapshots, final adapter card, and evaluation records.

This example uses trusted in-process Python and disables the learner-program sandbox. For a fresh repeat, add
`trainer.run_name=interface-demo-02` to the commands. See [Trainer](trainer.md) for resume behavior.

## Agent and command implementations

You can reuse a launcher instead of implementing a new Python learning loop:

| Class | What you supply | How it hands back an adapter |
|---|---|---|
| `harnessadapter.evolver.agent:AgentEvolver` | Backend, prompt files, model selection, and host arguments. | The learner agent edits the working directory. |
| `harnessadapter.evolver.agent:CommandEvolver` | An external command and optional environment values. | The program edits the working directory and may write a result file. |

`AgentEvolver` supports `codex` and `claude`. Its parameters include `backend`, `prompt`, `resume_prompt`, `model`,
`args`, and `home`. Prompt placeholders include the workspace paths, `message`, `epoch`, and `step`.
The bundled definitions are under `harnessadapter/algorithms/evolvers/`.

`CommandEvolver` supplies `HA_CONTEXT`, a path to JSON describing the workspace, access, progress, session, and
parameters, plus `HA_TOOL_URL` for the tool endpoint. A program may write `output/step_result.json` using the
`StepResult` fields. Without that file, the process exit code determines success or failure.
Return a failure status when the program cannot produce a usable result.

The same protocol can wrap an external service. Declare required data access, preserve the adapter's specification,
and report known usage; the wrapper still owns translating the service's result into the working directory.

## Execution and cancellation

Custom classes run in the Trainer process. Their Python code is trusted; the sandbox object wraps programs launched
through it, not the class itself. Framework tools are callable in-process and can also be exposed to programs through
HTTP or MCP.

During a step, long-running custom code should check `ctx.cancel` when present. A budget or timeout stops work at
the next boundary the framework controls; it cannot forcibly interrupt arbitrary Python code. See
[budgets](trainer.md#budgets) for accounting and cancellation limits.

Report calls made outside metered framework tools through `StepResult.usage`. Leave cost null when unknown.
Calls through the run's metered model tool must not be counted a second time.

## Python types

These excerpts come directly from the implementation. The [Evolver page](evolver.md#interface) explains their
semantics and intended access boundaries.

### Lifecycle

```{literalinclude} ../../harnessadapter/evolver/base.py
:language: python
:pyobject: Evolver
```

### Workspace and context

```{literalinclude} ../../harnessadapter/evolver/base.py
:language: python
:pyobject: Workspace
```

```{literalinclude} ../../harnessadapter/evolver/base.py
:language: python
:pyobject: Access
```

```{literalinclude} ../../harnessadapter/evolver/base.py
:language: python
:pyobject: Context
```

```{literalinclude} ../../harnessadapter/evolver/base.py
:language: python
:pyobject: Progress
```

```{literalinclude} ../../harnessadapter/evolver/base.py
:language: python
:pyobject: StepContext
```

### Results and usage

```{literalinclude} ../../harnessadapter/evolver/base.py
:language: python
:pyobject: StepResult
```

```{literalinclude} ../../harnessadapter/evolver/base.py
:language: python
:pyobject: FinalResult
```

```{literalinclude} ../../harnessadapter/evolver/base.py
:language: python
:pyobject: Usage
```
