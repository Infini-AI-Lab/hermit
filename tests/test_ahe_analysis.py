"""AHE's deterministic analysis (hermit/agent/evolver/methods/ahe_adapter/analysis.py, queries.py) on hand-built learning
material: upstream's trial and task verdicts, Chen pass@k with exceptions in n, the nine diff buckets, the five
attribution verdicts, the manifest gate, strict-greater best-ever, the Debugger's overview and detail files byte for
byte, the Debugger's query text and the rendered evolution query (no model, no network, no Docker)."""
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import pytest

from hermit.agent.evolver.methods.ahe_adapter import analysis as A
from hermit.agent.evolver.methods.ahe_adapter import queries as Q


@pytest.fixture
def tmp_path():
    """A directory outside /tmp (the house rule for data directories)."""
    base = Path.home() / ".cache" / "hermit-tests"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)


def rec(task: str, n: int, *, score=None, status="ok", pstatus="ok", tokens=120_000, difficulty="medium",
        exception_type=None, verifier_raw=None, error=None) -> dict:
    """A task record as the Trainer writes it (material.py) for the TB2 pipeline of spec D."""
    return {"id": f"{task}#{n}", "input": {"id": f"{task}#{n}", "task": task, "rollout": n, "difficulty": difficulty},
            "status": status, "error": error,
            "output": {"task": task, "rollout": n, "status": pstatus} if status == "ok" else None,
            "judge": {"score": score, "feedback": ""}, "cost_usd": None, "wall_s": 10.0, "api_speed": None,
            "tokens": tokens if pstatus == "ok" else None,
            "pipeline_extra": {"status": pstatus, "timed_out": pstatus == "timeout", "exception_type": exception_type,
                               "verifier_reward_raw": verifier_raw},
            "trajectory": f"trajectories/{task}#{n}"}


def pair(task, a, b, **kw):
    return [rec(task, 1, **a, **kw), rec(task, 2, **b, **kw)]


PASS = {"score": 1.0}
FAIL = {"score": 0.0}
TIMEOUT = {"score": 0.0, "pstatus": "timeout", "exception_type": "AgentTimeoutError"}
INFRA = {"score": 0.0, "pstatus": "infra", "exception_type": "NetworkConnectionError"}


# ---------- compute_stats: trial and task verdicts ----------

def test_trial_and_task_verdicts_follow_upstream():
    records = (pair("all-pass", PASS, PASS) + pair("one-fail", PASS, FAIL) + pair("pass-timeout", PASS, TIMEOUT)
               + pair("two-timeouts", TIMEOUT, TIMEOUT) + pair("infra", INFRA, INFRA)
               + pair("fail-infra", FAIL, INFRA))
    stats = A.compute_stats(records)
    assert stats["k"] == 2 and stats["n_total"] == 6
    assert stats["task_results"] == {"all-pass": "pass", "one-fail": "fail", "pass-timeout": "fail",
                                     "two-timeouts": "exception", "infra": "exception", "fail-infra": "fail"}
    # 1 pass + 1 timeout = fail (not all pass, not all exception); 2 timeouts = exception; timeouts flagged either way
    assert stats["timeout_tasks"] == ["pass-timeout", "two-timeouts"]
    assert stats["per_task_rollouts"]["pass-timeout"] == {"n_pass": 1, "n_fail": 0, "n_exception": 1, "total": 2}
    assert stats["exception_types"] == {"AgentTimeoutError": 3, "NetworkConnectionError": 3}
    assert stats["task_exception_types"]["infra"] == "NetworkConnectionError"
    assert stats["counts"] == {"timeout": 3, "infra": 3, "agent_error": 0, "timeout_would_pass": 0}
    assert stats["trial_stats"] == {"n_pass": 4, "n_fail": 2, "n_exception": 6, "n_total": 12,
                                    "trial_pass_rate": 4 / 12}
    assert stats["n_pass"] == 1 and stats["n_fail"] == 3 and stats["n_exception"] == 2
    assert stats["tokens_per_trial_k"] == 120.0            # over the 7 rollouts the pipeline reported ok
    assert stats["difficulty"]["all-pass"] == "medium"
    assert [t["id"] for t in stats["per_task_trials"]["one-fail"]] == ["one-fail#1", "one-fail#2"]
    json.dumps(stats)                                      # what the Evolver writes to state/stats/step_N.json


def test_timeout_as_fail_turns_timed_out_trials_into_failures():
    records = pair("pass-timeout", PASS, TIMEOUT) + pair("two-timeouts", TIMEOUT, TIMEOUT, verifier_raw=1.0)
    as_exc = A.compute_stats(records, timeout_as="exception")
    as_fail = A.compute_stats(records, timeout_as="fail")
    assert as_exc["task_results"] == {"pass-timeout": "fail", "two-timeouts": "exception"}
    assert as_fail["task_results"] == {"pass-timeout": "fail", "two-timeouts": "fail"}
    assert as_fail["per_task_rollouts"]["two-timeouts"] == {"n_pass": 0, "n_fail": 2, "n_exception": 0, "total": 2}
    assert as_fail["timeout_tasks"] == as_exc["timeout_tasks"] == ["pass-timeout", "two-timeouts"]
    assert as_fail["counts"]["timeout"] == 3 and as_fail["counts"]["timeout_would_pass"] == 2
    assert as_fail["pass_rate"] == as_exc["pass_rate"] == 0.25      # pass@1 counts a timeout 0 either way
    with pytest.raises(ValueError):
        A.compute_stats(records, timeout_as="pass")


