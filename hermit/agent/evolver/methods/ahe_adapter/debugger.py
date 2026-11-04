"""The Agent Debugger stage of the AHE Adapter (spec B.5): one Codex session per task over the step's rollouts.

Upstream (evolve.py:1216-1315, 1381-1543, 1722-1790) built one `TaskAnalysisJob` per task from the trial directories
of a Harbor job and asked `adb ask` — a NexAU agent with a 20-iteration budget — one question per task, the first
job serially and the rest in a thread pool, each attempt retried with a doubling backoff and replaced by a fallback
text when every attempt failed. Here the same three phases run on Hermit's learning material:

1. `build_jobs(records, params)` — the port of `_build_adb_jobs`: records are `learning_material/current/tasks/
   <id>.json` grouped by `input[task_field]`; a trial is pass / fail / timeout by B.3's rules (`status == "ok"` and
   the pipeline's status ok with `judge.score >= 1.0` passes, below fails; `pipeline_extra.status == "timeout"`, or
   the Codex agent's `pipeline_extra.timed_out`, is a timeout; infra and agent errors are exceptions that carry
   nothing to analyze). A task of exceptions only is skipped unless one of them is a timeout; `mode` is `debug` when
   any trial failed or timed out, else `summary`; jobs are sorted debug-first, most failures first, and cut at
   `max_tasks`. A trial without a trajectory under the record's `trajectory` directory is left out.
2. `prepare_job_dir(job, scratch, params)` — `scratch/debugger/<task>/`: per trial `trace0i.events.jsonl` (the Codex
   event stream, `agent-codex.jsonl`) with the optional per-step explode `trace0i/NNNN-<kind>.md` of its standard
   steps (hermit.agent.base.trajectory), `prompt0i.txt` and `task0i.md` (the prompt and the task file the log
   carries), `hooks0i.jsonl` (the hook audit), `verifier_0i.txt` (the verdict's feedback) for non-passing trials,
   and `question.md` = DEBUGGER.md's rules + the file list + upstream's query (`queries.debugger_query`,
   byte-for-byte) + where to write the answer. A trial Harbor recorded gives `trace0i.json` (its ATIF trajectory,
   exploded as `trace0i/NNNN-<source>.md`) or its `agent/codex.txt` instead (LOG_FILES: the record's own files
   first, a Harbor trial's as the fallback). The middleware's per-session state (`hook-state/` in the log) is not
   copied: upstream's Debugger read the traces and the verifier output alone, and the Evolve Agent reads the state
   in the learning material, where START.md and the evolution query name it.
3. `run_jobs(jobs, ctx, params, base=...)` — each attempt is one call of the codex Base Agent (Layer 2,
   hermit.agent.base): `make("codex", settings).run(question, workspace=job_dir)`, with the Evolve Agent's settings
   (`base`: the run's sandbox as the isolation and Codex's own sandbox off, the login and its plan, web search, the
   price) and the Debugger's own model, effort, program and clock, the clock a deadline over the whole attempt (a wait
   for the login's plan included, as upstream's subprocess timeout) that the Trainer's cancel also sets; the question
   on stdin, a home per job (`scratch/debugger-homes/<task>/`: a call rewrites its home's config and last message)
   and a stream per attempt (`output/debugger/<task>.<attempt>.jsonl`). The first job runs alone (warm-up), the rest
   under `ThreadPoolExecutor(concurrency)`. The answer is the `answer.md` the agent wrote, else its last message (the
   Base Agent's reply). An attempt fails when it was cancelled, timed out, never started (the provider or the plan could
   not serve it), exited non-zero or left no answer; after `retries` attempts the upstream fallback text stands in
   (`queries.fallback_response`). Results keep upstream's result keys so analysis.py's overview writer reads them
   unchanged, and one `Usage(model)` sums the attempts' usage (tokens, cost and model calls), which the Base Agent
   read and priced.

`queries` is imported lazily (it is the module holding upstream's verbatim text); a test may pass a stand-in through
`texts`.
"""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
import threading
import time

