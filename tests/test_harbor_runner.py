"""The Harbor runner's host side without Docker or Harbor: the verdicts of a trial's result, the curated log, the
dataset rows, lock and splits, the judge, the adapter's staging, one trial through a fake `harbor` command, and
one stream step through a fake driver."""
import json
import os
from pathlib import Path
import socket
import stat
import sys
import textwrap
import threading

import pytest
import yaml

from hermit.harbor import curate, dataset, errors, judge, stream
from hermit.harbor.runner import HarborAgent
from hermit.task import RunOutput

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "hermit" / "algorithms" / "templates"


# ---- helpers -----------------------------------------------------------------------------------------------------

def write(path: Path, text: str, exe: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if exe:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def make_task(root: Path, name: str, *, steps: tuple[str, ...] = (), image: str | None = None) -> Path:
    toml = 'schema_version = "1.4"\n[metadata]\ndifficulty = "easy"\n[verifier]\ntimeout_sec = 30.0\n[agent]\ntimeout_sec = 45.0\n' \
           '[environment]\nbuild_timeout_sec = 120.0\n'
    if image:
        toml += f'docker_image = "{image}"\n'
    toml += "".join(f'\n[[steps]]\nname = "{s}"\n' for s in steps)
    task = root / name
    write(task / "task.toml", toml)
    write(task / "instruction.md", f"Do {name}.\n")
    write(task / "environment" / "Dockerfile", "FROM scratch\n")
    write(task / "tests" / "test.sh", "#!/bin/bash\necho 1 > /logs/verifier/reward.txt\n", exe=True)
    for s in steps:
        write(task / "steps" / s / "instruction.md", f"Step {s} of {name}.\n")
        write(task / "steps" / s / "tests" / "test.sh", "#!/bin/bash\necho 1 > /logs/verifier/reward.txt\n", exe=True)
    return task


def trial_result(reward=1.0, *, exc=None, tokens=(100, 20), metadata=None) -> dict:
    result = {"task_name": "t", "trial_name": "t__abc1234", "started_at": "2026-01-01T00:00:00Z",
              "finished_at": "2026-01-01T00:01:00Z",
              "agent_execution": {"started_at": "2026-01-01T00:00:10Z", "finished_at": "2026-01-01T00:00:50Z"},
              "agent_result": {"n_input_tokens": tokens[0], "n_output_tokens": tokens[1], "n_cache_tokens": 5,
                               "metadata": metadata or {}},
              "verifier_result": {"rewards": {"reward": reward}} if reward is not None else None,
              "exception_info": None}
    if exc:
        result["exception_info"] = {"exception_type": exc, "exception_message": f"{exc} happened",
                                    "exception_traceback": "Traceback..."}
    return result


FAKE_HARBOR = textwrap.dedent('''\
    #!/usr/bin/env python3
    """A stand-in for `harbor run -c job.yaml`: writes the trial directory a job would leave."""
    import json, os, sys, yaml
    from pathlib import Path
    args = sys.argv[1:]
    assert args[:3] == ["run", "-c", args[2]], args
    job = yaml.safe_load(Path(args[2]).read_text())
    jobs_dir, name = Path(job["jobs_dir"]), job["job_name"]
    task = Path(job["tasks"][0]["path"])
    trial = jobs_dir / name / f"{task.name}__abc1234"
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    (trial / "agent" / "codex.txt").write_text("the agent's output\\n")
    (trial / "agent" / "sessions").mkdir()
    (trial / "agent" / "sessions" / "rollout.jsonl").write_text("secret transcript\\n")
    (trial / "agent" / "trajectory.json").write_text(json.dumps({"steps": [{"metrics": {"a": 1}}, {"metrics": {}}, {}]}))
    (trial / "verifier" / "test-stdout.txt").write_text("PASS\\n")
    (trial / "verifier" / "reward.txt").write_text("1\\n")
    (trial / "job-seen.json").write_text(json.dumps({"env": {k: v for k, v in os.environ.items()
                                                             if k in ("PYTHONPATH", "TMPDIR", "CODEX_FORCE_AUTH_JSON")}}))
    mode = os.environ.get("FAKE_HARBOR_MODE", "ok")
    agent = job["agents"][0]
    result = {"task_name": task.name, "trial_name": trial.name, "started_at": "2026-01-01T00:00:00Z",
              "finished_at": "2026-01-01T00:01:00Z",
              "agent_execution": {"started_at": "2026-01-01T00:00:10Z", "finished_at": "2026-01-01T00:00:50Z"},
              "agent_result": {"n_input_tokens": 100, "n_output_tokens": 20, "n_cache_tokens": 5,
                               "metadata": {"hooks_active": "deploy_dir" in agent.get("kwargs", {}),
                                            "adapter_deployed": "deploy_dir" in agent.get("kwargs", {})}},
              "verifier_result": {"rewards": {"reward": 1.0}}, "exception_info": None}
    if mode == "usage_limit":
        result["exception_info"] = {"exception_type": "ApiUsageLimitError", "exception_message": "limit", "exception_traceback": "tb"}
        result["verifier_result"] = None
    elif mode == "hang":
        import time; time.sleep(30)
    (trial / "result.json").write_text(json.dumps(result))
    ''')


@pytest.fixture
def fake_harbor(tmp_path):
    """A `harbor` command and a `python` beside it (the runner derives the driver's interpreter from it)."""
    bin_dir = tmp_path / "harbor-venv" / "bin"
    harbor = write(bin_dir / "harbor", FAKE_HARBOR, exe=True)
    python = bin_dir / "python"
    python.symlink_to(sys.executable)
    return harbor


# ---- errors ------------------------------------------------------------------------------------------------------

def test_classify_reads_the_result():
    ok = errors.classify(trial_result(0.5))
    assert (ok["status"], ok["reward"], ok["failure_cause"], ok["halt"]) == ("ok", 0.5, None, False)
    timeout = errors.classify(trial_result(1.0, exc="AgentTimeoutError"))
    assert (timeout["status"], timeout["timed_out"], timeout["reward"]) == ("timeout", True, 1.0)
    infra = errors.classify(trial_result(None, exc="ApiUsageLimitError"))
    assert (infra["status"], infra["failure_cause"], infra["halt"]) == ("infra", "model_provider_failed", True)
    agent = errors.classify(trial_result(None, exc="NonZeroAgentExitCodeError"))
    assert (agent["status"], agent["failure_cause"]) == ("agent_error", "unclassified")
    unknown = errors.classify(trial_result(None, exc="SomethingNewError"))
    assert (unknown["status"], unknown["failure_cause"]) == ("agent_error", "unclassified")
    assert errors.classify(trial_result(None))["failure_cause"] == "verifier_failed"
    assert errors.classify(None, expired=True)["failure_cause"] == "harbor_timeout"
    assert errors.classify(None, harbor_exit=3)["exception_message"].startswith("harbor run exited 3")
    assert errors.classify(trial_result(0.0), reward_key="f1")["failure_cause"] == "verifier_failed"


# ---- curate ------------------------------------------------------------------------------------------------------

def test_curate_keeps_outputs_and_drops_sessions(tmp_path):
    job = tmp_path / "job"
    trial = job / "t__abc1234"
    write(trial / "agent" / "codex.txt", "out\n")
    write(trial / "agent" / "sessions" / "rollout.jsonl", "secret\n")
    write(trial / "agent" / "hook-state" / "state.json", "{}")
    write(trial / "verifier" / "test-stdout.txt", "x" * 25_000)
    write(trial / "verifier" / "reward.txt", "1\n")
    write(trial / "result.json", json.dumps(trial_result()))
    write(job / "harbor.log", "harbor\n")
    write(job / "job.yaml", "job_name: j\n")
    log = curate.curate(trial, job, errors.classify(trial_result(None, exc="VerifierTimeoutError")))
    names = sorted(str(p.relative_to(log)) for p in log.rglob("*") if p.is_file())
    assert names == ["agent/codex.txt", "agent/hook-state/state.json", "exception.txt", "harbor.log", "job.yaml",
                     "result.json", "verifier/reward.txt", "verifier/test-stdout.txt"]
    text = (log / "verifier" / "test-stdout.txt").read_text()
    assert text.startswith("[... 5000 characters dropped ...]") and len(text) < 25_100
    assert "VerifierTimeoutError" in (log / "exception.txt").read_text()
    assert curate.seconds(trial_result()["agent_execution"]) == 40.0
    write(trial / "agent" / "trajectory.json", json.dumps({"steps": [{"metrics": {"a": 1}}, {}]}))
    assert curate.model_calls(trial / "agent" / "trajectory.json") == 1


# ---- dataset -----------------------------------------------------------------------------------------------------

def test_dataset_rows_lock_and_splits(tmp_path):
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    make_task(tasks, "beta", steps=("s1", "s2"))
    rows = dataset.index(tasks)
    assert [r["id"] for r in rows] == ["alpha", "beta:s1", "beta:s2"]
    alpha, s2 = rows[0], rows[2]
    assert alpha["harbor_task"] == "alpha" and alpha["docker_image"] == "example/alpha:1" and alpha["rollout"] == 1
    assert (alpha["agent_timeout_sec"], alpha["verifier_timeout_sec"], alpha["build_timeout_sec"]) == (45.0, 30.0, 120.0)
    assert alpha["instruction"] == "Do alpha." and "step" not in alpha
    assert (s2["step"], s2["step_index"], s2["steps"], s2["instruction"]) == ("s2", 2, 2, "Step s2 of beta.")
    two = dataset.index(tasks, rollouts=2)
    assert [r["id"] for r in two][:4] == ["alpha#1", "alpha#2", "beta:s1#1", "beta:s2#1"]
    assert dataset.index(tasks, include=["beta"])[0]["id"] == "beta:s1"
    with pytest.raises(ValueError):
        dataset.index(tasks, include=["gamma"])
    lock = dataset.lock_of(tasks, source={"ref": "local"})
    assert set(lock["tasks"]) == {"alpha", "beta"} and lock["source"] == {"ref": "local"}
    lock_path = tmp_path / "dataset.lock"
    lock_path.write_text(json.dumps(lock))
    assert dataset.check_lock(tasks, lock_path) == []
    (tasks / "alpha" / "tests" / "test.sh").write_text("#!/bin/bash\necho 0 > /logs/verifier/reward.txt\n")
    make_task(tasks, "gamma")
    problems = dataset.check_lock(tasks, lock_path)
    assert problems == ["task not in the lock: gamma", "task changed since the lock: alpha"]
    parts = dataset.split(tasks, train=2, test=1, seed=3)
    assert sorted(parts["train"] + parts["test"]) == ["alpha", "beta", "gamma"]
    assert dataset.ids_for(dataset.index(tasks), ["beta"]) == ["beta:s1", "beta:s2"]
    out = tmp_path / "tasks.jsonl"
    assert dataset.main(["index", str(tasks), "--out", str(out), "--rollouts", "2"]) == 0
    assert len(out.read_text().splitlines()) == 8
    assert dataset.main(["check", str(tasks), "--lock", str(lock_path)]) == 1


# ---- judge -------------------------------------------------------------------------------------------------------

def test_judge_scores_reward_and_names_failures(tmp_path):
    passed = RunOutput(output={"status": "ok", "reward": 1.0})
    assert judge.score({}, passed) == {"score": 1.0, "feedback": "passed", "status": "ok", "rewards": None}
    log = tmp_path / "log"
    write(log / "verifier" / "test-stdout.txt", "FAIL: expected 3 got 2\n")
    failed = RunOutput(output={"status": "ok", "reward": 0.0, "rewards": {"reward": 0.0}}, log=log)
    verdict = judge.score({}, failed)
    assert verdict["score"] == 0.0 and "FAIL: expected 3 got 2" in verdict["feedback"]
    timeout = RunOutput(output={"status": "timeout", "failure_cause": "agent_exhausted_budget",
                                "exception_type": "AgentTimeoutError", "reward": 1.0})
    verdict = judge.score({}, timeout)
    assert verdict["score"] == 0.0 and "time budget" in verdict["feedback"] and "scored 1" in verdict["feedback"]
    assert judge.score({}, RunOutput(output=None))["feedback"].startswith("infra")


# ---- staging -----------------------------------------------------------------------------------------------------

def simple_template() -> Path:
    """A built-in template without tools or a context tool: an adapter the staging can deploy."""
    for candidate in sorted(TEMPLATES.iterdir()):
        exports = candidate / "exports.yaml"
        if not exports.is_file():
            continue
        declared = yaml.safe_load(exports.read_text()) or {}
        if not declared.get("tools") and not declared.get("context_tool") and not declared.get("hooks"):
            return candidate
    pytest.skip("no built-in template without tools")


def test_stage_deploys_the_adapter_for_the_container(tmp_path):
    from hermit.harbor.staging import stage
    adapter = simple_template()
    info = stage(adapter, tmp_path / "job", sys.executable, "best_effort")
    deploy = tmp_path / "job" / "deploy"
    assert info["adapter_id"] and (deploy / "AGENTS.md").is_file() and info["deploy"] == str(deploy)
    assert (deploy / "hermit" / info["adapter_id"]).is_dir() and info["adapter_dir"] == f"hermit/{info['adapter_id']}"
    assert info["hooks"] is False and not (deploy / ".codex" / "hooks.json").exists()
    assert json.loads((tmp_path / "job" / "staging.json").read_text())["adapter_id"] == info["adapter_id"]
    assert stage(None, tmp_path / "none", sys.executable) is None


def test_stage_rewrites_hooks_relative_to_the_workspace(tmp_path):
    import shutil
    from hermit.harbor.staging import stage
    adapter = tmp_path / "adapter"
    shutil.copytree(simple_template(), adapter)
    exports = adapter / "exports.yaml"
    declared = yaml.safe_load(exports.read_text()) or {}
    (adapter / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": "python3 scripts/start.py"}]}]}}))
    declared["hooks"] = "hooks.json"
    exports.write_text(yaml.safe_dump(declared))
    info = stage(adapter, tmp_path / "job", sys.executable, "best_effort")
    hooks = json.loads((tmp_path / "job" / "deploy" / ".codex" / "hooks.json").read_text())
    command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert info["hooks"] is True and command.startswith(f"cd hermit/{info['adapter_id']} || exit 1\n")
    assert "python3 scripts/start.py" in command and str(tmp_path) not in command


# ---- the runner: one trial through the fake harbor ------------------------------------------------------------------

def make_agent(tmp_path, fake_harbor, tasks, **params) -> HarborAgent:
    return HarborAgent({"tasks_dir": str(tasks), "jobs_dir": str(tmp_path / "jobs"), "harbor": str(fake_harbor),
                        "disk_gate_gb": 0, **params}, base=tmp_path)


def test_runner_is_a_builtin_and_validates_params(tmp_path, fake_harbor):
    from hermit.agents.base import BUILTIN_NAMES, load_agent
    assert "harbor" in BUILTIN_NAMES
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    agent = load_agent("harbor", {"tasks_dir": str(tasks), "harbor": str(fake_harbor)}, tmp_path)
    assert isinstance(agent, HarborAgent) and agent.tasks_dir == tasks
    with pytest.raises(ValueError, match="tasks_dir"):
        HarborAgent({}, tmp_path)
    with pytest.raises(ValueError, match="docker_cleanup"):
        HarborAgent({"tasks_dir": str(tasks), "docker_cleanup": "prune"}, tmp_path)
    with pytest.raises(ValueError, match="both a name and an import_path"):
        HarborAgent({"tasks_dir": str(tasks), "acting_agent": {"name": "codex", "import_path": "x:Y"}}, tmp_path)


def test_runner_merges_a_benchmark_file_under_params(tmp_path, fake_harbor):
    tasks = tmp_path / "utils" / "tasks"
    make_task(tasks, "alpha")
    write(tmp_path / "utils" / "benchmark.yaml", yaml.safe_dump({
        "tasks_dir": "tasks", "model": "gpt-5.6-luna", "effort": "medium", "docker_cleanup": "rmi_over_gb",
        "acting_agent": {"kwargs": {"version": "0.164.0"}, "env": {"A": "1"}}}))
    agent = HarborAgent({"benchmark": "../utils/benchmark.yaml", "effort": "high", "harbor": str(fake_harbor),
                         "acting_agent": {"env": {"B": "2"}}}, base=tmp_path / "method")
    assert agent.tasks_dir == tasks.resolve() and agent.params["model"] == "gpt-5.6-luna"
    assert agent.params["effort"] == "high" and agent.docker_cleanup == "rmi_over_gb"
    assert agent.acting_agent == {"kwargs": {"version": "0.164.0"}, "env": {"B": "2"}, "import_path": "harborside.codex:HermitCodex"}


def test_job_config_binds_model_timeout_and_login(tmp_path, fake_harbor):
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks, model="gpt-5.6-luna", effort="medium", codex_version="0.164.0",
                       agent_timeout_sec=3600, retry={"max_retries": 1}, verifier={"override_timeout_sec": 900})
    config = agent.job_config(row, "alpha-1", agent.agent_config(row, None, None))
    harbor_agent = config["agents"][0]
    assert harbor_agent["import_path"] == "harborside.codex:HermitCodex" and harbor_agent["model_name"] == "gpt-5.6-luna"
    assert harbor_agent["override_timeout_sec"] == 3600 and harbor_agent["env"] == {"CODEX_FORCE_AUTH_JSON": "1"}
    assert harbor_agent["kwargs"] == {"reasoning_effort": "medium", "version": "0.164.0"}
    assert config["tasks"] == [{"path": str(tasks / "alpha")}] and config["n_concurrent_trials"] == 1
    assert config["retry"] == {"max_retries": 1} and config["verifier"] == {"override_timeout_sec": 900}
    assert config["environment"] == {"delete": True}
    # A bound backend: Harbor gets the endpoint and the key's variable; no login upload.
    bound = make_agent(tmp_path, fake_harbor, tasks, model={"model": "glm", "base_url": "https://x/v1", "api_key_env": "X_KEY"})
    cfg = bound.agent_config(row, None, None)
    assert (cfg["model_name"], cfg["base_url"], cfg["model_api_key_name"]) == ("glm", "https://x/v1", "X_KEY")
    assert "env" not in cfg
    # The wait budget: agent + setup + verifier + teardown, plus the build for a Dockerfile task.
    assert agent.wait_budget(row) == 3600 + 360 + 30 + 600
    make_task(tasks, "built")
    built = dataset.index(tasks, include=["built"])[0]
    assert make_agent(tmp_path, fake_harbor, tasks).wait_budget(built) == 45 + 360 + 30 + 120 + 600
    # Another acting agent, no adapter possible.
    other = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"name": "mini-swe-agent", "kwargs": {"k": 1}})
    assert other.agent_config(row, None, None) == {"name": "mini-swe-agent", "kwargs": {"k": 1}}
    with pytest.raises(ValueError, match="takes no adapter"):
        other.agent_config(row, tmp_path, {"hooks": False})