def test_trial_from_record_without_pipeline_status_and_with_a_pipeline_crash():
    plain = {"id": "t#1", "input": {"task": "t"}, "status": "ok", "judge": {"score": 1.0}, "tokens": 5}
    assert A.trial_from_record(plain)["result"] == "pass"
    crashed = {"id": "t#2", "input": {"task": "t"}, "status": "error", "error": "RuntimeError: harbor died",
               "judge": {"score": 0.0}, "tokens": None}
    trial = A.trial_from_record(crashed)
    assert trial["result"] == "exception" and trial["exception_type"] == "RuntimeError" and not trial["timeout"]
    # a record of a judge error never reached a verdict either
    judge_error = {"id": "t#3", "input": {"task": "t"}, "status": "judge_error", "judge": {"score": 0.0},
                   "pipeline_extra": {"status": "ok"}}
    assert A.trial_from_record(judge_error)["result"] == "exception"
    assert A.task_name_of({"id": "fix-git#2", "input": {}}) == "fix-git"
    assert A.task_name_of({"id": "x", "input": {"name": "n"}}, task_field="name") == "n"


def codex_rec(uid: str, n: int, *, score: float, timed_out: bool = False, tokens=90_000) -> dict:
    """A task record of the framework's Codex agent (hermit/agent/acting/workspace.py) on OfficeQA rows `<uid>#<n>`:
    no pipeline status, the session's `timed_out`, the judge's score and feedback."""
    return {"id": f"{uid}#{n}", "input": {"id": f"{uid}#{n}", "uid": uid, "rollout": n}, "status": "ok", "error": None,
            "output": {"answer": "42" if score else "", "fail_reason": ""}, "tokens": tokens,
            "judge": {"score": score, "feedback": "correct" if score else "predicted '41' but expected '42'",
                      "extra": {"f1": score, "answered": True}},
            "pipeline_extra": {"backend": "codex", "exit_code": -1 if timed_out else 0, "timed_out": timed_out,
                               "attempts": 1}, "trajectory": f"trajectories/{uid}_{n}"}


def test_the_codex_agents_records_count_a_timed_out_session_as_a_timeout():
    """The Codex agent reports no pipeline status: its `timed_out` is the timeout (an exception by default, a failure
    with timeout_as: fail), the judge's score of what the session answered is the verifier's word on it, and its
    tokens are not a completed rollout's."""
    records = [codex_rec("UID1", 1, score=1.0), codex_rec("UID1", 2, score=0.0),
               codex_rec("UID2", 1, score=1.0, timed_out=True), codex_rec("UID2", 2, score=1.0)]
    trial = A.trial_from_record(records[2])
    assert (trial["result"], trial["timeout"], trial["pipeline_status"], trial["exception_type"],
            trial["verifier_passed"]) == ("exception", True, "timeout", "timeout", True)
    assert A.trial_from_record(records[2], timeout_as="fail")["result"] == "fail"
    assert A.trial_from_record(records[0])["result"] == "pass" and A.trial_from_record(records[1])["result"] == "fail"
    stats = A.compute_stats(records, k=2, task_field="uid")
    assert stats["task_results"] == {"UID1": "fail", "UID2": "fail"} and stats["timeout_tasks"] == ["UID2"]
    assert stats["counts"] == {"timeout": 1, "infra": 0, "agent_error": 0, "timeout_would_pass": 1}
    assert stats["tokens_per_trial_k"] == 90.0                 # the three that completed
    assert A.compute_stats(records, k=2, timeout_as="fail", task_field="uid")["per_task_rollouts"]["UID2"] == {
        "n_pass": 1, "n_fail": 1, "n_exception": 0, "total": 2}


# ---------- pass@k ----------

def test_chen_pass_at_k_counts_exceptions_in_n():
    assert A.pass_at_k_est(2, 1, 1) == 0.5 and A.pass_at_k_est(2, 1, 2) == 1.0
    assert A.pass_at_k_est(2, 0, 1) == 0.0 and A.pass_at_k_est(3, 2, 2) == 1.0
    assert A.pass_at_k_est(1, 1, 2) != A.pass_at_k_est(1, 1, 2)     # nan: k > n
    records = pair("pass-infra", PASS, INFRA) + pair("both-fail", FAIL, FAIL) + pair("both-pass", PASS, PASS)
    stats = A.compute_stats(records, k=2)
    pa = stats["pass_at_k"]
    assert pa["per_task_pass_at"]["pass-infra"] == {1: 0.5, 2: 1.0}     # n = 2 (the exception counts), c = 1
    assert pa["pass_at"] == {1: (0.5 + 0.0 + 1.0) / 3, 2: (1.0 + 0.0 + 1.0) / 3}
    assert pa["eligible_counts"] == {1: 3, 2: 3}
    assert stats["pass_rate"] == pa["pass_at"][1]
    assert A.pass_at({"pass_at_k": {"pass_at": {"1": 0.5, "2": 0.75}}}) == {1: 0.5, 2: 0.75}


