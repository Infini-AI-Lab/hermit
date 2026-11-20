"""Run tasks through the acting agent and judge each answer; shared by training rollouts, eval and the acting-agent
runner offered to Evolvers. This is where a task (hermit.task) meets the agent that answers it (hermit.agent): the
agent is handed the task's public half, the verifier reads the whole task, and neither knows the other."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import sys
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
    environment is stopped. A rollout that is not the agent's result (infra: the environment did not start, failed
    under the verifier, the session never started: hermit.environment.harbor_errors) runs again in a fresh environment
    (environment_rollout); one that stays so is marked `unscored`, and the judge refuses to score it: a failed run
    (judge_error), as Hermit's Harbor runner recorded one."""
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
        out = environment_rollout(task, given, adapter, pipeline, environments, started)
        return out if isinstance(out, dict) else _judged(task, given, out, verify, started)
    try:
        out = _given(pipeline, given, adapter, None, task.private.get("needs") or {})
    except Exception as exc:
        why = login_failure(exc)                     # as text from a sandbox's worker, or from a chat endpoint
        if why is not None:
            raise LoginFailed(why) from None
        return _failed_record(task, given, started, exc, "the pipeline failed")
    failed = (out.extra or {}).get("session_failed")
    if failed:                                           # no answer to judge; what it spent and its log still count
        return {"id": given.get("id", task.id), "input": given, "status": "error",
                "error": f"the agent's session failed: {failed}",
                "output": None, "judge": {"score": 0.0, "feedback": f"the agent's session failed: {failed}"},
                "cost_usd": out.cost_usd, "wall_s": round(out.wall_s or (time.time() - started), 2),
                "api_speed": out.api_speed, "tokens": out.tokens, "log": str(out.log) if out.log else None,
                "messages": out.messages, "pipeline_extra": out.extra}
    return _judged(task, given, out, verify, started)


def _read(out: RunOutput, env, task: Task, begun: float) -> RunOutput:
    """The agent's output with its environment's verdict: the verifier's (verify), or for an adapter that failed before
    any session the agent's result scored 0 with its message (the runner's adapter_failed, context_over_limit), or the
    agent's own word that the run is not its result (`extra.infra`, read by the caller)."""
    extra = out.extra or {}
    if extra.get("infra"):
        return out
    if extra.get("adapter_failed"):
        bar = getattr(getattr(env, "owner", None), "pass_reward", 1.0)          # no pass reward: no pass rate
        verdict = {"reward": None, "rewards": None, "status": "agent_error", "failure_cause": extra["adapter_failed"],
                   "passed": None if bar is None else False, "exception": None,
                   "exception_message": extra.get("exception_message"),
                   "feedback": str(extra.get("exception_message") or "")}
        out.extra = {**extra, "verified": verdict, "status": "agent_error", "failure_cause": extra["adapter_failed"],
                     "verifier_reward_raw": None}
        return out
    logs = Path(out.log) if out.log and Path(out.log).is_dir() else None
    verdict = env.verify(agent_logs=logs)
    out.extra = {**extra, "verified": verdict, **_status(extra, verdict)}
    if verdict["status"] == "infra":
        out.extra["unscored"] = verdict["failure_cause"] or "infra"
    return out


def _status(extra: dict, verdict: dict) -> dict:
    """The rollout's status as Hermit's Harbor runner wrote it beside the verdict (`status`, `failure_cause`), which
    the methods' readers take (AHE's classification): infra (not the agent's result), timeout (the agent's session ran
    out of its time, or the verifier on its solution), agent_error (the CLI exited with an error or its session failed;
    a verifier that left no reward), else the verifier's own, ok."""
    status, cause = verdict.get("status") or "infra", verdict.get("failure_cause")
    if status != "infra":
        if extra.get("timed_out"):
            status, cause = "timeout", "agent_exhausted_budget"
        elif extra.get("session_failed") or extra.get("exit_code") not in (None, 0):
            status, cause = "agent_error", cause or "unclassified"
    # and the verifier's own reward beside it (the runner's verifier_reward_raw: AHE asks whether a timed-out trial's
    # work passed)
    return {"status": status, "failure_cause": cause, "verifier_reward_raw": verdict.get("reward")}


