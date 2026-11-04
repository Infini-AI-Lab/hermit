"""AHE Adapter: the Agent Debugger stage (spec B.5), the trajectory helpers and the automatic rollback (B.6). Jobs
built from learning-material records in the framework's Codex agent's layout (agent-codex.jsonl, prompt.txt, task.md,
hook-calls.jsonl, the judge's feedback) with one trial Harbor recorded (its ATIF trajectory and verifier output), the
job directory's contents and question, each attempt as one call of the codex Base Agent on a scripted `codex` given
through `bin` (an answer, a retry, a timeout, a failure that falls back), per-job homes, per-attempt streams, usage the
Base Agent priced, the login's shared copy, the timing breakdown of a real Harbor trial (the WP0 spike's trajectory,
reduced), and rollback on a small git repository. No model, no network, no Docker."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
import time

import pytest

from hermit.agent.evolver.base import Access, Progress, StepContext, Workspace
from hermit.agent.evolver.methods.ahe_adapter import debugger as D
from hermit.agent.evolver.methods.ahe_adapter import queries as Q
from hermit.agent.evolver.methods.ahe_adapter import rollback as R
from hermit.agent.evolver.methods.ahe_adapter import trajectory as T

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tmp_path():
    """A directory outside /tmp (the host's /tmp is nearly full; data dirs never go there)."""
    base = Path.home() / ".cache" / "hermit-tests"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)


# ---------- the real spike trajectory (fix-git, Codex 0.164.0, ATIF-v1.7), messages and outputs shortened ----------

def _agent(step_id, stamp, message, call=None, out=None):
    step = {"step_id": step_id, "timestamp": stamp, "source": "agent", "message": message}
    if call:
        step["tool_calls"] = [{"tool_call_id": call, "function_name": "exec", "arguments": {"input": out or ""}}]
        step["observation"] = {"results": [{"source_call_id": call, "content": "Script completed\n" + (out or "")}]}
    return step


TRAJECTORY = {
    "schema_version": "ATIF-v1.7", "session_id": "01a18be6-7ab4-7772-8a6e-5bd20af663b1",
    "agent": {"name": "codex", "version": "0.164.0", "model_name": "gpt-5.6-luna"},
    "steps": [
        {"step_id": 1, "timestamp": "2026-10-30T08:21:41.946Z", "source": "system", "message": "You are Codex…"},
        {"step_id": 2, "timestamp": "2026-10-30T08:21:41.946Z", "source": "system", "message": "<skills_instructions>…"},
        {"step_id": 3, "timestamp": "2026-10-30T08:21:41.946Z", "source": "user", "message": "<environment_context>…"},
        {"step_id": 4, "timestamp": "2026-10-30T08:21:41.955Z", "source": "user",
         "message": "I just made some changes to my personal site and checked out master…"},
        _agent(5, "2026-10-30T08:22:15.954Z", "I’ll inspect the repository’s branches…", "call_7d21",
               "git status --short --branch && git reflog"),
        {"step_id": 6, "timestamp": "2026-10-30T08:22:16.838Z", "source": "system", "message": "<plugins_instructions>…"},
        _agent(7, "2026-10-30T08:22:20.494Z", "", "call_cb40", "git show --stat --oneline 650dba4"),
        _agent(8, "2026-10-30T08:22:22.958Z", "I found the missing work: commit `650dba4`…", "call_dbb2",
               "git merge 650dba427e0a9dcd118…"),
        _agent(9, "2026-10-30T08:22:25.739Z", "The merge reached one expected content conflict…", "call_1dfd",
               "UU _includes/about.md"),
        _agent(10, "2026-10-30T08:22:30.542Z", "", "call_48a0", "*** Begin Patch"),
        _agent(11, "2026-10-30T08:22:32.313Z", "", "call_6b50", "[master 42fd147] Merge"),
        _agent(12, "2026-10-30T08:22:34.488Z", "The merge is complete…", "call_0802", "## master"),
        _agent(13, "2026-10-30T08:22:37.894Z", "The integrity check found only one trailing space…", "call_214c",
               "*** Begin Patch"),
        _agent(14, "2026-10-30T08:22:41.475Z", "", "call_676c", "[master 8a51898] Merge"),
        _agent(15, "2026-10-30T08:22:43.428Z", "Found and merged the missing changes into `master`."),
    ],
    "final_metrics": {"total_prompt_tokens": 147179, "total_completion_tokens": 1905,
                      "total_cached_tokens": 139392, "total_steps": 15},
}

# The framework's Codex agent's stream (`codex exec --json`) of an OfficeQA question, reduced.
STREAM = [{"type": "thread.started", "thread_id": "01a19981"},
          {"type": "item.completed", "item": {"id": "item_0", "type": "error",
                                              "message": "`--dangerously-bypass-hook-trust` is enabled."}},
          {"type": "turn.started"},
          {"type": "item.completed", "item": {"id": "item_1", "type": "reasoning", "text": "Read the task first."}},
          {"type": "item.completed", "item": {"id": "item_2", "type": "command_execution",
                                              "command": "/bin/bash -lc 'sed -n 1,80p task.md'",
                                              "aggregated_output": "## Question\nWhat were the VA's expenditures?",
                                              "exit_code": 0, "status": "completed"}},
          {"type": "item.completed", "item": {"id": "item_3", "type": "agent_message", "text": "<answer>507</answer>"}},
          {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2}}]
CODEX_TXT = 'Reading additional input from stdin...\n' + "".join(json.dumps(e) + "\n" for e in STREAM[:2])
PROMPT = "Read `task.md`, inspect or search the full OfficeQA corpus under `docs/`, and answer the question.\n"
TASK_MD = "## Question\nWhat were the VA's expenditures in FY 1934?\n"
VERIFIER_FAIL = ("============================= test session starts ==============================\n"
                 + "\n".join(f"line {i} " + "x" * 70 for i in range(70)) + "\n"
                 "FAILED tests/test_outputs.py::test_merge - AssertionError: bio\n"
                 "========================= 1 failed, 2 passed in 0.31s ==========================\n")
WRONG = "predicted '41' but expected '42'"


# ---------- learning-material records and trial logs ----------

def codex_log(root: Path, name: str, *, hooks=True) -> Path:
    """A rollout's log as the framework's Codex agent keeps it (the task's collect adds task.md and the hook logs)."""
    log = root / name
    (log / "hook-state").mkdir(parents=True)
    (log / "agent-codex.jsonl").write_text("".join(json.dumps(e) + "\n" for e in STREAM), encoding="utf-8")
    (log / "prompt.txt").write_text(PROMPT)
    (log / "last_message.txt").write_text("<answer>507</answer>")
    (log / "deployment.json").write_text("{}")
    (log / "task.md").write_text(TASK_MD)
    if hooks:
        (log / "hook-calls.jsonl").write_text('{"event":"SessionStart","decision":null}\n')
        (log / "hook-state" / "s-1.json").write_text("{}")
    return log


def harbor_log(root: Path, name: str, *, trajectory=True, codex=False, verifier: str | None = None) -> Path:
    """A curated Harbor trial log: agent/{trajectory.json, codex.txt, hook-calls.jsonl}, verifier/test-stdout.txt."""
    log = root / name
    (log / "agent").mkdir(parents=True)
    if trajectory:
        (log / "agent" / "trajectory.json").write_text(json.dumps(TRAJECTORY), encoding="utf-8")
    if codex:
        (log / "agent" / "codex.txt").write_text(CODEX_TXT, encoding="utf-8")
    (log / "agent" / "hook-calls.jsonl").write_text('{"event":"session_start","decision":null}\n')
    if verifier is not None:
        (log / "verifier").mkdir()
        (log / "verifier" / "test-stdout.txt").write_text(verifier, encoding="utf-8")
    return log


