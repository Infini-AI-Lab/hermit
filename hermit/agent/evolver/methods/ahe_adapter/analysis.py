"""AHE's deterministic analysis of one evaluation (arXiv:2604.25850; china-qijizhifeng/agentic-harness-engineering
@ 8b2a55d, `evolve.py`; see NOTICE), ported function by function onto Hermit's learning material.

Upstream reads a Harbor job directory; here the input is the step's task records (`learning_material/current/tasks/
<id>.json`, `hermit/trainer/material.py`): `input[task_field]` names the benchmark task that k rows `<task>#1..#k`
share (OfficeQA: `uid`), `status` / `judge.score` say what the judge made of the rollout, the pipeline's own word
says whether the rollout reached the verifier at all (a Harbor trial's `pipeline_extra.status`: ok | timeout | infra
| agent_error; the framework's Codex agent's `pipeline_extra.timed_out`), `tokens` and `trajectory` complete the
record. `trial_from_record` is the only place that reads a record; the rest works on the trial it returns, so a
pipeline with another record shape adjusts that one function.

What is ported, with upstream's names and line numbers:

- `compute_stats` (599-736): trial verdicts (pass / fail / exception, timeout flagged), task verdicts (pass iff all
  k rollouts pass, exception iff all are exceptions, else fail), pass@i by the Chen estimator (`pass_at_k_est`,
  `compute_pass_at_k_metrics`, 557-596) with exceptions counted in n. Hermit adds `tokens_per_trial_k`, the counts of
  timeout / infra / agent_error / timeout_would_pass trials, the per-task difficulty and the trial list. `timeout_as`
  decides what a timed-out rollout is in the diff and the attribution: an exception (upstream's released code, the
  default) or a failure; pass@1 counts it 0 either way.
- `update_task_history` (762-780), `compute_task_stability` (783-827), `compute_iteration_diff` (830-914): the nine
  transition buckets with the rollout-level moves inside fail->fail.
- `history_before` (1803-1945): the `evolution_history.md` entry written before the Evolve Agent runs.
- `load_change_manifest` (2205-2221): the manifest counts only when its `iteration` is the step it was written in.
- `evaluate_changes` (2239-2329): the five verdicts, upstream's rules verbatim; `update_best_ever` (2361-2413),
  strict `>`, with Hermit's `step` and `commit`.
- `extract_one_liner` (1546-1586) and `write_debugger_analyse` (1589-1719): the overview and detail files of the
  Debugger stage, from answers the Debugger (debugger.py) collected.
- `update_iteration_scores` / `regenerate_scores_md` (1975-2056, 2059-2130): `iteration_scores.yaml` + `.md`.
- `build_evolution_query` (2563-2919): sections 1-8 and the Execution Instructions, text verbatim, paths rewritten
  to the Evolver's workspace (`{learning_material}/current/...`, `{state}/analysis/step_N/...`, `git -C {adapter}
  show <commit>:<path>`); the Best-of-N and strategy-hint sections are not ported.

All functions are pure or write only the files they are named for; the Evolver (evolver.py) owns `state/`.
"""
from __future__ import annotations

import collections
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re

from hermit.agent.evolver.methods.ahe_adapter.queries import SUGGESTION

PASS, FAIL, EXCEPTION = "pass", "fail", "exception"
TIMEOUT_AS = ("exception", "fail")
PIPELINE_STATUSES = ("ok", "timeout", "infra", "agent_error")
VERDICTS = ("EFFECTIVE", "PARTIALLY_EFFECTIVE", "MIXED", "INEFFECTIVE", "HARMFUL")


# ---------- the record adapter ----------

def task_file_name(task: str) -> str:
    """The file-name stem of a task's own files (its detail report, its Debugger directory, home and logs): the task
    name itself when it is a plain file name, else a slug of it with a hash of the whole name, so that a task name
    the framework allows (`suite/question-1`) neither leaves its directory nor collides with another."""
    task = str(task)
    if re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,59}", task):
        return task
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", task).strip("._")[:40] or "task"
    return f"{slug}-{hashlib.sha1(task.encode('utf-8')).hexdigest()[:8]}"


def task_name_of(record: dict, task_field: str = "task") -> str:
    """The benchmark task a record belongs to: `input[task_field]`, else the record id without its `#n` suffix."""
    task = (record.get("input") or {}).get(task_field)
    if task not in (None, ""):
        return str(task)
    return re.sub(r"#\d+$", "", str(record.get("id", "")))


def trial_from_record(record: dict, timeout_as: str = "exception") -> dict:
    """One trial from a learning-material record (spec B.3): `result` is pass iff the run and the pipeline both
    report ok and the judge scored >= 1.0, fail iff both report ok and the score is below 1, else exception;
    `timeout` is set when the pipeline says the agent ran out of time (upstream: `"Timeout" in exc_type`, 643-644;
    without a pipeline status, the Codex agent's `timed_out`), and with `timeout_as="fail"` such a trial counts as a
    failure instead of an exception. `exception_type` names the exception as upstream's `_extract_exception_type`
    would (the pipeline's `exception_type`, else the error's class, else the pipeline status); `verifier_passed` is
    the verifier's own verdict on a timed-out trial, when known (the judge's score of what a timed-out Codex session
    answered)."""
    if timeout_as not in TIMEOUT_AS:
        raise ValueError(f"timeout_as must be one of {TIMEOUT_AS}, not {timeout_as!r}")
    extra = record.get("pipeline_extra")
    if extra is None:
        extra = record.get("extra") or {}
    run_ok = record.get("status") == "ok"
    pstatus = extra.get("status")
    cut = pstatus is None and run_ok and extra.get("timed_out") is True     # the Codex agent's session ran out of time
    if pstatus is None:
        pstatus = "timeout" if cut else "ok" if run_ok else "infra"
    judge = record.get("judge") or {}
    try:
        score = float(judge.get("score")) if judge.get("score") is not None else None
    except (TypeError, ValueError):
        score = None
    reached = run_ok and pstatus == "ok"
    if reached and score is not None and score >= 1.0:
        result = PASS
    elif reached:
        result = FAIL
    else:
        result = EXCEPTION
    timeout = pstatus == "timeout"
    if timeout and timeout_as == "fail":
        result = FAIL
    exception_type = None
    if not reached:
        error = str(record.get("error") or "")
        exception_type = extra.get("exception_type") or (error.split(":", 1)[0].strip() if error else None) \
            or str(pstatus)
    raw = extra.get("verifier_reward_raw")
    if raw is None and cut:
        raw = score
    try:
        verifier_passed = float(raw) >= 1.0 if raw is not None else None
    except (TypeError, ValueError):
        verifier_passed = None
    tokens = record.get("tokens")
    return {
        "id": str(record.get("id", "")),
        "result": result,
        "timeout": timeout,
        "pipeline_status": str(pstatus),
        "score": score,
        "exception_type": exception_type,
        "verifier_passed": verifier_passed,
        "tokens": tokens if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) else None,
        "difficulty": (record.get("input") or {}).get("difficulty"),
        "trajectory": record.get("trajectory"),
    }


# ---------- pass@k (evolve.py:557-596) ----------

def pass_at_k_est(n: int, c: int, k: int) -> float:
    """Chen et al. unbiased estimator for single-task pass@k.
    n = total samples, c = number of correct samples."""
    if k > n or k < 0 or n == 0:
        return float("nan")
    num_wrong = n - c
    if num_wrong >= k:
        return 1.0 - math.comb(num_wrong, k) / math.comb(n, k)
    return 1.0 if c > 0 else 0.0


pass_at_k = pass_at_k_est


def compute_pass_at_k_metrics(per_task_rollouts: dict, k: int) -> dict:
    """Compute pass@1, pass@2, ..., pass@k aggregate metrics using Chen et al. estimator.

    Returns dict:
      pass_at: {1: rate, 2: rate, ..., k: rate}  - macro-averaged over eligible tasks (n>=i)
      per_task_pass_at: {task_name: {1: est, 2: est, ...}}  - per-task estimates
      eligible_counts: {1: n_tasks, 2: n_tasks, ...}  - tasks with n>=i for each i
    """
    pass_at: dict[int, float] = {}
    per_task_pass_at: dict[str, dict[int, float]] = {}
    eligible_counts: dict[int, int] = {}

    for i in range(1, k + 1):
        estimates = []
        for task_name, ro in sorted(per_task_rollouts.items()):
            n = ro["n_pass"] + ro["n_fail"] + ro.get("n_exception", 0)
            c = ro["n_pass"]
            if n >= i:
                est = pass_at_k_est(n, c, i)
                estimates.append(est)
                per_task_pass_at.setdefault(task_name, {})[i] = est
        eligible_counts[i] = len(estimates)
        pass_at[i] = sum(estimates) / len(estimates) if estimates else 0.0

    return {
        "pass_at": pass_at,
        "per_task_pass_at": per_task_pass_at,
        "eligible_counts": eligible_counts,
    }


# ---------- compute_stats (evolve.py:599-736) ----------

