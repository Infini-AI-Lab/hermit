"""The roles besides the learner (#11): the task's agent in rollout, eval and check, and declared tools, each in a
sandbox of its own, with the run's secrets covered and only the worker able to hand a result back."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

import harnessadapter.sandbox as sandbox_module
from harnessadapter.config import Config
from harnessadapter.isolate import (SandboxedAgent, ToolRunner, _model_access, agent_pipeline, profile, run_secrets,
                                    validate)
from harnessadapter.sandbox import Sandbox
from harnessadapter.tools import assemble
from test_training import make_task

REAL_WHICH = shutil.which


def fake_bwrap(monkeypatch):
    monkeypatch.setattr(sandbox_module.shutil, "which",
                        lambda name, *a, **k: "/usr/bin/bwrap" if name == "bwrap" else REAL_WHICH(name, *a, **k))


def _bwrap_works() -> bool:
    if not REAL_WHICH("bwrap"):
        return False
    return subprocess.run(["bwrap", "--unshare-user", "--unshare-net", "--ro-bind", "/", "/", "true"],
                          capture_output=True).returncode == 0


needs_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="needs a working bubblewrap (unprivileged user namespaces)")


# --- configuration --------------------------------------------------------------------------------------------------

def test_profiles_merge_with_the_top_level_unless_told_not_to():
    s = {"enabled": True, "read": ["shared"], "allow": ["a.example"], "packages": ["p"],
         "profiles": {"rollout": {"read": ["env"], "write": ["out"]}, "eval": {"inherit": False, "allow": ["b"]},
                      "check": {"enabled": False}}}
    assert profile(s, "rollout") == {"enabled": True, "read": ["shared", "env"], "write": ["out"],
                                     "allow": ["a.example"], "packages": ["p"]}
    assert profile(s, "eval")["read"] == [] and profile(s, "eval")["allow"] == ["b"]
    assert profile(s, "check")["enabled"] is False and profile(s, "learner")["read"] == ["shared"]
    assert profile({**s, "enabled": False}, "rollout")["enabled"] is False     # the global switch wins


def test_profiles_reject_unknown_roles_and_keys():
    with pytest.raises(ValueError, match="unknown role"):
        validate({"profiles": {"judge": {}}})
    with pytest.raises(ValueError, match="unknown keys"):
        validate({"profiles": {"rollout": {"reed": []}}})


def test_run_secrets_name_the_data_the_judge_the_runs_and_the_logs(tmp_path):
    config = Config.load(make_task(tmp_path), [])
    secrets = {p.name for p in run_secrets(config)}
    assert {"tasks.jsonl", "train.txt", "val.txt", "test.txt", "judge.py", "runs", ".logs"} <= secrets


def test_the_agents_model_endpoint_is_allowed_or_forwarded(tmp_path):
    remote = {"agent": {"name": "react", "params": {"model": {"base_url": "https://api.example.com/v1", "model": "m",
                                                             "api_key_env": "EXAMPLE_KEY"}}}}
    allow, keys, ports = _model_access(remote)
    assert "https://api.example.com/v1" in allow and {u.split("/")[2] for u in allow} == {"api.example.com"}
    assert keys == ["EXAMPLE_KEY"] and ports == []
    local = {"agent": {"name": "react", "params": {"model": {"base_url": "http://127.0.0.1:8123/v1", "model": "m"}}}}
    assert _model_access(local)[0] == [] and _model_access(local)[2] == [8123]


def test_a_disabled_role_runs_the_agent_in_process(tmp_path):
    config = Config.load(make_task(tmp_path, sandbox={"enabled": True, "network": "isolated",
                                                      "profiles": {"rollout": {"enabled": False}}}), [])
    pipeline, close = agent_pipeline(config, "rollout", records=None)
    assert not isinstance(pipeline, SandboxedAgent)
    close()


# --- masks ----------------------------------------------------------------------------------------------------------

def test_masks_cover_secrets_and_a_calls_paths_go_on_top(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)
    base, runs = tmp_path / "base", tmp_path / "base" / "runs"
    (runs / "r" / "adapter").mkdir(parents=True)
    (runs / "trees" / "t").mkdir(parents=True)
    (base / "tasks.jsonl").write_text("secret")
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    box = Sandbox(network="open", readable=[base], writable=[base],
                  masked=[base / "tasks.jsonl", runs / "trees" / "t", runs, outside, base / "missing"])
    args, _ = box.wrap(["true"], readable=[runs / "r" / "adapter"])
    tasks, runs_s, adapter = str((base / "tasks.jsonl").resolve()), str(runs.resolve()), str((runs / "r" / "adapter").resolve())
    # A file is covered by an empty regular file (/dev/null cannot be opened where devices are not allowed).
    cover = args[args.index(tasks) - 1]
    assert args[args.index(tasks) - 2] == "--ro-bind" and Path(cover).is_file() and Path(cover).stat().st_size == 0
    tmpfs = next(i for i, a in enumerate(args) if a == "--tmpfs" and args[i + 1] == runs_s)
    # A path under a covered directory is hidden already; covering it again would leave its mount point visible.
    assert str((runs / "trees" / "t").resolve()) not in args
    # The writable base is bound before the masks, so it cannot uncover them; the adapter goes on top of the mask,
    # and the mask is made read-only only after that.
    assert args.index(str(base.resolve())) < tmpfs < args.index(adapter) < args.index("--remount-ro")
    assert str(outside.resolve()) not in args                     # not shown, so nothing to cover
    box.close()
    assert not Path(cover).exists()


# --- declared tools -------------------------------------------------------------------------------------------------

def test_a_declared_tool_with_a_runner_is_never_imported_here(tmp_path):
    (tmp_path / "tools.py").write_text("raise RuntimeError('imported in the framework process')\n")
    calls = []
    tools = assemble(adapter=tmp_path / "adapter", root=tmp_path, template_decls=[
        {"name": "probe", "description": "d", "entrypoint": "tools.py:probe",
         "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}}}],
        task_decls=[], task_base=tmp_path, budget=type("B", (), {"remaining": lambda s: {}, "snapshot": lambda s: {}})(),
        runner=lambda entry, base, arguments, bound: calls.append((entry, arguments)) or {"ok": 1})
    assert tools["probe"](x=3) == {"ok": 1}
    assert calls == [("tools.py:probe", {"x": 3})]


# --- inside a real sandbox ------------------------------------------------------------------------------------------

SNOOP = r'''
import json, os, subprocess, sys
from pathlib import Path
from harnessadapter.task import RunOutput

def _peek(path):
    try:
        return Path(path).read_text()[:40]
    except Exception as e:
        return "ERR " + type(e).__name__

def run(task, adapter):
    here = Path(__file__).resolve().parent
    text = (Path(adapter) / "ADAPTER.md").read_text() if adapter else ""
    runs = here / "runs"
    seen = {"tasks": _peek(here / "tasks.jsonl"), "judge": _peek(here / "judge.py"), "test": _peek(here / "test.txt"),
            "runs": sorted(os.listdir(runs)) if runs.is_dir() else None,
            "result_fd_env": os.environ.get("HA_RESULT_FD")}
    # A child started without closing descriptors tries every one: the result pipe must not be among them.
    forged = subprocess.run([sys.executable, "-c",
        "import os\nn=0\nfor fd in range(3, 64):\n    try:\n        os.write(fd, b'{\"ok\": true, \"output\": {\"output\": \"FORGED\"}}'); n += 1\n    except OSError:\n        pass\nprint(n)"],
        capture_output=True, text=True, close_fds=False).stdout.strip()
    log = here / "logs" / f"{task['id']}.txt"         # beside its own code, as real agents do
    log.parent.mkdir(exist_ok=True)
    log.write_text("ok")
    answer = str(task["a"] + task["b"]) if "add the numbers" in text else "?"
    return RunOutput(output=answer, log=log, cost_usd=0.01, extra={"seen": seen, "forged_writes": forged})
'''


def sandboxed_task(tmp_path, **sandbox):
    path = make_task(tmp_path, sandbox={"enabled": True, "network": "isolated", **sandbox})
    (tmp_path / "pipeline.py").write_text(SNOOP)
    return path


def an_adapter(tmp_path, text="Always add the numbers.\n") -> Path:
    adapter = tmp_path / "runs" / "some-run" / "rollout_adapter"        # under runs/, which is covered
    adapter.mkdir(parents=True)
    (adapter / "ADAPTER.md").write_text(text)
    return adapter


@needs_bwrap
def test_a_rollout_sees_its_adapter_and_not_the_runs_secrets(tmp_path):
    config = Config.load(sandboxed_task(tmp_path), [])
    agent = SandboxedAgent(config, "rollout", records=tmp_path / "records")
    try:
        out = agent({"id": "t1", "a": 1, "b": 2}, an_adapter(tmp_path))
    finally:
        agent.close()
    assert out.output == "3"                                          # the adapter was readable
    seen = out.extra["seen"]
    assert seen["tasks"] == "" and seen["judge"] == "" and seen["test"] == ""   # covered: they read as empty
    assert seen["runs"] == ["some-run"]                              # only the call's own adapter is under runs/
    assert seen["result_fd_env"] is None and out.extra["forged_writes"] == "0"
    assert (tmp_path / "logs" / "t1.txt").read_text() == "ok"        # written beside the agent's code, kept
    record = json.loads((tmp_path / "records" / "rollout-1.json").read_text())
    assert record["probe"]["passed"] and str((tmp_path / "tasks.jsonl").resolve()) in record["masked"]


@needs_bwrap
def test_a_failed_agent_reports_its_error_through_the_worker(tmp_path):
    config = Config.load(sandboxed_task(tmp_path), [])
    (tmp_path / "pipeline.py").write_text("def run(task, adapter):\n    raise ValueError('no answer here')\n")
    agent = SandboxedAgent(config, "rollout", records=None)
    try:
        with pytest.raises(RuntimeError, match="ValueError: no answer here"):
            agent({"id": "t1", "a": 1, "b": 2}, None)
    finally:
        agent.close()


@needs_bwrap
def test_training_and_eval_with_sandboxed_rollouts(tmp_path):
    from harnessadapter.evaluate import evaluate
    from harnessadapter.trainer.trainer import Trainer
    config = Config.load(sandboxed_task(tmp_path), ["trainer.epochs=1"])
    Trainer(config, log=lambda *_: None).train()
    steps = [json.loads(l) for l in (tmp_path / "runs/toy-run/steps.jsonl").read_text().splitlines()]
    assert steps[0]["rollout"]["mean_score"] == 0.0 and steps[1]["rollout"]["mean_score"] == 1.0
    records = [json.loads(p.read_text()) for p in (tmp_path / "runs/toy-run/sandbox").glob("rollout-*.json")]
    assert len(records) >= 6 and all(r["probe"]["passed"] for r in records)   # one per call, each checked
    summary = evaluate(config, "final", log=lambda *_: None)
    assert summary["mean_score"] == 1.0 and summary["errors"] == 0
    assert list((tmp_path / "runs/toy-run/eval/final/sandbox").glob("eval-*.json"))


TOOL = r'''
import os
from pathlib import Path
def probe(_adapter, _root, x=0, secret=""):
    (Path(_adapter) / "note.txt").write_text("from the tool")
    return {"x": x, "root": os.path.isdir(_root), "secret_visible": os.path.exists(secret)}
'''


@needs_bwrap
def test_a_declared_tool_runs_in_the_tool_sandbox(tmp_path):
    config = Config.load(sandboxed_task(tmp_path), [])
    tools_dir = tmp_path / "tooldir"
    tools_dir.mkdir()
    (tools_dir / "tools.py").write_text(TOOL)
    adapter, root = tmp_path / "ws" / "adapter", tmp_path / "root"
    adapter.mkdir(parents=True)
    root.mkdir()
    runner = ToolRunner(config, records=tmp_path / "records", work_root=tmp_path / "calls")
    try:
        # the real home's .ssh (HOME inside is the call's own), which the sandbox does not show
        result = runner("tools.py:probe", tools_dir, {"x": 5, "secret": str(Path.home() / ".ssh")},
                        {"_adapter": adapter, "_root": root})
    finally:
        runner.close()
    assert result["x"] == 5 and result["root"] and not result["secret_visible"]
    assert (adapter / "note.txt").read_text() == "from the tool"     # the adapter it acts on is writable
    assert json.loads((tmp_path / "records" / "tool-1.json").read_text())["probe"]["passed"]