def test_one_trial_through_the_fake_harbor(tmp_path, fake_harbor, monkeypatch):
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks, model="m")
    out = agent.run(row, None)
    assert out.output["status"] == "ok" and out.output["reward"] == 1.0 and out.output["trial"] == "alpha__abc1234"
    assert out.tokens == 120 and out.wall_s == 40.0 and out.api_speed == {"model_calls": 1, "status": "ok"}
    assert out.output["hooks_active"] is False and out.extra["harbor_exit"] == 0 and out.extra["deployed"] is None
    log = Path(out.log)
    assert (log / "agent" / "codex.txt").is_file() and not (log / "agent" / "sessions").exists()
    job = Path(out.extra["harbor_job"])
    seen = json.loads(next(job.glob("alpha__*/job-seen.json")).read_text())["env"]
    assert seen["PYTHONPATH"].split(os.pathsep)[0] == str(ROOT / "hermit" / "harbor")
    assert seen["TMPDIR"] == str(tmp_path / "jobs" / "tmp") and seen["CODEX_FORCE_AUTH_JSON"] == "1"
    assert yaml.safe_load((job / "job.yaml").read_text())["agents"][0]["model_name"] == "m"
    assert judge.score(row, out)["score"] == 1.0
    # With an adapter: staged and named in the job.
    with_adapter = agent.run(row, simple_template())
    kwargs = yaml.safe_load((Path(with_adapter.extra["harbor_job"]) / "job.yaml").read_text())["agents"][0]["kwargs"]
    assert kwargs["deploy_dir"].endswith("/deploy") and Path(kwargs["deploy_dir"], "AGENTS.md").is_file()
    assert with_adapter.output["hooks_active"] is True and with_adapter.extra["deployed"]["adapter_id"]
    # A usage limit halts the run: the next rollout returns without a trial.
    monkeypatch.setenv("FAKE_HARBOR_MODE", "usage_limit")
    halted = agent.run(row, None)
    assert halted.output["status"] == "infra" and halted.extra["halt"] is True and agent.halt_file().is_file()
    monkeypatch.setenv("FAKE_HARBOR_MODE", "ok")
    after = agent.run(row, None)
    assert after.output["failure_cause"] == "halted" and after.log is None and after.wall_s == 0.0
    agent.halt_file().unlink()
    # The wait budget kills a hung harbor.
    monkeypatch.setenv("FAKE_HARBOR_MODE", "hang")
    monkeypatch.setattr("hermit.harbor.docker.compose_down", lambda name, **k: None)
    hung = make_agent(tmp_path, fake_harbor, tasks, wait_budget_s=2).run(row, None)
    assert hung.output["status"] == "infra" and hung.output["failure_cause"] == "harbor_timeout" and hung.extra["timed_out"]