from hermit.agent.base.interface import make
from hermit.agent.base.trajectory import CODEX_STREAM, STREAMS, log_steps
from hermit.agent.evolver.base import Usage
from hermit.agent.evolver.methods.ahe_adapter import trajectory as _trajectory
from hermit.agent.evolver.methods.ahe_adapter.analysis import task_file_name, task_name_of

DEFAULTS = {"enabled": True, "model": None, "effort": None, "bin": None, "args": None, "env": None,
            "concurrency": 16, "timeout_s": 600, "retries": 3, "backoff_s": 2.0, "max_tasks": 90,
            "verifier_output": True, "verifier_tail_chars": 4000, "verifier_tail_lines": 60, "explode_traces": True}
RULES = Path(__file__).with_name("DEBUGGER.md")
FAILURE_PREFIX = "[adb"             # upstream's marker: a response starting with it is a failure summary
# The answer's sections per (mode, k == 1): the question's numbered items; ROOT CAUSE leads where there is one.
SECTIONS = {("debug", False): ["ROOT CAUSE", "PASS vs FAIL", "CRITICAL MISTAKE", "GENERAL MECHANISM"],
            ("debug", True): ["ROOT CAUSE", "FAILURE POINT", "WHAT SHOULD HAVE BEEN DONE", "GENERAL LESSON"],
            ("summary", False): ["KEY STRATEGY", "REUSABLE PATTERN", "FRAGILITY RISK"],
            ("summary", True): ["KEY STRATEGY", "SUCCESS FACTORS", "REUSABLE PATTERN"]}
# Where a rollout's log (the record's `trajectory` directory) keeps each thing: the framework's Codex agent writes the
# first name of each (the task's collect adds `task.md` and the hook audit), a trial Harbor recorded the others.
LOG_FILES = {"atif": ("agent/trajectory.json", "trajectory.json"),
             "harbor_stream": ("agent/codex.txt", "codex.txt"),
             "hooks": ("hook-calls.jsonl", "agent/hook-calls.jsonl"),
             "verifier": ("verifier/test-stdout.txt", "test-stdout.txt"),
             "prompt": ("prompt.txt",), "task": ("task.md",)}


def settings(evolver_params: dict) -> dict:
    """The Debugger's effective parameters: DEFAULTS under `params.debugger`, the model, effort, program and args
    falling back to the Evolver's own (`debugger.model: null` = the Evolve Agent's model)."""
    own = dict(evolver_params.get("debugger") or {})
    merged = {**DEFAULTS, **{k: v for k, v in own.items() if v is not None or k not in DEFAULTS}}
    for key in ("model", "effort", "bin"):
        if merged.get(key) is None:
            merged[key] = evolver_params.get(key)
    if merged.get("args") is None:
        merged["args"] = list(evolver_params.get("args") or [])
    return merged


# ---------- jobs ----------

@dataclass
class Trial:
    id: str
    verdict: str                      # pass | fail | timeout
    reward: float                     # upstream's convention: >= 1 pass, < 0 timeout, else fail
    log: Path | None                  # the trial's log directory in the learning material
    verifier_output: str = ""         # the tail of the verdict's feedback (empty when not collected)


@dataclass
class Job:
    task: str
    trials: list[Trial] = field(default_factory=list)
    n_pass: int = 0
    n_fail: int = 0
    n_timeout: int = 0
    is_timeout: bool = False
    mode: str = "debug"               # debug | summary

    @property
    def trace_rewards(self) -> list[float]:
        return [t.reward for t in self.trials]

    @property
    def verifier_outputs(self) -> list[str]:
        return [t.verifier_output for t in self.trials]

    @property
    def n_total(self) -> int:
        return self.n_pass + self.n_fail + self.n_timeout