def test_k1_pass_rate_equals_the_fraction_of_passing_tasks():
    records = [rec("a", 1, **PASS), rec("b", 1, **FAIL), rec("c", 1, **INFRA), rec("d", 1, **PASS)]
    stats = A.compute_stats(records)
    assert stats["k"] == 1 and stats["pass_rate"] == 0.5
    assert stats["task_results"] == {"a": "pass", "b": "fail", "c": "exception", "d": "pass"}


# ---------- the diff, its nine buckets and the rollout moves ----------

def test_iteration_diff_has_all_nine_buckets_and_rollout_moves():
    prev = {"task_results": {"flip": "fail", "regress": "pass", "recover": "exception", "lose": "pass",
                             "sp": "pass", "sf": "fail", "e2f": "exception", "f2e": "fail", "ee": "exception",
                             "up": "fail", "down": "fail", "same": "fail"},
            "per_task_rollouts": {"up": {"n_pass": 0, "total": 2}, "down": {"n_pass": 1, "total": 2},
                                  "same": {"n_pass": 1, "total": 2}, "sf": {"n_pass": 0, "total": 2}}}
    cur = {"task_results": {"flip": "pass", "regress": "fail", "recover": "pass", "lose": "exception",
                            "sp": "pass", "sf": "fail", "e2f": "fail", "f2e": "exception", "ee": "exception",
                            "up": "fail", "down": "fail", "same": "fail", "new": "pass"},
           "per_task_rollouts": {"up": {"n_pass": 1, "total": 2}, "down": {"n_pass": 0, "total": 2},
                                 "same": {"n_pass": 1, "total": 2}, "sf": {"n_pass": 0, "total": 2},
                                 "new": {"n_pass": 2, "total": 2}}}
    diff = A.compute_iteration_diff(cur, prev)
    assert diff["flipped"] == ["flip"] and diff["regressed"] == ["regress"] and diff["net"] == 0
    assert diff["infra_recovered"] == ["new", "recover"]       # a task absent before counts as an exception then
    assert diff["infra_lost"] == ["lose"]
    assert diff["stable_pass"] == ["sp"]
    assert diff["stable_fail"] == ["down", "same", "sf", "up"]
    assert diff["exception_to_fail"] == ["e2f"] and diff["fail_to_exception"] == ["f2e"]
    assert diff["exception_stable"] == ["ee"]
    assert diff["rollout_improved"] == ["up"] and diff["rollout_regressed"] == ["down"]
    assert diff["rollout_unchanged"] == ["same", "sf"]
    assert diff["rollout_details"]["up"] == (0, 1, 2) and diff["rollout_details"]["new"] == (0, 2, 2)
    assert A.compute_iteration_diff(cur, None) is None


def test_diff_from_compute_stats_outputs():
    s1 = A.compute_stats(pair("t", PASS, FAIL) + pair("u", PASS, PASS))
    s2 = A.compute_stats(pair("t", PASS, PASS) + pair("u", PASS, FAIL))
    diff = A.compute_iteration_diff(s2, s1)
    assert diff["flipped"] == ["t"] and diff["regressed"] == ["u"]
    assert diff["rollout_details"] == {"t": (1, 2, 2), "u": (2, 1, 2)}


# ---------- history, stability ----------

def test_task_history_and_stability():
    history = {}
    A.update_task_history(history, 1, {"a": "pass", "b": "fail", "c": "exception"}, {"a": {"n_pass": 2}})
    A.update_task_history(history, 1, {"a": "fail"})                   # the same iteration is never recorded twice
    A.update_task_history(history, 2, {"a": "fail", "b": "fail", "c": "exception", "d": "pass"})
    A.update_task_history(history, 3, {"a": "pass", "b": "fail", "c": "exception", "d": "fail"})
    assert history["a"] == [[1, "pass", {"n_pass": 2}], [2, "fail"], [3, "pass"]]
    stab = A.compute_task_stability(history, min_iterations=3)
    assert stab == {"stable_pass": [], "stable_fail": ["b"], "unstable": ["a"], "possibly_unstable": ["d"],
                    "infra_only": ["c"]}


def test_history_before_writes_the_header_once_and_the_entry(tmp_path):
    path = tmp_path / "evolution_history.md"
    s1 = A.compute_stats(pair("t", PASS, FAIL) + pair("u", PASS, PASS) + pair("v", TIMEOUT, TIMEOUT))
    entry = A.history_before(path, 1, s1, None, source="step_0001 @ abc123", now="2026-10-30 12:00")
    assert path.read_text().startswith(A.HISTORY_HEADER + "## Iteration 1 — 2026-10-30 12:00\n")
    assert "- pass@1=50.0% | pass@2=66.7%" in entry
    assert "- 3 tasks breakdown: 1 tasks passed all 2 rollouts; 1 tasks passed 1 of 2 rollouts; 0 tasks failed all 2 rollouts; 1 tasks hit exception" in entry
    assert "- Exception stats: AgentTimeoutError: 2" in entry and "- Rollouts: `step_0001 @ abc123`" in entry
    assert "- 🔶 Partial pass (1): t (1/2)" in entry
    s2 = A.compute_stats(pair("t", PASS, PASS) + pair("u", PASS, FAIL) + pair("v", FAIL, FAIL))
    entry2 = A.history_before(path, 2, s2, A.compute_iteration_diff(s2, s1), now="2026-10-30 13:00")
    assert path.read_text().count(A.HISTORY_HEADER) == 1
    assert "### Cross-Iteration Changes (iteration 1 -> 2)" in entry2
    assert "- 🎉 fail->all-pass (1): t (1/2->2/2)" in entry2 and "- 🔴 all-pass->fail (1): u (2/2->1/2)" in entry2
    assert "- 🆕 exception->fail (1): v (0/2->0/2)" in entry2
    assert "- Retention rate: 0.0% (0/1 previously passed and still passing)" in entry2