# ---- the runner: one stream step through a fake driver ---------------------------------------------------------------

FAKE_DRIVER = textwrap.dedent('''\
    #!/usr/bin/env python3
    """A stand-in for harborside.driver: the protocol, no Harbor. Steps succeed; step "s2" aborts the stream."""
    import argparse, json, os, socket, sys, threading
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument("-m"); parser.add_argument("--task-dir"); parser.add_argument("--stream-dir")
    parser.add_argument("--name"); parser.add_argument("--socket"); parser.add_argument("--agent-json")
    parser.add_argument("--environment-json"); parser.add_argument("--verifier-json"); parser.add_argument("--idle-s")
    args, _ = parser.parse_known_args()
    stream_dir, sock_path = Path(args.stream_dir), args.socket
    agent = json.loads(args.agent_json)
    state = {"next": 1, "finished": False, "aborted": None}
    (stream_dir / "seen-agent.json").write_text(json.dumps(agent))
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path); srv.listen(8)
    (stream_dir / "driver.json").write_text(json.dumps({"pid": os.getpid(), "socket": sock_path, "trial_dir": str(stream_dir / "trials" / args.name)}))
    def status():
        return {"ok": True, "ready": True, "error": None, "next_index": state["next"], "running": None,
                "finished": state["finished"], "aborted": state["aborted"]}
    while not state["finished"]:
        conn, _ = srv.accept()
        req = json.loads(conn.makefile().readline() or "{}")
        if req["op"] == "status":
            reply = status()
        elif req["op"] == "stop":
            state["finished"] = True; reply = {"ok": True, "stopped": True}
        elif req["op"] == "step":
            if req["index"] != state["next"]:
                reply = {"ok": False, "out_of_order": True, "next_index": state["next"]}
            else:
                step_dir = stream_dir / "trials" / args.name / "steps" / req["name"]
                (step_dir / "agent").mkdir(parents=True, exist_ok=True); (step_dir / "verifier").mkdir(exist_ok=True)
                (step_dir / "verifier" / "test-stdout.txt").write_text("PASS\\n")
                (step_dir / "agent" / "deploy-seen.txt").write_text(str(req.get("deploy_dir")))
                aborted = req["name"] == "s2"
                result = {"step_name": req["name"], "agent_result": {"n_input_tokens": 10, "n_output_tokens": 2, "metadata": {}},
                          "verifier_result": None if aborted else {"rewards": {"reward": 1.0}},
                          "exception_info": {"exception_type": "RuntimeError", "exception_message": "boom"} if aborted else None,
                          "agent_execution": {"started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:05Z"}}
                state["next"] = req["index"] + 1
                if aborted: state["aborted"] = "step s2 failed"
                reply = {"ok": True, "index": req["index"], "step": result, "aborted": state["aborted"],
                         "paths": {"trial_dir": str(stream_dir / "trials" / args.name), "step_dir": str(step_dir)},
                         "last": req["index"] >= 3}
        else:
            reply = {"ok": False, "error": "unknown op"}
        conn.sendall((json.dumps(reply) + "\\n").encode()); conn.close()
    os.unlink(sock_path)
    ''')