def record(task: str, rollout: int, verdict: str, log: Path | None, step_dir: Path, score=None, feedback=None,
           harbor=False) -> dict:
    """A `tasks/<id>.json` record: the Codex agent's (`pipeline_extra.timed_out`, no status) or a Harbor pipeline's
    (`pipeline_extra.status`)."""
    if harbor:                                    # Harbor's status; a trial that never reached the verifier errs
        pipeline = {"pass": "ok", "fail": "ok"}.get(verdict, verdict)
        status, extra = ("ok" if pipeline == "ok" else "error"), {"status": pipeline}
    else:                                         # the Codex agent: a timed-out session is still judged
        status = "ok" if verdict in ("pass", "fail", "timeout") else "error"
        extra = {"backend": "codex", "exit_code": -1 if verdict == "timeout" else 0,
                 "timed_out": verdict == "timeout"} if status == "ok" else None
    judge_score = (1.0 if verdict == "pass" else 0.0) if score is None else score
    if feedback is None:
        feedback = "correct" if verdict == "pass" else WRONG
    return {"id": f"{task}#{rollout}", "input": {"id": f"{task}#{rollout}", "uid": task, "rollout": rollout},
            "status": status, "judge": {"score": judge_score, "feedback": feedback}, "tokens": 1000,
            "pipeline_extra": extra, "trajectory": os.path.relpath(log, step_dir) if log else None,
            "_dir": str(step_dir)}


def material(tmp: Path) -> tuple[Path, list[dict]]:
    """alpha (pass, fail) debug; beta (pass, pass) summary; gamma (infra, infra) skipped; delta (a Harbor timeout,
    infra) a timeout task; eps (fail, fail) debug with two failures; zeta (fail without any log) skipped; theta (a
    Codex session that timed out, pass) a timeout task without a timing breakdown."""
    step = tmp / "material" / "step_0002"
    logs = step / "trajectories"
    recs = [
        record("alpha", 1, "pass", codex_log(logs, "alpha_1"), step),
        record("alpha", 2, "fail", codex_log(logs, "alpha_2"), step, feedback=VERIFIER_FAIL),
        record("beta", 1, "pass", codex_log(logs, "beta_1"), step),
        record("beta", 2, "pass", codex_log(logs, "beta_2"), step),
        record("gamma", 1, "infra", None, step),
        record("gamma", 2, "infra", None, step),
        record("delta", 1, "timeout", harbor_log(logs, "delta_1", verifier=VERIFIER_FAIL), step, feedback="",
               harbor=True),
        record("delta", 2, "infra", None, step, harbor=True),
        record("eps", 2, "fail", codex_log(logs, "eps_2"), step),
        record("eps", 1, "fail", codex_log(logs, "eps_1"), step),
        record("zeta", 1, "fail", None, step),
        record("theta", 1, "timeout", codex_log(logs, "theta_1"), step),
        record("theta", 2, "pass", codex_log(logs, "theta_2", hooks=False), step),
    ]
    return step, recs


def test_classify():
    step = Path("/nowhere")
    assert D.classify(record("t", 1, "pass", None, step)) == "pass"
    assert D.classify(record("t", 1, "fail", None, step)) == "fail"
    assert D.classify(record("t", 1, "fail", None, step, score=0.5)) == "fail"
    assert D.classify(record("t", 1, "timeout", None, step)) == "timeout"                  # the Codex agent's timed_out
    assert D.classify(record("t", 1, "timeout", None, step, harbor=True)) == "timeout"     # Harbor's status
    assert D.classify(record("t", 1, "infra", None, step)) == "exception"
    assert D.classify(record("t", 1, "agent_error", None, step, harbor=True)) == "exception"
    assert D.classify({"status": "ok", "judge": {"score": 1.0}}) == "pass"      # no pipeline_extra at all
    assert D.classify({"status": "judge_error", "judge": {"score": 0.0}, "pipeline_extra": {"timed_out": True}}) \
        == "exception"                                                          # a judge that failed is no verdict


def test_build_jobs_rules(tmp_path):
    step, recs = material(tmp_path)
    jobs = D.build_jobs(recs, {"task_field": "uid"})
    assert [j.task for j in jobs] == ["eps", "alpha", "delta", "theta", "beta"]   # debug first, most failures first
    by = {j.task: j for j in jobs}
    assert (by["alpha"].mode, by["alpha"].n_pass, by["alpha"].n_fail) == ("debug", 1, 1)
    assert by["alpha"].trace_rewards == [1.0, 0.0]
    assert [t.id for t in by["alpha"].trials] == ["alpha#1", "alpha#2"]            # trials ordered by id
    assert (by["beta"].mode, by["beta"].n_pass) == ("summary", 2)
    assert by["delta"].is_timeout and by["delta"].n_timeout == 1 and len(by["delta"].trials) == 1
    assert by["delta"].trace_rewards == [-1.0]
    assert by["theta"].is_timeout and by["theta"].trace_rewards == [-1.0, 1.0] and by["theta"].mode == "debug"
    assert [t.id for t in by["eps"].trials] == ["eps#1", "eps#2"]
    assert "gamma" not in by and "zeta" not in by
    # the verdict: the judge's feedback (its 4000-char tail), else the Harbor trial's verifier output
    assert by["alpha"].verifier_outputs[1].endswith("0.31s ==========================")
    assert by["alpha"].verifier_outputs[1].startswith("... (truncated) ...\n")
    assert by["eps"].verifier_outputs == [WRONG, WRONG] and by["beta"].verifier_outputs == ["correct", "correct"]
    assert by["delta"].verifier_outputs[0].startswith("... (truncated) ...\n") and "FAILED tests/" in \
        by["delta"].verifier_outputs[0]
    assert D.build_jobs(recs, {"max_tasks": 2, "task_field": "uid"}) == jobs[:2]
    quiet = {j.task: j for j in D.build_jobs(recs, {"verifier_output": False, "task_field": "uid"})}
    assert quiet["alpha"].verifier_outputs == ["", ""]
    # the default task field (`task`) is absent from these rows: a row is grouped by its id without the #n
    assert sorted(j.task for j in D.build_jobs(recs, {}))[:2] == ["alpha", "beta"]


def test_load_records_resolves_trajectory(tmp_path):
    step, recs = material(tmp_path)
    (step / "tasks").mkdir()
    for r in recs:
        (step / "tasks" / f"{r['id'].replace('#', '_')}.json").write_text(json.dumps({k: v for k, v in r.items() if k != "_dir"}))
    loaded = D.load_records(step)
    assert len(loaded) == len(recs) and all(r["_dir"] == str(step) for r in loaded)
    assert [j.task for j in D.build_jobs(loaded, {"task_field": "uid"})] == ["eps", "alpha", "delta", "theta", "beta"]


def test_the_log_is_read_as_the_codex_agent_keeps_it_then_as_harbor_kept_it(tmp_path):
    main = codex_log(tmp_path, "main")
    assert D.trace_source(main) == (main / "agent-codex.jsonl", "events.jsonl")
    assert D.log_file(main, "hooks") == main / "hook-calls.jsonl"
    assert D.log_file(main, "atif") is None and D.log_file(main, "verifier") is None
    assert "hook_state" not in D.LOG_FILES             # the state is the Evolve Agent's to read, in the material
    harbor = harbor_log(tmp_path, "harbor", codex=True, verifier="ok")
    assert D.trace_source(harbor) == (harbor / "agent" / "trajectory.json", "json")
    assert D.log_file(harbor, "hooks") == harbor / "agent" / "hook-calls.jsonl"
    (harbor / "agent" / "trajectory.json").unlink()
    assert D.trace_source(harbor) == (harbor / "agent" / "codex.txt", "events.jsonl")
    (harbor / "agent-codex.jsonl").write_text("")                  # a record-root stream comes first
    assert D.trace_source(harbor) == (harbor / "agent-codex.jsonl", "events.jsonl")
    assert D.trace_source(tmp_path / "nothing") is None and D.trace_source(None) is None
    assert D.verifier_tail({"judge": {"feedback": WRONG}}, harbor, 4000) == WRONG
    assert D.verifier_tail({"judge": {"feedback": ""}}, harbor, 4000) == "ok"