# ---------- attribution ----------

def test_evaluate_changes_gives_the_five_verdicts_and_unattributed_regressions():
    manifest = {"iteration": 2, "changes": [
        {"id": "chg-1", "description": "fix A", "files": ["AGENTS.md"], "predicted_fixes": ["a1", "a2"], "risk_tasks": []},
        {"id": "chg-2", "predicted_fixes": ["b1", "b2"], "risk_tasks": ["r0"]},
        {"id": "chg-3", "predicted_fixes": ["c1"], "risk_tasks": ["r1"]},
        {"id": "chg-4", "predicted_fixes": ["d1"], "risk_tasks": []},
        {"id": "chg-5", "predicted_fixes": [], "risk_tasks": ["r2"]},
        {"id": "chg-6", "predicted_fixes": [], "risk_tasks": []},
    ]}
    diff = {"flipped": ["a1", "a2", "b1", "c1"], "regressed": ["r1", "r2", "stray"]}
    ev = A.evaluate_changes(manifest, diff)
    verdicts = {e["change_id"]: e["verdict"] for e in ev["change_evaluations"]}
    assert verdicts == {"chg-1": "EFFECTIVE", "chg-2": "PARTIALLY_EFFECTIVE", "chg-3": "MIXED",
                        "chg-4": "INEFFECTIVE", "chg-5": "HARMFUL", "chg-6": "INEFFECTIVE"}
    by_id = {e["change_id"]: e for e in ev["change_evaluations"]}
    assert by_id["chg-1"]["hit_rate"] == "2/2" and by_id["chg-6"]["hit_rate"] == "0/0"
    assert by_id["chg-2"]["still_failed"] == ["b2"] and by_id["chg-3"]["risk_realized"] == ["r1"]
    assert by_id["chg-1"]["files"] == ["AGENTS.md"] and by_id["chg-1"]["description"] == "fix A"
    assert ev["unattributed_regressions"] == ["stray"]
    assert ev["evaluating_iteration"] == 2
    assert ev["summary"] == ("chg-1: EFFECTIVE, chg-2: PARTIALLY_EFFECTIVE, chg-3: MIXED, chg-4: INEFFECTIVE, "
                             "chg-5: HARMFUL, chg-6: INEFFECTIVE")
    assert set(verdicts.values()) == set(A.VERDICTS) and set(Q.SUGGESTION) == set(A.VERDICTS)


def test_manifest_iteration_gate(tmp_path):
    path = tmp_path / "change_manifest.json"
    assert A.load_change_manifest(path, 3) is None                      # missing
    path.write_text(json.dumps({"iteration": 2, "changes": [{"id": "chg-1"}]}))
    assert A.load_change_manifest(path, 3) is None                      # stale: written in another iteration
    assert A.load_change_manifest(path, 2)["changes"][0]["id"] == "chg-1"
    path.write_text("{not json")
    assert A.load_change_manifest(path, 2) is None


# ---------- best ever ----------

def test_best_ever_updates_only_on_a_strictly_greater_pass_rate():
    s_half = A.compute_stats(pair("t", PASS, PASS) + pair("u", FAIL, FAIL))
    s_half_again = A.compute_stats(pair("t", PASS, FAIL) + pair("u", PASS, FAIL))
    s_better = A.compute_stats(pair("t", PASS, PASS) + pair("u", PASS, FAIL))
    best = A.update_best_ever(None, 1, s_half, commit="c1")
    assert best["iteration"] == best["step"] == 1 and best["commit"] == "c1" and best["pass_rate"] == 0.5
    assert best["tasks"] == {"total": 2, "passed_2_of_2": 1, "passed_1_of_2": 0, "passed_0_of_2": 1, "all_exception": 0}
    assert best["pass_at"] == {1: 0.5, 2: 0.5} and best["trials"]["pass"] == 2
    assert A.update_best_ever(best, 2, s_half_again, commit="c2") is best      # equal: not replaced
    newer = A.update_best_ever(best, 3, s_better, commit="c3")
    assert newer["step"] == 3 and newer["commit"] == "c3" and newer["pass_rate"] == 0.75
    assert A.update_best_ever({"capability_rate": 0.9, "iteration": 0}, 4, s_better)["capability_rate"] == 0.9


# ---------- the Debugger's overview and detail files ----------

