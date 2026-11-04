"""The AHE Adapter Evolver end to end on a toy task: three Trainer steps with a scripted `codex` (no model, no network,
no Docker) given through `bin`, standing in for both the Agent Debugger and the Evolve Agent, each a call of the codex
Base Agent.

The toy task is `test_training.make_task`'s with k = 2: two questions (`add-1`, `add-2`), each as two rows
`<uid>#1`, `<uid>#2` sharing the `uid` field; the pipeline answers the sum only when ADAPTER.md says "add the
numbers" and leaves a log in the framework's Codex agent's layout (agent-codex.jsonl, prompt.txt, task.md and the hook
logs at its root), and the toy judge's feedback is the verdict. The scripted `codex` plays the step: step 1 adds the
rule (predicted fixes: both tasks) -> step 2 scores 100% and attributes it EFFECTIVE; step 2 breaks the rule (risk:
both tasks) -> step 3 scores 0%, attributes it HARMFUL and rolls it back before the session; step 3 writes something
else, so the last commit is not the best one and `finalize` must hand back step 2's commit. Sandbox disabled.
"""
import json
from pathlib import Path

import pytest
import yaml

from hermit.config import Config
from hermit.trainer.trainer import Trainer
from hermit.tree import git
from test_training import make_task

TEMPLATE = Path(__file__).resolve().parents[1] / "hermit" / "adapter" / "templates" / "ahe"
RULE = "Always add the numbers."
TASKS = ("add-1", "add-2")

PIPELINE = '''
import json
from pathlib import Path
from hermit.task import RunOutput

def run(task, adapter):
    text = (Path(adapter) / "ADAPTER.md").read_text() if adapter else ""
    answer = str(task["a"] + task["b"]) if "add the numbers" in text else "?"
    log = Path(__file__).parent / ".logs" / task["id"].replace("#", "_")
    (log / "hook-state").mkdir(parents=True, exist_ok=True)
    events = [{"type": "thread.started", "thread_id": "t-" + task["id"]},
              {"type": "item.completed", "item": {"type": "command_execution", "command": "cat task.md",
                                                  "aggregated_output": "%d + %d\\n" % (task["a"], task["b"]), "exit_code": 0}},
              {"type": "item.completed", "item": {"type": "agent_message", "text": answer}},
              {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 5}}]
    (log / "agent-codex.jsonl").write_text("".join(json.dumps(e) + "\\n" for e in events))
    (log / "prompt.txt").write_text("Read task.md and answer.\\n")
    (log / "task.md").write_text("%d + %d\\n" % (task["a"], task["b"]))
    (log / "last_message.txt").write_text(answer)
    (log / "hook-calls.jsonl").write_text(json.dumps({"event": "PreToolUse", "decision": None, "error": None}) + "\\n")
    (log / "hook-state" / "s.json").write_text("{}")
    return RunOutput(output=answer, log=log, cost_usd=0.01, wall_s=0.1, tokens=100,
                     extra={"backend": "codex", "exit_code": 0, "timed_out": False, "attempts": 1})
'''

# The scripted codex. A Debugger call works in its job directory (question.md) and writes answer.md there (the
# manifest lists AGENTS.md alone, as START.md says ADAPTER.md is the Evolver's copy); an Evolve call works in the
# Evolver's workspace, whose manifest block names the step (`"iteration": N`). Both read their prompt on stdin.
# AHE_FAKE_* variables select the variants the tests need.
FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, re, sys, time
from pathlib import Path
args = sys.argv[1:]
assert args[-1] == "-" and "--json" in args, args
prompt = sys.stdin.read()
if os.environ.get("AHE_FAKE_CAPTURE"):                            # every prompt, for the tests that read them
    _cap = Path(os.environ["AHE_FAKE_CAPTURE"])
    _kind = "debugger" if (Path(args[args.index("-C") + 1]) / "question.md").is_file() else "evolve"
    (_cap / ("%s-%d.md" % (_kind, len(list(_cap.glob(_kind + "-*.md"))) + 1))).write_text(prompt)
home = os.environ["CODEX_HOME"]
assert 'web_search = "disabled"' in (Path(home) / "config.toml").read_text()
def usage(n):
    print(json.dumps({"type": "thread.started", "thread_id": "sess-%d" % n}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100 * n, "cached_input_tokens": 10, "output_tokens": n}}))