def classify(record: dict) -> str:
    """A trial's verdict by B.3: pass / fail / timeout / exception. The pipeline's status is its own word (Harbor's
    `status`), else a timeout when the Codex agent's session ran out of time (`timed_out`), else the run's status."""
    extra = record.get("pipeline_extra") or {}
    pipeline = extra.get("status", "timeout" if extra.get("timed_out") is True and record.get("status") == "ok"
                         else "ok" if record.get("status") == "ok" else "error")
    if pipeline == "timeout":
        return "timeout"
    if record.get("status") != "ok" or pipeline != "ok":
        return "exception"
    score = (record.get("judge") or {}).get("score")
    return "pass" if score is not None and float(score) >= 1.0 else "fail"


def load_records(step_dir: Path) -> list[dict]:
    """The step's task records (`tasks/*.json`), each with `_dir` = the step directory the `trajectory` path is
    relative to."""
    records = []
    for path in sorted((Path(step_dir) / "tasks").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        record["_dir"] = str(step_dir)
        records.append(record)
    return records


def log_file(log: Path | None, kind: str) -> Path | None:
    """The first of LOG_FILES[kind] that the log holds, or None."""
    if log is None:
        return None
    for name in LOG_FILES[kind]:
        path = log / name
        if path.is_file():
            return path
    return None


def trace_source(log: Path | None) -> tuple[Path, str] | None:
    """The trial's trajectory and the suffix it gets in the job dir: the CLI's event stream the framework's agent
    keeps at the log's root (`agent-codex.jsonl`, which hermit.agent.base.trajectory reads) -> `events.jsonl`; else a
    Harbor trial's `agent/trajectory.json` -> `json`, else its `agent/codex.txt` -> `events.jsonl`; None when the
    trial carries none."""
    if log is None:
        return None
    for name in STREAMS:
        if (log / name).is_file():
            return log / name, "events.jsonl"
    atif = log_file(log, "atif")
    if atif is not None:
        return atif, "json"
    stream = log_file(log, "harbor_stream")
    return (stream, "events.jsonl") if stream is not None else None


# The texts the learners read name the acting agent's event stream as the framework's Codex agent keeps it; with
# another acting agent the same sentences name its own (the Debugger and the Evolve Agent run on codex either way).
STREAM_TEXTS = {"agent-claude.jsonl": (("agent-codex.jsonl", "agent-claude.jsonl"),
                                       ("the raw Codex event stream", "the raw Claude Code event stream"),
                                       ("the Codex event stream", "the Claude Code event stream"),
                                       ("`codex exec --json` event stream",
                                        "`claude -p --output-format stream-json` event stream"))}


def stream_of(logs) -> str:
    """The event stream file the rollouts' logs carry (hermit.agent.base.trajectory.STREAMS): the first one found,
    else Codex's."""
    for log in logs:
        for name in STREAMS:
            if log is not None and (Path(log) / name).is_file():
                return name
    return CODEX_STREAM


def rollout_stream(records: list[dict], base: Path | None = None) -> str:
    """stream_of the step's records (a record's `trajectory` is relative to `record["_dir"]` or `base`)."""
    logs = []
    for record in records:
        trajectory, home = record.get("trajectory"), record.get("_dir") or base
        if isinstance(trajectory, str) and trajectory and home is not None:
            logs.append(Path(home) / trajectory)
    return stream_of(logs)


def for_stream(text: str, stream: str) -> str:
    """`text`, written for Codex's event stream, naming `stream` instead (unchanged for Codex's own)."""
    for old, new in STREAM_TEXTS.get(stream, ()):
        text = text.replace(old, new)
    return text


def _tail(text: str, limit: int) -> str:
    text = text.strip()
    return "... (truncated) ...\n" + text[-limit:] if len(text) > limit else text


def verifier_tail(record: dict, log: Path | None, limit: int) -> str:
    """What decided the trial, as upstream truncates its verifier output (1270-1279): the judge's feedback in the
    record (the Codex agent's tasks: "predicted '...' but expected '...'"), else the last `limit` characters of a
    Harbor trial's `verifier/test-stdout.txt`."""
    feedback = (record.get("judge") or {}).get("feedback")
    if isinstance(feedback, str) and feedback.strip():
        return _tail(feedback, limit)
    path = log_file(log, "verifier")
    if path is None:
        return ""
    try:
        return _tail(path.read_text(encoding="utf-8", errors="replace"), limit)
    except OSError:
        return ""


def build_jobs(records: list[dict], params: dict, base: Path | None = None) -> list[Job]:
    """One Job per task from the step's records (`_build_adb_jobs`, evolve.py:1216-1315). A record's `trajectory`
    is relative to its step directory: `record["_dir"]` (set by `load_records`) or `base`."""
    task_field = params.get("task_field", "task")
    limit = int(params.get("verifier_tail_chars", DEFAULTS["verifier_tail_chars"]))
    collect = bool(params.get("verifier_output", True))
    by_task: dict[str, list[dict]] = {}
    for record in records:
        by_task.setdefault(task_name_of(record, task_field), []).append(record)
    jobs: list[Job] = []
    for task, group in by_task.items():
        verdicts = [classify(r) for r in group]
        is_timeout = "timeout" in verdicts
        if all(v in ("exception", "timeout") for v in verdicts) and not is_timeout:
            continue
        job = Job(task=task, is_timeout=is_timeout)
        for record, verdict in sorted(zip(group, verdicts), key=lambda rv: str(rv[0].get("id", ""))):
            if verdict == "exception":
                continue
            rel = record.get("trajectory")
            root = Path(record.get("_dir") or base or ".")
            log = (Path(rel) if Path(rel).is_absolute() else root / rel) if rel else None
            if trace_source(log) is None:
                continue
            reward = -1.0 if verdict == "timeout" else (1.0 if verdict == "pass"
                                                       else float((record.get("judge") or {}).get("score") or 0.0))
            job.trials.append(Trial(id=str(record.get("id", task)), verdict=verdict, reward=reward, log=log,
                                    verifier_output=verifier_tail(record, log, limit) if collect else ""))
            if verdict == "pass":
                job.n_pass += 1
            elif verdict == "timeout":
                job.n_timeout += 1
            else:
                job.n_fail += 1
        if not job.trials:
            continue
        job.mode = "debug" if (job.n_fail > 0 or job.n_timeout > 0) else "summary"
        jobs.append(job)
    jobs.sort(key=lambda j: (j.mode == "summary", -(j.n_fail + j.n_timeout)))
    return jobs[:int(params.get("max_tasks", DEFAULTS["max_tasks"]))]


# ---------- the job directory and the question ----------

def _texts(texts=None):
    """The module holding upstream's verbatim text (`debugger_query`, `fallback_response`,
    `render_timing_breakdown`), or a stand-in passed by a test."""
    if texts is not None:
        return texts
    from hermit.agent.evolver.methods.ahe_adapter import queries
    return queries


def timings_of(job: Job, texts=None) -> list[str]:
    """The `⏱️ TIMING BREAKDOWN` of every timed-out trial, in trace order (empty strings for trials without an ATIF
    trajectory: the Codex event stream carries no timestamps), as the question and the fallback append them."""
    q = _texts(texts)
    out = []
    for trial in job.trials:
        if trial.reward >= 0:
            continue
        atif = log_file(trial.log, "atif")
        out.append(_trajectory.timing_breakdown(atif, q.render_timing_breakdown) if atif is not None else "")
    return out


def question_for(job: Job, files: list[str], k: int, params: dict, texts=None) -> str:
    """`question.md`: DEBUGGER.md's rules, the files of the job directory, upstream's query and the answer format."""
    q = _texts(texts)
    query = q.debugger_query(job.mode, k, job.n_pass, job.n_fail, job.n_timeout, job.trace_rewards,
                             job.verifier_outputs, timings_of(job, q),
                             verifier_tail_lines=int(params.get("verifier_tail_lines",
                                                                DEFAULTS["verifier_tail_lines"])))
    sections = SECTIONS[(job.mode, k == 1)]
    listing = "\n".join(f"- `{name}`" for name in files)
    answer = ("Write your answer to `answer.md` in this directory, leading with `" + sections[0] + ":` then "
              + ", ".join(f"`{s}:`" for s in sections[1:]) + ", one section per line.")
    rules = for_stream(RULES.read_text(encoding="utf-8").rstrip(), stream_of(t.log for t in job.trials))
    return (rules + "\n\n## Files in this directory\n\n" + listing
            + f"\n\n## Question (task `{job.task}`)\n\n" + query + "\n\n" + answer + "\n")


def prepare_job_dir(job: Job, scratch: Path, params: dict | None = None, texts=None) -> Path:
    """Lay out `scratch/debugger/<task>/` for one job and return it (an existing directory is replaced)."""
    params = {**DEFAULTS, **(params or {})}
    job_dir = Path(scratch) / "debugger" / task_file_name(job.task)
    shutil.rmtree(job_dir, ignore_errors=True)
    job_dir.mkdir(parents=True)
    files: list[str] = []
    for i, trial in enumerate(job.trials, 1):
        label = f"trace{i:02d}"
        source = trace_source(trial.log)
        if source is not None:
            path, suffix = source
            shutil.copy2(path, job_dir / f"{label}.{suffix}")
            files.append(f"{label}.{suffix}")
            if params.get("explode_traces"):
                if suffix == "json" and _trajectory.explode(path, job_dir / label):
                    files.append(f"{label}/NNNN-<source>.md")
                elif path.name in STREAMS and _trajectory.explode_steps(log_steps(trial.log) or [], job_dir / label):
                    files.append(f"{label}/NNNN-<kind>.md")
        for kind, name in (("prompt", f"prompt{i:02d}.txt"), ("task", f"task{i:02d}.md"),
                           ("hooks", f"hooks{i:02d}.jsonl")):
            path = log_file(trial.log, kind)
            if path is not None:
                shutil.copy2(path, job_dir / name)
                files.append(name)
        if trial.reward < 1.0 and trial.verifier_output and params.get("verifier_output"):
            (job_dir / f"verifier_{i:02d}.txt").write_text(trial.verifier_output, encoding="utf-8")
            files.append(f"verifier_{i:02d}.txt")
    k = max(1, len(job.trials))
    (job_dir / "question.md").write_text(question_for(job, files, k, params, texts), encoding="utf-8")
    return job_dir


# ---------- running Codex: one call of the codex Base Agent per attempt ----------

ANSWER = "answer.md"                     # what the question asks the agent to write
NOT_THE_DEBUGGERS = ("tools_name", "home", "log", "writable")   # the Evolve Agent's own settings in `base`


def _last_line(text: str) -> str:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


class Deadline(threading.Event):
    """An event set by deadline_cancel's watcher, with `cause` recorded before it is set: "cancel" (the Trainer's
    cancel) or "deadline" (the clock ran out). Read from the cause, not from the Trainer's event afterwards: the
    Trainer may stop while a timed-out call is still tearing down."""
    cause: str | None = None


def deadline_cancel(cancel, seconds: float | None) -> tuple[Deadline, threading.Event]:
    """An event that is set when the Trainer's `cancel` is set or `seconds` have passed, for a process the Trainer's
    runner kills on it; and the event that stops the watcher."""
    local, stop = Deadline(), threading.Event()
    end = time.monotonic() + float(seconds) if seconds else None

    def watch() -> None:
        while not stop.is_set():
            if cancel is not None and cancel.is_set():
                local.cause = "cancel"
            elif end is not None and time.monotonic() >= end:
                local.cause = "deadline"
            if local.cause:
                local.set()
                return
            stop.wait(1)
    threading.Thread(target=watch, daemon=True).start()
    return local, stop


class Runner:
    """The per-job attempt loop with the run-wide pieces (the Base Agent's settings, the logs and homes) bound once."""

    def __init__(self, ctx, params: dict, base: dict, scratch: Path, texts=None):
        self.ctx, self.params, self.scratch, self.texts = ctx, params, Path(scratch), texts
        self.base = {k: v for k, v in (base or {}).items() if k not in NOT_THE_DEBUGGERS}
        self.logs = Path(ctx.workspace.output) / "debugger"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.homes = self.scratch / "debugger-homes"
        self.usage: list[Usage] = []
        self._lock = threading.Lock()

    def agent_settings(self, job: Job, job_dir: Path, attempt: int, timeout_s: float, cancel=None) -> dict:
        """The codex Base Agent's settings for one attempt: the Evolve Agent's (the isolation, the login, web search,
        the price), the Debugger's model, effort, program and args (after a bound model's provider arguments), its
        clock, `cancel` (the attempt's deadline; the Trainer's cancel when None), the question on stdin, the job's
        own home and the attempt's own stream; the job directory writable."""
        p, name = self.params, task_file_name(job.task)
        args = list(p.get("args") or [])
        if self.base.get("binding"):
            # A bound model reaches its provider through Codex arguments only (the Evolver's `args` begin with them):
            # `debugger.args` given in place of the Evolve Agent's must not drop them.
            from hermit.agent.model.backends import Binding
            provider = Binding.from_spec(self.base["binding"]).codex_args()
            if args[:len(provider)] != provider:
                args = [*provider, *args]
        out = {**self.base, **{k: p[k] for k in ("model", "effort", "bin") if p.get(k) is not None},
               "args": args, "stdin": True, "timeout_s": timeout_s,
               "cancel": self.ctx.cancel if cancel is None else cancel,
               "home": self.homes / name, "log": self.logs / f"{name}.{attempt}.jsonl", "writable": [job_dir]}
        if p.get("env"):
            out["env"] = {**(self.base.get("env") or {}), **p["env"]}
        return out

    def attempt(self, job: Job, job_dir: Path, attempt: int, timeout_s: float) -> tuple[str | None, str]:
        """(answer text, "") on success, else (None, the `[adb ...]` reason) — upstream's `_invoke_adb_ask_once`."""
        answer = job_dir / ANSWER
        answer.unlink(missing_ok=True)
        question = (job_dir / "question.md").read_text(encoding="utf-8")
        # The attempt's clock covers the whole call, as upstream's subprocess timeout did: the Base Agent's own
        # `timeout_s` starts only once the CLI runs, after any wait for the login's plan or a session slot.
        deadline, stop = deadline_cancel(self.ctx.cancel, timeout_s)
        try:
            result = make("codex", self.agent_settings(job, job_dir, attempt, timeout_s, deadline)).run(
                question, workspace=job_dir)
        except Exception as exc:                                 # noqa: BLE001 - no program, no login: this attempt's
            return None, f"{FAILURE_PREFIX} exception] {type(exc).__name__}: {str(exc)[:300]}"
        finally:
            stop.set()
        with self._lock:
            self.usage.extend(result.usage)
        extra = result.extra or {}
        tail = _last_line(extra.get("output_tail", ""))
        if deadline.cause == "cancel" or (deadline.cause is None and extra.get("cancelled")):
            return None, f"{FAILURE_PREFIX} cancelled] stopped by the Trainer"
        if deadline.cause == "deadline" or result.timed_out:
            return None, f"{FAILURE_PREFIX} timeout] exceeded {timeout_s:g}s"
        if not extra.get("started", True) and (extra.get("unavailable") or result.exit_code in (0, None)):
            # held back by the login's plan past its wait, or gone before a single event: nothing served it
            return None, f"{FAILURE_PREFIX} unavailable] {tail or 'the session never started'}"
        if result.exit_code not in (0, None):
            return None, f"{FAILURE_PREFIX} error] exit={result.exit_code}: {tail}"
        written = answer.read_text(encoding="utf-8", errors="replace").strip() if answer.is_file() else ""
        if written or (result.reply or "").strip():              # the file the agent wrote, else its last message
            return written or result.reply.strip(), ""
        return None, f"{FAILURE_PREFIX} failed] empty {ANSWER}"

    def run(self, job: Job) -> dict:
        """One job: prepare its directory, try up to `retries` times with a doubling backoff, fall back."""
        q = _texts(self.texts)
        started = time.monotonic()
        job_dir = prepare_job_dir(job, self.scratch, self.params, self.texts)
        retries = max(1, int(self.params.get("retries", 3)))
        backoff = float(self.params.get("backoff_s", 2.0))
        timeout_s = float(self.params.get("timeout_s", 600))
        cancel = self.ctx.cancel
        response, reason, attempts = None, "", 0
        for attempt in range(retries):
            if cancel is not None and cancel.is_set():
                reason = reason or f"{FAILURE_PREFIX} cancelled] stopped by the Trainer"
                break
            attempts += 1
            response, reason = self.attempt(job, job_dir, attempts, timeout_s)
            if response is not None or reason.startswith(f"{FAILURE_PREFIX} cancelled]"):
                break
            if attempt < retries - 1:
                if cancel is not None:
                    cancel.wait(backoff)
                else:
                    time.sleep(backoff)
                backoff *= 2.0
        status = "ok" if response is not None else "fallback"
        if response is None:
            response = q.fallback_response(reason or f"{FAILURE_PREFIX} failed] no attempt", job.verifier_outputs,
                                           timings_of(job, q))
        return self.result(job, response, job_dir=str(job_dir), status=status, attempts=attempts,
                           error=reason if status == "fallback" else "", elapsed_s=round(time.monotonic() - started, 1))

    @staticmethod
    def result(job: Job, response: str, **extra) -> dict:
        """Upstream's result dict (evolve.py:1532-1543) plus Hermit's bookkeeping."""
        return {"task_name": job.task, "mode": job.mode, "n_pass": job.n_pass, "n_fail": job.n_fail,
                "n_timeout": job.n_timeout, "is_timeout": job.is_timeout, "response": response,
                "trace_paths": [str(trace_source(t.log)[0]) if trace_source(t.log) else "" for t in job.trials],
                "trace_rewards": job.trace_rewards, "verifier_outputs": job.verifier_outputs,
                "trial_ids": [t.id for t in job.trials], **extra}


def _crash_safe(runner: "Runner", job: Job, texts=None) -> dict:
    """runner.run(job), or the upstream fallback when it raises (a job directory that could not be prepared, say):
    one job's crash is its own."""
    try:
        return runner.run(job)
    except Exception as exc:                                    # noqa: BLE001 — one job's crash is its own
        q = _texts(texts)
        return Runner.result(job, q.fallback_response(
            f"{FAILURE_PREFIX} exception] {type(exc).__name__}: {str(exc)[:200]}", job.verifier_outputs, []),
            status="fallback", attempts=0, error=f"{type(exc).__name__}: {str(exc)[:200]}", job_dir="", elapsed_s=0.0)


def run_jobs(jobs: list[Job], ctx, params: dict, *, base: dict | None = None, scratch: Path | None = None,
             texts=None) -> tuple[list[dict], Usage]:
    """Run every job (the first alone, the rest `concurrency` at a time) and return the results sorted as upstream
    (debug first, most failures first) and one Usage over all sessions (`calls`: the model calls they reported).
    `base` is the codex Base Agent's settings the Evolve Agent runs with (AHEAdapterEvolver.settings), `scratch` the
    step's scratch directory (default: the workspace's)."""
    params = {**DEFAULTS, **params}
    runner = Runner(ctx, params, dict(base or {}), scratch if scratch is not None else ctx.workspace.scratch, texts)
    results: list[dict] = []
    if jobs:
        results.append(_crash_safe(runner, jobs[0], texts))               # the warm-up falls back like the rest
    rest = jobs[1:]
    if rest:
        workers = max(1, int(params.get("concurrency", 16)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_crash_safe, runner, job, texts) for job in rest]
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
    results.sort(key=lambda r: (r["mode"] == "summary", -r["n_fail"]))
    usage = Usage(model=str(params.get("model") or "codex"))
    for u in runner.usage:
        usage.calls += u.calls                          # the model calls each attempt's stream reported, retries too
        usage.input_tokens += u.input_tokens
        usage.cached_input_tokens += u.cached_input_tokens
        usage.output_tokens += u.output_tokens
    costs = [u.cost_usd for u in runner.usage]
    usage.cost_usd = sum(costs) if costs and all(c is not None for c in costs) else None
    return results, usage