FIXTURE_RESULTS = [
    {"task_name": "fix-git", "mode": "debug", "n_pass": 1, "n_fail": 1, "n_timeout": 0, "is_timeout": False,
     "response": "ROOT CAUSE: The agent rewrote history.\n\nPASS vs FAIL: trace01 kept the reflog.",
     "trace_paths": ["/lm/current/trajectories/fix-git#1/agent/trajectory.json",
                     "/lm/current/trajectories/fix-git#2/agent/trajectory.json"],
     "trace_rewards": [1.0, 0.0],
     "verifier_outputs": ["", "FAILED tests/test_a.py::test_a - x\n1 failed"]},
    {"task_name": "slow-task", "mode": "debug", "n_pass": 0, "n_fail": 0, "n_timeout": 2, "is_timeout": True,
     "response": Q.fallback_response("[adb timeout] exceeded 600s",
                                     ["FAILED tests/test_b.py::test_b - AssertionError\n1 failed", ""],
                                     [Q.render_timing_breakdown([(12.0, ["Bash"]), (2000.0, [])]), ""]),
     "trace_paths": ["/lm/current/trajectories/slow-task#1/agent/trajectory.json",
                     "/lm/current/trajectories/slow-task#2/agent/trajectory.json"],
     "trace_rewards": [-1.0, -1.0],
     "verifier_outputs": ["FAILED tests/test_b.py::test_b - AssertionError\n1 failed", ""]},
    {"task_name": "easy", "mode": "summary", "n_pass": 2, "n_fail": 0, "n_timeout": 0, "is_timeout": False,
     "response": "## Answer\n\n1. KEY STRATEGY: read the tests first.",
     "trace_paths": ["/lm/current/trajectories/easy#1/agent/trajectory.json",
                     "/lm/current/trajectories/easy#2/agent/trajectory.json"],
     "trace_rewards": [1.0, 1.0], "verifier_outputs": ["", ""]},
]

EXPECTED_OVERVIEW = """# Debugger Analysis Overview — Iteration 3

Analyzed 3 tasks (1 debug + 1 timeout + 1 summary).

### Timeout (agent exceeded time limit)

- **slow-task** (2/2 timed out): [adb timeout] verifier: FAILED tests/test_b.py::test_b - AssertionError

### Debug (has failures)

- **fix-git** (1/2 pass): The agent rewrote history.

### Summary (all pass): 1 tasks

easy

(All rollouts passed — no per-task analysis included. Read `detail/{task}.md` if needed.)

Detail per task: `state/analysis/step_0003/detail/`"""

EXPECTED_FIX_GIT_DETAIL = """# fix-git (1/2 pass)

Analyzed 2 traces (1 pass, 1 fail).

## Trace Paths

- **trace01** (PASS): `/lm/current/trajectories/fix-git#1/agent/trajectory.json`
- **trace02** (FAIL): `/lm/current/trajectories/fix-git#2/agent/trajectory.json`

## QA Analysis

ROOT CAUSE: The agent rewrote history.

PASS vs FAIL: trace01 kept the reflog.

## Verifier Test Output

### trace02 (FAIL)

```
FAILED tests/test_a.py::test_a - x
1 failed
```
"""

EXPECTED_SLOW_TASK_DETAIL = """# slow-task (0/2 pass) ⏱️ TIMEOUT

Analyzed 2 traces (0 pass, 0 fail, 2 timeout).

## Trace Paths

- **trace01** (TIMEOUT): `/lm/current/trajectories/slow-task#1/agent/trajectory.json`
- **trace02** (TIMEOUT): `/lm/current/trajectories/slow-task#2/agent/trajectory.json`

## QA Analysis

[adb timeout] exceeded 600s

The debugger analysis timed out. Verifier test failures:
  - FAILED tests/test_b.py::test_b - AssertionError
(Full verifier output is in the Verifier Test Output section below.)

⏱️ TIMING BREAKDOWN (total 2012s, 2 turns):
  Turn 1: 12.0s (Bash)
  Turn 2: 2000.0s (no tools)

**NOTE**: Debugger LLM analysis was not available for this task. The evolve agent should read the raw trace directly if deeper analysis is needed.

## Verifier Test Output

### trace01 (TIMEOUT)

```
FAILED tests/test_b.py::test_b - AssertionError
1 failed
```
"""


def test_overview_and_detail_files_are_byte_exact(tmp_path):
    analysis_dir = tmp_path / "state" / "analysis" / "step_0003"
    out_dir, snippet = A.write_debugger_analyse(FIXTURE_RESULTS, analysis_dir, 3,
                                                detail_label="state/analysis/step_0003/detail")
    assert out_dir == analysis_dir
    assert (analysis_dir / "overview.md").read_text(encoding="utf-8") == EXPECTED_OVERVIEW
    assert EXPECTED_OVERVIEW.endswith(snippet) and snippet.startswith("Analyzed 3 tasks")
    assert (analysis_dir / "detail" / "fix-git.md").read_text(encoding="utf-8") == EXPECTED_FIX_GIT_DETAIL
    assert (analysis_dir / "detail" / "slow-task.md").read_text(encoding="utf-8") == EXPECTED_SLOW_TASK_DETAIL
    easy = (analysis_dir / "detail" / "easy.md").read_text(encoding="utf-8")
    assert easy.startswith("# easy (2/2 pass)\n\nAnalyzed 2 traces (2 pass, 0 fail).\n") and "Verifier" not in easy
    # the default label is the detail directory itself
    _, snippet2 = A.write_debugger_analyse(FIXTURE_RESULTS[:1], tmp_path / "other", 3)
    assert snippet2.endswith(f"Detail per task: `{tmp_path / 'other' / 'detail'}/`")


