"""Run tasks through the acting agent and judge each answer; shared by training rollouts, eval and the acting-agent
runner offered to Evolvers. This is where a task (hermit.task) meets the agent that answers it (hermit.agent): the
agent is handed the task's public half, the verifier reads the whole task, and neither knows the other."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
import threading
import time
import traceback

from hermit.agent.base.interface import LoginFailed, login_failure
from hermit.common import RunOutput
from hermit.task import Task, as_task, judge_verifier
from hermit.trainer.budget import Budget, BudgetExceeded


def _given(pipeline, given: dict, adapter: Path | None, environment, needs: dict | None = None) -> RunOutput:
    """The agent's run: handed the task's environment when it has its own, and a sandboxed agent (one that
    `refuses`) the task's needs, whose agent timeout bounds the call."""
    kwargs = {"environment": environment} if environment is not None else {}
    if getattr(pipeline, "refuses", None) is not None and needs is not None:
        kwargs["needs"] = needs
    out = pipeline(dict(given), adapter, **kwargs)
    if not isinstance(out, RunOutput):
        raise TypeError(f"the pipeline returned {type(out).__name__}, not RunOutput")
    return out


def _failed_record(task: Task, given: dict, started: float, exc: Exception, what: str) -> dict:
    return {"id": given.get("id", task.id), "input": given, "status": "error", "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-3000:], "output": None,
            "judge": {"score": 0.0, "feedback": f"{what}: {type(exc).__name__}: {exc}"},
            "cost_usd": None, "wall_s": round(time.time() - started, 2), "api_speed": None, "tokens": None,
            "log": None, "messages": None}


def run_one(task: Task | dict, adapter: Path | None, pipeline, judge, environments=None) -> dict:
    """One task: the agent (`pipeline(given, adapter) -> RunOutput`) on the task's public half, then the judge on
    its answer: a verifier (`verifier(task, output) -> JudgeResult`, hermit.task.verify) or a bare judge
    (`score(row, output)`); the record the run keeps. A record keeps the row's own id (7 stays 7).

    With `environments` (a task set whose tasks give their own environment: hermit.environment.harbor), the task's
    environment is started first (it may wait for the disk gate), the agent is handed it (`pipeline(given, adapter,
    environment=env)`), the environment's own verifier runs on the agent's work, whatever the session's end, and its
    verdict is the output's `extra["verified"]`, which the judge reads (hermit.task.verify.environment); then the
    environment is stopped. An environment that cannot be given is a failed run (not the agent's result)."""
    task = as_task(task)
    verify = judge if getattr(judge, "takes_task", False) else judge_verifier(judge)
    given = task.given()                                 # what the agent is handed, and what the record shows
    started = time.time()
    refuses = getattr(pipeline, "refuses", None)         # a sandboxed agent: what the task asks that the run lacks
    problems = refuses(task.private.get("needs") or {}) if refuses and environments is None else []
    if problems:                                         # (a task's own environment gives what it asks itself)
        exc = ValueError(f"task {task.id!r} cannot run in this run's environment: {'; '.join(problems)}")
        return _failed_record(task, given, started, exc, "the pipeline failed")
    if environments is not None:
        from hermit.environment.harbor import EnvironmentUnavailable
        try:
            with environments.session(environments.task_dir(task), task.id) as env:
                try:
                    out = _given(pipeline, given, adapter, env, task.private.get("needs") or {})
                except Exception as exc:
                    why = login_failure(exc)
                    if why is not None:
                        raise LoginFailed(why) from None
                    return _failed_record(task, given, started, exc, "the pipeline failed")
                logs = Path(out.log) if out.log and Path(out.log).is_dir() else None
                out.extra = {**(out.extra or {}), "verified": env.verify(agent_logs=logs)}
        except (EnvironmentUnavailable, ValueError) as exc:
            return _failed_record(task, given, started, exc, "the task's environment could not be given")
        return _judged(task, given, out, verify, started)
    try:
        out = _given(pipeline, given, adapter, None, task.private.get("needs") or {})
    except Exception as exc:
        why = login_failure(exc)                     # as text from a sandbox's worker, or from a chat endpoint
        if why is not None:
            raise LoginFailed(why) from None
        return _failed_record(task, given, started, exc, "the pipeline failed")
    failed = (out.extra or {}).get("session_failed")
    if failed:                                           # no answer to judge; what it spent and its log still count
        return {"id": given.get("id", task.id), "input": given, "status": "error", "error": f"the agent's session failed: {failed}",
                "output": None, "judge": {"score": 0.0, "feedback": f"the agent's session failed: {failed}"},
                "cost_usd": out.cost_usd, "wall_s": round(out.wall_s or (time.time() - started), 2),
                "api_speed": out.api_speed, "tokens": out.tokens, "log": str(out.log) if out.log else None,
                "messages": out.messages, "pipeline_extra": out.extra}
    return _judged(task, given, out, verify, started)


def _judged(task: Task, given: dict, out: RunOutput, verify, started: float) -> dict:
    try:
        verdict = verify(task, out)
        judged = asdict(verdict)
        status, error = "ok", None
    except Exception as exc:
        judged = {"score": 0.0, "feedback": f"the judge failed: {type(exc).__name__}: {exc}"}
        status, error = "judge_error", f"{type(exc).__name__}: {exc}"
    return {"id": given.get("id", task.id), "input": given, "status": status, "error": error, "output": out.output,
            "judge": judged, "cost_usd": out.cost_usd, "wall_s": round(out.wall_s or (time.time() - started), 2),
            "api_speed": out.api_speed, "tokens": out.tokens, "log": str(out.log) if out.log else None,
            "messages": out.messages, "pipeline_extra": out.extra}


def run_tasks(tasks: list[Task | dict], adapter: Path | None, pipeline, judge, *, concurrency: int,
              budget: Budget | None = None, part: str = "rollout", environments=None) -> list[dict]:
    """All tasks, in the given order. Raises BudgetExceeded (after in-flight tasks end) when a hard budget is hit.
    `environments`: the task set's own environments (run_one)."""
    results: dict[str, dict] = {}
    stop = threading.Event()

    def work(task):
        if stop.is_set():
            return None
        try:
            record = run_one(task, adapter, pipeline, judge, environments)
        except LoginFailed:
            stop.set()                                   # the tasks waiting would fail the same way
            raise
        if budget is not None:
            budget.add(part, usd=record["cost_usd"] or 0.0, tokens=record["tokens"] or 0)
            if record["cost_usd"] is None:
                with budget._lock:
                    budget.unknown_cost_calls += 1
            if budget.exceeded():
                stop.set()
        return record

    tasks = [as_task(t) for t in tasks]
    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
        futures = {pool.submit(work, t): t.id for t in tasks}
        for future in as_completed(futures):
            record = future.result()
            if record is not None:
                results[futures[future]] = record        # by the task's id: a record keeps the row's own
    if stop.is_set() and budget is not None:
        raise BudgetExceeded(budget.exceeded() or "budget reached")
    return [results[t.id] for t in tasks if t.id in results]
