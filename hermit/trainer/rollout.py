"""Run tasks through the user's pipeline and score them with the judge; shared by training rollouts, eval and the
acting-agent runner offered to Evolvers.

A record's status: `ok` (judged), `error` (the pipeline or the agent's session failed: scored 0, the agent's result),
`judge_error`, or `infra` (the pipeline says the run is not the agent's result, `RunOutput.extra["unscored"]`: the
host, the provider, a disk gate, a halt; never judged: `mean_score` leaves it out, and so do the Trainer's and hermit
eval's means, which count it instead; its record's judge score is 0, which an Evolver that averages the records itself
takes as a 0: GEPA, SkillOpt, ACE, Meta-Harness Adapter, AHE Adapter (an exception trial, in its pass@1) and RRSI
Adapter (a missing trial with reward 0, in its score and pass rate); OPRO runs it again)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
import threading
import time
import traceback

from hermit.agent.base.interface import LoginFailed, login_failure
from hermit.task import RunOutput, normalise_judge
from hermit.trainer.budget import Budget, BudgetExceeded


def scored(records: list[dict]) -> list[dict]:
    """The records that carry a verdict: every status but `infra`."""
    return [r for r in records if r.get("status") != "infra"]


def mean_score(records: list[dict]) -> float | None:
    """The mean judge score of the scored records (None when none was scored)."""
    scores = [r["judge"]["score"] for r in scored(records) if r.get("judge")]
    return sum(scores) / len(scores) if scores else None


def run_one(task: dict, adapter: Path | None, pipeline, judge) -> dict:
    started = time.time()
    try:
        out = pipeline(dict(task), adapter)
        if not isinstance(out, RunOutput):
            raise TypeError(f"the pipeline returned {type(out).__name__}, not RunOutput")
    except Exception as exc:
        why = login_failure(exc)                     # as text from a sandbox's worker, or from a chat endpoint
        if why is not None:
            raise LoginFailed(why) from None
        return {"id": task["id"], "input": task, "status": "error", "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-3000:], "output": None,
                "judge": {"score": 0.0, "feedback": f"the pipeline failed: {type(exc).__name__}: {exc}"},
                "cost_usd": None, "wall_s": round(time.time() - started, 2), "api_speed": None, "tokens": None,
                "log": None, "messages": None}
    unscored = (out.extra or {}).get("unscored")
    if unscored:                                         # not the agent's result: no verdict, a cause; still billed
        cause = str(unscored)
        return {"id": task["id"], "input": task, "status": "infra", "error": cause, "output": out.output,
                "judge": {"score": 0.0, "feedback": f"not scored: {cause}", "extra": {"unscored": True}},
                "cost_usd": out.cost_usd, "wall_s": round(out.wall_s or (time.time() - started), 2),
                "api_speed": out.api_speed, "tokens": out.tokens, "log": str(out.log) if out.log else None,
                "messages": out.messages, "pipeline_extra": out.extra}
    failed = (out.extra or {}).get("session_failed")
    if failed:                                           # no answer to judge; what it spent and its log still count
        return {"id": task["id"], "input": task, "status": "error", "error": f"the agent's session failed: {failed}",
                "output": None, "judge": {"score": 0.0, "feedback": f"the agent's session failed: {failed}"},
                "cost_usd": out.cost_usd, "wall_s": round(out.wall_s or (time.time() - started), 2),
                "api_speed": out.api_speed, "tokens": out.tokens, "log": str(out.log) if out.log else None,
                "messages": out.messages, "pipeline_extra": out.extra}
    try:
        verdict = normalise_judge(judge(dict(task), out))
        judged = asdict(verdict)
        status, error = "ok", None
    except Exception as exc:
        judged = {"score": 0.0, "feedback": f"the judge failed: {type(exc).__name__}: {exc}"}
        status, error = "judge_error", f"{type(exc).__name__}: {exc}"
    return {"id": task["id"], "input": task, "status": status, "error": error, "output": out.output,
            "judge": judged, "cost_usd": out.cost_usd, "wall_s": round(out.wall_s or (time.time() - started), 2),
            "api_speed": out.api_speed, "tokens": out.tokens, "log": str(out.log) if out.log else None,
            "messages": out.messages, "pipeline_extra": out.extra}


def run_tasks(tasks: list[dict], adapter: Path | None, pipeline, judge, *, concurrency: int,
              budget: Budget | None = None, part: str = "rollout") -> list[dict]:
    """All tasks, in the given order. Raises BudgetExceeded (after in-flight tasks end) when a hard budget is hit."""
    results: dict[str, dict] = {}
    stop = threading.Event()

    def work(task):
        if stop.is_set():
            return None
        try:
            record = run_one(task, adapter, pipeline, judge)
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

    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
        futures = [pool.submit(work, t) for t in tasks]
        for future in as_completed(futures):
            record = future.result()
            if record is not None:
                results[record["id"]] = record
    if stop.is_set() and budget is not None:
        raise BudgetExceeded(budget.exceeded() or "budget reached")
    return [results[t["id"]] for t in tasks if t["id"] in results]