def test_a_task_name_that_is_no_file_name_gets_a_file_of_its_own(tmp_path):
    """A plain name keeps `<task>.md`; any other gets a slug and a hash: it stays in detail/, two names that slug
    alike do not collide, and the overview names the file beside the task."""
    assert A.task_file_name("fix-git") == "fix-git" and A.task_file_name("UID0123") == "UID0123"
    names = [A.task_file_name(t) for t in ("suite/q-1", "suite_q-1", "../q", ".hidden", "x" * 80)]
    assert names[1] == "suite_q-1" and len(set(names)) == 5
    assert all(re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", n) for n in names)
    result = {**FIXTURE_RESULTS[0], "task_name": "suite/q-1"}
    _, snippet = A.write_debugger_analyse([result], tmp_path / "a", 2)
    assert [f.name for f in (tmp_path / "a" / "detail").iterdir()] == [names[0] + ".md"]
    assert f"**suite/q-1** (detail: `{names[0]}.md`)" in snippet


def test_extract_one_liner_rules():
    assert A.extract_one_liner("## 回答\n\nROOT CAUSE: it looped\nover and over.\n\nCRITICAL MISTAKE: x") == "it looped over and over."
    assert A.extract_one_liner("ROOT CAUSE: no newline then\nPASS vs FAIL: y") == "no newline then"
    assert A.extract_one_liner("# Heading\n\n1. First prose line here.\n2. second") == "First prose line here."
    assert A.extract_one_liner("[adb error] exit=1: boom\n\n**NOTE**: x") == "[adb error] exit=1: boom"
    assert A.extract_one_liner("[adb failed] x\n  - FAILED t.py::a - e\n") == "[adb failed] verifier: FAILED t.py::a - e"


# ---------- the Debugger's query text ----------

def test_debugger_query_is_assembled_as_upstream():
    timing = Q.render_timing_breakdown([(1.5, ["Bash", "apply_patch"])], 1.5)
    q = Q.debugger_query("debug", 2, 1, 0, 1, [1.0, -1.0], ["", "x\nFAILED t.py::a - e"], ["", timing])
    assert q.startswith(Q.TIMEOUT_INFO.format(n_timeout=1, n_total=2) + "This task has 2 rollouts: 1 passed, 0 failed.\n"
                        "Trace verdict: trace01=PASS, trace02=TIMEOUT\n")
    assert "\n\n⏱️ TIMING BREAKDOWN (total 2s, 1 turns):\n  Turn 1: 1.5s (Bash, apply_patch)" in q
    assert q.endswith(Q.VERIFIER_HEADER + "--- Verifier test output (rollout 2, TIMEOUT) ---\nx\nFAILED t.py::a - e\n"
                      "--- end verifier output ---")
    assert "4. GENERAL MECHANISM" in q
    k1 = Q.debugger_query("debug", 1, 0, 1, 0, [0.0], [""])
    assert k1.startswith("This task has a single rollout which FAILED.\nTrace verdict: trace01=FAIL\n") and "⚠️" not in k1
    summary = Q.debugger_query("summary", 2, 2, 0, 0, [1.0, 1.0], ["noise", "noise"])
    assert summary.startswith("This task passed all 2 rollouts.\n") and "VERIFIER" not in summary
    assert Q.debugger_query("summary", 1, 1, 0, 0, [1.0], [""]).startswith("This task has a single rollout which PASSED.")
    custom = Q.debugger_query("debug", 2, 1, 1, 0, [1.0, 0.0], ["", ""], templates={"qa_query_debug": "T {n_fail}"})
    assert custom == "T 1"
    tail = Q.build_verifier_context([0.0], ["\n".join(f"l{i}" for i in range(100))], tail_lines=60)
    assert "l39\nl40" not in tail and tail.count("\nl") == 60 and "l99\n--- end" in tail
    assert Q.render_timing_breakdown([]) == "" and Q.trace_labels([]) == ""


def test_extract_trace_timing_reads_upstream_traces(tmp_path):
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"latency": 4000, "messages": [
        {"role": "user", "content": "x"},
        {"role": "assistant", "latency": 1500, "tool_calls": [{"name": "Tool: run_shell_command"}]},
        {"role": "assistant", "latency": 2500}]}))
    assert Q.extract_trace_timing(trace) == ("⏱️ TIMING BREAKDOWN (total 4s, 2 turns):\n"
                                             "  Turn 1: 1.5s (run_shell_command)\n  Turn 2: 2.5s (no tools)")
    assert Q.extract_trace_timing(tmp_path / "missing.json") == ""


def test_fallback_response_without_failed_lines():
    text = Q.fallback_response("[adb error] exit=1: boom", ["all good"], None)
    assert text == "[adb error] exit=1: boom\n\n" + Q.FALLBACK_NOTE       # upstream's blank line before the NOTE
    assert A.extract_one_liner(text) == "[adb error] exit=1: boom"


# ---------- iteration scores ----------