def compute_stats(records: list[dict], k: int | None = None, timeout_as: str = "exception", *,
                  task_field: str = "task") -> dict:
    """Compute detailed evaluation statistics from the step's task records (upstream: a Harbor job_dir).

    Groups trials by task name: a task passes only if ALL k rollouts pass, is an exception only if all are.
    `k` is inferred as the largest number of trials of a task when None.

    Returns dict (JSON-serialisable):
      pass_rate:         pass@1 (Chen et al.; equal to n_pass/n_total when k == 1)
      n_pass / n_fail / n_exception / n_total:  task counts
      k:                 rollout count per task
      exception_types:   dict[str, int] - exception type distribution over exception trials
      task_results:      dict[task_name, "pass"|"fail"|"exception"]
      timeout_tasks:     sorted list of tasks with a timed-out trial (upstream: a set)
      per_task_rollouts: dict[task_name, {n_pass, n_fail, n_exception, total}] (upstream: only when k>1)
      pass_at_k:         compute_pass_at_k_metrics output (upstream: only when k>1)
      trial_stats:       raw per-trial counts (upstream: only when k>1)
      Hermit extras: timeout_as, tokens_per_trial_k (mean tokens/1000 over trials the pipeline reports ok, None
      without any), counts {timeout, infra, agent_error, timeout_would_pass}, task_exception_types (the exception type
      of a task's first exception trial), difficulty {task: level}, per_task_trials {task: [trial dicts, by id]}.
    """
    task_trials: dict[str, list[dict]] = {}
    for record in records:
        task_trials.setdefault(task_name_of(record, task_field), []).append(trial_from_record(record, timeout_as))
    for trials in task_trials.values():
        trials.sort(key=lambda t: t["id"])
    if k is None:
        k = max((len(t) for t in task_trials.values()), default=1)
    k = max(1, int(k))

    exception_types: dict[str, int] = {}
    task_exception_types: dict[str, str] = {}
    timeout_tasks: set[str] = set()
    counts = {"timeout": 0, "infra": 0, "agent_error": 0, "timeout_would_pass": 0}
    token_values: list[float] = []
    difficulty: dict[str, object] = {}
    task_results: dict[str, str] = {}
    per_task_rollouts: dict[str, dict] = {}
    trial_n_pass = trial_n_fail = trial_n_exception = 0

    for task_name, trials in sorted(task_trials.items()):
        results = [t["result"] for t in trials]
        for t in trials:
            if t["result"] == EXCEPTION:
                et = t["exception_type"] or "Unknown"
                exception_types[et] = exception_types.get(et, 0) + 1
                task_exception_types.setdefault(task_name, et)
            if t["timeout"]:
                timeout_tasks.add(task_name)
                if t["verifier_passed"]:
                    counts["timeout_would_pass"] += 1
            if t["pipeline_status"] in counts:
                counts[t["pipeline_status"]] += 1
            if t["pipeline_status"] == "ok" and t["tokens"] is not None:
                token_values.append(t["tokens"] / 1000.0)
            if t["difficulty"] is not None:
                difficulty.setdefault(task_name, t["difficulty"])
        tp, tf, te = results.count(PASS), results.count(FAIL), results.count(EXCEPTION)
        trial_n_pass += tp
        trial_n_fail += tf
        trial_n_exception += te
        if tp == len(trials):
            task_results[task_name] = PASS
        elif te == len(trials):
            task_results[task_name] = EXCEPTION
        else:
            task_results[task_name] = FAIL
        per_task_rollouts[task_name] = {"n_pass": tp, "n_fail": tf, "n_exception": te, "total": len(trials)}

    n_pass = sum(1 for r in task_results.values() if r == PASS)
    n_fail = sum(1 for r in task_results.values() if r == FAIL)
    n_exception = sum(1 for r in task_results.values() if r == EXCEPTION)
    n_total = len(task_results)
    n_trials_total = trial_n_pass + trial_n_fail + trial_n_exception

    pass_at_k_metrics = compute_pass_at_k_metrics(per_task_rollouts, k)
    pass_rate = pass_at_k_metrics["pass_at"].get(1, 0.0) if n_total else 0.0

    return {
        "pass_rate": pass_rate,
        "n_pass": n_pass,
        "n_fail": n_fail,
        "n_exception": n_exception,
        "n_total": n_total,
        "k": k,
        "timeout_as": timeout_as,
        "exception_types": exception_types,
        "task_results": task_results,
        "timeout_tasks": sorted(timeout_tasks),
        "per_task_rollouts": per_task_rollouts,
        "pass_at_k": pass_at_k_metrics,
        "trial_stats": {
            "n_pass": trial_n_pass,
            "n_fail": trial_n_fail,
            "n_exception": trial_n_exception,
            "n_total": n_trials_total,
            "trial_pass_rate": trial_n_pass / n_trials_total if n_trials_total > 0 else 0.0,
        },
        "tokens_per_trial_k": sum(token_values) / len(token_values) if token_values else None,
        "counts": counts,
        "task_exception_types": task_exception_types,
        "difficulty": difficulty,
        "per_task_trials": {t: trials for t, trials in sorted(task_trials.items())},
    }


def pass_at(stats: dict) -> dict[int, float]:
    """`stats["pass_at_k"]["pass_at"]` with integer keys, whether the stats came from memory or from JSON."""
    rates = (stats.get("pass_at_k") or {}).get("pass_at") or {}
    return {int(i): float(v) for i, v in rates.items()}


# ---------- task history and stability (evolve.py:762-827) ----------

def update_task_history(history: dict, iteration: int, task_results: dict,
                        per_task_rollouts: dict | None = None) -> dict:
    """Update task history with current iteration results, return updated full history.

    Each entry is [iteration, result] or [iteration, result, rollout_info] when rollouts are known.
    rollout_info = {n_pass, n_fail, n_exception, total}. Upstream loads and saves `task_history.json` around this;
    here the caller does (state/task_history.json).
    """
    for task_name, result in task_results.items():
        if task_name not in history:
            history[task_name] = []
        existing_iters = {entry[0] for entry in history[task_name]}
        if iteration not in existing_iters:
            if per_task_rollouts and task_name in per_task_rollouts:
                history[task_name].append([iteration, result, per_task_rollouts[task_name]])
            else:
                history[task_name].append([iteration, result])
    return history


def compute_task_stability(task_history: dict, min_iterations: int = 3) -> dict:
    """Compute stability classification for each task based on historical data.

    Returns dict:
      stable_pass:      list[task_name] - Passed in all iterations that reached verifier
      stable_fail:      list[task_name] - Failed in all iterations that reached verifier
      unstable:         list[task_name] - Flipped between pass and fail (>=min_iterations data points)
      possibly_unstable: list[task_name] - Has both pass and fail but insufficient data (<min_iterations)
      infra_only:       list[task_name] - All exceptions, never reached verifier
    """
    stable_pass = []
    stable_fail = []
    unstable = []
    possibly_unstable = []
    infra_only = []

    for task_name, entries in task_history.items():
        verdicts = [e[1] for e in entries if e[1] in ("pass", "fail")]
        exceptions = [e[1] for e in entries if e[1] == "exception"]

        if not verdicts:
            if exceptions:
                infra_only.append(task_name)
            continue

        has_pass = "pass" in verdicts
        has_fail = "fail" in verdicts

        if has_pass and has_fail:
            if len(verdicts) >= min_iterations:
                unstable.append(task_name)
            else:
                possibly_unstable.append(task_name)
        elif has_pass and not has_fail:
            stable_pass.append(task_name)
        elif has_fail and not has_pass:
            stable_fail.append(task_name)

    return {
        "stable_pass": sorted(stable_pass),
        "stable_fail": sorted(stable_fail),
        "unstable": sorted(unstable),
        "possibly_unstable": sorted(possibly_unstable),
        "infra_only": sorted(infra_only),
    }


# ---------- compute_iteration_diff (evolve.py:830-914) ----------