def test_stream_steps_through_a_fake_driver(tmp_path, fake_harbor, monkeypatch):
    bin_dir = fake_harbor.parent
    (bin_dir / "python").unlink()
    write(bin_dir / "python", FAKE_DRIVER, exe=True)          # the "interpreter" the driver is started with
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("s1", "s2", "s3"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, model="m",
                       stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
    first = agent.run(rows["flow:s1"], simple_template())
    assert first.output["status"] == "ok" and first.output["reward"] == 1.0 and first.output["step"] == "s1"
    assert first.output["trial"] == "flow__s1" and first.tokens == 12 and first.wall_s == 5.0
    stream_dir = tmp_path / "jobs" / "streams" / "flow__s1"
    seen = json.loads((stream_dir / "seen-agent.json").read_text())
    assert seen["kwargs"]["deploy_dir"] == str(stream_dir / "steps" / "001-s1" / "deploy") and seen["model_name"] == "m"
    assert (Path(first.log) / "verifier" / "test-stdout.txt").read_text() == "PASS\n"
    assert (Path(first.log) / "agent" / "deploy-seen.txt").read_text().endswith("001-s1/deploy")
    assert stream.StreamClient(stream_dir, python=str(bin_dir / "python")).running() is not None
    # Out of order: step 3 before step 2 waits for its turn, then gives up; the stream goes on.
    skipped = agent.run(rows["flow:s3"], None)
    assert skipped.output["status"] == "infra" and skipped.output["failure_cause"] == "stream_order"
    assert "in order" in skipped.extra["exception_message"]
    assert stream.StreamClient(stream_dir, python=str(bin_dir / "python")).running() is not None
    # Step 2 fails: the stream aborts and the driver is stopped.
    second = agent.run(rows["flow:s2"], None)
    assert second.output["status"] == "agent_error" and second.extra["aborted"] is True
    assert "RuntimeError" in (Path(second.log) / "exception.txt").read_text()
    client = stream.StreamClient(stream_dir, python=str(bin_dir / "python"))
    deadline = __import__("time").time() + 10
    while client.running() is not None and __import__("time").time() < deadline:
        __import__("time").sleep(0.2)
    assert client.running() is None
    # A sandboxed worker may not start a driver.
    monkeypatch.setenv("HA_ADAPTER_SANDBOX", "1")
    blocked = agent.run(rows["flow:s3"], None)
    assert blocked.output["failure_cause"] == "stream_lost" and "sandboxed rollout" in blocked.extra["exception_message"]


def test_socket_path_is_short(tmp_path, monkeypatch):
    short = tmp_path / "r"
    short.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
    path = stream.socket_path(tmp_path / ("x" * 200))
    assert len(str(path)) <= 100 and path.parent.is_dir()
    assert path.is_relative_to(short) if len(str(short)) < 60 else not path.is_relative_to(short)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / ("y" * 120)))     # too long: another base is taken
    (tmp_path / ("y" * 120)).mkdir()
    assert len(str(stream.socket_path(tmp_path))) <= 100