def _infra_output(task: Task, cause: str, message: str, started: float, *, ran: bool,
                  spent: RunOutput | None = None) -> RunOutput:
    """A rollout that ended infra before its environment's verdict (the environment did not start, the session never
    did): no verdict but this one, which the judge refuses (hermit.task.verify.environment). Nothing ran when its
    environment never came up (its cost 0); a session that may have run leaves its cost unknown."""
    verdict = {"status": "infra", "failure_cause": cause, "reward": None, "rewards": None, "passed": None,
               "exception": {"type": None, "message": message[-2000:]}, "exception_message": message[-2000:],
               "feedback": ""}
    # what its sessions spent, when the agent said (`spent`: its output); unknown when one may have run unsaid
    cost = spent.cost_usd if spent is not None else (None if ran else 0.0)
    tokens = spent.tokens if spent is not None else (None if ran else 0)
    calls = (spent.api_speed or {}).get("model_calls") if spent is not None else (None if ran else 0)
    return RunOutput(output={"harbor_task": task.public.get("harbor_task"), "reward": None, "status": "infra",
                             "failure_cause": cause},
                     log=spent.log if spent is not None else None,
                     cost_usd=cost, tokens=tokens, wall_s=round(time.time() - started, 1),
                     api_speed={"model_calls": calls, "status": "infra"},
                     extra={"status": "infra", "failure_cause": cause, "exception_message": message[-2000:],
                            "verified": verdict, "unscored": cause})


def _sleep(seconds: float) -> None:
    """The pause before a rollout runs again (a test's stand-in replaces it)."""
    time.sleep(seconds)


# The rollout's retry and admission states (the runner's _run_trial), after each run: the agent's result or a cause
# that runs no better again (DONE), not the agent's result run again in a fresh environment after infra_wait_s (INFRA,
# counted in infra_retries), a session the provider could not serve run again after n x busy_wait_s (PROVIDER_BUSY,
# its own allowance, busy_retries), a session the login's plan refused run again once the plan allows (PLAN_WAIT, not
# counted: the admission waits for the window; a hold that could not be written pauses HOLD_UNWRITTEN_S first), and
# the run ended meanwhile (STOP: this rollout as it stands).
DONE, INFRA, PROVIDER_BUSY, PLAN_WAIT, STOP = "done", "infra", "provider_busy", "plan_wait", "stop"


class Retries:
    """The one retry and admission state machine of a rollout in its own environment: `next(out, final, said)`, the
    state after a run and how long to pause before the next."""

    def __init__(self, environments):
        self.retries, self.counted = int(environments.infra_retries), 0
        self.busy, self.busy_retries = 0, int(getattr(environments, "busy_retries", 3))
        self.busy_wait_s = float(getattr(environments, "busy_wait_s", 120.0))
        self.infra_wait_s = float(environments.infra_wait_s)
        self.stopping = getattr(environments, "stopping", None)

    def next(self, out: RunOutput, final: bool, said_by_agent: bool) -> tuple[str, float]:
        from hermit.environment.harbor_errors import NO_RETRY
        cause = out.extra.get("unscored")
        if cause is None:
            return DONE, 0.0
        provider = cause == "model_provider_failed" and said_by_agent and not final     # a CLI session's provider
        if provider:
            self.busy += 1                               # the provider's own allowance (busy_retries), not infra's
            if self.busy > self.busy_retries:
                return DONE, 0.0
        elif cause != "usage_limit":                     # the plan's refusal runs again once it allows: not counted
            self.counted += 1
        if cause in NO_RETRY or final or self.counted > self.retries:
            return DONE, 0.0
        if self.stopping is not None and self.stopping.is_set():
            return STOP, 0.0
        if cause == "usage_limit":
            from hermit.agent.base.login import HOLD_UNWRITTEN_S
            return PLAN_WAIT, HOLD_UNWRITTEN_S if out.extra.get("hold_unwritten") else 0.0
        if provider:
            return PROVIDER_BUSY, self.busy * self.busy_wait_s                 # the n-th after n x busy_wait_s
        return INFRA, self.infra_wait_s


