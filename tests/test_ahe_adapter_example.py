"""The AHE Adapter's OfficeQA example (examples/officeqa/ahe_adapter/) on the repository's own Codex route: both configs
load and resolve the template and the Evolver in the per-layer form, with the model, login and effort reaching the
Evolve Agent and the Agent Debugger alike; make_rows.py writes the k trial rows; and trials.py hands ../utils the
question's own row: prepare writes the question's task.md, the judge scores the question, and collect brings the hook
runner's logs to the log's root, where the learning material keeps them and the Debugger reads them. A rollout is the
framework's Codex Acting Agent on a scripted `codex` that runs the deployed hooks as Codex does (no model, no network,
no Docker); the tests that need the prepared OfficeQA data or skillopt are skipped without them."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from hermit.agent.acting import workspace as cli
from hermit.agent.evolver.base import Access, Progress, StepContext, Workspace
from hermit.agent.evolver.loader import find, instantiate
from hermit.agent.evolver.methods.ahe_adapter import debugger as D
from hermit.agent.model.backends import registry
from hermit.config import Config
from hermit.task import RunOutput, TaskData, load_callable
from hermit.trainer.material import Material
from hermit.trainer.rollout import run_one

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "officeqa" / "ahe_adapter"
UTILS = EXAMPLE.parent / "utils"
TEMPLATE = ROOT / "hermit" / "adapter" / "templates" / "ahe"
LUNA = {"model": "gpt-5.6-luna", "login": "chatgpt", "effort": "medium", "web_search": False}
EVENTS = ["SessionStart", "UserPromptSubmit", "PreModelRequest", "PreToolUse", "PostToolUse", "Stop"]


def trials():
    """trials.py as the framework loads the configs' task functions (`trials.py:score`)."""
    return sys.modules[load_callable("trials.py:score", EXAMPLE).__module__]


def prepared() -> dict:
    """../utils's data.json once utils/prepare.py has run (tasks.jsonl, splits/, the answers, the corpus it names), and
    skillopt; the test is skipped without them."""
    pytest.importorskip("skillopt")
    if not all((UTILS / name).exists() for name in ("tasks.jsonl", "splits", "data.json")):
        pytest.skip("OfficeQA is not prepared here (examples/officeqa/utils/prepare.py)")
    data = json.loads((UTILS / "data.json").read_text())
    if not all((Path(data["docs"]) / sub).is_dir() for sub in ("transformed", "jsons")):
        pytest.skip(f"the OfficeQA corpus is not at {data['docs']}")
    return data


def context(tmp: Path, params: dict) -> StepContext:
    ws = Workspace(**{name: tmp / "ws" / name for name in ("adapter", "learning_material", "task_info", "history",
                                                           "state", "scratch", "output")})
    for path in ws.__dict__.values():
        path.mkdir(parents=True, exist_ok=True)
    return StepContext(workspace=ws, access=Access(), tools={}, params=params, sandbox=None,
                       progress=Progress(epoch=1, step=1, is_epoch_end=False))


# ---------- the configs ----------

@pytest.mark.parametrize("name, train, test, step_size, concurrency, target", [
    ("run_codex_luna.yaml", "rows/train_k2.txt", "../utils/splits/test.txt", 100, 8, 0.95),
    ("smoke_codex_luna.yaml", "rows/smoke_k2.txt", "../utils/splits/smoke_test.txt", 8, 4, 1.01)])