root = Path(args[args.index("-C") + 1])
if (root / "question.md").is_file():                              # the Agent Debugger
    assert "/debugger-homes/" in home and prompt == (root / "question.md").read_text()
    assert "Trace verdict" in prompt and "- `trace01.events.jsonl`" in prompt and "- `hooks01.jsonl`" in prompt
    verdicts = re.search(r"Trace verdict: (.*)", prompt).group(1)
    assert "- `task01.md`" in prompt and ("FAIL" not in verdicts or "expected the sum of" in prompt)   # the verdict
    (root / "answer.md").write_text(                                # the file the question asks for ...
        "ROOT CAUSE: the agent answered '?' instead of computing the sum.\n"
        "PASS vs FAIL: no trace passed.\nCRITICAL MISTAKE: no arithmetic rule in the prompt.\n"
        "GENERAL MECHANISM: state the arithmetic rule in AGENTS.md.\n")
    Path(args[args.index("-o") + 1]).write_text("Done; the analysis is in answer.md.\n")   # ... and the last message
    usage(1)
    sys.exit(0)
assert home.endswith("agent_home"), home                          # the Evolve Agent
step = int(re.search(r'"iteration": (\d+)', prompt).group(1))
assert "# Evolution query" in prompt and "## 1. Current Iteration Overview" in prompt
(root / "output" / "prompt_seen.json").write_text(json.dumps({
    "step": step, "chars": len(prompt), "analysis_dir": "state/analysis/step_%04d/" % step in prompt,
    "unfilled": "{step_dir}" in prompt or "step_%d/" % step in prompt}))
if os.environ.get("AHE_FAKE_EDIT_FIRST"):                         # an edit made before the session is stopped
    for name in ("AGENTS.md", "ADAPTER.md"):
        (root / "adapter" / name).write_text((root / "adapter" / "AGENTS.md").read_text() + "\n- Edited first.\n"
                                             if name == "AGENTS.md" else (root / "adapter" / "AGENTS.md").read_text())
if os.environ.get("AHE_FAKE_SLEEP"):
    time.sleep(float(os.environ["AHE_FAKE_SLEEP"]))
if os.environ.get("AHE_FAKE_EXIT"):                               # the session fails after its edits
    (root / "adapter" / "AGENTS.md").write_text((root / "adapter" / "AGENTS.md").read_text() + "\n- Half done.\n")
    (root / "state" / "change_manifest.json").write_text(json.dumps({"iteration": step, "changes": [
        {"id": "chg-1", "files": ["AGENTS.md"], "predicted_fixes": ["add-1"], "risk_tasks": []}]}))
    usage(step)
    sys.exit(int(os.environ["AHE_FAKE_EXIT"]))
adapter = root / "adapter"
seed = (adapter / "AGENTS.md").read_text()
seed = seed.split("\n- Always add")[0].split("\n- Never sum")[0].split("\n- Be careful")[0].rstrip() + "\n"
plan = {1: ("Always add the numbers.", {"type": "new", "predicted_fixes": ["add-1", "add-2"], "risk_tasks": []}),
        2: ("Never sum anything.", {"type": "improvement", "predicted_fixes": [], "risk_tasks": ["add-1", "add-2"]}),
        3: ("Be careful.", {"type": "improvement", "predicted_fixes": [], "risk_tasks": []})}
line, change = plan.get(step, plan[3])
(adapter / "AGENTS.md").write_text(seed + "\n- " + line + "\n")
(adapter / "ADAPTER.md").write_text((adapter / "AGENTS.md").read_text())
if not os.environ.get("AHE_FAKE_NO_MANIFEST"):
    (root / "state" / "change_manifest.json").write_text(json.dumps({
        "iteration": step if not os.environ.get("AHE_FAKE_WRONG_ITERATION") else step + 1,
        "changes": [{"id": "chg-1", "description": "prompt rule: " + line, "files": ["AGENTS.md"],
                     "failure_pattern": "arithmetic", "constraint_level": "prompt",
                     "why_this_component": "the rule belongs in the always-on prompt", **change}]}, indent=1))
