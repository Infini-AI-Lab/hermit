"""The judge's verdict on one answer: a score in [0, 1], feedback a learning method can read, and anything else the
judge records (JudgeResult). How a task is judged is the task set's own, not the experiment's: a judge function in
this process on the answer it reads from where the agent could not (`judge.mode: host`), or in a sandbox beside the
agent's (`shared` over the agent's work, `separate` over what is collected; hermit.isolate builds those).

    verifier = judge_verifier(score)          # score(task_row, output) -> JudgeResult | dict | float
    verdict = verifier(task, output)          # JudgeResult
"""
from __future__ import annotations

from typing import Callable, Protocol

from hermit.common import RunOutput
from hermit.task.data import JudgeResult, normalise_judge
from hermit.task.task import Task


class Verifier(Protocol):
    def __call__(self, task: Task, output: RunOutput) -> JudgeResult: ...


def judge_verifier(judge: Callable) -> Verifier:
    """A verifier over a judge as a task set writes it: called with the task's row (its public half and nothing the
    agent was not given) and the agent's output; whatever it returns, normalised."""
    def verify(task: Task, output: RunOutput) -> JudgeResult:
        return normalise_judge(judge(task.given(), output))
    verify.judge = judge                       # the judge it wraps (a sandboxed one keeps its own state)
    verify.takes_task = True                   # called with a Task (hermit.trainer.rollout tells it from a bare judge)
    return verify