def _scrub_log(out: RunOutput) -> None:
    """The agent's log, scrubbed of this process's key values (hermit.redact) before a record names it: an agent in a
    task's own environment runs in this process (no worker scrubs what it returns)."""
    from hermit.redact import redact_text, redact_tree
    log = Path(out.log) if out.log else None
    if log is None or log.is_symlink():
        return
    if log.is_dir():
        redact_tree(log)
    elif log.is_file():
        try:
            text = log.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        clean = redact_text(text)
        if clean != text:
            log.write_text(clean, encoding="utf-8")


def environment_rollout(task: Task, given: dict, adapter: Path | None, pipeline, environments, started: float,
                        *, pause=None):
    """One task in its own environment, run again while it ends infra (not the agent's result: `unscored`) as the
    Retries state machine says (the runner's `_run_trial`: infra_retries after infra_wait_s, never for a cause that
    runs no better again; the provider's busy_retries; the plan's refusal once it allows); on a login the run's
    admission to the plan (its wait and one of its slots, environments.admission) is held around the whole
    environment, as the runner held it around a trial. What every run used is the rollout's (Attempts). Returns the
    output, or the record of a pipeline that failed on its own (no environment's fault: the run's failed record, as
    for any task)."""
    from hermit.agent.model.usage import Attempts
    attempts, retries = Attempts(), Retries(environments)
    stopping = retries.stopping
    while True:
        if stopping is not None and stopping.is_set():   # the run ended: no environment starts for this task
            raise LoginFailed("the run ended (a lost sign-in or the account's limit) before the task started")
        run = _environment_run(task, given, adapter, pipeline, environments, started)
        if isinstance(run, dict):                        # the pipeline's own failure: the run's failed record, with
            if attempts.runs:                            # what the runs before it spent
                earlier = attempts.output() if len(attempts.runs) > 1 else attempts.runs[0]
                run = {**run, "tokens": earlier.tokens, "cost_usd": None,
                       "api_speed": {"model_calls": (earlier.api_speed or {}).get("model_calls")},
                       "pipeline_extra": {**(run.get("pipeline_extra") or {}),
                                          "attempts": (earlier.extra or {}).get("attempts") or
                                          [{"tokens": earlier.tokens, "cost_usd": earlier.cost_usd,
                                            "model_calls": (earlier.api_speed or {}).get("model_calls"),
                                            "unscored": (earlier.extra or {}).get("unscored")}]}}
            return run
        out, final, said_by_agent = run
        _scrub_log(out)                                  # the log the record names, scrubbed of the run's keys
        attempts.add(out)
        state, wait = retries.next(out, final, said_by_agent)
        if state in (DONE, STOP):
            return attempts.output()
        message = str(out.extra.get("exception_message") or (out.extra.get("verified") or {}).get("exception_message")
                      or "")
        print(f"hermit {task.id}: not the agent's result ({out.extra.get('unscored')}: {message[:300]}); again in "
              f"{wait:.0f} s ({retries.counted}/{retries.retries})", file=sys.stderr, flush=True)
        if not wait:
            continue
        if pause is not None:
            pause(wait)
        elif stopping is not None:
            if stopping.wait(wait):                      # the run ended meanwhile: this rollout as it stands
                return attempts.output()
        else:
            _sleep(wait)