(root / "output" / "report.md").write_text("step %d: wrote the rule '%s'\n\ndetails...\n" % (step, line))
usage(step)
'''


# The same pipeline with Claude Code as the acting agent: its stream-json events as the framework's Claude agent
# keeps them (agent-claude.jsonl).
CLAUDE_PIPELINE = PIPELINE.replace(
    '''    events = [{"type": "thread.started", "thread_id": "t-" + task["id"]},
              {"type": "item.completed", "item": {"type": "command_execution", "command": "cat task.md",
                                                  "aggregated_output": "%d + %d\\n" % (task["a"], task["b"]), "exit_code": 0}},
              {"type": "item.completed", "item": {"type": "agent_message", "text": answer}},
              {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 5}}]
    (log / "agent-codex.jsonl").write_text("".join(json.dumps(e) + "\\n" for e in events))''',
    '''    events = [{"type": "system", "subtype": "init", "session_id": "s-" + task["id"]},
              {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                                              "input": {"command": "cat task.md"}}]}},
              {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                                         "content": "%d + %d\\n" % (task["a"], task["b"]),
                                                         "is_error": False}]}},
              {"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}},
              {"type": "result", "subtype": "success", "is_error": False, "result": answer,
               "usage": {"input_tokens": 50, "output_tokens": 5}}]
    (log / "agent-claude.jsonl").write_text("".join(json.dumps(e) + "\\n" for e in events))''').replace(
    '"backend": "codex"', '"backend": "claude"')


def _write_rows(tmp: Path) -> None:
    rows = [{"id": f"{t}#{r}", "uid": t, "rollout": r, "a": 2 * i + r, "b": 3 * i + 1}
            for i, t in enumerate(TASKS) for r in (1, 2)]
    rows.append({"id": "held", "uid": "held", "a": 7, "b": 8})
    (tmp / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp / "train.txt").write_text("".join(f"{t}#{r}\n" for t in TASKS for r in (1, 2)))
    (tmp / "test.txt").write_text("held\n")
    (tmp / "pipeline.py").write_text(PIPELINE)


def _task(tmp: Path, *, evolver: dict | None = None, **overrides) -> Path:
    """The toy run: the ahe template, k = 2 rows per question sharing `uid`, the scripted codex as `bin`."""
    fake = tmp / "fakecodex" / "codex"
    fake.parent.mkdir()
    fake.write_text(FAKE_CODEX)
    fake.chmod(0o755)
    params = {"bin": str(fake), "home": [], "model": "fake-model", "task_field": "uid", "target_pass_rate": 2.0,
              "evolve_timeout_s": 120, "price": {"input": 1.0, "cached_input": 0.5, "output": 2.0},
              "debugger": {"concurrency": 2, "timeout_s": 60, "retries": 1, "backoff_s": 0.1}, **(evolver or {})}
    path = make_task(tmp, data={"tasks": "tasks.jsonl", "train": "train.txt", "test": "test.txt", "step_size": 4,
                                "shuffle": False},
                     template=str(TEMPLATE), evolver={"name": "ahe_adapter", "params": params},
                     sandbox={"enabled": False, "network": "open"}, **overrides)
    _write_rows(tmp)
    return path


def _steps(tmp: Path) -> list[dict]:
    return [json.loads(l) for l in (tmp / "runs" / "toy-run" / "steps.jsonl").read_text().splitlines()]


def _state(tmp: Path, step: int) -> Path:
    return tmp / "runs" / "toy-run" / f"step_{step:04d}" / "state"


def test_three_steps_attribute_roll_back_and_finalize_the_best(tmp_path):
    cfg = Config.load(_task(tmp_path), ["trainer.epochs=3"])
    summary = Trainer(cfg, log=lambda *_: None).train()
    steps = _steps(tmp_path)
    assert [s["step"] for s in steps] == [1, 2, 3]
    assert [s["status"] for s in steps] == ["done"] * 3, [s["evolver"]["summary"] for s in steps]
    assert [s["rollout"]["mean_score"] for s in steps] == [0.0, 1.0, 0.0]          # seed, the rule, the rule broken
    assert all(s["changed"] for s in steps)

    # Every state/ file of B.2, as the step's snapshot has it.
    state = _state(tmp_path, 3)
    for rel in ("ahe.json", "task_history.json", "iteration_scores.yaml", "iteration_scores.md", "best_ever.json",
                "evolution_history.md", "change_manifest.json", "manifests/step_0001.json", "manifests/step_0002.json",
                "manifests/step_0003.json", "evaluations/step_0002.json", "evaluations/step_0003.json",
                "rollbacks/step_0003.json", "stats/step_0001.json", "stats/step_0003.json", "queries/step_0001.md",
                "queries/step_0003.md", "analysis/step_0001/overview.md", "analysis/step_0001/detail/add-1.md",
                "analysis/step_0001/jobs.json", "analysis/step_0001/usage.json", "analysis/step_0003/overview.md"):
        assert (state / rel).exists(), rel
    ahe = json.loads((state / "ahe.json").read_text())
    assert ahe["k"] == 2 and set(ahe["steps"]) == {"1", "2", "3"}
    assert ahe["steps"]["2"]["pass_rate"] == 1.0 and ahe["steps"]["2"]["rollout_commit"] == steps[1]["parent"]
    assert set(ahe["steps"]["3"]["timing"]) >= {"eval_min", "analysis_min", "evolve_min", "total_min"}
    stats1 = json.loads((state / "stats/step_0001.json").read_text())
    assert stats1["k"] == 2 and stats1["task_results"] == {"add-1": "fail", "add-2": "fail"}
    assert stats1["tokens_per_trial_k"] == pytest.approx(0.1)

    # Attribution: step 2 scores step 1's manifest EFFECTIVE on the fake's predicted fixes; step 3 scores step 2's
    # manifest HARMFUL (its risk tasks regressed, nothing fixed) and the framework rolled it back before the session.
    ev2 = json.loads((state / "evaluations/step_0002.json").read_text())
    assert ev2["evaluating_iteration"] == 1
    assert ev2["change_evaluations"][0]["verdict"] == "EFFECTIVE"
    assert sorted(ev2["change_evaluations"][0]["actually_fixed"]) == list(TASKS)
    ev3 = json.loads((state / "evaluations/step_0003.json").read_text())
    assert ev3["change_evaluations"][0]["verdict"] == "HARMFUL"
    rb3 = json.loads((state / "rollbacks/step_0003.json").read_text())
    assert rb3["mode"] == "auto" and not rb3["rollback_failed"] and rb3["parent_commit"] == steps[1]["parent"]
    # the manifest lists AGENTS.md alone: ADAPTER.md is synced to it so the template check passes
    assert {f["path"]: f["action"] for f in rb3["changes"][0]["files"]} == {"AGENTS.md": "restored", "ADAPTER.md": "synced"}
    assert rb3["check"]["passed"] and (state / "rollbacks/step_0003/AGENTS.md").read_text().rstrip("\n") == \
        git(Path(summary["tree"]), "show", "toy-run/ckpt-step-1:AGENTS.md")           # the snapshot for step 4
    m3 = json.loads((state / "manifests/step_0003.json").read_text())
    assert m3["iteration"] == 3 and [c["id"] for c in m3["changes"]] == ["rb-1", "chg-1"]
    assert m3["changes"][0]["type"] == "rollback" and m3["changes"][0]["files"] == ["AGENTS.md", "ADAPTER.md"]
    assert "already rolled back by the framework: chg-1" in (state / "queries/step_0003.md").read_text()
    m1 = json.loads((state / "manifests/step_0001.json").read_text())
    assert m1["iteration"] == 1 and m1["changes"][0]["predicted_fixes"] == list(TASKS)

    # The Debugger ran one session per task, the Evolve session one more; both are in the step's usage, priced by the
    # codex Base Agent.
    usage = steps[0]["evolver"]["usage"]
    assert {u["role"] for u in usage} == {"debugger", "evolve"}
    debugger = next(u for u in usage if u["role"] == "debugger")
    assert debugger["calls"] == 2 and debugger["input_tokens"] == 200 and debugger["model"] == "fake-model"
    # (100 - 10) x 1 + 10 x 0.5 + 1 x 2 per million, for each of the two sessions
    assert debugger["cost_usd"] == pytest.approx(2 * (90 + 5 + 2) / 1e6)
    assert next(u for u in usage if u["role"] == "evolve")["output_tokens"] == 1
    assert next(u for u in usage if u["role"] == "evolve")["cost_usd"] == pytest.approx((90 + 5 + 2) / 1e6)
    overview = (state / "analysis/step_0001/overview.md").read_text()
    assert "Analyzed 2 tasks (2 debug)" in overview and "add-1" in overview
    detail = (state / "analysis/step_0001/detail/add-2.md").read_text()
    assert "answered '?'" in detail and "trajectories/add-2_1/agent-codex.jsonl" in detail
    assert "expected the sum of" in detail                                     # the judge's feedback as the verdict
    assert "Analyzed 2 tasks (2 summary)" in (state / "analysis/step_0002/overview.md").read_text()
    for n in (1, 2, 3):                                                       # every session answered at once
        jobs = json.loads((state / f"analysis/step_{n:04d}/jobs.json").read_text())
        assert [j["status"] for j in jobs] == ["ok", "ok"] and all(j["attempts"] == 1 for j in jobs), jobs
    out1 = tmp_path / "runs/toy-run/step_0001/output"
    assert sorted(p.name for p in (out1 / "debugger").iterdir()) == ["add-1.1.jsonl", "add-2.1.jsonl"]
    assert (out1 / "agent-codex.jsonl").is_file()                              # the Evolve session's own stream
    seen = json.loads((tmp_path / "runs/toy-run/step_0003/output/prompt_seen.json").read_text())
    assert seen["step"] == 3 and seen["analysis_dir"] and not seen["unfilled"]   # state/analysis/step_0003/, not step_3
    history = (state / "evolution_history.md").read_text()
    assert history.count("**Evolve Agent Output:**") == 3 and "wrote the rule 'Be careful.'" in history
    assert "Target reached" not in history and "WARNING" not in history
    scores = yaml.safe_load((state / "iteration_scores.yaml").read_text())["scores"]
    assert [s["iteration"] for s in scores] == [1, 2, 3] and scores[1]["pass_rate"] == 1.0
    # the report's two columns: the verdicts of the previous step's changes and what was rolled back
    assert [s["verdicts"] for s in scores] == [{}, {"chg-1": "EFFECTIVE"}, {"chg-1": "HARMFUL"}]
    assert [s["rollbacks"] for s in scores] == [[], [], ["chg-1: AGENTS.md, ADAPTER.md"]]
    best = json.loads((state / "best_ever.json").read_text())
    assert best["step"] == 2 and best["commit"] == steps[1]["parent"] and best["pass_rate"] == 1.0
    result3 = json.loads((tmp_path / "runs/toy-run/step_0003/step_result.json").read_text())
    assert result3["extra"]["verdicts"] == [{"change_id": "chg-1", "verdict": "HARMFUL"}]
    assert result3["extra"]["manifest"]["changes"] == ["rb-1", "chg-1"] and not result3["extra"]["evolve_timed_out"]
    assert result3["extra"]["debugger"]["jobs"] == 2 and result3["extra"]["debugger"]["fallbacks"] == 0

    # finalize: the best-ever commit's tree (the rule), not the last edit ("Be careful.").
    assert "best-ever adapter from step 2" in summary["finalize"]
    tree = Path(summary["tree"])
    final_agents = git(tree, "show", "toy-run/final:AGENTS.md")
    assert RULE in final_agents and "Be careful." not in final_agents
    assert git(tree, "show", "toy-run/final:ADAPTER.md") == final_agents
    last_agents = git(tree, "show", "toy-run/ckpt-step-3:AGENTS.md")
    assert "Be careful." in last_agents and RULE not in last_agents
    assert "Prefer short replies" in final_agents                              # the seed rules survived throughout
    selection = json.loads((tmp_path / "runs/toy-run/workspace/output/final_selection.json").read_text())
    assert selection["step"] == 2 and selection["commit"] == steps[1]["parent"]


def test_early_stop_returns_stop_before_the_session(tmp_path):
    path = _task(tmp_path, evolver={"target_pass_rate": 0.95})
    cfg = Config.load(path, ["trainer.epochs=3", "trainer.allow_early_stop=true"])
    summary = Trainer(cfg, log=lambda *_: None).train()
    steps = _steps(tmp_path)
    assert summary["steps"] == 2 and "stopped the run" in summary["stop_reason"]
    assert steps[1]["status"] == "stop" and steps[1]["rollout"]["mean_score"] == 1.0
    assert "nothing to evolve" in steps[1]["evolver"]["summary"] and steps[1]["evolver"]["usage"] == []
    assert not steps[1]["changed"]
    state = _state(tmp_path, 2)
    assert not (state / "manifests/step_0002.json").exists() and not (state / "queries/step_0002.md").exists()
    assert (state / "evaluations/step_0002.json").exists()                     # attribution still happened
    assert "Target reached at step 2" in (state / "evolution_history.md").read_text()
    assert [s["iteration"] for s in yaml.safe_load((state / "iteration_scores.yaml").read_text())["scores"]] == [1, 2]
    assert "best-ever adapter from step 2" in summary["finalize"]


def test_missing_manifest_archives_an_empty_one_and_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("AHE_FAKE_NO_MANIFEST", "1")
    cfg = Config.load(_task(tmp_path), ["trainer.epochs=1"])
    Trainer(cfg, log=lambda *_: None).train()
    steps = _steps(tmp_path)
    assert steps[0]["status"] == "done" and steps[0]["changed"]
    state = _state(tmp_path, 1)
    archived = json.loads((state / "manifests/step_0001.json").read_text())
    assert archived["iteration"] == 1 and archived["changes"] == [] and "no change_manifest.json" in archived["note"]
    assert "WARNING: no change_manifest.json" in (state / "evolution_history.md").read_text()
    assert "WARNING" in (tmp_path / "runs/toy-run/step_0001/output/report.md").read_text()
    result = json.loads((tmp_path / "runs/toy-run/step_0001/step_result.json").read_text())
    assert result["extra"]["manifest"] == {"iteration": 1, "changes": [], "note": archived["note"]}
    assert any("unattributable" in w for w in result["extra"]["warnings"])


def test_wrong_iteration_in_the_manifest_is_not_archived_as_the_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("AHE_FAKE_WRONG_ITERATION", "1")
    cfg = Config.load(_task(tmp_path), ["trainer.epochs=2"])
    Trainer(cfg, log=lambda *_: None).train()
    state = _state(tmp_path, 2)
    archived = json.loads((state / "manifests/step_0001.json").read_text())
    assert archived["changes"] == [] and "iteration 2, this step is 1" in archived["note"]
    # The empty archive is step 1's, so step 2 attributes it: no change to score, the flips are unattributed.
    ev2 = json.loads((state / "evaluations/step_0002.json").read_text())
    assert ev2["change_evaluations"] == [] and ev2["evaluating_iteration"] == 1
    assert "WARNING" in (state / "evolution_history.md").read_text()


def test_evolve_timeout_stops_the_session_and_keeps_its_edits(tmp_path, monkeypatch):
    monkeypatch.setenv("AHE_FAKE_EDIT_FIRST", "1")                             # it edits, then outlives its clock
    monkeypatch.setenv("AHE_FAKE_SLEEP", "30")
    cfg = Config.load(_task(tmp_path, evolver={"evolve_timeout_s": 2}), ["trainer.epochs=1"])
    summary = Trainer(cfg, log=lambda *_: None).train()
    steps = _steps(tmp_path)
    assert steps[0]["status"] == "done" and "timed out" in steps[0]["evolver"]["summary"]
    assert steps[0]["evolver"]["wall_s"] < 25
    result = json.loads((tmp_path / "runs/toy-run/step_0001/step_result.json").read_text())
    assert result["extra"]["evolve_timed_out"] and any("evolve_timeout_s=2" in w for w in result["extra"]["warnings"])
    assert steps[0]["changed"]                                                 # the edit before the timeout shipped
    assert "- Edited first." in git(Path(summary["tree"]), "show", "toy-run/ckpt-step-1:AGENTS.md")   # step 1's adapter


def test_failed_session_archives_nothing_of_the_step(tmp_path, monkeypatch):
    """Codex exits non-zero after editing: the Trainer discards the edits, so the archived manifest is empty (the
    agent's chg-1 never shipped) and the history says so."""
    monkeypatch.setenv("AHE_FAKE_EXIT", "1")
    cfg = Config.load(_task(tmp_path), ["trainer.epochs=1"])
    Trainer(cfg, log=lambda *_: None).train()
    steps = _steps(tmp_path)
    assert steps[0]["status"] == "failed" and not steps[0]["changed"]
    state = _state(tmp_path, 1)
    archived = json.loads((state / "manifests/step_0001.json").read_text())
    assert archived["iteration"] == 1 and archived["changes"] == [] and "nothing of step 1 ships" in archived["note"]
    assert "the Evolve session failed" in (state / "evolution_history.md").read_text()
    result = json.loads((tmp_path / "runs/toy-run/step_0001/step_result.json").read_text())
    assert result["extra"]["manifest"]["changes"] == [] and any("discards" in w for w in result["extra"]["warnings"])


def test_an_evolve_session_that_cannot_start_fails_the_step_and_keeps_the_debuggers_usage(tmp_path):
    """The Evolve Agent's program is missing while the Debugger's is not: the Base Agent raises, the step fails as a
    failed session does, and the Debugger's sessions are still in the step's usage (a step that raised has none)."""
    fake = str(tmp_path / "fakecodex" / "codex")
    path = _task(tmp_path, evolver={"bin": str(tmp_path / "no-such-codex"),
                                    "debugger": {"bin": fake, "concurrency": 2, "retries": 1, "backoff_s": 0.1}})
    Trainer(Config.load(path, ["trainer.epochs=1"]), log=lambda *_: None).train()
    (step,) = _steps(tmp_path)
    assert step["status"] == "failed" and not step["changed"]
    assert "the Evolve session raised FileNotFoundError" in step["evolver"]["summary"]
    assert [u["role"] for u in step["evolver"]["usage"]] == ["debugger"]
    assert step["evolver"]["usage"][0]["calls"] == 2 and step["evolver"]["usage"][0]["input_tokens"] == 200
    state = _state(tmp_path, 1)
    assert "the Evolve session failed (the Evolve session raised FileNotFoundError" in \
        (state / "evolution_history.md").read_text()
    assert json.loads((state / "manifests/step_0001.json").read_text())["changes"] == []


def test_task_names_that_are_no_file_names_get_their_reports(tmp_path):
    """A task name the framework allows but no file name can be (`suite/add-1`): the Debugger's directories and the
    detail reports are named by task_file_name, the overview names the file beside the task, and the step is done
    with the Debugger's usage in it."""
    path = _task(tmp_path)
    rows = [{"id": f"suite/{t}#{r}", "uid": f"suite/{t}", "rollout": r, "a": 2 * i + r, "b": 3 * i + 1}
            for i, t in enumerate(TASKS) for r in (1, 2)] + [{"id": "held", "uid": "held", "a": 7, "b": 8}]
    (tmp_path / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "train.txt").write_text("".join(f"suite/{t}#{r}\n" for t in TASKS for r in (1, 2)))
    Trainer(Config.load(path, ["trainer.epochs=1"]), log=lambda *_: None).train()
    (step,) = _steps(tmp_path)
    assert step["status"] == "done", step["evolver"]["summary"]
    assert "debugger" in [u["role"] for u in step["evolver"]["usage"]]
    from hermit.agent.evolver.methods.ahe_adapter.analysis import task_file_name
    analysis = _state(tmp_path, 1) / "analysis" / "step_0001"
    names = {task_file_name(f"suite/{t}") for t in TASKS}
    assert len(names) == 2 and not any("/" in n for n in names)
    assert {f.stem for f in (analysis / "detail").iterdir()} == names
    overview = (analysis / "overview.md").read_text()
    assert all(f"(detail: `{n}.md`)" in overview for n in names)


def test_a_step_that_raises_after_the_debugger_reports_its_usage(tmp_path, monkeypatch):
    """Whatever raises once the Debugger's sessions ran, the step fails with their usage in it (a step that raised
    has none), and the history says nothing of it ships."""
    from hermit.agent.evolver.methods.ahe_adapter import analysis

    def broken(*args, **kwargs):
        raise RuntimeError("the overview could not be written")
    monkeypatch.setattr(analysis, "write_debugger_analyse", broken)
    Trainer(Config.load(_task(tmp_path), ["trainer.epochs=1"]), log=lambda *_: None).train()
    (step,) = _steps(tmp_path)
    assert step["status"] == "failed" and not step["changed"]
    assert "step 1 raised RuntimeError: the overview could not be written" in step["evolver"]["summary"]
    assert [u["role"] for u in step["evolver"]["usage"]] == ["debugger"] and step["evolver"]["usage"][0]["calls"] == 2
    assert "nothing of step 1 ships" in (_state(tmp_path, 1) / "evolution_history.md").read_text()


def test_a_separate_debugger_model_is_refused_with_a_price_or_a_binding(tmp_path):
    """The Debugger takes the Evolve Agent's binding and price: under another model its usage would be recorded and
    priced as the Evolve Agent's, so the setup refuses that; the same model, or no price and no binding, pass."""
    from types import SimpleNamespace
    from hermit.agent.evolver.methods.ahe_adapter.evolver import AHEAdapterEvolver

    def setup(params):
        state = tmp_path / f"state-{len(list(tmp_path.iterdir()))}"
        AHEAdapterEvolver().setup(SimpleNamespace(params={"web_search": False, "model": "m", **params},
                                                  workspace=SimpleNamespace(state=state)))
    for priced in ({"price": {"input": 1.0, "output": 2.0}}, {"binding": {"ref": "fireworks/m", "model": "m"}}):
        with pytest.raises(ValueError, match="runs on the Evolve Agent's model"):
            setup({**priced, "debugger": {"model": "other"}})
        setup({**priced, "debugger": {"model": "m"}})
        setup({**priced, "debugger": {"model": None}})
    setup({"login": "chatgpt", "debugger": {"model": "other"}})          # the login's own provider, no price


def test_the_setup_refuses_another_base_agent_and_an_unset_web_search(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with pytest.raises(SystemExit, match="sessions of the codex Base Agent"):
        Trainer(Config.load(_task(tmp_path / "a", evolver={"backend": "claude"}), ["trainer.epochs=1"]),
                log=lambda *_: None).train()
    with pytest.raises(SystemExit, match="web_search must be set"):
        Trainer(Config.load(_task(tmp_path / "b", evolver={"web_search": None}), ["trainer.epochs=1"]),
                log=lambda *_: None).train()


def test_the_evolve_prompt_is_start_md_and_the_query():
    """The evolve prompt reaches Codex on stdin: START.md and the query in one file, however long."""
    import tempfile
    from hermit.agent.evolver.loader import find, load_class
    import sys
    load_class(find("ahe_adapter"))
    E = sys.modules["_evolver_ahe_adapter_evolver"]
    with tempfile.TemporaryDirectory(dir=Path.home() / ".cache" / "hermit-tests") as scratch:
        prompt = E.evolve_prompt("START", "q" * 200_000, Path(scratch))
        assert prompt.read_text() == "START\n\n# Evolution query\n" + "q" * 200_000
    assert not hasattr(E, "PROMPT_MAX_BYTES") and E.OVERVIEW_MAX_BYTES == 40_000


def test_the_overview_the_query_embeds_is_bounded(tmp_path):
    """90 tasks with 2 KB Debugger answers (Markdown-bold labels, which upstream's one-liner did not stop at): the
    overview on disk is whole, the query gets a bounded one that names it."""
    from hermit.agent.evolver.loader import find, load_class
    from hermit.agent.evolver.methods.ahe_adapter import analysis as A
    import sys
    load_class(find("ahe_adapter"))
    E = sys.modules["_evolver_ahe_adapter_evolver"]
    answer = ("ROOT CAUSE: " + "the agent looped on a flaky build step " * 40 + "\n**PASS vs FAIL:** " + "x" * 300
              + "\n**CRITICAL MISTAKE:** " + "y" * 300 + "\n**GENERAL MECHANISM:** " + "z" * 300 + "\n")
    assert len(answer) > 2000
    assert len(A.extract_one_liner(answer)) < 1800 and "PASS vs FAIL" not in A.extract_one_liner(answer)
    results = [{"task_name": f"task-{i:02d}", "mode": "debug", "n_pass": 1, "n_fail": 1, "n_timeout": 0,
                "is_timeout": False, "response": answer, "trace_paths": [], "trace_rewards": [1.0, 0.0],
                "verifier_outputs": ["", ""]} for i in range(90)]
    analysis_dir = tmp_path / "state" / "analysis" / "step_0002"
    _, overview = A.write_debugger_analyse(results, analysis_dir, 2, detail_label="detail")
    assert len(overview.encode()) > E.OVERVIEW_MAX_BYTES                      # the full overview is on disk ...
    bounded = E.bounded_overview(overview, analysis_dir / "overview.md")
    assert len(bounded.encode()) < E.OVERVIEW_MAX_BYTES + 200 and "overview.md" in bounded   # ... the query gets a bounded one


def test_with_claude_code_acting_the_learners_read_its_stream(tmp_path, monkeypatch):
    """A step whose rollouts are Claude Code sessions (agent-claude.jsonl): the evolution query, the Evolve Agent's
    prompt and the Debugger's question name that stream; none names Codex's. The learners are codex sessions all the
    same (the fake codex answers them)."""
    assert "agent-claude.jsonl" in CLAUDE_PIPELINE and "agent-codex" not in CLAUDE_PIPELINE
    capture = tmp_path / "capture"
    capture.mkdir()
    monkeypatch.setenv("AHE_FAKE_CAPTURE", str(capture))
    path = _task(tmp_path)
    (tmp_path / "pipeline.py").write_text(CLAUDE_PIPELINE)
    Trainer(Config.load(path, ["trainer.epochs=1"]), log=lambda *_: None).train()
    evolve = (capture / "evolve-1.md").read_text()
    query = (_state(tmp_path, 1) / "queries" / "step_0001.md").read_text()
    questions = [f.read_text() for f in sorted(capture.glob("debugger-*.md"))]
    assert questions and query.strip() in evolve                         # the prompt is START.md and the query
    for text in (evolve, query):
        assert "agent-claude.jsonl" in text and "agent-codex.jsonl" not in text, text[:300]
    assert "the Claude Code event stream" in evolve and "the Codex event stream" not in evolve
    for question in questions:
        assert "`claude -p --output-format stream-json` event stream" in question and "codex exec --json" not in question


@pytest.mark.parametrize("change, field", [
    ({"id": "c", "files": ["AGENTS.md"], "predicted_fixes": None, "risk_tasks": []}, "predicted_fixes"),
    ({"id": "c", "files": "AGENTS.md"}, "files"),
    ({"id": "c", "risk_tasks": [1]}, "risk_tasks"),
    ("chg-1", None),
    ({"id": "c", "description": None}, "description"),
])
def test_a_manifest_the_next_step_could_not_read_is_refused(change, field):
    """The next step's attribution iterates `predicted_fixes` and `risk_tasks`, the rollback `files` and the query
    slices `description`: a manifest with another type there is refused now (the step archives an empty one, loudly), not left to fail step N+1."""
    from hermit.agent.evolver.methods.ahe_adapter.evolver import validate_manifest
    manifest, note = validate_manifest({"iteration": 3, "changes": [change]}, 3)
    expected = {None: "changes[0] is not an object", "description": "changes[0].description is not a string"}
    assert manifest is None and expected.get(field, f"changes[0].{field} is not a list of strings") in note
    ok, note = validate_manifest({"iteration": 3, "changes": [{"id": "c", "files": ["AGENTS.md"]}]}, 3)
    assert ok is not None and note == ""

