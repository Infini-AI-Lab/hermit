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
from harnessadapter.isolate import (SandboxedAgent, ToolRunner, _model_access, agent_pipeline, config_paths, profile,
                                    project_records, project_root, run_secrets, validate)
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
                                     "allow": ["a.example"], "packages": ["*", "p"], "mask": [],
                                     "network": "open", "env": None}
    assert profile(s, "eval")["read"] == [] and profile(s, "eval")["allow"] == ["b"]
    assert profile(s, "check")["enabled"] is False and profile(s, "learner")["read"] == ["shared"]
    assert profile({**s, "enabled": False}, "rollout")["enabled"] is False     # the global switch wins


def test_the_agent_roles_preset_and_the_strict_roles():
    s = {"network": "isolated", "env": ["A"], "packages": [],
         "profiles": {"rollout": {"read": ["../env"], "mask": ["../utils/.private"]}}}
    for role in ("eval", "check"):                                # the same agent: rollout's profile
        assert profile(s, role)["read"] == ["../env"] and profile(s, role)["mask"] == ["../utils/.private"]
    for role in ("rollout", "eval", "check"):                     # task code: open network, its environment, all packages
        p = profile(s, role)
        assert p["network"] == "open" and p["env"] is None and "*" in p["packages"]
    for role in ("learner", "tool"):                              # strict: the top-level settings
        p = profile(s, role)
        assert p["network"] == "isolated" and p["env"] == ["A"] and "*" not in p["packages"]
    tight = {"profiles": {"rollout": {"network": "isolated", "env": ["SLB_WORKDIR"]}}}
    assert profile(tight, "rollout")["network"] == "isolated" and profile(tight, "eval")["env"] == ["SLB_WORKDIR"]
    with pytest.raises(ValueError):
        profile({"profiles": {"rollout": {"network": "sideways"}}}, "rollout")


def test_the_project_is_the_directory_holding_the_config_and_the_task(tmp_path):
    """An example keeps its config in a method's directory and the task in ../utils."""
    utils, method = tmp_path / "ex" / "utils", tmp_path / "ex" / "prompt_only"
    (utils / "agent").mkdir(parents=True)
    method.mkdir()
    for f in ("tasks.jsonl", "train.txt", "judge.py", "agent/agent.py"):
        (utils / f).write_text("")
    (method / "run.yaml").write_text(yaml.safe_dump({
        "data": {"tasks": "../utils/tasks.jsonl", "train": "../utils/train.txt"}, "task_info": "../utils",
        "agent": {"name": "../utils/agent/agent.py:A"}, "judge": {"path": "../utils/judge.py"},
        "template": "prompt-only@1.0", "evolver": {"name": "prompt_only"}}))
    config = Config.load(method / "run.yaml", [])
    assert project_root(config) == [(tmp_path / "ex").resolve()]


def test_a_profile_path_naming_an_unset_variable_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("HA_TEST_UNSET", raising=False)
    config = Config.load(make_task(tmp_path, sandbox={"profiles": {"rollout": {"read": ["$HA_TEST_UNSET/x"]}}}), [])
    with pytest.raises(ValueError, match="unset environment variable"):
        config_paths(config, "rollout", "read", profile(config.data["sandbox"], "rollout")["read"])
    monkeypatch.setenv("HA_TEST_UNSET", str(tmp_path))
    assert config_paths(config, "rollout", "read", ["$HA_TEST_UNSET/x"]) == [tmp_path / "x"]


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

def _try_write(path):
    try:
        with open(path, "a") as f:
            f.write("# tampered\n")
        return "WROTE"
    except Exception as e:
        return "ERR " + type(e).__name__

