# Custom Evolver Integration

:::{note}
Agreed 2026-10-05; implemented in 0.2.0. Implementation notes at the end of this page.
:::

The Evolver is a swappable component. The framework ships two ready implementations; anything else is a subclass of
one abstract class. The Trainer calls only this interface: what happens inside is up to the implementation.

## Interface

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol


# ---------- What the Trainer gives the Evolver ----------

@dataclass(frozen=True)
class Workspace:
    adapter: Path             # working copy of the current adapter (read/write)
    learning_material: Path   # step_NNNN/ directories, current, index.jsonl, README.md (read-only)
    task_info: Path           # task description (read-only)
    history: Path             # read-only view of the adapter's git history
    state: Path               # Training State (read/write, the Evolver's own format)
    scratch: Path             # temporary files (read/write, not kept)
    output: Path              # what the call hands back besides the adapter (write)


@dataclass(frozen=True)
class Access:
    validation: Path | None = None        # set when data.val is given
    environment: list[Path] = field(default_factory=list)   # from evolver_access.environment


@dataclass(frozen=True)
class Progress:
    epoch: int
    step: int
    is_epoch_end: bool


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict
    output_schema: dict
    source: Literal["template", "task", "run"]

    def __call__(self, **arguments: Any) -> dict: ...


@dataclass(frozen=True)
class Context:
    workspace: Workspace
    access: Access
    tools: dict[str, Tool]        # always includes "check_spec"; every call is logged and metered
    params: dict[str, Any]        # evolver.params from the run config, over the manifest defaults


@dataclass(frozen=True)
class StepContext(Context):
    progress: Progress
    session: str | None = None    # previous call's session handle, set only when resuming


# ---------- What the Evolver hands back ----------

@dataclass
class StepResult:
    status: Literal["done", "failed", "out_of_budget", "stop"]
    summary: str                           # one line: what changed, or that nothing did
    rationale: str = ""                    # optional: why it changed the adapter this way, or why not
    session: str | None = None             # handle for resuming next time, if any
    usage: list[Usage] = field(default_factory=list)   # model usage of this call (Usage: see Evolver page)
    extra: dict[str, Any] = field(default_factory=dict)  # recorded unchanged


@dataclass
class FinalResult:
    adapter: Path                          # the adapter to hand back as the final result
    summary: str
    extra: dict[str, Any] = field(default_factory=dict)


# ---------- The interface ----------

class Evolver(ABC):
    supports_resume: bool = False

    def setup(self, ctx: Context) -> None:
        """Optional: before the first step (for example, initialise Training State)."""

    @abstractmethod
    def step(self, ctx: StepContext) -> StepResult:
        """Read the learning material and change the adapter in ctx.workspace.adapter (or leave it)."""

    def finalize(self, ctx: Context) -> FinalResult:
        """Optional: after training. Default: hand back the current adapter."""
        return FinalResult(adapter=ctx.workspace.adapter, summary="current adapter")
```

Only `step` must be implemented.

| `status` | Meaning | What the Trainer does |
|---|---|---|
| `done` | The step finished | Check, commit if changed, continue |
| `failed` | The step could not finish | Keep the previous adapter, record, continue |
| `out_of_budget` | The Evolver ran out of the budget it was given | Same as `failed` |
| `stop` | The Evolver considers training converged | Stop and call `finalize` if `trainer.allow_early_stop` is set; otherwise record it and treat it as `done` |

## Manifest

An Evolver is declared by a small YAML manifest naming its class, the resources it cannot run without, and its default
parameters:

```yaml
# evolver.yaml
class: my_evolvers.gepa:GepaEvolver     # or harnessadapter:AgentEvolver
needs: [validation_data]                # validation_data | environment | send_material_out
params:
  pool_size: 8
  minibatch: 4
```

`train` refuses to start if a need is not given by the run config. The task user selects an Evolver by name
(`evolver.name`) and may leave `evolver.params` empty.

## Built-in implementations

A provider who does not want to write code supplies prompts or a command:

| Implementation | What it does inside | The provider supplies |
|---|---|---|
| `AgentEvolver` | Starts a built-in agent (Codex or Claude Code) on the workspace with the prompt | A start prompt, optionally a resume prompt, and a model |
| `CommandEvolver` | Starts any external program and passes it the call's information and tool endpoints | A command |

Anything else (a ReAct loop, a multi-agent system, GEPA, SkillOpt) is a subclass of `Evolver`.

## Example: a minimal custom Evolver

```python
class OneShotRewriter(Evolver):
    """Rewrites ADAPTER.md once per step from the failures of the current step."""

    def step(self, ctx: StepContext) -> StepResult:
        material = ctx.workspace.learning_material / "current"
        failures = [t for t in load_tasks(material) if t["score"] < 1]
        if not failures:
            return StepResult(status="done", summary="no failures; adapter unchanged")
        text, usage = rewrite_with_model(ctx.workspace.adapter / "ADAPTER.md", failures, ctx.params)
        (ctx.workspace.adapter / "ADAPTER.md").write_text(text)
        report = ctx.tools["check_spec"](path=str(ctx.workspace.adapter))
        if not report["passed"]:
            restore(ctx.workspace.adapter / "ADAPTER.md", ctx.workspace.history)
            return StepResult(status="done", summary="rewrite failed check_spec; reverted", usage=[usage])
        return StepResult(status="done", summary=f"rewrote ADAPTER.md from {len(failures)} failures",
                          usage=[usage])
```

## Black-box Evolvers

An Evolver may be a complete black box (a third-party service or a closed binary) wrapped by a thin subclass that
translates the call to and from it. The Trainer's guarantees do not depend on what is inside: test data is never
given to it, the binding check runs after every call, and wall time and reported usage are recorded. What a black box
cannot offer is stated, not assumed: its usage may be unknown (then only wall time and any price the provider reports
are recorded), and it may not support resume. If it sends learning material to an outside service, it must declare
`send_material_out` in `needs`, and the user must set `evolver_access.send_material_out: true`.

## Trust

A custom class runs inside the Trainer process, so its code is trusted. The agents and commands it starts run in the
sandbox when the sandbox is enabled. Running untrusted third-party Evolver code would require running the class
itself in a sandboxed subprocess.

## Implementation notes

- `harnessadapter.evolver.base` holds the classes above; `AgentEvolver` and `CommandEvolver` are in
  `harnessadapter.evolver.agent`.
- `AgentEvolver` params: `backend` (codex | claude), `model`, `prompt`, `resume_prompt`, `args` (extra command-line
  arguments) and `home` (login files copied from the user's agent home into the run's agent home; removed again by
  `finalize`). Prompts may use `{message} {epoch} {step} {adapter} {learning_material} {task_info} {history} {state}
  {scratch} {output}`.
- `CommandEvolver` params: `command`, `env`. The program receives `HA_CONTEXT` (a JSON file) and `HA_TOOL_URL`, and
  may write `output/step_result.json`.
- An Evolver class named in `evolver.yaml` as `file.py:Class` is loaded from the Evolver's directory; `module:Class`
  is imported. The Evolver's directory stays readable inside the sandbox.
- A hard budget or a per-call timeout stops an Evolver at the next point the Trainer controls: its framework tools
  refuse further calls, programs it started are killed, and the Trainer waits for the call to return. Work between two
  such points finishes, so spending may slightly exceed the budget (official GEPA does the same: its stop conditions
  are checked once per iteration). An Evolver that calls models outside the framework should check `ctx.cancel`.