def test_the_configs_resolve_the_template_and_the_evolver_on_the_luna_row(tmp_path, name, train, test, step_size,
                                                                           concurrency, target):
    config = Config.load(EXAMPLE / name)
    d = config.data
    assert config.template_dir() == TEMPLATE
    assert (d["data"]["tasks"], d["data"]["train"], d["data"]["val"], d["data"]["test"], d["data"]["step_size"],
            d["data"]["shuffle"]) == ("rows/tasks.jsonl", train, None, test, step_size, False)
    # the acting agent: gepa/run_codex_luna.yaml's, with the trial rows' task functions and a judge that maps them
    agent, gepa = d["agent"], Config.load(EXAMPLE.parent / "gepa" / "run_codex_luna.yaml").data["agent"]
    assert (agent["name"], agent["concurrency"]) == ("codex", concurrency)
    params = agent["params"]
    same = ("prompt", "answer", *LUNA, "timeout_s", "project_doc_max_bytes", "isolation", "container")
    assert {k: params[k] for k in same} == {k: gepa["params"][k] for k in same}
    assert {k: params[k] for k in LUNA} == LUNA
    assert (params["prepare"], params["collect"], d["judge"]) == ("trials.py:prepare", "trials.py:collect",
                                                                  {"path": "trials.py", "name": "score"})
    assert params["workspaces"] != gepa["params"]["workspaces"]
    # the Evolver: the per-layer form read into its params, as the Trainer instantiates it
    evolver, p = instantiate(find(d["evolver"]["name"], config.base), d["evolver"]["params"],
                             backends=registry(d.get("backends")), default_model=d.get("model"))
    assert type(evolver).__name__ == "AHEAdapterEvolver"
    assert p["backend"] == "codex" and {k: p[k] for k in LUNA} == LUNA
    assert (p["task_field"], p["k"], p["rollback"], p["final"]) == ("uid", 2, "auto", "best_ever")
    assert p["target_pass_rate"] == target and d["trainer"]["allow_early_stop"] is True   # 1.01: no early stop
    ctx = context(tmp_path, p)
    evolver.setup(ctx)                                      # refuses another Base Agent or an unset web_search
    evolve = evolver.settings(ctx, "codex")                 # the Evolve Agent's session ...
    debugger = D.Runner(ctx, D.settings(p), evolve, ctx.workspace.scratch).agent_settings(
        D.Job(task="UID0002"), tmp_path / "job", 1, 600.0)  # ... and one attempt of the Agent Debugger
    for role in (evolve, debugger):
        assert {k: role[k] for k in LUNA} == LUNA
    assert d["sandbox"]["allow"] == ["chatgpt.com", "auth.openai.com"] and d["sandbox"]["network"] == "restricted"
    assert d["sandbox"]["profiles"]["rollout"] == {"enabled": False}       # the agent's role drives Docker


# ---------- make_rows.py ----------

def make_rows(tasks: Path, splits: Path, out: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", str(EXAMPLE / "make_rows.py"), "--tasks", str(tasks), "--splits",
                           str(splits), "--out", str(out), *args], capture_output=True, text=True)


def test_make_rows_writes_k_trial_rows_of_each_training_question(tmp_path):
    own = [{"id": f"Q{i}", "question": f"question {i}?", "category": "easy", "source_files": [f"b{i}.txt"],
            "source_docs": []} for i in range(1, 6)]
    lines = [json.dumps(r, ensure_ascii=False) for r in own]
    (tmp_path / "tasks.jsonl").write_text("".join(line + "\n" for line in lines))
    (tmp_path / "splits").mkdir()
    for split, ids in (("train", "Q1 Q2 Q3"), ("smoke_train", "Q1 Q2"), ("test", "Q4 Q5")):
        (tmp_path / "splits" / f"{split}.txt").write_text("".join(f"{i}\n" for i in ids.split()))
    rows = tmp_path / "rows"
    done = make_rows(tmp_path / "tasks.jsonl", tmp_path / "splits", rows)
    assert done.returncode == 0, done.stderr
    written = (rows / "tasks.jsonl").read_text().splitlines()
    assert written[:5] == lines                                            # every question's own row, as it is
    trial_rows = [json.loads(line) for line in written[5:]]
    by_trial = ["Q1#1", "Q2#1", "Q3#1", "Q1#2", "Q2#2", "Q3#2"]               # every first rollout, then every second
    assert [r["id"] for r in trial_rows] == by_trial
    for row in trial_rows:
        uid, j = row["id"].split("#")
        assert row == {**own[int(uid[1:]) - 1], "id": row["id"], "uid": uid, "rollout": int(j)}
    assert (rows / "train_k2.txt").read_text().split() == by_trial
    assert (rows / "smoke_k2.txt").read_text().split() == ["Q1#1", "Q2#1", "Q1#2", "Q2#2"]
    data = TaskData(rows / "tasks.jsonl", rows / "train_k2.txt", None, tmp_path / "splits" / "test.txt")
    assert [r["uid"] for r in data.rows("train")] == ["Q1", "Q2", "Q3"] * 2 and "uid" not in data.tasks["Q4"]
    # another k; no smoke split; a split naming a question that is not there
    (tmp_path / "splits" / "smoke_train.txt").unlink()
    done = make_rows(tmp_path / "tasks.jsonl", tmp_path / "splits", rows, "--k", "3")
    assert done.returncode == 0 and "no smoke split" in done.stdout
    assert len((rows / "train_k3.txt").read_text().split()) == 9 and not (rows / "smoke_k3.txt").exists()
    (tmp_path / "splits" / "train.txt").write_text("Q1\nQ9\n")
    done = make_rows(tmp_path / "tasks.jsonl", tmp_path / "splits", rows)
    assert done.returncode != 0 and "Q9" in done.stderr


