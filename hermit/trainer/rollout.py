"""Run tasks through the user's pipeline and score them with the judge; shared by training rollouts, eval and the
acting-agent runner offered to Evolvers."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
import threading
import time
import traceback

from hermit.task import RunOutput, normalise_judge
from hermit.trainer.budget import Budget, BudgetExceeded


def run_one(task: dict, adapter: Path | None, pipeline, judge) -> dict:
    started = time.time()
    try:
        out = pipeline(dict(task), adapter)
        if not isinstance(out, RunOutput):
            raise TypeError(f"the pipeline returned {type(out).__name__}, not RunOutput")
    except Exception as exc:
        return {"id": task["id"], "input": task, "status": "error", "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-3000:], "output": None,
                "judge": {"score": 0.0, "feedback": f"the pipeline failed: {type(exc).__name__}: {exc}"},
                "cost_usd": None, "wall_s": round(time.time() - started, 2), "api_speed": None, "tokens": None,
                "log": None, "messages": None}
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
        record = run_one(task, adapter, pipeline, judge)
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