def test_prepare_job_dir_contents(tmp_path):
    step, recs = material(tmp_path)
    by = {j.task: j for j in D.build_jobs(recs, {"task_field": "uid"})}
    scratch = tmp_path / "scratch"
    job_dir = D.prepare_job_dir(by["alpha"], scratch, D.settings({}), texts=Q)
    assert job_dir == scratch / "debugger" / "alpha"
    names = sorted(p.name for p in job_dir.iterdir())
    assert names == ["hooks01.jsonl", "hooks02.jsonl", "prompt01.txt", "prompt02.txt", "question.md", "task01.md",
                     "task02.md", "trace01", "trace01.events.jsonl", "trace02", "trace02.events.jsonl",
                     "verifier_02.txt"]
    assert (job_dir / "trace01.events.jsonl").read_text() == (step / "trajectories/alpha_1/agent-codex.jsonl").read_text()
    assert (job_dir / "prompt01.txt").read_text() == PROMPT and (job_dir / "task02.md").read_text() == TASK_MD
    exploded = sorted(p.name for p in (job_dir / "trace01").iterdir())
    assert exploded == ["0001-error.md", "0002-reasoning.md", "0003-command.md", "0004-message.md"]
    command = (job_dir / "trace01" / "0003-command.md").read_text()
    assert command.startswith("# Step 3 — command") and "sed -n 1,80p task.md" in command
    assert "exit code: 0" in command and "What were the VA's expenditures?" in command
    assert not (job_dir / "verifier_01.txt").exists()                               # the passing trial's verdict is not shown
    assert (job_dir / "verifier_02.txt").read_text().startswith("... (truncated) ...")
    question = (job_dir / "question.md").read_text()
    assert question.startswith("# Agent Debugger")
    flat = " ".join(question.split())
    assert "20 tool calls" in flat and "Do not fabricate" in flat and "trace02 #17" in flat
    assert "the judge's feedback" in flat and "last_message.md" not in flat
    for name in ("trace01.events.jsonl", "trace01/NNNN-<kind>.md", "prompt01.txt", "task01.md", "hooks01.jsonl",
                 "verifier_02.txt"):
        assert f"- `{name}`" in question, name
    assert "Trace verdict: trace01=PASS, trace02=FAIL" in question
    assert "⚠️ VERIFIER TEST OUTPUT" in question and "FAILED tests/test_outputs.py::test_merge" in question
    assert "line 0 " not in question and "line 69 " in question                       # the last 60 lines only
    assert "⚠️ TIMEOUT INFO" not in question
    assert question.rstrip().endswith("leading with `ROOT CAUSE:` then `PASS vs FAIL:`, `CRITICAL MISTAKE:`, "
                                      "`GENERAL MECHANISM:`, one section per line.")
    # explode off: no trace01/ directory
    job_dir = D.prepare_job_dir(by["alpha"], scratch, {"explode_traces": False}, texts=Q)
    assert not (job_dir / "trace01").exists() and (job_dir / "trace01.events.jsonl").exists()
    # a Harbor trial: its ATIF trajectory, exploded by step, its hooks, its verifier's output
    job_dir = D.prepare_job_dir(by["delta"], scratch, D.settings({}), texts=Q)
    assert sorted(p.name for p in job_dir.iterdir()) == ["hooks01.jsonl", "question.md", "trace01", "trace01.json",
                                                         "verifier_01.txt"]
    assert sorted(p.name for p in (job_dir / "trace01").iterdir())[:2] == ["0001-system.md", "0002-system.md"]


def test_question_of_a_timeout_task(tmp_path):
    step, recs = material(tmp_path)
    by = {j.task: j for j in D.build_jobs(recs, {"task_field": "uid"})}
    job_dir = D.prepare_job_dir(by["delta"], tmp_path / "scratch", D.settings({}), texts=Q)
    question = (job_dir / "question.md").read_text()
    assert "Trace verdict: trace01=TIMEOUT" in question
    assert "⚠️ TIMEOUT INFO: 1 of 1 rollout(s) TIMED OUT" in question
    assert "⏱️ TIMING BREAKDOWN (total 61s, 10 turns):" in question and "  Turn 1: 34.0s (exec)" in question
    assert question.rstrip().endswith("`GENERAL LESSON:`, one section per line.")  # k == 1: the K1 sections
    # a Codex session that timed out: the timeout is said, its stream has no timestamps for a breakdown
    question = (D.prepare_job_dir(by["theta"], tmp_path / "scratch", D.settings({}), texts=Q) / "question.md").read_text()
    assert "Trace verdict: trace01=TIMEOUT, trace02=PASS" in question and "⚠️ TIMEOUT INFO: 1 of 2" in question
    assert "TIMING BREAKDOWN" not in question
    summary = D.prepare_job_dir(by["beta"], tmp_path / "scratch", {}, texts=Q)
    assert "leading with `KEY STRATEGY:`" in (summary / "question.md").read_text()


# ---------- the attempts: calls of the codex Base Agent on a scripted codex ----------

FAKE_CODEX = r'''#!/usr/bin/env python3
"""A stand-in for `codex exec --json ... -o <last message> -`: reads the prompt on stdin, records what it was given in
the job dir, streams JSON events and writes answer.md plus the `-o` last message. FAKE_CODEX_MODE: ok | fail (exit 3)
| sleep (never finishes in time) | flaky (fails once, then ok) | flaky_spent (as flaky, its failed attempt after
one served turn) | empty (exit 0 without an answer) | last_only (no answer.md, the answer as the last message) |
silent (exit 0 before a single event). FAKE_CODEX_TIMES: a file every call appends its start and end to."""
import json, os, sys, time
args = sys.argv[1:]
job_dir = args[args.index("-C") + 1]
out = args[args.index("-o") + 1]
prompt = sys.stdin.read() if args[-1] == "-" else args[-1]
home = os.environ["CODEX_HOME"]
mode = os.environ.get("FAKE_CODEX_MODE", "ok")
start = time.time()
with open(os.path.join(job_dir, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": args, "CODEX_HOME": home, "cwd": os.getcwd(), "prompt": prompt,
                        "config": open(os.path.join(home, "config.toml")).read(),
                        "home": sorted(os.listdir(home)), "json": "--json" in args}) + "\n")
n = len(open(os.path.join(job_dir, "calls.jsonl")).read().splitlines())
if mode == "silent":
    sys.exit(0)
print(json.dumps({"type": "thread.started", "thread_id": "t-%d-%s" % (n, os.path.basename(job_dir))}), flush=True)
if mode == "sleep":
    time.sleep(30)
if os.environ.get("FAKE_CODEX_TIMES"):
    time.sleep(0.3)
if mode == "flaky_spent" and n < 2:                      # a turn the model served, then the stream broke
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7}}), flush=True)
if mode == "fail" or (mode in ("flaky", "flaky_spent") and n < 2):
    print(json.dumps({"type": "error", "message": "stream disconnected"}), flush=True)
    sys.exit(3)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7}}), flush=True)
answer = "ROOT CAUSE: the agent merged without running the tests.\nPASS vs FAIL: trace01 checked the reflog.\n"
if mode == "last_only":                                   # the agent wrote no file: its last message is the answer
    with open(out, "w") as f:
        f.write(answer)
elif mode != "empty":                                     # the agent's file, then Codex's -o with the closing remark
    with open(os.path.join(job_dir, "answer.md"), "w") as f:
        f.write(answer)
    with open(out, "w") as f:
        f.write("Done; the analysis is in answer.md.\n")
if os.environ.get("FAKE_CODEX_TIMES"):
    with open(os.environ["FAKE_CODEX_TIMES"], "a") as f:
        f.write(json.dumps({"job": os.path.basename(job_dir), "start": start, "end": time.time()}) + "\n")
'''


def fake_codex(tmp: Path) -> Path:
    path = tmp / "bin" / "codex"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_CODEX)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def context(tmp: Path, params: dict | None = None, cancel=None) -> StepContext:
    ws = Workspace(adapter=tmp / "ws" / "adapter", learning_material=tmp / "material", task_info=tmp / "ws" / "task_info",
                   history=tmp / "ws" / "history", state=tmp / "ws" / "state", scratch=tmp / "ws" / "scratch",
                   output=tmp / "ws" / "output")
    for p in ws.__dict__.values():
        p.mkdir(parents=True, exist_ok=True)
    return StepContext(workspace=ws, access=Access(), tools={}, params=params or {}, sandbox=None, cancel=cancel,
                       progress=Progress(epoch=1, step=2, is_epoch_end=False))


