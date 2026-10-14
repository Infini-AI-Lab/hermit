"""The built-in Codex AgentEvolver with the learner's tools gated to `check_spec`.

The Trainer's tool endpoint also serves `run_agent` (play any training task with the working copy and read the
judge's verdict), `llm` and `budget`. On a CL-Bench stream that is look-ahead: a learner that plays question 10
at step 1 writes its answer into ADAPTER.md before the host reaches it. The MWM learner of the same venue never
had those tools (its gate passes the venue's channels only), so this baseline gets the same restriction: the
agent sees one tool, `check_spec`. Everything else is the built-in Evolver unchanged.
"""
from __future__ import annotations

import dataclasses
import re

from harnessadapter.evolver.agent import AgentEvolver
from harnessadapter.evolver.base import StepContext, StepResult
from harnessadapter.evolvers.mwm.evolver import _ToolGate

ALLOWED = ("check_spec",)


class GatedCodexEvolver(AgentEvolver):
    def step(self, ctx: StepContext) -> StepResult:
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