def compute_iteration_diff(stats: dict, prev_stats: dict | None) -> dict | None:
    """Compare current and previous iteration task results, compute the full 9-state transition matrix.

    Takes the two `compute_stats` outputs (upstream: their task_results and per_task_rollouts). When rollout data
    is available, also subdivides stable_fail into rollout_improved / rollout_regressed / rollout_unchanged, and
    annotates each task with (prev_pass_count, cur_pass_count, k) in rollout_details.
    """
    if prev_stats is None:
        return None
    current_results = stats["task_results"]
    prev_results = prev_stats["task_results"]
    current_rollouts = stats.get("per_task_rollouts")
    prev_rollouts = prev_stats.get("per_task_rollouts")

    flipped = []           # fail -> pass (real capability improvement)
    regressed = []         # pass -> fail (real capability regression)
    infra_recovered = []   # exception -> pass (infra recovery)
    infra_lost = []        # pass -> exception (infra failure)
    stable_pass = []       # pass -> pass (consistently passing)
    stable_fail = []       # fail -> fail (consistently failing)
    exception_to_fail = [] # exception -> fail (infra recovered but task still fails, new optimization target)
    fail_to_exception = [] # fail -> exception (infra failure coverage)
    exception_stable = []  # exception -> exception (persistent infra error)

    rollout_improved = []  # fail -> fail but more rollouts passing
    rollout_regressed = [] # fail -> fail but fewer rollouts passing
    rollout_unchanged = [] # fail -> fail with same rollout pass count
    rollout_details = {}   # task -> (prev_n_pass, cur_n_pass, total)

    has_rollouts = bool(current_rollouts and prev_rollouts)

    all_tasks = set(current_results) | set(prev_results)
    for task in sorted(all_tasks):
        cur = current_results.get(task, "exception")
        prev = prev_results.get(task, "exception")

        if has_rollouts and (task in current_rollouts or task in prev_rollouts):
            cur_ro = current_rollouts.get(task, {})
            prev_ro = prev_rollouts.get(task, {})
            cur_np = cur_ro.get("n_pass", 0)
            prev_np = prev_ro.get("n_pass", 0)
            total = cur_ro.get("total", prev_ro.get("total", 0))
            rollout_details[task] = (prev_np, cur_np, total)

        if prev == cur == "pass":
            stable_pass.append(task)
        elif prev == cur == "fail":
            if has_rollouts and task in rollout_details:
                prev_np, cur_np, _ = rollout_details[task]
                if cur_np > prev_np:
                    rollout_improved.append(task)
                elif cur_np < prev_np:
                    rollout_regressed.append(task)
                else:
                    rollout_unchanged.append(task)
            stable_fail.append(task)
        elif prev == cur == "exception":
            exception_stable.append(task)
        elif prev == "fail" and cur == "pass":
            flipped.append(task)
        elif prev == "pass" and cur == "fail":
            regressed.append(task)
        elif prev == "exception" and cur == "pass":
            infra_recovered.append(task)
        elif prev == "pass" and cur == "exception":
            infra_lost.append(task)
        elif prev == "exception" and cur == "fail":
            exception_to_fail.append(task)
        elif prev == "fail" and cur == "exception":
            fail_to_exception.append(task)

    return {
        "flipped": flipped,
        "regressed": regressed,
        "net": len(flipped) - len(regressed),
        "infra_recovered": infra_recovered,
        "infra_lost": infra_lost,
        "stable_pass": stable_pass,
        "stable_fail": stable_fail,
        "exception_to_fail": exception_to_fail,
        "fail_to_exception": fail_to_exception,
        "exception_stable": exception_stable,
        "rollout_improved": rollout_improved,
        "rollout_regressed": rollout_regressed,
        "rollout_unchanged": rollout_unchanged,
        "rollout_details": rollout_details,
    }


# ---------- evolution_history.md, before the Evolve Agent runs (evolve.py:1803-1945) ----------

HISTORY_HEADER = "# Agentic Harness Engineering Evolution History\n\n"


def history_before(path: Path, iteration: int, stats: dict, diff: dict | None = None, source: str = "",
                   now: str | None = None) -> str:
    """Before evolve agent runs, append current iteration evaluation results to evolution_history.md.

    Includes per-task pass/fail, cross-iteration changes (flipped/regressed) and retention rate. `source` names the
    rollouts (upstream: the Harbor job directory; here the learning-material step and its adapter commit). Returns
    the entry appended.
    """
    path = Path(path)
    now = now or datetime.now().strftime("%Y-%m-%d %H:%M")
    exc_types = stats.get("exception_types", {})
    exc_str = ", ".join(f"{ek}: {ev}" for ek, ev in exc_types.items()) if exc_types else "None"
    k = stats.get("k", 1)

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HISTORY_HEADER, encoding="utf-8")

    task_results = stats.get("task_results", {})

    lines = []
    lines.append(f"## Iteration {iteration} — {now}")
    if k > 1:
        trial_stats = stats.get("trial_stats", {})
        pass_at_rates = pass_at(stats)
        lines.append(f"- **Evaluation** (each task x{k} rollouts)")
        if pass_at_rates:
            pak_str = " | ".join(f"pass@{i}={pass_at_rates[i]:.1%}" for i in sorted(pass_at_rates))
            lines.append(f"- {pak_str}")
        else:
            lines.append(f"- Pass rate (pass@1): {stats['pass_rate']:.1%}")
        per_task_rollouts = stats.get("per_task_rollouts", {})
        pass_count_dist = collections.Counter()
        for t, r in task_results.items():
            if r == "fail":
                np_ = per_task_rollouts.get(t, {}).get("n_pass", 0)
                pass_count_dist[np_] += 1
        breakdown_parts = []
        breakdown_parts.append(f"{stats['n_pass']} tasks passed all {k} rollouts")
        for np_count in sorted(pass_count_dist.keys(), reverse=True):
            if np_count > 0:
                breakdown_parts.append(f"{pass_count_dist[np_count]} tasks passed {np_count} of {k} rollouts")
        n_all_fail = pass_count_dist.get(0, 0)
        breakdown_parts.append(f"{n_all_fail} tasks failed all {k} rollouts")
        breakdown_parts.append(f"{stats['n_exception']} tasks hit exception")
        lines.append(f"- {stats['n_total']} tasks breakdown: {'; '.join(breakdown_parts)}")
        if trial_stats:
            lines.append(f"- Per-trial pass rate: {trial_stats['trial_pass_rate']:.1%} ({trial_stats['n_pass']}/{trial_stats['n_total']} trials)")
    else:
        lines.append(f"- Pass rate: {stats['pass_rate']:.1%} ({stats['n_pass']}/{stats['n_total']})")

    if diff is not None:
        n_regressed = len(diff.get("regressed", []))
        n_stable_pass = len(diff.get("stable_pass", []))
        n_prev_pass_tested = n_stable_pass + n_regressed
        if n_prev_pass_tested > 0:
            retention_rate = n_stable_pass / n_prev_pass_tested
            lines.append(f"- Regressions: {n_regressed} (passed last iteration -> failed this iteration)")
            lines.append(f"- Retention rate: {retention_rate:.1%} ({n_stable_pass}/{n_prev_pass_tested} previously passed and still passing)")

    lines.append(f"- Exception stats: {exc_str}")
    if stats.get("tokens_per_trial_k") is not None:
        lines.append(f"- Tokens per trial: {stats['tokens_per_trial_k']:.1f}k")
    if source:
        lines.append(f"- Rollouts: `{source}`")

    pass_tasks = sorted(t for t, r in task_results.items() if r == "pass")
    fail_tasks = sorted(t for t, r in task_results.items() if r == "fail")
    exc_tasks = sorted(t for t, r in task_results.items() if r == "exception")

    lines.append(f"\n### Task Details")
    if k > 1:
        per_task_rollouts = stats.get("per_task_rollouts", {})
        partial_pass_tasks = sorted(t for t in fail_tasks if per_task_rollouts.get(t, {}).get("n_pass", 0) > 0)
        all_fail_tasks = sorted(t for t in fail_tasks if per_task_rollouts.get(t, {}).get("n_pass", 0) == 0)

        lines.append(f"- ✅ All passed ({len(pass_tasks)}): {', '.join(pass_tasks) if pass_tasks else 'None'}")
        if partial_pass_tasks:
            partial_strs = [f"{t} ({per_task_rollouts[t]['n_pass']}/{per_task_rollouts[t]['total']})" for t in partial_pass_tasks]
            lines.append(f"- 🔶 Partial pass ({len(partial_pass_tasks)}): {', '.join(partial_strs)}")
        lines.append(f"- ❌ All failed ({len(all_fail_tasks)}): {', '.join(all_fail_tasks) if all_fail_tasks else 'None'}")
        lines.append(f"- ⚠️ Exception ({len(exc_tasks)}): {', '.join(exc_tasks[:15]) if exc_tasks else 'None'}")
    else:
        lines.append(f"- ✅ Passed ({len(pass_tasks)}): {', '.join(pass_tasks) if pass_tasks else 'None'}")
        lines.append(f"- ❌ Failed ({len(fail_tasks)}): {', '.join(fail_tasks) if fail_tasks else 'None'}")
        lines.append(f"- ⚠️ Exception ({len(exc_tasks)}): {', '.join(exc_tasks[:15]) if exc_tasks else 'None'}")

    if diff is not None:
        rollout_details = diff.get("rollout_details", {})

        def _fmt_task(t: str) -> str:
            """Format task name, appending rollout change annotation if available."""
            if t not in rollout_details:
                return t
            prev_np, cur_np, total = rollout_details[t]
            return f"{t} ({prev_np}/{total}->{cur_np}/{total})"

        def _fmt_tasks(tasks: list) -> str:
            return ", ".join(_fmt_task(t) for t in tasks) if tasks else "None"

        lines.append(f"\n### Cross-Iteration Changes (iteration {iteration-1} -> {iteration})")
        if k > 1 and rollout_details:
            if diff.get("flipped"):
                lines.append(f"- 🎉 fail->all-pass ({len(diff['flipped'])}): {_fmt_tasks(diff['flipped'])}")
            if diff.get("regressed"):
                lines.append(f"- 🔴 all-pass->fail ({len(diff['regressed'])}): {_fmt_tasks(diff['regressed'])}")
            if diff.get("stable_pass"):
                lines.append(f"- 🛡️ Stable all-pass ({len(diff['stable_pass'])}): {', '.join(diff['stable_pass'])}")
            if diff.get("rollout_improved"):
                lines.append(f"- 📈 Rollout improved, still not all-pass ({len(diff['rollout_improved'])}): {_fmt_tasks(diff['rollout_improved'])}")
            if diff.get("rollout_regressed"):
                lines.append(f"- 📉 Rollout regressed, still not all-pass ({len(diff['rollout_regressed'])}): {_fmt_tasks(diff['rollout_regressed'])}")
            if diff.get("rollout_unchanged"):
                lines.append(f"- 📌 Stable fail, same rollout results ({len(diff['rollout_unchanged'])}): {_fmt_tasks(diff['rollout_unchanged'])}")
            if diff.get("exception_to_fail"):
                lines.append(f"- 🆕 exception->fail ({len(diff['exception_to_fail'])}): {_fmt_tasks(diff['exception_to_fail'])}")
            if diff.get("infra_recovered"):
                lines.append(f"- exception->pass ({len(diff['infra_recovered'])}): {_fmt_tasks(diff['infra_recovered'])}")
            if diff.get("infra_lost"):
                lines.append(f"- pass->exception ({len(diff['infra_lost'])}): {_fmt_tasks(diff['infra_lost'])}")
            if diff.get("fail_to_exception"):
                lines.append(f"- fail->exception ({len(diff['fail_to_exception'])}): {_fmt_tasks(diff['fail_to_exception'])}")
        else:
            if diff.get("flipped"):
                lines.append(f"- 🎉 fail->pass ({len(diff['flipped'])}): {', '.join(diff['flipped'])}")
            if diff.get("regressed"):
                lines.append(f"- 🔴 pass->fail ({len(diff['regressed'])}): {', '.join(diff['regressed'])}")
            if diff.get("stable_pass"):
                lines.append(f"- 🛡️ Stable pass ({len(diff['stable_pass'])}): {', '.join(diff['stable_pass'])}")
            if diff.get("stable_fail"):
                lines.append(f"- 📌 Stable fail ({len(diff['stable_fail'])}): {', '.join(diff['stable_fail'])}")
            if diff.get("exception_to_fail"):
                lines.append(f"- 🆕 exception->fail ({len(diff['exception_to_fail'])}): {', '.join(diff['exception_to_fail'])}")
            if diff.get("infra_recovered"):
                lines.append(f"- exception->pass ({len(diff['infra_recovered'])}): {', '.join(diff['infra_recovered'])}")
            if diff.get("infra_lost"):
                lines.append(f"- pass->exception ({len(diff['infra_lost'])}): {', '.join(diff['infra_lost'])}")
            if diff.get("fail_to_exception"):
                lines.append(f"- fail->exception ({len(diff['fail_to_exception'])}): {', '.join(diff['fail_to_exception'])}")

    lines.append("")
    entry = "\n".join(lines) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)
    return entry