FORGE_PROC = """
import os
n = 0
for pid in os.listdir('/proc'):
    if not pid.isdigit() or int(pid) == os.getpid():
        continue
    try:
        fds = os.listdir(f'/proc/{pid}/fd')
    except OSError:
        continue
    for fd in fds:
        if fd == '0':       # the job's stdin, read to its end before the agent started: writing it changes nothing
            continue
        try:
            if os.readlink(f'/proc/{pid}/fd/{fd}').startswith('pipe:'):
                with open(f'/proc/{pid}/fd/{fd}', 'w') as f:
                    f.write('{"ok": true, "output": {"output": "FORGED"}}')
                n += 1
        except OSError:
            pass
print(n)
"""

def _host_marker_visible():
    for pid in os.listdir("/proc"):
        try:
            if pid.isdigit() and b"HA_HOST_MARKER" in open(f"/proc/{pid}/environ", "rb").read():
                return pid
        except OSError:
            pass
    return None

def run(task, adapter):
    here = Path(__file__).resolve().parent
    text = (Path(adapter) / "ADAPTER.md").read_text() if adapter else ""
    runs = here / "runs"
    seen = {"tasks": _peek(here / "tasks.jsonl"), "judge": _peek(here / "judge.py"), "test": _peek(here / "test.txt"),
            "runs": sorted(os.listdir(runs)) if runs.is_dir() else None,
            "result_fd_env": os.environ.get("HA_RESULT_FD"),
            "sibling_runs": _peek(here / "other_method" / "runs" / "r" / "feedback.txt"),
            "sibling_logs": _peek(here / "other_method" / ".logs" / "x.txt"),
            "write_code": _try_write(here / "pipeline.py"), "write_helper": _try_write(here / "helper.py"),
            "home": sorted(n for n in (".ssh", ".bashrc", ".codex", ".claude")
                           if os.path.lexists(os.path.join(task.get("home", "/nonexistent"), n))),
            "host_marker": _host_marker_visible()}
    forged_proc = subprocess.run([sys.executable, "-c", FORGE_PROC], capture_output=True, text=True).stdout.strip()
    # A child started without closing descriptors tries every one: the result pipe must not be among them.
    forged = subprocess.run([sys.executable, "-c",
        "import os\nn=0\nfor fd in range(3, 64):\n    try:\n        os.write(fd, b'{\"ok\": true, \"output\": {\"output\": \"FORGED\"}}'); n += 1\n    except OSError:\n        pass\nprint(n)"],
        capture_output=True, text=True, close_fds=False).stdout.strip()
    log = here / ".logs" / f"{task['id']}.txt"        # beside its own code, as real agents do
    log.parent.mkdir(exist_ok=True)
    log.write_text("ok")
    answer = str(task["a"] + task["b"]) if "add the numbers" in text else "?"
    return RunOutput(output=answer, log=log, cost_usd=0.01,
                     extra={"seen": seen, "forged_writes": forged, "forged_proc": forged_proc})
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
def test_a_rollout_sees_its_adapter_and_not_the_runs_secrets(tmp_path, monkeypatch):
    # Attacks from inside: another method's runs and logs beside this one (judge feedback carries the answers),
    # the agent's own code and a helper beside it, the host's processes (their environment), and the result pipe
    # through /proc.
    for rel, text in (("other_method/runs/r/feedback.txt", "expected '42'"), ("other_method/.logs/x.txt", "log"),
                      ("helper.py", "X = 1\n")):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    monkeypatch.setenv("HA_HOST_MARKER", "1")             # in the host's environment, not passed to the agent
    config = Config.load(sandboxed_task(tmp_path, profiles={"rollout": {"env": ["PYTHONPATH"]}}), [])
    code_before = (tmp_path / "pipeline.py").read_text() + (tmp_path / "helper.py").read_text()
    agent = SandboxedAgent(config, "rollout", records=tmp_path / "records")
    try:
        out = agent({"id": "t1", "a": 1, "b": 2, "home": str(Path.home())}, an_adapter(tmp_path))
    finally:
        agent.close()
    assert out.output == "3"                                          # the adapter was readable
    seen = out.extra["seen"]
    assert seen["tasks"] == "" and seen["judge"] == "" and seen["test"] == ""   # covered: they read as empty
    assert seen["runs"] == ["some-run"]                              # only the call's own adapter is under runs/
    assert seen["result_fd_env"] is None and out.extra["forged_writes"] == "0"
    assert out.extra["forged_proc"] == "0" and out.output != "FORGED"    # no pipe of another process reachable
    assert seen["sibling_runs"] in ("", "ERR FileNotFoundError") and seen["sibling_logs"] in ("", "ERR FileNotFoundError")
    assert seen["write_code"].startswith("ERR") and seen["write_helper"].startswith("ERR")   # code is read-only
    assert (tmp_path / "pipeline.py").read_text() + (tmp_path / "helper.py").read_text() == code_before
    # The home holds only the folders on the way to bound paths (the interpreter, the checkout), none of its own.
    assert seen["home"] == [] and seen["host_marker"] is None           # own process namespace
    assert (tmp_path / ".logs" / "t1.txt").read_text() == "ok"       # written beside the agent's code, kept
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


