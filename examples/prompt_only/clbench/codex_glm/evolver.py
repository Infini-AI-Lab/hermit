"""The built-in Codex AgentEvolver with the learner's tools gated to `check_spec`.

The Trainer's tool endpoint also serves `run_agent` (play any training task with the working copy and read the
judge's verdict), `llm` and `budget`. On a CL-Bench stream that is look-ahead: a learner that plays question 10
at step 1 writes its answer into ADAPTER.md before the host reaches it. The MWM learner of the same venue never
had those tools (its gate passes the venue's channels only), so this baseline gets the same restriction: the
agent sees one tool, `check_spec`. Everything else is the built-in Evolver unchanged.
"""
from __future__ import annotations

import dataclasses
import importlib.util
from pathlib import Path
import re

from harnessadapter.evolver.agent import AgentEvolver
from harnessadapter.evolver.base import StepContext, StepResult
from harnessadapter.algorithms.evolvers.mwm.evolver import _ToolGate

ALLOWED = ("check_spec",)


FRAMEWORK = Path(__file__).resolve().parents[4]


class _HidingSandbox:
    """The run's sandbox with paths covered: a directory by an empty tmpfs, a file by /dev/null, and `keep` bound
    back on top. The framework's sandbox shows the whole Python installation and the framework checkout; a learner
    that reads the task's data (tasks.jsonl holds the test tasks), the checkout's git objects or a benchmark
    installed beside the framework (its evaluator and task generator) is reading past the learning material. The
    smoke of 2026-10-14 saw the GLM learner search for exactly these."""

    def __init__(self, inner, hidden: list[Path], keep: list[Path] = ()):
        self.inner, self.hidden, self.keep = inner, hidden, list(keep)

    def wrap(self, command, **kw):
        args, env = self.inner.wrap(command, **kw)
        if args and args[0] == "bwrap" and "--" in args:
            cut = args.index("--")
            cover = []
            for p in self.hidden:
                p = p.resolve()
                if p.is_dir():
                    cover += ["--tmpfs", str(p)]
                elif p.is_file():
                    cover += ["--ro-bind", "/dev/null", str(p)]
            for p in self.keep:
                p = p.resolve()
                if p.exists():
                    cover += ["--ro-bind", str(p), str(p)]
            args = args[:cut] + cover + args[cut:]
        return args, env


def hidden_paths(params: dict, ctx: StepContext | None = None) -> list[Path]:
    """Always: the framework checkout's task code and data, tests, docs and git objects (the task description is
    bound back by the caller). `hide`: more paths, or "module:<name>" for an installed Python package."""
    out = [FRAMEWORK / "examples", FRAMEWORK / "tests", FRAMEWORK / "docs", FRAMEWORK / ".git"]
    for item in params.get("hide") or []:
        item = str(item)
        if item.startswith("module:"):
            spec = importlib.util.find_spec(item[len("module:"):])
            if spec is None or not spec.submodule_search_locations:
                raise RuntimeError(f"hide: no installed package named {item[len('module:'):]!r}")
            out += [Path(loc) for loc in spec.submodule_search_locations]
        else:
            out.append(Path(item).expanduser())
    return out


class GatedCodexEvolver(AgentEvolver):
    def step(self, ctx: StepContext) -> StepResult:
        if ctx.sandbox is not None:
            ctx = dataclasses.replace(ctx, sandbox=_HidingSandbox(ctx.sandbox, hidden_paths(ctx.params, ctx),
                                                                   keep=[ctx.workspace.task_info]))
        if not ctx.tool_url:
            return super().step(ctx)
        gate = _ToolGate(ctx.tool_url, list(ALLOWED))
        # The Trainer's message names every tool and the endpoint's URL (with its token); a learner with a shell
        # reads it and curls the endpoint past any MCP-side filter. The learner gets the gate's URL and the gated
        # tool list instead, in the message and in the MCP server alike.
        message = ctx.message.replace(ctx.tool_url, gate.url)
        message = re.sub(r"^Tools: .*$", "Tools: " + ", ".join(f"{n} (template)" for n in ALLOWED), message,
                         flags=re.M)
        try:
            return super().step(dataclasses.replace(ctx, tool_url=gate.url, message=message))
        finally:
            gate.close()