def _environment_run(task: Task, given: dict, adapter: Path | None, pipeline, environments, started: float):
    """One run of the task in a fresh environment: (its output, whether it runs no better again, whether the agent
    itself said it is not its result), or the pipeline's failed record."""
    import contextlib
    from hermit.environment.harbor import EnvironmentUnavailable
    from hermit.environment.harbor_errors import AGENT_PHASE_OVER, pipeline_cause
    stopping = getattr(environments, "stopping", None)
    begun, final, out, said_by_agent = time.time(), False, None, False
    admission = getattr(environments, "admission", None)
    try:
        with (admission() if admission is not None else contextlib.nullcontext()):
            installs = getattr(environments, "installs_for", None)
            with environments.session(environments.task_dir(task), task.id,
                                      installs(adapter) if installs is not None else None) as env:
                env.plan_admitted = admission is not None
                try:
                    out = _given(pipeline, given, adapter, env, task.private.get("needs") or {})
                except LoginFailed:
                    if stopping is not None:             # the run ends now: no task admitted while this one stops
                        stopping.set()
                    raise
                except Exception as exc:
                    why = login_failure(exc)
                    if why is not None:
                        if stopping is not None:
                            stopping.set()
                        raise LoginFailed(why) from None
                    if AGENT_PHASE_OVER in str(exc):     # Harbor's agent timeout ended the phase: its verdict
                        out = _read(RunOutput(output=None, extra={"timed_out": True, "session_failed":
                                                                  f"{type(exc).__name__}: {exc}"[-600:]}),
                                    env, task, begun)
                        kept = (out.extra.get("verified") or {}).get("agent_dir")
                        if out.log is None and kept and Path(kept).is_dir():
                            out.log = Path(kept)         # the trial's agent logs: the stream the CLI wrote there
                            usage_of = getattr(environments, "usage_of", None)
                            reported = usage_of(Path(kept)) if usage_of is not None else []
                            if reported:                 # what the session had reported before it was cut off
                                from hermit.agent.model.usage import Ledger
                                ledger = Ledger().add(reported)
                                out.cost_usd, out.tokens = ledger.cost(None), ledger.tokens(None)
                                out.api_speed = {**(out.api_speed or {}), "model_calls": ledger.calls()}
                    elif isinstance(exc, OSError):       # this machine's (the runner's host_failed): never run again
                        out = _infra_output(task, "host_failed", f"{type(exc).__name__}: {exc}", begun, ran=True)
                        final = True
                    else:
                        read = pipeline_cause(f"{type(exc).__name__}: {exc}")
                        if read is None:
                            return _failed_record(task, given, started, exc, "the pipeline failed")
                        cause, again = read
                        final = not again
                        out = _infra_output(task, cause, f"{type(exc).__name__}: {exc}", begun, ran=True)
                else:
                    out = _read(out, env, task, begun)
                    if out.extra.get("infra"):
                        final, said_by_agent = bool(out.extra.get("infra_final")), True
                        unwritten = bool(out.extra.get("hold_unwritten"))
                        out = _infra_output(task, out.extra["infra"], str(out.extra.get("exception_message")),
                                            begun, ran=True, spent=out)
                        if unwritten:
                            out.extra["hold_unwritten"] = True
    except EnvironmentUnavailable as exc:
        if exc.cause == "stopped":                       # the run ended (another task's lost sign-in)
            raise LoginFailed(str(exc)) from None
        out = _infra_output(task, exc.cause, str(exc), begun, ran=False)
    except ValueError as exc:                            # no task directory: the task set's, not the environment's
        return _failed_record(task, given, started, exc, "the task's environment could not be given")
    except OSError as exc:                               # this machine's, the environment's teardown among it
        if out is None or out.extra.get("unscored") is None and "verified" not in out.extra:
            out = _infra_output(task, "host_failed", f"{type(exc).__name__}: {exc}", begun, ran=out is not None,
                                spent=out)
            final = True
        print(f"WARNING hermit {task.id}: this machine failed after the task's verdict: {exc}", file=sys.stderr,
              flush=True)
    return out, final, said_by_agent


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
            if environments is not None and getattr(environments, "stopping", None) is not None:
                environments.stopping.set()              # and no disk gate or pause before a run again waits on
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