# ---------- trials.py ----------

def test_a_trial_row_is_the_questions_own_row():
    t = trials()
    own = {"id": "UID0002", "question": "q?", "category": "easy", "source_files": ["a.txt"], "source_docs": ["u"]}
    trial = {**own, "id": "UID0002#2", "uid": "UID0002", "rollout": 2}
    assert t.question(trial) == own and list(t.question(trial)) == list(own)
    assert t.question(own) is own                                          # a validation or test row passes as is


def test_prepare_writes_the_questions_own_task_md(tmp_path):
    prepared()
    assert make_rows(UTILS / "tasks.jsonl", UTILS / "splits", tmp_path / "rows").returncode == 0
    rows = {r["id"]: r for r in map(json.loads, (tmp_path / "rows" / "tasks.jsonl").read_text().splitlines())}
    uid = (tmp_path / "rows" / "train_k2.txt").read_text().split()[0].split("#")[0]
    trial_prepare = load_callable("trials.py:prepare", EXAMPLE)              # as the configs name them
    own_prepare = load_callable("../utils/task.py:prepare", EXAMPLE)          # as gepa/run_codex_luna.yaml names it
    own = tmp_path / "own"
    own.mkdir()
    own_mounts = own_prepare(rows[uid], own, {}, EXAMPLE)
    for j in (1, 2):
        work = tmp_path / f"trial{j}"
        work.mkdir()
        assert trial_prepare(rows[f"{uid}#{j}"], work, {}, EXAMPLE) == own_mounts
        assert (work / "task.md").read_bytes() == (own / "task.md").read_bytes()


def test_the_judge_scores_the_question(monkeypatch):
    t = trials()
    seen = []
    monkeypatch.setattr(t, "utils", lambda spec: lambda task, output: seen.append((spec, task)) or {"score": 1.0})
    t.score({"id": "UID0002#1", "uid": "UID0002", "rollout": 1, "question": "q?"}, RunOutput(output={}))
    t.score({"id": "UID0003", "question": "r?"}, RunOutput(output={}))
    assert seen == [("judge.py:score", {"id": "UID0002", "question": "q?"}),
                    ("judge.py:score", {"id": "UID0003", "question": "r?"})]