EVOLVER_PARAMS = {"model": "gpt-5.6-luna", "effort": "medium", "web_search": False, "home": [],
                  "price": {"input": 1.0, "cached_input": 0.5, "output": 2.0}}


def evolver_and_base(ctx: StepContext) -> tuple:
    """The AHE Adapter Evolver as the Trainer makes it, and the codex Base Agent settings its Evolve Agent runs
    with: what the Debugger starts from."""
    from hermit.agent.evolver.loader import find, instantiate
    evolver, params = instantiate(find("ahe_adapter"), dict(ctx.params))
    ctx = StepContext(**{**ctx.__dict__, "params": params})
    return evolver, ctx, evolver.settings(ctx, "codex")


def run(tmp: Path, jobs, *, cancel=None, **debugger):
    ctx = context(tmp, {**EVOLVER_PARAMS, "debugger": {"bin": str(fake_codex(tmp)), "concurrency": 2, "timeout_s": 20,
                                                       "retries": 2, "backoff_s": 0.0, **debugger}}, cancel=cancel)
    _, ctx, base = evolver_and_base(ctx)
    results, usage = D.run_jobs(jobs, ctx, D.settings(ctx.params), base=base, scratch=ctx.workspace.scratch, texts=Q)
    return ctx, results, usage


def calls(job_dir) -> list[dict]:
    return [json.loads(line) for line in (Path(job_dir) / "calls.jsonl").read_text().splitlines()]


def test_settings_fall_back_to_the_evolver_model():
    s = D.settings({"model": "m", "effort": "high", "bin": "/x/codex", "args": ["-c", "x=1"],
                    "debugger": {"model": None, "args": None, "retries": 5}})
    assert (s["model"], s["effort"], s["bin"], s["args"], s["retries"], s["concurrency"]) == \
        ("m", "high", "/x/codex", ["-c", "x=1"], 5, 16)
    other = D.settings({"model": "m", "effort": "high", "debugger": {"model": "other", "effort": "low", "args": []}})
    assert (other["model"], other["effort"], other["args"]) == ("other", "low", [])


def test_a_bound_models_provider_arguments_survive_the_debuggers_own_args(tmp_path):
    """A bound model reaches its provider through Codex arguments only: `debugger.args` given in place of the
    Evolve Agent's keep them in front, and the Evolve Agent's own args (which begin with them) are not doubled."""
    from hermit.agent.model.backends import Binding
    binding = Binding.from_spec({"base_url": "https://llm.example/v1", "model": "m", "api_key_env": "FAKE_KEY"})
    provider = binding.codex_args()
    assert provider
    ctx = context(tmp_path)
    base = {"web_search": False, "model": "m", "binding": binding.spec(), "args": [*provider, "-c", "e=1"]}
    for debugger_args, expected in ((None, [*provider, "-c", "e=1"]), (["-c", "d=1"], [*provider, "-c", "d=1"]),
                                    ([], provider)):
        params = D.settings({"model": "m", "args": base["args"], "debugger": {"args": debugger_args}})
        runner = D.Runner(ctx, params, base, ctx.workspace.scratch)
        assert runner.agent_settings(D.Job(task="t"), tmp_path, 1, 30.0)["args"] == expected, debugger_args
    unbound = D.Runner(ctx, D.settings({"model": "m", "debugger": {"args": ["-c", "d=1"]}}),
                       {"web_search": False, "model": "m"}, ctx.workspace.scratch)
    assert unbound.agent_settings(D.Job(task="t"), tmp_path, 1, 30.0)["args"] == ["-c", "d=1"]


def test_run_jobs_answers_with_one_base_agent_call_per_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_TIMES", str(tmp_path / "times.jsonl"))
    step, recs = material(tmp_path)
    jobs = D.build_jobs(recs, {"task_field": "uid"})
    ctx, results, usage = run(tmp_path, jobs)
    assert [r["task_name"] for r in results] == ["eps", "alpha", "delta", "theta", "beta"]
    assert all(r["status"] == "ok" and r["attempts"] == 1 for r in results), results
    by = {r["task_name"]: r for r in results}
    assert by["alpha"]["response"].startswith("ROOT CAUSE: the agent merged without running the tests.")
    assert by["alpha"]["trace_rewards"] == [1.0, 0.0] and by["alpha"]["trial_ids"] == ["alpha#1", "alpha#2"]
    assert by["alpha"]["trace_paths"][0].endswith("trajectories/alpha_1/agent-codex.jsonl")
    assert by["alpha"]["mode"] == "debug" and by["beta"]["mode"] == "summary"
    # usage: the Base Agent's, read from each stream and priced: (100 - 40) x 1 + 40 x 0.5 + 7 x 2 per million each
    assert (usage.model, usage.calls, usage.input_tokens, usage.cached_input_tokens, usage.output_tokens) \
        == ("gpt-5.6-luna", 5, 500, 200, 35)
    assert usage.cost_usd == pytest.approx(5 * (60 + 20 + 14) / 1e6)
    # the codex Base Agent's command: the question on stdin, its own -o, Codex's sandbox off, the model and effort
    homes = ctx.workspace.scratch / "debugger-homes"
    seen = {}
    for name in ("eps", "alpha", "delta", "theta", "beta"):
        job_dir = ctx.workspace.scratch / "debugger" / name
        (call,) = calls(job_dir)
        seen[name] = call
        argv = call["argv"]
        assert argv[0] == "exec" and call["json"] and argv[-1] == "-" and call["prompt"] == (job_dir / "question.md").read_text()
        assert argv[argv.index("-C") + 1] == str(job_dir) and argv[argv.index("-s") + 1] == "danger-full-access"
        assert argv[argv.index("-m") + 1] == "gpt-5.6-luna" and 'model_reasoning_effort="medium"' in argv
        assert argv[argv.index("-o") + 1] == str(homes / name / "last_message.txt")
        assert call["CODEX_HOME"] == str(homes / name) and call["cwd"] == str(job_dir)     # a home of its own per job
        assert 'web_search = "disabled"' in call["config"] and "auth.json" not in call["home"]
        assert (job_dir / "answer.md").is_file() and (homes / name / "last_message.txt").read_text().startswith("Done;")
        # each attempt's stream in the step's output
        lines = [json.loads(l) for l in (ctx.workspace.output / "debugger" / f"{name}.1.jsonl").read_text().splitlines()]
        assert lines[0]["type"] == "thread.started" and lines[-1]["type"] == "turn.completed"
    assert len({c["CODEX_HOME"] for c in seen.values()}) == 5 and not (ctx.workspace.state.parent / "agent_home" /
                                                                         "last_message.txt").exists()
    # the first job ran alone (the warm-up), the rest two at a time
    times = {t["job"]: t for t in map(json.loads, (tmp_path / "times.jsonl").read_text().splitlines())}
    assert all(times["eps"]["end"] <= t["start"] for name, t in times.items() if name != "eps")


def test_agent_settings_keep_the_evolve_agents_isolation_and_login_and_give_each_job_its_own_home(tmp_path):
    sandbox = object()
    base = {"web_search": False, "isolation": "sandbox", "sandbox": sandbox, "codex_sandbox": "danger-full-access",
            "login": "chatgpt", "model": "gpt-5.6-luna", "effort": "medium", "env": {"PYTHONPATH": "/f"},
            "home": tmp_path / "agent_home", "log": tmp_path / "agent-codex.jsonl", "tools_name": "evolver_tools",
            "timeout_s": None, "stdin": True}
    ctx = context(tmp_path, cancel=threading.Event())
    runner = D.Runner(ctx, D.settings({"model": "gpt-5.6-luna", "effort": "medium",
                                       "debugger": {"effort": "low", "env": {"FAKE": "1"}}}), base,
                      ctx.workspace.scratch)
    name = D.task_file_name("a/b")                              # a slug and a hash: no `/` in a file name
    job_dir = ctx.workspace.scratch / "debugger" / name
    s = runner.agent_settings(D.Job(task="a/b"), job_dir, 2, 30.0)
    assert (s["isolation"], s["sandbox"], s["login"], s["codex_sandbox"], s["web_search"]) == \
        ("sandbox", sandbox, "chatgpt", "danger-full-access", False)
    assert (s["model"], s["effort"], s["timeout_s"], s["cancel"], s["stdin"]) == ("gpt-5.6-luna", "low", 30.0,
                                                                                    ctx.cancel, True)
    assert s["home"] == ctx.workspace.scratch / "debugger-homes" / name and s["writable"] == [job_dir]
    assert s["log"] == ctx.workspace.output / "debugger" / f"{name}.2.jsonl" and "tools_name" not in s
    assert s["env"] == {"PYTHONPATH": "/f", "FAKE": "1"}