# ---------- the change manifest and its attribution (evolve.py:2205-2329) ----------

def load_change_manifest(path: Path, iteration: int) -> dict | None:
    """The change manifest at `path` when it is the one written in `iteration` (`data["iteration"] == iteration`,
    the gate of evolve.py:2215); None when the file is missing, unreadable or from another iteration, which
    upstream treats as "no manifest, no attribution"."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if isinstance(data, dict) and data.get("iteration") == iteration:
        return data
    return None


def evaluate_changes(manifest: dict, diff: dict, current_task_results: dict | None = None) -> dict:
    """Generate attribution evaluation for each change based on change_manifest and iteration diff.

    Returns:
      {
        "evaluating_iteration": int,
        "change_evaluations": [
          {
            "change_id": str,
            "description": str,
            "files": list[str],
            "predicted_fixes": list[str],
            "actually_fixed": list[str],
            "still_failed": list[str],
            "predicted_risks": list[str],
            "risk_realized": list[str],
            "hit_rate": str,
            "verdict": "EFFECTIVE" | "PARTIALLY_EFFECTIVE" | "MIXED" | "INEFFECTIVE" | "HARMFUL"
          }
        ],
        "unattributed_regressions": list[str],
        "summary": str
      }
    (`current_task_results` is accepted for signature fidelity; upstream does not use it either.)
    """
    changes = manifest.get("changes", [])
    flipped_set = set(diff.get("flipped", []))
    regressed_set = set(diff.get("regressed", []))

    all_predicted = set()
    all_risk = set()
    evaluations = []

    for chg in changes:
        chg_id = chg.get("id", "unknown")
        predicted = chg.get("predicted_fixes", [])
        risks = chg.get("risk_tasks", [])

        all_predicted.update(predicted)
        all_risk.update(risks)

        actually_fixed = [t for t in predicted if t in flipped_set]
        still_failed = [t for t in predicted if t not in flipped_set]
        risk_realized = [t for t in risks if t in regressed_set]

        # Additional check: whether regressions have tasks related to this change's files but not in predicted/risk
        # (This requires knowing which file affects which task - we cannot determine this precisely, so we only use declared risk_tasks)

        n_fixed = len(actually_fixed)
        n_predicted = len(predicted)
        n_risk_hit = len(risk_realized)

        if n_risk_hit > 0 and n_fixed == 0:
            verdict = "HARMFUL"
        elif n_risk_hit > 0 and n_fixed > 0:
            verdict = "MIXED"
        elif n_fixed == n_predicted and n_predicted > 0:
            verdict = "EFFECTIVE"
        elif n_fixed > 0:
            verdict = "PARTIALLY_EFFECTIVE"
        else:
            verdict = "INEFFECTIVE"

        evaluations.append({
            "change_id": chg_id,
            "description": chg.get("description", ""),
            "files": chg.get("files", []),
            "predicted_fixes": predicted,
            "actually_fixed": actually_fixed,
            "still_failed": still_failed,
            "predicted_risks": risks,
            "risk_realized": risk_realized,
            "hit_rate": f"{n_fixed}/{n_predicted}" if n_predicted > 0 else "0/0",
            "verdict": verdict,
        })

    # Find unattributed regressions (not in any change's predicted or risk lists)
    attributed_tasks = all_predicted | all_risk
    unattributed = [t for t in regressed_set if t not in attributed_tasks]

    summary_parts = [f"{e['change_id']}: {e['verdict']}" for e in evaluations]

    return {
        "evaluating_iteration": manifest.get("iteration", 0),
        "change_evaluations": evaluations,
        "unattributed_regressions": sorted(unattributed),
        "summary": ", ".join(summary_parts),
    }


# ---------- best ever (evolve.py:2361-2413) ----------

def _task_breakdown(stats: dict) -> dict:
    """The `tasks` entry of best_ever.json and iteration_scores.yaml: how many tasks passed i of k rollouts."""
    k = stats.get("k", 1)
    n_total = stats["n_pass"] + stats["n_fail"] + stats["n_exception"]
    if k > 1:
        per_task_rollouts = stats.get("per_task_rollouts", {})
        task_results = stats.get("task_results", {})
        pass_count_dist = collections.Counter()
        for t, r in task_results.items():
            if r == "pass":
                pass_count_dist[k] += 1
            elif r == "exception":
                pass_count_dist["exception"] += 1
            else:
                pass_count_dist[per_task_rollouts.get(t, {}).get("n_pass", 0)] += 1
        tasks_entry = {"total": n_total}
        for i in range(k, -1, -1):
            tasks_entry[f"passed_{i}_of_{k}"] = pass_count_dist.get(i, 0)
        tasks_entry["all_exception"] = pass_count_dist.get("exception", 0)
        return tasks_entry
    return {"total": n_total, "pass": stats["n_pass"], "fail": stats["n_fail"], "exception": stats["n_exception"]}


def _trials_entry(stats: dict) -> dict | None:
    trial_stats = stats.get("trial_stats", {})
    if not trial_stats:
        return None
    return {"total": trial_stats["n_total"], "pass": trial_stats["n_pass"], "fail": trial_stats["n_fail"],
            "exception": trial_stats["n_exception"], "pass_rate": round(trial_stats["trial_pass_rate"], 4)}


def update_best_ever(current: dict | None, iteration: int, stats: dict, commit: str | None = None) -> dict:
    """Update best-ever with current iteration, return latest best-ever info (the new record when this iteration's
    pass rate is strictly greater, else `current` unchanged). Hermit adds `step` (= iteration) and `commit`, the
    adapter commit these rollouts evaluated, which finalize restores (spec B.9)."""
    pass_rate = stats["pass_rate"]
    if current is None or pass_rate > current.get("pass_rate", current.get("capability_rate", 0)):
        k = stats.get("k", 1)
        best = {"iteration": iteration, "step": iteration, "commit": commit, "pass_rate": stats["pass_rate"], "k": k,
                "tasks": _task_breakdown(stats)}
        if k > 1:
            trials = _trials_entry(stats)
            if trials:
                best["trials"] = trials
            if pass_at(stats):
                best["pass_at"] = {i: round(v, 4) for i, v in pass_at(stats).items()}
        return best
    return current


# ---------- the Debugger's overview and detail files (evolve.py:1546-1719) ----------

def extract_one_liner(qa_response: str) -> str:
    """Extract a short plain-text summary for overview lines (single line).

    adb replies are often Markdown (## 回答, numbered sections). The old
    `. ` split misfired on ``1. ...`` list markers and produced broken overviews.
    """
    s = qa_response.strip()
    if s.startswith("[adb"):
        # Try to extract a FAILED test line from verifier output in the fallback
        for line in s.splitlines():
            stripped = line.strip()
            # Handle both raw "FAILED ..." and list-prefixed "- FAILED ..."
            if stripped.startswith("- "):
                stripped = stripped[2:].strip()
            if stripped.startswith("FAILED "):
                summary = stripped[:100] if len(stripped) > 100 else stripped
                tag = s.split("]", 1)[0] + "]"
                return f"{tag} verifier: {summary}"
        return s.split("\n", 1)[0][:100]

    # Prefer the full ROOT CAUSE paragraph: the prompt asks adb to lead with
    # "ROOT CAUSE:" and follow with sibling sections (PASS vs FAIL, CRITICAL
    # MISTAKE, GENERAL MECHANISM) or a blank line. Capture everything up to
    # the next such boundary so the overview keeps the full diagnosis.
    # Hermit: a `**`-bold label (`**PASS vs FAIL:**`, what Codex writes despite the Markdown-free instruction) is a
    # boundary too; upstream's lookahead would capture the whole answer.
    rc = re.search(
        r"ROOT CAUSE\s*[:：]\s*(.+?)(?=\n\s*\n|\n\s*\**\s*(?:PASS\s+vs\s+FAIL|CRITICAL\s+MISTAKE|GENERAL\s+MECHANISM)\b|\Z)",
        s, flags=re.DOTALL | re.IGNORECASE,
    )
    if rc:
        return re.sub(r"\s+", " ", rc.group(1)).strip()

    # Fallback: first non-trivial prose line, no length cap.
    for raw in s.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        line = re.sub(r"^\d+[\.)]\s*", "", line)
        if line:
            return re.sub(r"\s+", " ", line).strip()
    return re.sub(r"\s+", " ", s).strip()


def _reward_label(rv: float | None) -> str:
    """PASS / FAIL / TIMEOUT of a trace from its reward; upstream reads a missing reward from `reward.txt`, which
    Hermit's results carry inline (None = fail)."""
    if rv is not None and rv < 0:
        return "TIMEOUT"
    return "PASS" if rv is not None and rv >= 1.0 else "FAIL"


def _detail_note(task: str) -> str:
    """Beside a task in the overview: its detail file, when that is not `<task>.md` (see task_file_name)."""
    name = task_file_name(task)
    return "" if name == task else f" (detail: `{name}.md`)"


def write_debugger_analyse(results: list[dict], analyse_dir: Path, iteration: int,
                           detail_label: str | None = None) -> tuple[Path, str]:
    """Write overview.md + detail/{task}.md under ``analyse_dir`` (the file name is `task_file_name(task)`, which the
    overview names beside a task whose name is no plain file name).

    Each result is the Debugger's record of one task: task_name, mode ("debug" | "summary"), n_pass, n_fail,
    n_timeout, is_timeout, response, trace_paths, trace_rewards (>= 1 pass, < 0 timeout, else fail),
    verifier_outputs. `detail_label` is the path the overview names for the detail files (upstream: relative to the
    experiment; here `{state}/analysis/step_N/detail`); it defaults to the detail directory itself.

    Returns (analyse_dir, query_snippet) where query_snippet is the overview
    body suitable for embedding in the evolution query (no top-level heading).
    """
    analyse_dir = Path(analyse_dir)
    detail_dir = analyse_dir / "detail"
    detail_dir.mkdir(parents=True, exist_ok=True)

    debug_results = [r for r in results if r["mode"] == "debug" and not r.get("is_timeout")]
    timeout_results = [r for r in results if r.get("is_timeout")]
    summary_results = [r for r in results if r["mode"] == "summary"]

    n_debug = len(debug_results)
    n_timeout = len(timeout_results)
    n_summary = len(summary_results)
    parts = []
    if n_debug:
        parts.append(f"{n_debug} debug")
    if n_timeout:
        parts.append(f"{n_timeout} timeout")
    if n_summary:
        parts.append(f"{n_summary} summary")
    body_lines: list[str] = [
        f"Analyzed {len(results)} tasks ({' + '.join(parts)}).\n",
    ]

    if timeout_results:
        body_lines.append("### Timeout (agent exceeded time limit)\n")
        for r in timeout_results:
            n_to = r.get("n_timeout", 0)
            n_total = r["n_pass"] + r["n_fail"] + n_to
            one_liner = extract_one_liner(r["response"])
            body_lines.append(
                f"- **{r['task_name']}**{_detail_note(r['task_name'])} ({n_to}/{n_total} timed out): {one_liner}"
            )
        body_lines.append("")

    if debug_results:
        body_lines.append("### Debug (has failures)\n")
        for r in debug_results:
            n_total = r["n_pass"] + r["n_fail"] + r.get("n_timeout", 0)
            one_liner = extract_one_liner(r["response"])
            body_lines.append(
                f"- **{r['task_name']}**{_detail_note(r['task_name'])} ({r['n_pass']}/{n_total} pass): {one_liner}"
            )
        body_lines.append("")

    if summary_results:
        names = ", ".join(r["task_name"] + _detail_note(r["task_name"]) for r in summary_results)
        body_lines.append(f"### Summary (all pass): {len(summary_results)} tasks\n")
        body_lines.append(f"{names}\n")
        body_lines.append(f"(All rollouts passed — no per-task analysis included. Read `detail/{{task}}.md` if needed.)\n")

    detail_rel = detail_label if detail_label is not None else str(detail_dir)
    body_lines.append(f"Detail per task: `{detail_rel}/`")
    query_snippet = "\n".join(body_lines)

    overview_lines = [f"# Debugger Analysis Overview — Iteration {iteration}\n", query_snippet]
    overview_path = analyse_dir / "overview.md"
    overview_path.write_text("\n".join(overview_lines), encoding="utf-8")

    for r in results:
        n_to = r.get("n_timeout", 0)
        n_total = r["n_pass"] + r["n_fail"] + n_to
        trace_rewards = r.get("trace_rewards", [])
        verifier_outputs = r.get("verifier_outputs", [])

        status_parts = [f"{r['n_pass']} pass", f"{r['n_fail']} fail"]
        if n_to > 0:
            status_parts.append(f"{n_to} timeout")
        status_str = ", ".join(status_parts)

        heading_extra = " ⏱️ TIMEOUT" if r.get("is_timeout") else ""
        detail_lines = [
            f"# {r['task_name']} ({r['n_pass']}/{n_total} pass){heading_extra}\n",
            f"Analyzed {n_total} traces ({status_str}).\n",
        ]

        # Trace Paths at the top so trace01/trace02 labels are visible first
        detail_lines.append("## Trace Paths\n")
        for idx, tp in enumerate(r["trace_paths"]):
            rv = trace_rewards[idx] if idx < len(trace_rewards) else None
            detail_lines.append(f"- **trace{idx+1:02d}** ({_reward_label(rv)}): `{tp}`")
        detail_lines.append("")

        detail_lines.append("## QA Analysis\n")
        detail_lines.append(r["response"])

        # Add verifier test output section for failing tasks
        has_verifier = False
        for idx, vout in enumerate(verifier_outputs):
            rv = trace_rewards[idx] if idx < len(trace_rewards) else 0.0
            if not vout or rv >= 1.0:
                continue
            if not has_verifier:
                detail_lines.append("\n## Verifier Test Output\n")
                has_verifier = True
            label = "TIMEOUT" if rv < 0 else "FAIL"
            lines = vout.strip().splitlines()
            if len(lines) > 80:
                vout_show = "\n".join(lines[-80:])
            else:
                vout_show = vout.strip()
            detail_lines.append(f"### trace{idx+1:02d} ({label})\n")
            detail_lines.append(f"```\n{vout_show}\n```\n")

        task_path = detail_dir / f"{task_file_name(r['task_name'])}.md"
        task_path.write_text("\n".join(detail_lines), encoding="utf-8")

    return analyse_dir, query_snippet


# ---------- iteration_scores.yaml / .md (evolve.py:1975-2130) ----------

def load_iteration_scores(state_dir: Path) -> list[dict]:
    """The `scores` list of `state/iteration_scores.yaml` (the `scores_trend` of the query), empty before step 1."""
    import yaml
    path = Path(state_dir) / "iteration_scores.yaml"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return list(data.get("scores", []))


def update_iteration_scores(state_dir: Path, iteration: int, stats: dict, *, timing: dict | None = None,
                            experiment: str = "", model: str = "", source: str = "",
                            timestamp: str | None = None, extra: dict | None = None) -> dict:
    """Append current iteration scores to iteration_scores.yaml and regenerate .md. Returns the entry.

    Hermit's entry carries, beyond upstream's, `tokens_per_trial_k` and the trial `counts` (timeout / infra /
    agent_error / timeout_would_pass), and `extra` as the caller gives it (the Evolver's verdicts and rollbacks of
    the step, which the TB2 report's iteration table reads); `source` stands for upstream's `job_dir` (the
    learning-material step).
    """
    import yaml
    state_dir = Path(state_dir)
    scores_path = state_dir / "iteration_scores.yaml"
    k = stats.get("k", 1)

    if scores_path.exists():
        with open(scores_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data.setdefault("scores", [])
    else:
        data = {"experiment": experiment, "model": model, "scores": []}

    entry = {
        "iteration": iteration,
        "pass_rate": round(stats["pass_rate"], 4),
        "k": k,
        "timestamp": timestamp or datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "source": source,
    }
    if timing:
        entry["timing"] = timing
    entry["tasks"] = _task_breakdown(stats)
    if k > 1:
        trials = _trials_entry(stats)
        if trials:
            entry["trials"] = trials
        if pass_at(stats):
            entry["pass_at"] = {i: round(v, 4) for i, v in pass_at(stats).items()}
    if stats.get("tokens_per_trial_k") is not None:
        entry["tokens_per_trial_k"] = round(float(stats["tokens_per_trial_k"]), 2)
    if stats.get("counts"):
        entry["counts"] = dict(stats["counts"])
    if extra:
        entry.update(extra)

    data["scores"].append(entry)
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(scores_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
    regenerate_scores_md(state_dir, data)
    return entry


def regenerate_scores_md(state_dir: Path, data: dict | None = None) -> None:
    """Regenerate iteration_scores.md from iteration_scores.yaml (or provided data dict)."""
    state_dir = Path(state_dir)
    if data is None:
        import yaml
        scores_path = state_dir / "iteration_scores.yaml"
        if not scores_path.exists():
            return
        with open(scores_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    scores = data.get("scores", [])
    md_path = state_dir / "iteration_scores.md"
    if not scores:
        md_path.write_text(f"# {data.get('experiment', '')} Iteration Scores\n\n(no data)\n", encoding="utf-8")
        return

    k = scores[0].get("k", 1)
    has_timing = any(s.get("timing") for s in scores)
    has_tokens = any(s.get("tokens_per_trial_k") is not None for s in scores)

    def _tail_cells(s: dict) -> str:
        """The Hermit columns (tokens, timing) of a row."""
        out = ""
        if has_tokens:
            tok = s.get("tokens_per_trial_k")
            out += f" {tok:.1f}k |" if tok is not None else " - |"
        if has_timing:
            t = s.get("timing", {})
            total = t.get("total_min", "")
            out += (f" {total}m | {t.get('eval_min', '')}m | {t.get('analysis_min', '')}m | {t.get('evolve_min', '')}m |"
                    if total else " - | - | - | - |")
        return out

    tail_header = (" Tok/trial |" if has_tokens else "") + (" Duration | Eval | Anlys | Evolve |" if has_timing else "")
    tail_sep = (" --------- |" if has_tokens else "") + (" -------- | ---- | ----- | ------ |" if has_timing else "")

    if k > 1:
        task_col_headers = " | ".join(f"{i}/{k}" for i in range(k, -1, -1)) + " | Exc"
        task_col_seps = " | ".join("---" for _ in range(k + 1)) + " | ---"
        lines = [
            f"# {data.get('experiment', '')} Iteration Scores (k={k})\n",
            f"| Iter | " + " | ".join(f"pass@{i}" for i in range(1, k + 1))
            + f" | Tasks | {task_col_headers}"
            + f" | Trials | Trial P | Trial F | Trial E |{tail_header} Time |",
            f"|------|" + " | ".join("------" for _ in range(1, k + 1))
            + f" | ----- | {task_col_seps}"
            + f" | ------ | ------ | ------ | ------ |{tail_sep} ------|",
        ]
        for s in scores:
            ts = s.get("timestamp", "")[:16].replace("T", " ")
            s_pass_at = s.get("pass_at", {})
            tasks = s.get("tasks", {})
            trials = s.get("trials", {})

            pak_cells = ""
            for i in range(1, k + 1):
                val = s_pass_at.get(i, s_pass_at.get(str(i), 0))
                pak_cells += f" {val:.1%} |"

            t_total = tasks.get("total", s.get("n_total", 0))
            task_dist_cells = ""
            for i in range(k, -1, -1):
                key = f"passed_{i}_of_{k}"
                if key in tasks:
                    task_dist_cells += f" {tasks[key]} |"
                elif i == k:
                    task_dist_cells += f" {tasks.get('all_pass', s.get('n_pass', 0))} |"
                elif i == 0:
                    task_dist_cells += f" {tasks.get('all_fail', s.get('n_fail', 0))} |"
                else:
                    task_dist_cells += " ? |"
            t_exc = tasks.get("all_exception", tasks.get("exception", s.get("n_exception", 0)))
            task_dist_cells += f" {t_exc} |"

            if trials:
                tr_total = trials.get("total", s.get("n_trials", 0))
                tr_pass = trials.get("pass", "?")
                tr_fail = trials.get("fail", "?")
                tr_exc = trials.get("exception", "?")
            else:
                tr_total = s.get("n_trials", 0)
                tr_pass = tr_fail = tr_exc = "?"

            lines.append(
                f"| {s['iteration']} |{pak_cells}"
                f" {t_total} |{task_dist_cells}"
                f" {tr_total} | {tr_pass} | {tr_fail} | {tr_exc} |{_tail_cells(s)} {ts} |"
            )
    else:
        lines = [
            f"# {data.get('experiment', '')} Iteration Scores\n",
            f"| Iter | Pass Rate | Pass | Fail | Exc | Total |{tail_header} Time |",
            f"|------|-----------|------|------|-----|-------|{tail_sep} ------|",
        ]
        for s in scores:
            ts = s.get("timestamp", "")[:16].replace("T", " ")
            rate = s.get("pass_rate", 0)
            tasks = s.get("tasks", {})
            t_pass = tasks.get("pass", s.get("n_pass", 0))
            t_fail = tasks.get("fail", s.get("n_fail", 0))
            t_exception = tasks.get("exception", s.get("n_exception", 0))
            t_total = tasks.get("total", s.get("n_total", 0))
            lines.append(f"| {s['iteration']} | {rate:.1%} | {t_pass} | {t_fail} | {t_exception} | {t_total} |"
                         f"{_tail_cells(s)} {ts} |")

    if len(scores) >= 2:
        first_rate = scores[0].get("pass_rate", 0)
        last_rate = scores[-1].get("pass_rate", 0)
        diff_val = last_rate - first_rate
        lines.append(
            f"\nPass rate trend: {first_rate:.1%} -> {last_rate:.1%} "
            f"({'+' if diff_val >= 0 else ''}{diff_val*100:.1f}pp over {len(scores)} iterations)"
        )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- the evolution query (evolve.py:2563-2919) ----------

def build_evolution_query(ctx_paths: dict, stats: dict, *, step: int, diff: dict | None = None,
                          prev_stats: dict | None = None, stability: dict | None = None,
                          best_ever: dict | None = None, scores_trend: list[dict] | None = None,
                          evaluation: dict | None = None, adb_overview: str | None = None,
                          analysis_dir: str | Path | None = None, rollbacks: list[dict] | None = None,
                          rollback_mode: str = "auto", prev_commit: str | None = None,
                          history_md: str | Path | None = None, commit: str | None = None) -> str:
    """Build the evolution agent query.

    Provides concise structured info: evaluation overview, task classification, cross-iteration changes,
    debugger analysis, historical trends, stability analysis, change attribution.
    Deep trajectory analysis is left for the evolve agent to explore.

    `ctx_paths` names the Evolver's directories as the agent sees them: `learning_material`, `state`, `adapter`
    (spec B.7). `step` is the Hermit step (= upstream's iteration); `commit` the adapter commit these rollouts
    evaluated; `evaluation` is `evaluate_changes` output for the previous step's manifest, `rollbacks` what the
    framework rolled back (B.6: {change_id, files, ok, error?}) under `rollback_mode` "auto", while "agent" renders
    upstream's "Rollback method" paragraph against `prev_commit`; `scores_trend` is `load_iteration_scores` output;
    `history_md` the evolution history file to point at.
    """
    lm = str(ctx_paths.get("learning_material", "learning_material")).rstrip("/")
    adapter = str(ctx_paths.get("adapter", "adapter")).rstrip("/")
    state = str(ctx_paths.get("state", "state")).rstrip("/")
    analysis_dir = str(analysis_dir) if analysis_dir is not None else f"{state}/analysis/step_{step:04d}"
    k = stats.get("k", 1)
    lines = [f"Iteration {step} evaluation completed.\n"]

    # -- 1. Current Iteration Overview --
    lines.append("## 1. Current Iteration Overview")
    lines.append(f"- Results path: `{lm}/current/` (one `tasks/<id>.json` per rollout, its verdict in `judge`: "
                 f"score and feedback; the trajectory of rollout `<id>` is `trajectories/<id>/agent-codex.jsonl` (the record's "
                 f"`trajectory` field names it; `<id>` is the name of the `tasks/<id>.json` file, the id written "
                 f"file-safe: `task#1` is `task_1`), the Codex event stream, with `prompt.txt`, `hook-calls.jsonl` and "
                 f"`hook-state/` beside it)")
    if commit:
        lines.append(f"- Evaluated adapter: commit `{commit}` of `{adapter}/` (your edits of step {step - 1})")

    if k > 1:
        trial_stats = stats.get("trial_stats", {})
        pass_at_rates = pass_at(stats)
        lines.append(f"- **Evaluation**: each task runs {k} times")
        lines.append(f"- Pass rate: **{stats['pass_rate']:.1%}**")
        if pass_at_rates:
            pak_str = " | ".join(f"pass@{i}={pass_at_rates[i]:.1%}" for i in sorted(pass_at_rates))
            lines.append(f"- **Pass@k**: {pak_str}")
        lines.append(f"- Tasks: {stats['n_pass']} all-pass | {stats['n_fail']} has-failure | {stats['n_exception']} exception")
        if trial_stats:
            lines.append(f"- Per-trial stats: {trial_stats['n_pass']} pass, {trial_stats['n_fail']} fail, {trial_stats['n_exception']} exception out of {trial_stats['n_total']} trials (trial pass rate: {trial_stats['trial_pass_rate']:.1%})")
    else:
        lines.append(f"- Pass rate: **{stats['pass_rate']:.1%}** ({stats['n_pass']}/{stats['n_total']})")
        lines.append(f"- Pass: {stats['n_pass']} | Fail: {stats['n_fail']} | Exception: {stats['n_exception']}")

    if stats["exception_types"]:
        exc_str = ", ".join(f"{ek}: {ev}" for ek, ev in stats["exception_types"].items())
        lines.append(f"- Exception types: {exc_str}")
    task_results = stats["task_results"]
    timeout_tasks_set = set(stats.get("timeout_tasks", ()))
    exc_tasks = sorted(t for t, r in task_results.items() if r == "exception")
    timeout_exc_tasks = sorted(t for t in exc_tasks if t in timeout_tasks_set)
    pure_exc_tasks = sorted(t for t in exc_tasks if t not in timeout_tasks_set)
    n_timeout_tasks = len(timeout_tasks_set)
    n_pure_infra = len(pure_exc_tasks)
    if n_timeout_tasks > 0:
        lines.append(f"- ⏱️ {n_timeout_tasks} tasks TIMED OUT (agent ran out of time, traces available for analysis)")
    if n_pure_infra > 0:
        lines.append(f"- Warning: {n_pure_infra} tasks had infra errors (NOT agent capability issues)")
    if stats.get("tokens_per_trial_k") is not None:
        lines.append(f"- Tokens per trial: {stats['tokens_per_trial_k']:.1f}k (mean over the rollouts that completed)")
    counts = stats.get("counts") or {}
    if any(counts.values()):
        lines.append(f"- Rollouts that never reached a verdict: {counts.get('timeout', 0)} timeout, "
                     f"{counts.get('infra', 0)} infra, {counts.get('agent_error', 0)} agent error"
                     + (f"; {counts['timeout_would_pass']} timed out although the verifier then passed (counted as "
                        f"failures)" if counts.get("timeout_would_pass") else ""))

    if diff is not None:
        n_regressed = len(diff.get("regressed", []))
        n_stable_pass = len(diff.get("stable_pass", []))
        n_prev_pass_tested = n_stable_pass + n_regressed
        if n_prev_pass_tested > 0:
            retention_rate = n_stable_pass / n_prev_pass_tested
            lines.append(f"- Regressions: **{n_regressed}** (passed last iteration -> failed this iteration)")
            lines.append(f"- Retention rate: **{retention_rate:.1%}** ({n_stable_pass}/{n_prev_pass_tested} previously passed and still passing)")

    # -- 2. Task Classification Details --
    lines.append("\n## 2. Task Classification Details")
    per_task_rollouts = stats.get("per_task_rollouts", {})
    per_task_trials = stats.get("per_task_trials", {})
    pass_tasks = sorted(t for t, r in task_results.items() if r == "pass")
    fail_tasks = sorted(t for t, r in task_results.items() if r == "fail")

    if k > 1:
        lines.append(f"\n### ✅ Passed ({len(pass_tasks)}) — all {k} rollouts passed")
    else:
        lines.append(f"\n### ✅ Passed ({len(pass_tasks)})")
    if pass_tasks:
        lines.append(", ".join(pass_tasks))

    if k > 1:
        per_task_pass_at = (stats.get("pass_at_k") or {}).get("per_task_pass_at", {})
        lines.append(f"\n### ❌ Failed ({len(fail_tasks)}) - Primary optimization target")
        if fail_tasks and per_task_rollouts:
            partial_pass = []
            zero_pass = []
            for t in fail_tasks:
                ro = per_task_rollouts.get(t, {})
                tp = ro.get("n_pass", 0)
                if tp > 0:
                    partial_pass.append((t, tp, ro.get("total", k)))
                else:
                    zero_pass.append(t)
            if partial_pass:
                lines.append(f"\n#### Partial pass ({len(partial_pass)}) — high-priority targets")
                lines.append(f"**Action required**: For each partial-pass task, read BOTH a passing and a failing rollout's `agent-codex.jsonl` (under `{lm}/current/trajectories/<id>/`), compare where they diverge, and identify why one succeeded and the other failed.")
                for t, tp, total in partial_pass:
                    task_pass_at = {int(i): v for i, v in (per_task_pass_at.get(t) or {}).items()}
                    pak_str = ", ".join(f"pass@{i}={task_pass_at[i]:.0%}" for i in sorted(task_pass_at)) if task_pass_at else ""
                    ids = ", ".join(f"{tr['id']}={tr['result'].upper() if not tr.get('timeout') else 'TIMEOUT'}"
                                    for tr in per_task_trials.get(t, []))
                    lines.append(f"- {t}: {tp}/{total} passed ({pak_str})" + (f" — rollouts: {ids}" if ids else ""))
            if zero_pass:
                lines.append(f"\n#### Zero pass ({len(zero_pass)}) — failed all {k} rollouts")
                lines.append(", ".join(zero_pass))
        elif fail_tasks:
            lines.append(", ".join(fail_tasks))
    else:
        lines.append(f"\n### ❌ Failed ({len(fail_tasks)}) - Primary optimization target")
        if fail_tasks:
            lines.append(", ".join(fail_tasks))

    if timeout_exc_tasks:
        lines.append(f"\n### ⏱️ Timed Out ({len(timeout_exc_tasks)}) - Agent exceeded time limit, analyze traces")
        lines.append("These tasks timed out (agent ran out of time). Their traces show what the agent did before being terminated.")
        lines.append("Analyze these traces to understand why the agent was too slow and how to improve efficiency.")
        lines.append(", ".join(timeout_exc_tasks))

    if pure_exc_tasks:
        lines.append(f"\n### ⚠️ Infrastructure Exceptions ({len(pure_exc_tasks)}, not agent issues, please ignore)")
        exc_by_type: dict[str, list[str]] = {}
        task_exception_types = stats.get("task_exception_types") or {}
        for tn in pure_exc_tasks:
            exc_by_type.setdefault(task_exception_types.get(tn, "Unknown"), []).append(tn)
        for et, tasks in exc_by_type.items():
            lines.append(f"- {et} ({len(tasks)}): {', '.join(tasks[:10])}")

    # -- 3. Cross-Iteration Change Matrix --
    if diff is not None:
        rollout_details = diff.get("rollout_details", {})
        lines.append(f"\n## 3. Detailed Cross-Iteration Analysis (iteration {step-1} -> {step})")

        if k > 1 and rollout_details:
            def _eq_fmt(t):
                if t in rollout_details:
                    pn, cn, tot = rollout_details[t]
                    return f"{t} ({pn}/{tot}->{cn}/{tot})"
                return t
            def _eq_fmt_list(tasks):
                return ", ".join(_eq_fmt(t) for t in tasks)

            if diff["flipped"]:
                lines.append(f"\n### 🎉 Improved fail->all-pass ({len(diff['flipped'])}) - Your changes worked on these tasks")
                lines.append(_eq_fmt_list(diff["flipped"]))
            if diff["regressed"]:
                lines.append(f"\n### 🔴 Regressed all-pass->fail ({len(diff['regressed'])}) - Must be addressed first")
                lines.append(_eq_fmt_list(diff["regressed"]))
            if diff.get("rollout_improved"):
                lines.append(f"\n### 📈 Rollout improved, still not all-pass ({len(diff['rollout_improved'])}) - Partial improvement")
                lines.append(_eq_fmt_list(diff["rollout_improved"]))
            if diff.get("rollout_regressed"):
                lines.append(f"\n### 📉 Rollout regressed, still not all-pass ({len(diff['rollout_regressed'])}) - Partial regression")
                lines.append(_eq_fmt_list(diff["rollout_regressed"]))
            if diff.get("rollout_unchanged"):
                lines.append(f"\n### 📌 Stable fail, same rollout results ({len(diff['rollout_unchanged'])}) - Needs new strategy")
                lines.append(_eq_fmt_list(diff["rollout_unchanged"]))
            if diff.get("stable_pass"):
                lines.append(f"\n### 🛡️ Stable all-pass ({len(diff['stable_pass'])}) - Protect, avoid breaking")
                lines.append(", ".join(diff["stable_pass"]))
            if diff.get("exception_to_fail"):
                lines.append(f"\n### 🆕 New optimization targets exception->fail ({len(diff['exception_to_fail'])}) - Previously blocked by infra errors, now optimizable")
                lines.append(_eq_fmt_list(diff["exception_to_fail"]))
            if diff.get("infra_recovered"):
                lines.append(f"\n### Infra recovered exception->pass ({len(diff['infra_recovered'])}, for reference only)")
            if diff.get("infra_lost"):
                lines.append(f"\n### Infra failure pass->exception ({len(diff['infra_lost'])}, for reference only)")
            if diff.get("fail_to_exception"):
                lines.append(f"\n### fail->exception ({len(diff['fail_to_exception'])}, for reference only)")
        else:
            if diff["flipped"]:
                lines.append(f"\n### 🎉 Improved fail->pass ({len(diff['flipped'])}) - Your changes worked on these tasks")
                lines.append(", ".join(diff["flipped"]))
            if diff["regressed"]:
                lines.append(f"\n### 🔴 Regressed pass->fail ({len(diff['regressed'])}) - Must be addressed first")
                lines.append(", ".join(diff["regressed"]))
            if diff.get("stable_fail"):
                lines.append(f"\n### 📌 Stable fail fail->fail ({len(diff['stable_fail'])}) - Needs new strategy")
                lines.append(", ".join(diff["stable_fail"]))
            if diff.get("stable_pass"):
                lines.append(f"\n### 🛡️ Stable pass pass->pass ({len(diff['stable_pass'])}) - Protect, avoid breaking")
                lines.append(", ".join(diff["stable_pass"]))
            if diff.get("exception_to_fail"):
                lines.append(f"\n### 🆕 New optimization targets exception->fail ({len(diff['exception_to_fail'])}) - Previously blocked by infra errors, now optimizable")
                lines.append(", ".join(diff["exception_to_fail"]))
            if diff.get("infra_recovered"):
                lines.append(f"\n### Infra recovered exception->pass ({len(diff['infra_recovered'])}, for reference only)")
            if diff.get("infra_lost"):
                lines.append(f"\n### Infra failure pass->exception ({len(diff['infra_lost'])}, for reference only)")
            if diff.get("fail_to_exception"):
                lines.append(f"\n### fail->exception ({len(diff['fail_to_exception'])}, for reference only)")

        lines.append(f"\n- Net change: {diff['net']:+d}")
        if prev_stats:
            prev_rate = prev_stats.get("pass_rate", 0)
            cur_rate = stats["pass_rate"]
            delta = cur_rate - prev_rate
            lines.append(f"- Pass rate change: {prev_rate:.1%} -> {cur_rate:.1%} ({delta:+.1%})")
        if diff["net"] < 0:
            lines.append(f"- ⚠️ **Net regression**: Previous iteration changes may be harmful")

    # -- 4. Agent Debugger Analysis --
    if adb_overview:
        lines.append(f"\n## 4. Agent Debugger Analysis (LLM-powered root cause analysis)")
        lines.append(adb_overview)
        lines.append(f"\nFor full per-task analysis: read `{analysis_dir}/detail/{{task_name}}.md` (or the detail file "
                     f"the overview names beside the task)")
        lines.append("For raw traces: see trace paths listed in each detail file.")

    # -- 5. Historical Trends --
    if scores_trend and len(scores_trend) >= 2:
        if k > 1:
            lines.append(f"\n## 5. Historical Trends")
            for i in range(1, k + 1):
                pak_parts = []
                for s in scores_trend:
                    s_pass_at = s.get("pass_at", {})
                    val = s_pass_at.get(i, s_pass_at.get(str(i)))
                    if val is not None:
                        pak_parts.append(f"iter{s['iteration']}: {val:.1%}")
                if pak_parts:
                    lines.append(f"- pass@{i}: {' -> '.join(pak_parts)}")
        else:
            lines.append(f"\n## 5. Historical Trends (Pass Rate)")
            trend_parts = []
            for s in scores_trend:
                rate = s.get("pass_rate", 0)
                trend_parts.append(f"iter{s['iteration']}: {rate:.1%}")
            lines.append(f"- {' -> '.join(trend_parts)}")
        if history_md:
            lines.append(f"- Full history of every iteration and what was changed: `{history_md}`")

    # -- 6. Best Ever --
    if best_ever:
        lines.append(f"\n## 6. Best Ever")
        lines.append(f"- Best pass rate: **{best_ever.get('pass_rate', best_ever.get('capability_rate', 0)):.1%}** (iteration {best_ever['iteration']})")
        if best_ever["iteration"] != step:
            if best_ever.get("commit"):
                lines.append(f"- Best version snapshot: commit `{best_ever['commit']}` of the adapter "
                             f"(`git -C {adapter} show {best_ever['commit']}:<path>` shows a file as it was)")
            else:
                lines.append(f"- Best version snapshot: the adapter commit recorded for step {best_ever['iteration']} "
                             f"in `{lm}/step_{int(best_ever['iteration']):04d}/summary.json`")

    # -- 7. Task Stability --
    if stability:
        lines.append(f"\n## 7. Task Stability Analysis (Across All Historical Iterations)")
        if stability["unstable"]:
            lines.append(f"- ⚠️ Unstable tasks ({len(stability['unstable'])}, do NOT optimize for these): {stability['unstable'][:20]}")
        if stability.get("possibly_unstable"):
            lines.append(f"- Possibly unstable ({len(stability['possibly_unstable'])}): {stability['possibly_unstable'][:15]}")
        lines.append(f"- Stable pass: {len(stability['stable_pass'])}")
        lines.append(f"- Stable fail: {len(stability['stable_fail'])} - These are the primary optimization targets")
        if stability["infra_only"]:
            lines.append(f"- Infra-only errors: {len(stability['infra_only'])} (ignore)")

    # -- 8. Change Attribution Report --
    if evaluation:
        evals = evaluation.get("change_evaluations", [])
        if evals:
            lines.append(f"\n## 8. Previous Iteration Change Attribution Report (Auto-Generated)")
            lines.append(f"You must use this report to decide whether to rollback previous changes. HARMFUL and INEFFECTIVE changes should be prioritized for rollback.")
            lines.append(f"\n| Change | Predicted Fixes | Actually Fixed | Regressions | Verdict | Suggested Action |")
            lines.append(f"|--------|----------------|----------------|-------------|---------|-----------------|")
            for e in evals:
                n_pred = len(e["predicted_fixes"])
                n_fixed = len(e["actually_fixed"])
                verdict = e["verdict"]
                suggestion = SUGGESTION.get(verdict, "?")
                lines.append(
                    f"| {e['change_id']}: {e['description'][:40]} "
                    f"| {n_fixed}/{n_pred} | {', '.join(e['actually_fixed'][:5]) or '-'} "
                    f"| {', '.join(e.get('risk_realized', [])[:5]) or '-'} "
                    f"| {verdict} | {suggestion} |"
                )

            for e in evals:
                if e["still_failed"]:
                    lines.append(f"- {e['change_id']} predicted but not fixed: {e['still_failed']}")

            unattr = evaluation.get("unattributed_regressions", [])
            if unattr:
                lines.append(f"\n⚠️ Unattributed regression tasks: {unattr}")
                lines.append(f"Please analyze the causes of these regressions, may be interaction effects of multiple changes")

            eval_iter = evaluation.get("evaluating_iteration", step - 1)
            before = f"`git -C {adapter} show {prev_commit}:<path>`" if prev_commit else \
                f"the adapter commit recorded in `{lm}/step_{int(eval_iter):04d}/summary.json` (`git -C {adapter} show <commit>:<path>`)"
            if rollback_mode == "agent":
                lines.append(f"\n**Rollback method**: Compare with {before} (the adapter before iteration {eval_iter}'s "
                             f"changes), restore files that need rollback to that version.")
            else:
                done = [r for r in (rollbacks or []) if r.get("ok", True)]
                failed = [r for r in (rollbacks or []) if not r.get("ok", True)]
                if done or failed:
                    parts = [f"{r['change_id']} (files {', '.join(r.get('files', [])) or '-'})" for r in done]
                    text = (f"\n**Rollback**: already rolled back by the framework: {'; '.join(parts)}." if parts
                            else "\n**Rollback**:")
                    if failed:
                        text += (" NOT rolled back (the result failed the adapter check and was restored): "
                                 + "; ".join(f"{r['change_id']} ({r.get('error', 'check failed')})" for r in failed)
                                 + f" — roll these back by hand, comparing with {before}.")
                    text += (f" These rollbacks are pre-filled as `type: rollback` entries of this iteration's change "
                             f"manifest; keep them. Files of other changes can be compared with {before}.")
                    lines.append(text)
                else:
                    lines.append(f"\n**Rollback**: no change met the framework's rollback rule, nothing was rolled "
                                 f"back. To roll a change back yourself, compare with {before} and restore the file.")

    # -- Execution Instructions --
    lines.append("\n## Execution Instructions")
    lines.append("Analyze failures → group into pattern classes → design general mechanisms → implement and commit.")
    if k > 1:
        lines.append(f"**Important**: This experiment uses {k} rollouts per task. The fundamental goal is to **maximize pass@1** — the single-attempt success rate.")
        lines.append(f"**Important**: 'Partial pass' tasks are your highest-leverage targets. Compare passing vs failing rollouts of the same task to find why one succeeded and the other failed, then make the successful strategy the reliable default. This is the fastest path to higher pass@1.")
    else:
        lines.append("**Important**: Use pass rate as the optimization target. Timed-out tasks should be analyzed — understand why the agent ran out of time.")
    lines.append(f"**Important**: Task classification and basic diagnostics are provided above. For deeper failed task analysis, read the corresponding `agent-codex.jsonl` (the raw Codex event stream) under `{lm}/current/trajectories/<id>/`, with `hook-calls.jsonl` beside it.")
    if stability and stability.get("unstable"):
        lines.append(f"**Important**: The following {len(stability['unstable'])} unstable tasks should NOT be optimization targets: {stability['unstable'][:10]}...")

    return "\n".join(lines)