# --- the holes an independent review found, closed ------------------------------------------------------------------

def test_other_methods_run_records_in_the_project_are_covered(tmp_path):
    utils, method, other = tmp_path / "ex" / "utils", tmp_path / "ex" / "prompt_only", tmp_path / "ex" / "gepa"
    (utils / "agent" / ".logs").mkdir(parents=True)
    for d in (method / "runs", other / "runs" / "r", other / ".logs", tmp_path / "ex" / ".git" / "runs"):
        d.mkdir(parents=True)
    (method / "run.yaml").write_text(yaml.safe_dump({"data": {"tasks": "../utils/t.jsonl", "train": "../utils/s.txt"},
                                                     "agent": {"name": "../utils/agent/a.py:A"}, "judge": {"path": "../utils/j.py"},
                                                     "template": "prompt-only@1.0", "evolver": {"name": "prompt_only"}}))
    config = Config.load(method / "run.yaml", [])
    found = {p.relative_to(tmp_path / "ex").as_posix()
             for p in project_records([tmp_path / "ex"], config, keep=[utils / "agent" / ".logs"])}
    assert found == {"prompt_only/runs", "gepa/runs", "gepa/.logs"}          # not the agent's own logs, not .git


def test_every_package_is_shown_but_not_the_working_directory_or_the_home(tmp_path, monkeypatch):
    from harnessadapter.sandbox import _installed_paths
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), str(Path.home()), *sys.path])   # `python -m` from the home
    shown = {p.resolve() for p in _installed_paths()}
    assert tmp_path.resolve() not in shown and Path.home().resolve() not in shown
    assert not any(Path.home().resolve().is_relative_to(p) for p in shown)
    import site
    assert Path(site.getsitepackages()[0]).resolve() in shown


def test_the_isolation_check_keeps_a_secret_shown_by_accident(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    box = Sandbox(probe_absent=[home / ".ssh"])
    assert box._absent([str(home.resolve())]) == [str((home / ".ssh").resolve())]   # bound by accident: checked


def test_a_calls_path_holding_a_mask_goes_in_before_it(tmp_path, monkeypatch):
    """A tool's task directory holds the run records: bound after the mask, it would uncover them."""
    fake_bwrap(monkeypatch)
    base = tmp_path / "base"
    (base / "runs" / "r").mkdir(parents=True)
    box = Sandbox(network="open", masked=[base / "runs"])
    args, _ = box.wrap(["true"], readable=[base])
    b, runs = str(base.resolve()), str((base / "runs").resolve())
    bind_at = args.index(b)
    tmpfs_at = next(i for i, a in enumerate(args) if a == "--tmpfs" and args[i + 1] == runs)
    assert bind_at < tmpfs_at and args.count(b) == 2               # bound once (src and dest), before the mask
    box.close()


def test_the_sandbox_has_its_own_process_namespace(monkeypatch):
    fake_bwrap(monkeypatch)
    args, _ = Sandbox(network="open").wrap(["true"])
    assert "--unshare-pid" in args