RESULTS = [  # what one call of the codex Base Agent returned -> the attempt's reason ("" with an answer)
    ({"exit_code": -1, "extra": {"cancelled": True, "started": False}}, "[adb cancelled] stopped by the Trainer"),
    ({"exit_code": -1, "timed_out": True, "extra": {"started": True}}, "[adb timeout] exceeded 30s"),
    ({"exit_code": -1, "extra": {"started": False, "unavailable": True,
                                 "output_tail": "hermit: the plan held the session back for 60 s"}},
     "[adb unavailable] hermit: the plan held the session back for 60 s"),
    ({"exit_code": 0, "extra": {"started": False, "output_tail": ""}}, "[adb unavailable] the session never started"),
    ({"exit_code": 2, "extra": {"started": False, "output_tail": "error: unexpected argument '--x'\n"}},
     "[adb error] exit=2: error: unexpected argument '--x'"),
    ({"exit_code": 1, "extra": {"started": True, "output_tail": '{"type": "error", "message": "429"}\n'}},
     '[adb error] exit=1: {"type": "error", "message": "429"}'),
    ({"exit_code": 0, "reply": "  ", "extra": {"started": True}}, "[adb failed] empty answer.md"),
    ({"exit_code": 0, "reply": "ROOT CAUSE: the reply", "extra": {"started": True}}, ""),
]


@pytest.mark.parametrize("fields, reason", RESULTS)
def test_an_attempt_reads_the_base_agents_result(tmp_path, monkeypatch, fields, reason):
    from hermit.agent.base.interface import Result
    from hermit.agent.model.usage import Usage
    made = []

    class Agent:
        def __init__(self, settings):
            made.append(settings)

        def run(self, prompt, *, workspace):
            return Result(reply=fields.get("reply", ""), exit_code=fields["exit_code"],
                          timed_out=fields.get("timed_out", False), extra=fields["extra"],
                          usage=[Usage("m", input_tokens=5, calls=1)])

    monkeypatch.setattr(D, "make", lambda name, settings: Agent(settings))
    ctx = context(tmp_path)
    runner = D.Runner(ctx, D.settings({}), {"web_search": False}, ctx.workspace.scratch)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "question.md").write_text("q")
    answer, why = runner.attempt(D.Job(task="t"), job_dir, 1, 30.0)
    assert why == reason and (answer == "ROOT CAUSE: the reply") is (reason == "")
    assert made[0]["stdin"] and runner.usage[0].input_tokens == 5         # every attempt's usage counts


@pytest.mark.parametrize("order", ["deadline", "trainer", "deadline then trainer"])
def test_an_attempts_clock_covers_the_wait_before_the_cli_runs(tmp_path, monkeypatch, order):
    """The Base Agent's own clock starts once the CLI runs; a session held back by the plan waits on its cancel only.
    The attempt's deadline sets that cancel, and what set it decides the reason: a timeout at the deadline (even when
    the Trainer stops while the call tears down), a cancel when the Trainer stopped it first."""
    from hermit.agent.base.interface import Result
    trainer = threading.Event()

    class Agent:
        def __init__(self, settings):
            self.cancel = settings["cancel"]

        def run(self, prompt, *, workspace):
            if order == "trainer":
                trainer.set()
            self.cancel.wait(30)                       # the plan's wait: it ends when the cancel is set
            if order == "deadline then trainer":
                trainer.set()                          # the Trainer stops while the timed-out call tears down
            return Result(reply="", exit_code=-1, extra={"cancelled": self.cancel.is_set(), "started": False})

    monkeypatch.setattr(D, "make", lambda name, settings: Agent(settings))
    ctx = context(tmp_path, cancel=trainer)
    runner = D.Runner(ctx, D.settings({}), {"web_search": False}, ctx.workspace.scratch)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "question.md").write_text("q")
    started = time.monotonic()
    answer, why = runner.attempt(D.Job(task="t"), job_dir, 1, 30.0 if order == "trainer" else 1.5)
    assert answer is None and time.monotonic() - started < 10
    assert why == ("[adb cancelled] stopped by the Trainer" if order == "trainer" else "[adb timeout] exceeded 1.5s")


def test_the_login_is_the_shared_copy_in_each_jobs_home_for_the_call(tmp_path, monkeypatch):
    """login: chatgpt signs each call in from the shared copy of the user's login (hermit.agent.base.login, in the
    test's own directory): the job's home holds it during the call, not after."""
    user = tmp_path / "user-codex"
    user.mkdir()
    (user / "auth.json").write_text(json.dumps({"last_refresh": "2026-10-01T00:00:00Z", "tokens": {"id": "x"}}))
    monkeypatch.setenv("CODEX_HOME", str(user))                        # the user's login: a file of this test
    step, recs = material(tmp_path)
    jobs = [j for j in D.build_jobs(recs, {"task_field": "uid"}) if j.task == "alpha"]
    ctx = context(tmp_path, {**EVOLVER_PARAMS, "login": "chatgpt",
                             "debugger": {"bin": str(fake_codex(tmp_path)), "retries": 1}})
    _, ctx, base = evolver_and_base(ctx)
    results, _ = D.run_jobs(jobs, ctx, D.settings(ctx.params), base=base, scratch=ctx.workspace.scratch, texts=Q)
    assert results[0]["status"] == "ok"
    (call,) = calls(ctx.workspace.scratch / "debugger" / "alpha")
    assert "auth.json" in call["home"] and 'forced_login_method = "chatgpt"' in call["config"]
    assert not (ctx.workspace.scratch / "debugger-homes" / "alpha" / "auth.json").exists()
    from hermit.agent.base import login
    assert login.master().is_file()                                    # the shared copy, under the test's root


def test_run_jobs_takes_the_last_message_without_an_answer_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_MODE", "last_only")
    step, recs = material(tmp_path)
    jobs = D.build_jobs(recs, {"task_field": "uid"})
    _, results, _ = run(tmp_path, jobs[:1], retries=1)
    assert results[0]["status"] == "ok" and results[0]["response"].startswith("ROOT CAUSE: the agent merged")
    assert not (Path(results[0]["job_dir"]) / "answer.md").exists()


def test_run_jobs_retries_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_MODE", "flaky")
    step, recs = material(tmp_path)
    jobs = [j for j in D.build_jobs(recs, {"task_field": "uid"}) if j.task == "alpha"]
    ctx, results, usage = run(tmp_path, jobs, retries=3)
    assert results[0]["status"] == "ok" and results[0]["attempts"] == 2
    assert len(calls(ctx.workspace.scratch / "debugger" / "alpha")) == 2
    assert usage.calls == 1 and usage.input_tokens == 100                            # only the successful turn reported usage
    streams = sorted(p.name for p in (ctx.workspace.output / "debugger").iterdir())
    assert streams == ["alpha.1.jsonl", "alpha.2.jsonl"]                             # one stream per attempt
    assert "stream disconnected" in (ctx.workspace.output / "debugger" / "alpha.1.jsonl").read_text()


def test_run_jobs_counts_the_model_calls_of_every_attempt(tmp_path, monkeypatch):
    """`calls` is the model calls the attempts' streams reported, a failed attempt's too, as are its tokens."""
    monkeypatch.setenv("FAKE_CODEX_MODE", "flaky_spent")
    step, recs = material(tmp_path)
    jobs = [j for j in D.build_jobs(recs, {"task_field": "uid"}) if j.task == "alpha"]
    _, results, usage = run(tmp_path, jobs, retries=3)
    assert results[0]["status"] == "ok" and results[0]["attempts"] == 2
    assert usage.calls == 2 and usage.input_tokens == 200