def test_iteration_scores_yaml_and_md(tmp_path):
    s1 = A.compute_stats(pair("t", PASS, FAIL) + pair("u", PASS, PASS))
    s2 = A.compute_stats(pair("t", PASS, PASS) + pair("u", PASS, PASS) + pair("v", TIMEOUT, TIMEOUT))
    e1 = A.update_iteration_scores(tmp_path, 1, s1, experiment="ahe-tb2", model="gpt-5.6-luna", source="step_0001",
                                   timestamp="2026-10-30T10:00:00")
    e2 = A.update_iteration_scores(tmp_path, 2, s2, timing={"eval_min": 60, "analysis_min": 10, "evolve_min": 30,
                                                            "total_min": 100}, timestamp="2026-10-30T12:00:00")
    assert e1["pass_at"] == {1: 0.75, 2: 1.0} and e1["tokens_per_trial_k"] == 120.0 and "timing" not in e1
    assert e2["counts"]["timeout"] == 2 and e2["tasks"]["all_exception"] == 1
    trend = A.load_iteration_scores(tmp_path)
    assert [s["iteration"] for s in trend] == [1, 2] and trend[0]["source"] == "step_0001"
    md = (tmp_path / "iteration_scores.md").read_text()
    assert md.startswith("# ahe-tb2 Iteration Scores (k=2)\n")
    assert "| Iter | pass@1 | pass@2 | Tasks | 2/2 | 1/2 | 0/2 | Exc | Trials | Trial P | Trial F | Trial E | Tok/trial | Duration | Eval | Anlys | Evolve | Time |" in md
    assert "| 1 | 75.0% | 100.0% | 2 | 1 | 1 | 0 | 0 | 4 | 3 | 1 | 0 | 120.0k | - | - | - | - | 2026-10-30 10:00 |" in md
    assert "| 2 | 66.7% | 66.7% | 3 | 2 | 0 | 0 | 1 | 6 | 4 | 0 | 2 | 120.0k | 100m | 60m | 10m | 30m | 2026-10-30 12:00 |" in md
    assert md.endswith("Pass rate trend: 75.0% -> 66.7% (-8.3pp over 2 iterations)\n")
    A.regenerate_scores_md(tmp_path / "empty")
    assert not (tmp_path / "empty" / "iteration_scores.md").exists()


# ---------- the evolution query ----------