def test_the_judge_scores_a_trial_against_the_questions_answer():
    data = prepared()
    answers = {r["id"]: r["answer"] for r in map(json.loads, Path(data["answers"]).read_text().splitlines()) if r}
    uid = next(iter(answers))
    score = load_callable("trials.py:score", EXAMPLE)                     # judge.path: trials.py
    trial = {"id": f"{uid}#2", "uid": uid, "rollout": 2}
    right = score(trial, RunOutput(output={"answer": answers[uid], "fail_reason": ""}))
    wrong = score(trial, RunOutput(output={"answer": "no such figure", "fail_reason": ""}))
    assert (right["score"], right["feedback"]) == (1.0, "correct")
    assert wrong["score"] == 0.0 and wrong["feedback"].endswith(f"but expected '{answers[uid]}'")


def test_collect_leaves_out_links_in_place_of_the_hook_logs(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("not the session's")
    work, log = tmp_path / "work", tmp_path / "log"
    (work / ".hermit-hooks" / "hook-state").mkdir(parents=True)
    log.mkdir()
    (work / "task.md").write_text("## Question\nq?\n")
    (work / ".hermit-hooks" / "hook-calls.jsonl").symlink_to(outside)
    (work / ".hermit-hooks" / "hook-state" / "s-1.json").write_text("{}")
    (work / ".hermit-hooks" / "hook-state" / "s-2.json").symlink_to(outside)
    assert trials().collect({"id": "Q#1", "uid": "Q", "rollout": 1}, work, log) == {}
    assert sorted(p.name for p in log.iterdir()) == ["hook-state", "task.md"]
    assert sorted(p.name for p in (log / "hook-state").iterdir()) == ["s-1.json"]
    shutil.rmtree(log)
    log.mkdir()
    shutil.rmtree(work / ".hermit-hooks")
    (work / ".hermit-hooks").symlink_to(tmp_path)                           # the whole log base a link
    trials().collect({"id": "Q#1", "uid": "Q", "rollout": 1}, work, log)
    assert sorted(p.name for p in log.iterdir()) == ["task.md"]


# ---------- a rollout on the example's route ----------

# A stand-in for `codex exec --json ... -o LAST PROMPT` in the session's workspace: one turn whose hooks run as Codex
# 0.164 runs them (each command of the deployed .codex/hooks.json through a shell, the event's JSON on stdin), then
# the event stream and the last message.
FAKE_CODEX = r'''#!__PYTHON__
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
prompt, last = args[-1], Path(args[args.index("-o") + 1])
hooks = json.loads(Path(".codex/hooks.json").read_text())["hooks"]
shell = {"command": "cat task.md"}
turn = [("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": prompt}), ("PreModelRequest", {}),
        ("PreToolUse", {"tool_name": "Bash", "tool_input": shell}),
        ("PostToolUse", {"tool_name": "Bash", "tool_input": shell, "tool_response": "507"}),
        ("Stop", {"stop_hook_active": False, "last_assistant_message": "<answer>507</answer>"})]
for event, fields in turn:
    for group in hooks[event]:
        for hook in group["hooks"]:
            payload = {"session_id": "sess-1", "cwd": os.getcwd(), "hook_event_name": event, **fields}
            done = subprocess.run(["sh", "-c", hook["command"]], input=json.dumps(payload), text=True,
                                  capture_output=True, timeout=hook["timeout"])
            if done.returncode:
                sys.exit(f"the {event} hook exited {done.returncode}: {done.stderr}")
for event in ({"type": "thread.started", "thread_id": "sess-1"},
              {"type": "item.completed", "item": {"type": "command_execution", "command": "cat task.md",
                                                  "aggregated_output": "507", "exit_code": 0}},
              {"type": "item.completed", "item": {"type": "agent_message", "text": "<answer>507</answer>"}},
              {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 5}}):
    print(json.dumps(event))
last.write_text("<answer>507</answer>")
'''


def test_a_rollout_brings_the_hook_logs_where_the_debugger_reads_them(tmp_path, monkeypatch):
    """The example's acting agent (run_codex_luna.yaml's, in no container and with a task.md file in place of the
    corpus) on a trial row with the ahe harness deployed: the session's hooks get the workspace's task.md as
    inp.task_text from SessionStart on and write their logs under .hermit-hooks/ in the workspace, trials.py:collect
    brings the logs to the log's root, the learning material copies the log, and the Agent Debugger's job directory
    gets the audit."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "codex").write_text(FAKE_CODEX.replace("__PYTHON__", sys.executable))
    (bin_dir / "codex").chmod(0o755)
    (bin_dir / "python3").symlink_to(sys.executable)                       # the hooks' python3: this interpreter
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    user = tmp_path / "user-codex"                                          # the user's login: a file of this test
    user.mkdir()
    (user / "auth.json").write_text(json.dumps({"last_refresh": "2026-10-01T00:00:00Z", "tokens": {"id": "x"}}))
    monkeypatch.setenv("CODEX_HOME", str(user))
    monkeypatch.delenv("HERMIT_HOOK_LOGS", raising=False)
    adapter = tmp_path / "adapter"
    shutil.copytree(TEMPLATE, adapter, ignore=shutil.ignore_patterns("__pycache__"))
    seed = (adapter / "middleware.py").read_text()                         # the seed, keeping what it was given
    (adapter / "middleware.py").write_text(seed.replace("    body = _memory_body(inp.adapter_dir)\n",
                                                        "    state['start_task'] = inp.task_text\n"
                                                        "    body = _memory_body(inp.adapter_dir)\n", 1))
    (tmp_path / "task.md").write_text("## Question\nWhat were the expenditures in FY 1934?\n")
    params = dict(Config.load(EXAMPLE / "run_codex_luna.yaml").data["agent"]["params"])
    for key in ("prepare", "container"):
        params.pop(key)
    params.update(isolation="none", workspaces=str(tmp_path / "ws"), files=[str(tmp_path / "task.md")])
    row = {"id": "UID0002#2", "question": "What were the expenditures in FY 1934?", "uid": "UID0002", "rollout": 2}
    record = run_one(row, adapter, lambda task, a: cli.run(task, a, params=params, base=EXAMPLE),
                     lambda task, output: {"score": 1.0, "feedback": "correct"})
    assert record["status"] == "ok", record.get("error")
    assert record["output"] == {"answer": "507", "fail_reason": ""}
    log = Path(record["log"])
    assert sorted(p.name for p in log.iterdir()) == ["agent-codex.jsonl", "deployment.json", "hook-calls.jsonl",
                                                     "hook-state", "last_message.txt", "prompt.txt", "task.md"]
    audit = [json.loads(line) for line in (log / "hook-calls.jsonl").read_text().splitlines()]
    assert [a["event"] for a in audit] == EVENTS
    assert all(a["error"] is None and a["session_id"] == "sess-1" for a in audit), audit
    assert audit[EVENTS.index("PreToolUse")]["command"] == "cat task.md"
    state = json.loads((log / "hook-state" / "sess-1.json").read_text())
    assert state["start_task"] == (log / "task.md").read_text() == (tmp_path / "task.md").read_text()
    assert state["_prompt"] == (log / "prompt.txt").read_text()            # kept, though task.md is the task text

    step = Material(tmp_path / "material").write(1, 1, "c0", False, [record], None)
    (rec,) = D.load_records(step)
    assert rec["input"]["uid"] == "UID0002" and rec["trajectory"] == "trajectories/UID0002_2"
    trial_log = step / rec["trajectory"]
    assert D.log_file(trial_log, "hooks") == trial_log / "hook-calls.jsonl"
    assert (trial_log / "hook-state" / "sess-1.json").is_file()      # where START.md sends the Evolve Agent
    (job,) = D.build_jobs([rec], {"task_field": "uid"})
    job_dir = D.prepare_job_dir(job, tmp_path / "scratch", D.settings({}))
    assert (job_dir / "hooks01.jsonl").read_text() == (log / "hook-calls.jsonl").read_text()
    assert (job_dir / "task01.md").read_text() == (tmp_path / "task.md").read_text()
    assert "- `hooks01.jsonl`" in (job_dir / "question.md").read_text()