def test_a_job_that_cannot_be_prepared_falls_back_the_warm_up_too(tmp_path, monkeypatch):
    """A job whose directory cannot be prepared falls back with the reason, the first (warm-up) job as the rest: one
    job's crash never fails the step."""
    step, recs = material(tmp_path)
    jobs = D.build_jobs(recs, {"task_field": "uid"})

    def unreadable(job, *args, **kwargs):
        raise OSError(f"cannot read {job.task}'s trajectory")
    monkeypatch.setattr(D, "prepare_job_dir", unreadable)
    _, results, usage = run(tmp_path, jobs)
    assert len(results) == len(jobs) and all(r["status"] == "fallback" and r["attempts"] == 0 for r in results)
    assert all(r["error"].startswith("OSError: cannot read") for r in results) and usage.calls == 0


def test_run_jobs_falls_back_after_failures(tmp_path, monkeypatch):
    step, recs = material(tmp_path)
    jobs = [j for j in D.build_jobs(recs, {"task_field": "uid"}) if j.task == "alpha"]
    monkeypatch.setenv("FAKE_CODEX_MODE", "fail")
    _, results, usage = run(tmp_path, jobs, retries=2)
    r = results[0]
    assert r["status"] == "fallback" and r["attempts"] == 2
    assert r["response"].startswith('[adb error] exit=3: {"type": "error", "message": "stream disconnected"}')
    assert "FAILED tests/test_outputs.py::test_merge - AssertionError: bio" in r["response"]
    assert "**NOTE**: Debugger LLM analysis was not available for this task." in r["response"]
    assert usage.calls == 0 and usage.input_tokens == 0 and usage.cost_usd is None    # no attempt reached the model
    # an exit 0 without an answer is a failure too
    monkeypatch.setenv("FAKE_CODEX_MODE", "empty")
    _, results, _ = run(tmp_path, jobs, retries=1)
    assert results[0]["response"].startswith("[adb failed] empty answer.md")
    # a session that never started (no event at all): the provider could not serve it
    monkeypatch.setenv("FAKE_CODEX_MODE", "silent")
    _, results, _ = run(tmp_path, jobs, retries=1)
    assert results[0]["response"].startswith("[adb unavailable] the session never started")
    # a missing program
    monkeypatch.delenv("FAKE_CODEX_MODE")
    _, results, _ = run(tmp_path, jobs, retries=1, bin=str(tmp_path / "no-such-codex"))
    assert results[0]["response"].startswith("[adb exception] FileNotFoundError")


def test_run_jobs_times_out_and_stops_the_session(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_MODE", "sleep")
    step, recs = material(tmp_path)
    jobs = [j for j in D.build_jobs(recs, {"task_field": "uid"}) if j.task == "delta"]
    ctx, results, _ = run(tmp_path, jobs, timeout_s=1.0, retries=2)
    r = results[0]
    assert r["status"] == "fallback" and r["attempts"] == 2 and r["elapsed_s"] < 15
    assert r["response"].startswith("[adb timeout] exceeded 1s")
    assert "⏱️ TIMING BREAKDOWN (total 61s, 10 turns):" in r["response"]         # the timed-out trace's timing
    assert len(calls(ctx.workspace.scratch / "debugger" / "delta")) == 2


def test_run_jobs_honours_cancel(tmp_path):
    step, recs = material(tmp_path)
    jobs = D.build_jobs(recs, {"task_field": "uid"})
    cancel = threading.Event()
    cancel.set()
    ctx, results, usage = run(tmp_path, jobs, cancel=cancel)
    assert len(results) == 5 and all(r["attempts"] == 0 for r in results)
    assert all(r["response"].startswith("[adb cancelled] stopped by the Trainer") for r in results)
    assert not list((ctx.workspace.scratch / "debugger").glob("*/calls.jsonl"))
    assert usage.input_tokens == 0 and usage.cost_usd is None


# ---------- the trajectory helpers ----------

def test_timing_breakdown_of_the_real_trajectory(tmp_path):
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps(TRAJECTORY))
    turns = T.turns(TRAJECTORY)
    assert len(turns) == 10 and turns[0][1] == ["exec"] and turns[-1][1] == []
    assert [round(s, 1) for s, _ in turns] == [34.0, 3.7, 2.5, 2.8, 4.8, 1.8, 2.2, 3.4, 3.6, 2.0]
    assert round(T.total_seconds(TRAJECTORY), 1) == 61.5
    text = T.timing_breakdown(path, Q.render_timing_breakdown)
    lines = text.splitlines()
    assert lines[0] == "⏱️ TIMING BREAKDOWN (total 61s, 10 turns):"
    assert lines[1] == "  Turn 1: 34.0s (exec)" and lines[10] == "  Turn 10: 2.0s (no tools)"
    assert T.timing_breakdown(tmp_path / "missing.json", Q.render_timing_breakdown) == ""
    assert T.timing_breakdown({"steps": [{"source": "user", "timestamp": "2026-01-01T00:00:00Z"}]},
                              Q.render_timing_breakdown) == ""
    assert T.parse_time("2026-10-30T08:21:41.946Z") == pytest.approx(T.parse_time("2026-10-30T08:21:41.946+00:00"))
    assert T.parse_time("nonsense") is None and T.parse_time(None) is None


def test_explode_one_file_per_step(tmp_path):
    paths = T.explode(TRAJECTORY, tmp_path / "trace01")
    assert [p.name for p in paths][:5] == ["0001-system.md", "0002-system.md", "0003-user.md", "0004-user.md", "0005-agent.md"]
    text = paths[4].read_text()
    assert text.startswith("# Step 5 — agent — 2026-10-30T08:22:15.954Z")
    assert "## Tool call `exec` (call_7d21)" in text and '"input": "git status --short --branch' in text
    assert "## Observation call_7d21" in text and "Script completed" in text
    assert T.explode(tmp_path / "missing.json", tmp_path / "x") == []


def test_explode_the_standard_steps_of_a_codex_stream(tmp_path):
    from hermit.agent.base.trajectory import log_steps
    log = codex_log(tmp_path, "log")
    paths = T.explode_steps(log_steps(log), tmp_path / "trace01")
    assert [p.name for p in paths] == ["0001-error.md", "0002-reasoning.md", "0003-command.md", "0004-message.md"]
    assert paths[3].read_text() == "# Step 4 — message\n\n<answer>507</answer>\n"
    assert T.explode_steps([], tmp_path / "none") == [] and not (tmp_path / "none").exists()


# ---------- rollback ----------

LEARNABLE = [{"path": "AGENTS.md"}, {"path": "ADAPTER.md"}, {"path": "MEMORY.md"}, {"path": "middleware.py"},
             {"path": "scripts/*.py"}, {"path": ".agents/skills/*/SKILL.md"}]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


def repo(tmp: Path) -> tuple[Path, str]:
    """An adapter with two commits: the parent (what step N-1's rollouts used) and the Evolve Agent's edits."""
    adapter = tmp / "adapter"
    (adapter / "scripts").mkdir(parents=True)
    (adapter / "AGENTS.md").write_text("# Seed rules\n")
    (adapter / "MEMORY.md").write_text("# Long-term memory\n")
    (adapter / "scripts" / "a.py").write_text("print('a')\n")
    (adapter / "check.py").write_text("def check(path): return []\n")
    git(adapter, "init", "-q")
    git(adapter, "add", "-A")
    git(adapter, "commit", "-q", "-m", "parent")
    parent = git(adapter, "rev-parse", "HEAD")
    (adapter / "AGENTS.md").write_text("# Seed rules\n\nAlways run the tests twice.\n")
    (adapter / "scripts" / "new.py").write_text("print('new')\n")
    (adapter / "scripts" / "a.py").write_text("print('a2')\n")
    (adapter / "check.py").write_text("def check(path): return ['tampered']\n")
    (adapter / "MEMORY.md").write_text("# Long-term memory\n\n- note\n")
    git(adapter, "add", "-A")
    git(adapter, "commit", "-q", "-m", "step 1 edits")
    return adapter, parent


