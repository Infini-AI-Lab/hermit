"""The Evolver interface: the only surface the Trainer calls. What happens inside is up to the implementation."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    cost_usd: float | None = None   # None when no price applies (for example a subscription)


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
    sandbox: Any = None           # the run's sandbox: wraps commands an Evolver starts (see harnessadapter.sandbox)
    cancel: Any = None            # threading.Event set by the Trainer when the call must stop (budget, timeout)
    tool_url: str | None = None   # HTTP endpoint serving the same tools (for agents and external programs)


@dataclass(frozen=True)
class StepContext(Context):
    progress: Progress | None = None
    session: str | None = None    # previous call's session handle, set only when resuming


# ---------- What the Evolver hands back ----------

@dataclass
class StepResult:
    status: Literal["done", "failed", "out_of_budget", "stop"]
    summary: str                                         # one line: what changed, or that nothing did
    rationale: str = ""                                  # optional: why it changed the adapter this way, or why not
    session: str | None = None                           # handle for resuming next time, if any
    usage: list[Usage] = field(default_factory=list)     # model usage of this call
    extra: dict[str, Any] = field(default_factory=dict)  # recorded unchanged


@dataclass
class FinalResult:
    adapter: Path                 # the adapter to hand back as the final result
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
