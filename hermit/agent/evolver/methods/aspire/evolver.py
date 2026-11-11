"""AspireEvolver: the coordinator of ASPIRE (Agentic Skills Discovery for Robotics; arXiv:2607.00272, §2.2 and §2.4;
github.com/NVlabs/ASPIRE, `.claude/skills/iterative-debugging/main-agent-prompt.md`) as an Evolver on the `aspire`
template: a Codex (or Claude Code or Pi) session, as the built-in agent Evolvers run it, given the guide's own sections on
updating skills, dispatching verifiers and choosing the curriculum (COORDINATOR.md).

In ASPIRE a coordinator agent dispatches one fix-loop subagent per robot task, runs its Stage 2 evaluation, updates
the shared skill library from the subagent's findings and skill report (repairs on graded entries first; proposals
only after a fresh verifier re-solved the task from the library and the proposals alone), and chooses which task runs
next. On the Trainer, one step is one finished task: the step's rollout is the subagent and its Stage 2 (the acting
agent and its `collect`), and this Evolver is the rest of the coordinator. Its session

1. reads the task's record in the learning material (findings, skill report, fix_code.py, Stage 2 and unseen-check
   results) and edits the library in the adapter;
2. verifies proposals with the Trainer's `run_agent` on the task's verifier row (`verify-<task id>`, data.val), on a
   copy of the library whose `skills/proposed.md` holds them;
3. chooses the next task from the epoch's remaining ones (it declares `chooses_tasks`), recording its round in
   `state/curriculum.md` and naming the task in `output/next_tasks.json`, which this class returns as
   StepResult.next_tasks.

The Trainer checks the library with the template's check (fields, snippets under the program rules, no privileged
access, no `skills/proposed.md`) and repairs what fails. ASPIRE's Phase 3 rerun is the evaluation of the final
library on every task (the example's `rerun-*` rows).
"""
from __future__ import annotations

import json
from pathlib import Path

from hermit.agent.evolver.agent import AgentEvolver
from hermit.agent.evolver.base import StepContext, StepResult
from hermit.sandbox import read_regular

NEXT = "next_tasks.json"


class AspireEvolver(AgentEvolver):
    chooses_tasks = True

    def step(self, ctx: StepContext) -> StepResult:
        result = super().step(ctx)
        result.next_tasks = next_tasks(ctx.workspace.output / NEXT)
        return result


def next_tasks(path: Path) -> list | None:
    """The task ids the session named in output/next_tasks.json (a JSON list), or None; the Trainer checks them."""
    text = read_regular(path)                  # the learner's file, read on the host: no link, no pipe
    try:
        value = json.loads(text) if text is not None else None
    except ValueError:
        return None
    return value if isinstance(value, list) else None