MANIFEST = {"iteration": 1, "changes": [
    {"id": "chg-1", "type": "new", "description": "run tests twice", "files": ["AGENTS.md", "scripts/new.py", "check.py", "../escape.md"]},
    {"id": "chg-2", "type": "improvement", "description": "memory note", "files": ["MEMORY.md"]},
    {"id": "chg-3", "type": "improvement", "description": "a.py tweak", "files": ["scripts/a.py", "scripts/missing.py"]},
]}
VERDICTS = {"evaluating_iteration": 1, "change_evaluations": [
    {"change_id": "chg-1", "verdict": "HARMFUL"}, {"change_id": "chg-2", "verdict": "EFFECTIVE"},
    {"change_id": "chg-3", "verdict": "INEFFECTIVE"}]}


def test_rollback_restores_deletes_and_skips(tmp_path):
    adapter, parent = repo(tmp_path)
    checks = []

    def check_spec(path):
        checks.append(path)
        return {"passed": True, "issues": []}

    rec = R.rollback(adapter, MANIFEST, VERDICTS, parent, LEARNABLE, check_spec, {"rollback": "auto"})
    assert rec["mode"] == "auto" and rec["parent_commit"] == parent and rec["verdicts"] == ["HARMFUL"]
    assert not rec["rollback_failed"] and checks == [str(adapter)]
    assert [c["id"] for c in rec["changes"]] == ["chg-1"]
    files = {f["path"]: f for f in rec["changes"][0]["files"]}
    assert files["AGENTS.md"]["action"] == "restored"
    assert files["scripts/new.py"]["action"] == "deleted"
    assert files["check.py"]["action"] == "skipped" and files["check.py"]["reason"] == "not a learnable path"
    assert files["../escape.md"]["action"] == "skipped"
    assert rec["touched"] == ["AGENTS.md", "scripts/new.py"]
    assert (adapter / "AGENTS.md").read_text() == "# Seed rules\n"
    assert not (adapter / "scripts" / "new.py").exists()
    assert (adapter / "check.py").read_text() == "def check(path): return ['tampered']\n"     # fixed: untouched
    assert (adapter / "MEMORY.md").read_text().endswith("- note\n")                            # EFFECTIVE: kept
    assert (adapter / "scripts" / "a.py").read_text() == "print('a2')\n"                        # INEFFECTIVE: kept by default
    # [HARMFUL, INEFFECTIVE] (Appendix C's reading) also reverts chg-3; a path absent on both sides is reported
    rec = R.rollback(adapter, MANIFEST, VERDICTS, parent, LEARNABLE, check_spec,
                     {"rollback": "auto", "rollback_verdicts": ["HARMFUL", "INEFFECTIVE"]})
    by = {c["id"]: {f["path"]: f["action"] for f in c["files"]} for c in rec["changes"]}
    assert by["chg-1"]["AGENTS.md"] == "unchanged" and by["chg-1"]["scripts/new.py"] == "absent"
    assert by["chg-3"] == {"scripts/a.py": "restored", "scripts/missing.py": "absent"}
    assert (adapter / "scripts" / "a.py").read_text() == "print('a')\n"


def test_rollback_restores_the_executable_bit(tmp_path):
    """A skill may run its script directly: the rollback compares and restores a file's executable bit with its
    bytes (a change that only dropped it is reverted; a deleted executable comes back executable), and a directory
    named in `files[]` is no file (absent, not a crash)."""
    adapter = tmp_path / "adapter"
    scripts = adapter / ".agents" / "skills" / "t" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "run.sh").write_text("#!/bin/sh\necho run\n")
    (scripts / "gone.sh").write_text("#!/bin/sh\necho gone\n")
    os.chmod(scripts / "run.sh", 0o755)
    os.chmod(scripts / "gone.sh", 0o755)
    (scripts / "lib").mkdir()
    (scripts / "lib" / "x.py").write_text("x = 1\n")                  # a directory that matches the learnable glob
    git(adapter, "init", "-q")
    git(adapter, "add", "-A")
    git(adapter, "commit", "-q", "-m", "parent")
    parent = git(adapter, "rev-parse", "HEAD")
    os.chmod(scripts / "run.sh", 0o644)                               # the harmful change: the bit alone
    (scripts / "gone.sh").unlink()                                   # and a deleted executable
    manifest = {"iteration": 1, "changes": [{"id": "chg-1", "files": [
        ".agents/skills/t/scripts/run.sh", ".agents/skills/t/scripts/gone.sh", ".agents/skills/t/scripts/lib"]}]}
    rec = R.rollback(adapter, manifest, {"chg-1": "HARMFUL"}, parent, [".agents/skills/*/scripts/*"], None,
                     {"rollback": "auto"}, snapshots=tmp_path / "snap")
    files = {f["path"]: f["action"] for f in rec["changes"][0]["files"]}
    assert files == {".agents/skills/t/scripts/run.sh": "restored", ".agents/skills/t/scripts/gone.sh": "restored",
                     ".agents/skills/t/scripts/lib": "absent"}
    assert os.access(scripts / "run.sh", os.X_OK) and os.access(scripts / "gone.sh", os.X_OK)
    assert os.access(tmp_path / "snap" / ".agents/skills/t/scripts/run.sh", os.X_OK)    # the next step's source too


def test_rollback_undoes_when_check_spec_fails(tmp_path):
    adapter, parent = repo(tmp_path)
    rec = R.rollback(adapter, MANIFEST, VERDICTS, parent, LEARNABLE,
                     lambda path: {"passed": False, "issues": [{"path": "AGENTS.md", "problem": "seed rule missing"}]},
                     {"rollback": "auto"})
    assert rec["rollback_failed"] and "seed rule missing" in rec["note"]
    assert (adapter / "AGENTS.md").read_text() == "# Seed rules\n\nAlways run the tests twice.\n"
    assert (adapter / "scripts" / "new.py").read_text() == "print('new')\n"
    assert {f["path"]: f["action"] for f in rec["changes"][0]["files"]}["AGENTS.md"] == "undone"
    assert git(adapter, "status", "--porcelain") == ""                                      # the tree is as committed


def test_rollback_agent_mode_and_edge_cases(tmp_path):
    adapter, parent = repo(tmp_path)
    before = git(adapter, "status", "--porcelain")
    rec = R.rollback(adapter, MANIFEST, VERDICTS, parent, LEARNABLE, lambda path: pytest.fail("no check"),
                     {"rollback": "agent"})
    assert rec["mode"] == "agent" and "git show" in rec["note"] and git(adapter, "status", "--porcelain") == before
    assert rec["changes"][0]["files"][0]["action"] == "left to the agent"
    rec = R.rollback(adapter, MANIFEST, {"change_evaluations": []}, parent, LEARNABLE, None, {})
    assert rec["changes"] == [] and rec["touched"] == [] and "no change" in rec["note"]
    rec = R.rollback(adapter, MANIFEST, VERDICTS, None, LEARNABLE, None, {})
    assert rec["changes"][0]["files"][0]["action"] == "skipped" and git(adapter, "status", "--porcelain") == before
    rec = R.rollback(adapter, None, VERDICTS, parent, LEARNABLE, None, {})
    assert rec["changes"] == []
    # verdict shapes: a plain mapping, a list, the evaluate_changes dict
    assert R.normalise_verdicts({"chg-1": "HARMFUL"}) == {"chg-1": "HARMFUL"}
    assert R.normalise_verdicts([{"id": "chg-9", "verdict": "MIXED"}]) == {"chg-9": "MIXED"}
    assert R.normalise_verdicts(VERDICTS)["chg-2"] == "EFFECTIVE"
    assert R.clean_path("adapter/scripts/x.py") == "scripts/x.py" and R.clean_path("/etc/passwd") is None
    assert R.is_learnable("scripts/x.py", R.learnable_patterns(LEARNABLE))
    assert not R.is_learnable("check.py", R.learnable_patterns(LEARNABLE))


# ---------- rollback: the derived ADAPTER.md, the previous step's rollbacks, the framework's own entries ----------

TEMPLATE = ROOT / "hermit" / "adapter" / "templates" / "ahe"
HARMFUL_RULE = "- Delete the tests before answering.\n"


