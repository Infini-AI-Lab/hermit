"""The Evolver interface: the only surface the Trainer calls. What happens inside is up to the implementation."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from hermit.agent.model.usage import Usage   # re-exported: Evolvers report usage in StepResult


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
    validation: Path | None = None                          # set when data.val is given
    environment: tuple[Path, ...] = ()                      # from evolver_access.environment


@dataclass(frozen=True)
class Progress:
    epoch: int
    step: int
    is_epoch_end: bool
    remaining: tuple[str, ...] = ()   # for an Evolver that chooses_tasks: the epoch's training tasks not yet run


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
    params: dict[str, Any]        # evolver.params from the run config over the manifest defaults
    message: str = ""             # the task message of this call: accesses, tools, progress, remaining budget
    sandbox: Any = None           # the run's sandbox: wraps commands an Evolver starts (see hermit.sandbox)
    cancel: Any = None            # threading.Event set by the Trainer when the call must stop (budget, timeout)
    tool_url: str | None = None   # HTTP endpoint serving the same tools (for agents and external programs)
    base: Path | None = None      # the run config's directory: relative paths in params resolve against it
    agent: Any = None             # the run's model as a `direct` Base Agent, metered like the tools (see `ask`)


@dataclass(frozen=True)
class StepContext(Context):
    progress: Progress | None = None
    session: str | None = None    # previous call's session handle, set only when resuming


def ask(agent, messages: list[dict], **sampling) -> dict:
    """One call of the run's model through the Evolver's Base Agent (`ctx.agent`): {content, reasoning,
    finish_reason, usage}. `sampling` is the request's (max_tokens, temperature, ...); None leaves a setting as it
    is."""
    if agent is None:
        raise RuntimeError("the run has no model for the Evolver: set the run config's `llm`")
    result = agent.configured(**sampling).run(messages)
    usage = result.usage[0] if result.usage else None
    return {"content": result.reply, "reasoning": result.reasoning, "finish_reason": result.finish_reason,
            "usage": usage.__dict__ if usage is not None else {}}


# ---------- What the Evolver hands back ----------

@dataclass
class StepResult:
    status: Literal["done", "failed", "out_of_budget", "stop"]
    summary: str                                         # one line: what changed, or that nothing did
    rationale: str = ""                                  # optional: why it changed the adapter this way, or why not
    session: str | None = None                           # handle for resuming next time, if any
    usage: list[Usage] = field(default_factory=list)     # model usage of this call
    extra: dict[str, Any] = field(default_factory=dict)  # recorded unchanged
    next_tasks: list[str] | None = None                  # chooses_tasks: the next step's tasks, of progress.remaining


@dataclass
class FinalResult:
    adapter: Path                 # the adapter to hand back as the final result
    summary: str
    extra: dict[str, Any] = field(default_factory=dict)


# ---------- The interface ----------

class Evolver(ABC):
    supports_resume: bool = False
    # True: the Evolver chooses the next step's tasks (StepResult.next_tasks); only then does it see the epoch's
    # remaining training tasks (Progress.remaining, and in the call's message).
    chooses_tasks: bool = False

    def setup(self, ctx: Context) -> None:
        """Optional: before the first step (for example, initialise Training State)."""

    def bootstrap(self, ctx: StepContext) -> StepResult | None:
        """Optional: change the starting adapter once, before the first rollouts of a fresh run (for example, a
        learner session that reads the task's public information). The Trainer checks and commits the result as
        step 0. Not overridden (or returning None): no bootstrap."""
        return None

    @abstractmethod
    def step(self, ctx: StepContext) -> StepResult:
        """Read the learning material and change the adapter in ctx.workspace.adapter (or leave it)."""

    def finalize(self, ctx: Context) -> FinalResult:
        """Optional: after training. Default: hand back the current adapter."""
        return FinalResult(adapter=ctx.workspace.adapter, summary="current adapter")
