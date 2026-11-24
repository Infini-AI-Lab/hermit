"""Tasks: what a task set is, what each task gives the agent and keeps for the judge, and how an answer is judged
(docs/framework/task.md). A task knows nothing of the agent that answers it (hermit.agent), and the agent nothing of
the task set or the judge: the run (hermit.trainer.rollout) brings the two together, in an environment
(hermit.environment) both may use.

    task.py        Task: one task, its public half (what the agent is given) and its private half (the judge's own)
    benchmark.py   Benchmark: a task set, its splits
    verify.py      the judge's verdict on one answer
    data.py        the rows of tasks.jsonl and the split files, the records a run keeps

The names code imports from here before the package existed (RunOutput, JudgeResult, TaskData, load_callable, ...)
are all still importable from `hermit.task`.
"""
from hermit.common import RunOutput, load_callable
from hermit.task.benchmark import Benchmark
from hermit.task.data import JudgeResult, TaskData, load_pipeline, normalise_judge, output_record, read_ids
from hermit.task.task import Task, as_task
from hermit.task.verify import Verifier, judge_verifier

__all__ = ["Benchmark", "JudgeResult", "RunOutput", "Task", "TaskData", "Verifier", "as_task", "judge_verifier",
           "load_callable", "load_pipeline", "normalise_judge", "output_record", "read_ids"]