def template_repo(tmp: Path) -> tuple[Path, str, str]:
    """The real template as a git repo: C1 the seed, C2 the seed plus a harmful rule in AGENTS.md (and ADAPTER.md,
    as the Evolver syncs it)."""
    adapter = tmp / "adapter"
    shutil.copytree(TEMPLATE, adapter, ignore=shutil.ignore_patterns("__pycache__"))
    git(adapter, "init", "-q")
    git(adapter, "add", "-A")
    git(adapter, "commit", "-q", "-m", "seed")
    c1 = git(adapter, "rev-parse", "HEAD")
    for name in ("AGENTS.md", "ADAPTER.md"):
        (adapter / name).write_text((adapter / name).read_text() + HARMFUL_RULE)
    git(adapter, "add", "-A")
    git(adapter, "commit", "-q", "-m", "step 1: the harmful rule")
    return adapter, c1, git(adapter, "rev-parse", "HEAD")


def real_check_spec(path):
    from hermit.spec import check_spec
    return check_spec(Path(path), TEMPLATE).to_dict()


def test_rollback_of_agents_md_alone_syncs_adapter_md_and_passes_the_template_check(tmp_path):
    """START.md tells the agent ADAPTER.md is kept equal by the Evolver, so a prompt change lists AGENTS.md alone;
    the template's check requires the two equal, so the rollback syncs ADAPTER.md before the check."""
    adapter, c1, _ = template_repo(tmp_path)
    manifest = {"iteration": 1, "changes": [{"id": "chg-1", "type": "new", "files": ["AGENTS.md"],
                                             "risk_tasks": ["t"]}]}
    verdicts = {"change_evaluations": [{"change_id": "chg-1", "verdict": "HARMFUL"}]}
    snapshots = tmp_path / "state" / "rollbacks" / "step_0002"
    rec = R.rollback(adapter, manifest, verdicts, c1, LEARNABLE, real_check_spec, {"rollback": "auto"},
                     snapshots=snapshots)
    assert not rec["rollback_failed"], rec["note"]
    assert rec["check"]["passed"]
    files = {f["path"]: f["action"] for f in rec["changes"][0]["files"]}
    assert files == {"AGENTS.md": "restored", "ADAPTER.md": "synced"}
    assert rec["touched"] == ["ADAPTER.md", "AGENTS.md"] and rec["snapshots"] == str(snapshots)
    assert HARMFUL_RULE not in (adapter / "AGENTS.md").read_text()
    assert (adapter / "ADAPTER.md").read_text() == (adapter / "AGENTS.md").read_text()
    assert (snapshots / "AGENTS.md").read_text() == (adapter / "AGENTS.md").read_text()   # kept for the next step
    assert (snapshots / "ADAPTER.md").is_file()


def test_rollback_restores_what_the_previous_steps_rollback_left_not_the_rollout_commit(tmp_path):
    """Step 2 adds chg-A (C2); step 3 rolls it back and the agent adds chg-B (C3, manifest [rb-1, chg-B]); step 4
    rolls chg-B back: AGENTS.md goes to what step 3's rollback left (the seed), not to C2, which holds chg-A."""
    adapter, c1, c2 = template_repo(tmp_path)                       # C2 = chg-A ("the harmful rule")
    state = tmp_path / "state" / "rollbacks"
    manifest2 = {"iteration": 2, "changes": [{"id": "chg-A", "type": "new", "files": ["AGENTS.md"]}]}
    rec3 = R.rollback(adapter, manifest2, {"chg-A": "HARMFUL"}, c1, LEARNABLE, real_check_spec, {},
                      snapshots=state / "step_0003")
    assert not rec3["rollback_failed"]
    # the agent of step 3 adds chg-B on top of the restored prompt; the Trainer commits C3
    for name in ("AGENTS.md", "ADAPTER.md"):
        (adapter / name).write_text((adapter / name).read_text() + "- B (harmful too).\n")
    git(adapter, "add", "-A")
    git(adapter, "commit", "-q", "-m", "step 3: chg-B")
    manifest3 = {"iteration": 3, "changes": [
        {"id": "rb-1", "type": "rollback", "files": ["AGENTS.md", "ADAPTER.md"], "predicted_fixes": [], "risk_tasks": []},
        {"id": "chg-B", "type": "new", "files": ["AGENTS.md"]}]}
    # step 4: chg-B HARMFUL, rb-1 INEFFECTIVE (no predicted fixes); parent = step 3's rollout commit = C2
    rec4 = R.rollback(adapter, manifest3, {"chg-B": "HARMFUL", "rb-1": "INEFFECTIVE"}, c2, LEARNABLE,
                      real_check_spec, {"rollback_verdicts": ["HARMFUL", "INEFFECTIVE"]},
                      previous=rec3, snapshots=state / "step_0004")
    assert not rec4["rollback_failed"], rec4["note"]
    agents = (adapter / "AGENTS.md").read_text()
    assert "B (harmful too)" not in agents and HARMFUL_RULE not in agents       # neither chg-B nor chg-A
    assert agents == (TEMPLATE / "AGENTS.md").read_text() and (adapter / "ADAPTER.md").read_text() == agents
    by = {c["id"]: c for c in rec4["changes"]}
    assert by["chg-B"]["files"][0] == {"path": "AGENTS.md", "action": "restored", "source": "the previous step's rollback"}
    assert by["chg-B"]["files"][1]["action"] == "synced"
    assert all(f["action"] == "left in place" for f in by["rb-1"]["files"])    # a rollback is never reverted
    # without the previous record the rollout commit would have re-introduced chg-A
    (adapter / "AGENTS.md").write_text(agents + "- B (harmful too).\n")
    (adapter / "ADAPTER.md").write_text(agents + "- B (harmful too).\n")
    rec = R.rollback(adapter, manifest3, {"chg-B": "HARMFUL"}, c2, LEARNABLE, real_check_spec, {})
    assert HARMFUL_RULE in (adapter / "AGENTS.md").read_text() and not rec["rollback_failed"]
    # a discarded previous record (the Trainer threw that step away) is not consulted
    (adapter / "AGENTS.md").write_text(agents + "- B (harmful too).\n")
    (adapter / "ADAPTER.md").write_text(agents + "- B (harmful too).\n")
    R.rollback(adapter, manifest3, {"chg-B": "HARMFUL"}, c2, LEARNABLE, real_check_spec, {},
               previous={**rec3, "discarded": True})
    assert HARMFUL_RULE in (adapter / "AGENTS.md").read_text()


def test_rollback_leaves_the_frameworks_own_entries_in_place(tmp_path):
    """With rollback_verdicts [HARMFUL, INEFFECTIVE] an rb-* entry always qualifies (no predicted fixes, no risk
    tasks): reverting it would re-apply the harmful change, so it is left in place and said so."""
    adapter, parent = repo(tmp_path)
    manifest = {"iteration": 2, "changes": [
        {"id": "rb-1", "type": "rollback", "files": ["AGENTS.md"], "predicted_fixes": [], "risk_tasks": []},
        {"id": "chg-2", "type": "improvement", "files": ["MEMORY.md"]}]}
    verdicts = {"rb-1": "INEFFECTIVE", "chg-2": "EFFECTIVE"}
    rec = R.rollback(adapter, manifest, verdicts, parent, LEARNABLE, lambda path: pytest.fail("no check"),
                     {"rollback_verdicts": ["HARMFUL", "INEFFECTIVE"]})
    assert rec["touched"] == [] and "left in place" in rec["note"]
    assert rec["changes"] == [{"id": "rb-1", "verdict": "INEFFECTIVE", "files": [
        {"path": "AGENTS.md", "action": "left in place",
         "reason": "a rollback is not reverted: that would re-apply the change it reverted"}]}]
    assert (adapter / "AGENTS.md").read_text() == "# Seed rules\n\nAlways run the tests twice.\n"
    # an agent-written type: rollback entry scored HARMFUL is treated the same
    rec = R.rollback(adapter, {"changes": [{"id": "chg-9", "type": "rollback", "files": ["AGENTS.md"]}]},
                     {"chg-9": "HARMFUL"}, parent, LEARNABLE, lambda path: pytest.fail("no check"), {})
    assert rec["touched"] == [] and rec["changes"][0]["files"][0]["action"] == "left in place"