def test_evolution_query_has_all_sections_with_hermit_paths():
    prev = A.compute_stats(pair("flip", FAIL, FAIL) + pair("regress", PASS, PASS) + pair("partial", PASS, FAIL)
                           + pair("keep", PASS, PASS) + pair("slow", TIMEOUT, TIMEOUT) + pair("infra", INFRA, INFRA)
                           + pair("zero", FAIL, FAIL))
    cur = A.compute_stats(pair("flip", PASS, PASS) + pair("regress", FAIL, PASS) + pair("partial", FAIL, PASS)
                          + pair("keep", PASS, PASS) + pair("slow", TIMEOUT, PASS) + pair("infra", INFRA, INFRA)
                          + pair("zero", FAIL, FAIL))
    diff = A.compute_iteration_diff(cur, prev)
    manifest = {"iteration": 1, "changes": [
        {"id": "chg-1", "description": "contract-first rule in AGENTS.md", "files": ["AGENTS.md"],
         "predicted_fixes": ["flip", "zero"], "risk_tasks": []},
        {"id": "chg-2", "description": "publish guard", "files": ["middleware.py"], "predicted_fixes": [],
         "risk_tasks": ["regress"]}]}
    evaluation = A.evaluate_changes(manifest, diff)
    history = {}
    for i, s in ((1, prev), (2, cur)):
        A.update_task_history(history, i, s["task_results"], s["per_task_rollouts"])
    stability = A.compute_task_stability(history, min_iterations=3)
    best = A.update_best_ever(A.update_best_ever(None, 1, prev, commit="c1"), 2, cur, commit="c2")
    paths = {"learning_material": "/run/workspace/learning_material", "state": "/run/workspace/state",
             "adapter": "/run/workspace/adapter"}
    trend = [{"iteration": 1, "pass_at": {1: prev["pass_rate"], 2: 0.5}}, {"iteration": 2, "pass_at": {"1": cur["pass_rate"], "2": 0.6}}]
    overview = "Analyzed 5 tasks (3 debug + 1 timeout + 1 summary).\n\nDetail per task: `x/`"
    rollbacks = [{"change_id": "chg-2", "files": ["middleware.py"], "ok": True}]
    q = A.build_evolution_query(paths, cur, step=2, diff=diff, prev_stats=prev, stability=stability, best_ever=best,
                                scores_trend=trend, evaluation=evaluation, adb_overview=overview,
                                analysis_dir="/run/workspace/state/analysis/step_0002", rollbacks=rollbacks,
                                rollback_mode="auto", prev_commit="c0", commit="c1",
                                history_md="/run/workspace/state/evolution_history.md")
    for heading in ("## 1. Current Iteration Overview", "## 2. Task Classification Details",
                    "## 3. Detailed Cross-Iteration Analysis (iteration 1 -> 2)",
                    "## 4. Agent Debugger Analysis (LLM-powered root cause analysis)", "## 5. Historical Trends",
                    "## 6. Best Ever", "## 7. Task Stability Analysis (Across All Historical Iterations)",
                    "## 8. Previous Iteration Change Attribution Report (Auto-Generated)", "## Execution Instructions"):
        assert heading in q, heading
    assert q.startswith("Iteration 2 evaluation completed.\n\n## 1. Current Iteration Overview\n- Results path: `/run/workspace/learning_material/current/`")
    assert "- Evaluated adapter: commit `c1` of `/run/workspace/adapter/` (your edits of step 1)" in q
    assert "- **Pass@k**: pass@1=" in q and "- Per-trial stats: " in q
    assert "- ⏱️ 1 tasks TIMED OUT" in q and "- Warning: 1 tasks had infra errors" in q
    assert "- Tokens per trial: 120.0k" in q and "- Rollouts that never reached a verdict: 1 timeout, 2 infra, 0 agent error" in q
    assert "- Regressions: **1**" in q and "- Retention rate: **50.0%** (1/2 previously passed and still passing)" in q
    assert "### ✅ Passed (2) — all 2 rollouts passed\nflip, keep" in q
    assert "#### Partial pass (3) — high-priority targets" in q
    assert "`agent-codex.jsonl` (under `/run/workspace/learning_material/current/trajectories/<id>/`)" in q
    assert ("the trajectory of rollout `<id>` is `trajectories/<id>/agent-codex.jsonl` (the record's `trajectory` "
            "field names it; `<id>` is the name of the `tasks/<id>.json` file, the id written file-safe: `task#1` is "
            "`task_1`), the Codex event stream, with `prompt.txt`, `hook-calls.jsonl` and `hook-state/` beside it)") in q
    assert "- partial: 1/2 passed (pass@1=50%, pass@2=100%) — rollouts: partial#1=FAIL, partial#2=PASS" in q
    assert "- slow: 1/2 passed (pass@1=50%, pass@2=100%) — rollouts: slow#1=TIMEOUT, slow#2=PASS" in q
    assert "#### Zero pass (1) — failed all 2 rollouts\nzero" in q
    assert "### ⚠️ Infrastructure Exceptions (1, not agent issues, please ignore)\n- NetworkConnectionError (1): infra" in q
    assert "### 🎉 Improved fail->all-pass (1) - Your changes worked on these tasks\nflip (0/2->2/2)" in q
    assert "### 🔴 Regressed all-pass->fail (1) - Must be addressed first\nregress (2/2->1/2)" in q
    assert "### 🆕 New optimization targets exception->fail (1) - Previously blocked by infra errors, now optimizable\nslow (0/2->1/2)" in q
    assert "- Net change: +0" in q and "- Pass rate change: " in q
    assert "\nFor full per-task analysis: read `/run/workspace/state/analysis/step_0002/detail/{task_name}.md`" in q
    assert "- pass@2: iter1: 50.0% -> iter2: 60.0%" in q
    assert "- Full history of every iteration and what was changed: `/run/workspace/state/evolution_history.md`" in q
    assert f"- Best pass rate: **{best['pass_rate']:.1%}** (iteration {best['iteration']})" in q
    assert "- Possibly unstable (2): ['flip', 'regress']" in q and "- Stable fail: 3 - These are the primary optimization targets" in q
    assert "- Stable pass: 1\n" in q and "- Infra-only errors: 1 (ignore)" in q
    assert "| chg-1: contract-first rule in AGENTS.md | 1/2 | flip | - | PARTIALLY_EFFECTIVE | ⚠️ Keep, continue monitoring |" in q
    assert "| chg-2: publish guard | 0/0 | - | regress | HARMFUL | ❌ Must rollback |" in q
    assert "- chg-1 predicted but not fixed: ['zero']" in q
    assert "**Rollback**: already rolled back by the framework: chg-2 (files middleware.py)." in q
    assert "`git -C /run/workspace/adapter show c0:<path>`" in q
    assert "read the corresponding `agent-codex.jsonl` (the raw Codex event stream) under `/run/workspace/learning_material/current/trajectories/<id>/`" in q
    assert "agent/trajectory.json" not in q and "agent/codex.txt" not in q
    assert "nexau" not in q.lower() and "runs/iteration_" not in q
    # the best-ever snapshot line names the commit only when the best is another step
    assert "- Best version snapshot" not in q if best["iteration"] == 2 else "git -C /run/workspace/adapter show c1:<path>" in q
    # rollback: agent renders upstream's paragraph against the previous commit; a failed framework rollback is said
    q_agent = A.build_evolution_query(paths, cur, step=2, diff=diff, evaluation=evaluation, rollback_mode="agent",
                                      prev_commit="c0")
    assert "**Rollback method**: Compare with `git -C /run/workspace/adapter show c0:<path>` (the adapter before iteration 1's changes), restore files that need rollback to that version." in q_agent
    q_failed = A.build_evolution_query(paths, cur, step=2, diff=diff, evaluation=evaluation, rollback_mode="auto",
                                       rollbacks=[{"change_id": "chg-2", "files": ["middleware.py"], "ok": False,
                                                   "error": "check_spec failed"}])
    assert "NOT rolled back (the result failed the adapter check and was restored): chg-2 (check_spec failed)" in q_failed
    q_none = A.build_evolution_query(paths, cur, step=2, diff=diff, evaluation=evaluation, rollbacks=[])
    assert "**Rollback**: no change met the framework's rollback rule" in q_none


def test_evolution_query_first_step_and_k1():
    stats = A.compute_stats([rec("a", 1, **PASS), rec("b", 1, **FAIL), rec("c", 1, **TIMEOUT)])
    q = A.build_evolution_query({"learning_material": "lm", "state": "st", "adapter": "ad"}, stats, step=1)
    assert "## 1. Current Iteration Overview\n" in q and "## 3." not in q and "## 8." not in q
    assert "- Pass rate: **33.3%** (1/3)\n- Pass: 1 | Fail: 1 | Exception: 1" in q
    assert "### ⏱️ Timed Out (1) - Agent exceeded time limit, analyze traces" in q
    assert "**Important**: Use pass rate as the optimization target." in q
    assert q.endswith("read the corresponding `agent-codex.jsonl` (the raw Codex event stream) under `lm/current/trajectories/<id>/`, with `hook-calls.jsonl` beside it.")
