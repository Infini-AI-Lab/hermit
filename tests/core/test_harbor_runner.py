"""The Harbor runner's host side without Docker or Harbor: the verdicts of a trial's result and of the CLI's stream,
the curated log, the dataset rows, lock and splits, the judge, the adapter's staging, the startup checks, one trial
through a fake `harbor` command that carries out the launch plan as harborside would, the answer agent's model on
this machine, the adapter's tools served for a container, and stream steps through a fake driver."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import shutil
import socket
import stat
import sys
import textwrap
import threading
import time
import urllib.request
import uuid

import pytest
import yaml

from hermit.agent.base.interface import LoginFailed
from hermit.harbor import curate, dataset, docker, errors, judge, stream
from hermit.harbor import runner as runner_module
from hermit.harbor.runner import HarborAgent
from hermit.task import RunOutput

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "hermit" / "adapter" / "templates"
KEY_ENV = "FAKE_MODEL_KEY"
KEY = "fake-model-key-0123456789abcdef"                 # never a real key: what a plan must never hold
# A model bound as the config's loading binds it (hermit.agent.model.backends.bind_agent_params): its id, and the
# whole spec under `binding`
BINDING = {"ref": "fake/glm-5", "backend": "fake", "model": "glm-5", "base_url": "https://models.example/v1",
           "api_key_env": KEY_ENV, "key_required": True, "apis": ["chat", "responses"],
           "price": {"input": 1.0, "cached_input": 0.5, "output": 2.0}}


# ---- helpers -----------------------------------------------------------------------------------------------------

def write(path: Path, text: str, exe: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if exe:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def make_task(root: Path, name: str, *, steps: tuple[str, ...] = (), image: str | None = None,
              network: str | None = None) -> Path:
    toml = 'schema_version = "1.4"\n[metadata]\ndifficulty = "easy"\n[verifier]\ntimeout_sec = 30.0\n[agent]\ntimeout_sec = 45.0\n' \
           '[environment]\nbuild_timeout_sec = 120.0\n'
    if image:
        toml += f'docker_image = "{image}"\n'
    if network:
        toml += f'network_mode = "{network}"\n'
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


# A Codex session's stream (hermit.agent.base.cli.Codex reads it): one turn served, its usage
CODEX_STREAM = ('{"type":"thread.started","thread_id":"th-1"}\n'
                '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"all done"}}\n'
                '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":20}}\n')

FAKE_HARBOR = textwrap.dedent('''\
    #!/usr/bin/env python3
    """A stand-in for `harbor run -c job.yaml`: the trial directory a job leaves, with the agent's outputs as harborside
    leaves them from the launch plan (the CLI's stream, the files it collects, a sign-in handed back to this machine;
    the answer agent's one call to the endpoint its plan names)."""
    import json, os, sys, time, urllib.error, urllib.request, yaml
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    args = sys.argv[1:]
    assert args[:3] == ["run", "-c", args[2]], args
    job = yaml.safe_load(Path(args[2]).read_text())
    jobs_dir, name = Path(job["jobs_dir"]), job["job_name"]
    task = Path(job["tasks"][0]["path"])
    trial = jobs_dir / name / f"{task.name[:32].rstrip('_-')}__abc1234"   # as Harbor names a trial
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    agent = job["agents"][0]
    plan_path = (agent.get("kwargs") or {}).get("plan")
    plan = json.loads(Path(plan_path).read_text()) if plan_path else None
    (trial / "agent" / "codex.txt").write_text("the agent's output\\n")
    (trial / "agent" / "sessions").mkdir()
    (trial / "agent" / "sessions" / "rollout.jsonl").write_text("secret transcript\\n")
    (trial / "agent" / "trajectory.json").write_text(json.dumps({"steps": [{"metrics": {"a": 1}}, {"metrics": {}}, {}]}))
    (trial / "verifier" / "test-stdout.txt").write_text("PASS\\n")
    (trial / "verifier" / "reward.txt").write_text("1\\n")
    names = list((plan or {}).get("env_from_host", {}).values()) + list((plan or {}).get("env_by_fd", {}))
    (trial / "job-seen.json").write_text(json.dumps({
        "env": {k: v for k, v in os.environ.items() if k in ("PYTHONPATH", "TMPDIR")},
        "token": "CLAUDE_CODE_OAUTH_TOKEN" in os.environ,
        "by_name": sorted(n for n in names if os.environ.get(n))}))
    mode = os.environ.get("FAKE_HARBOR_MODE", "ok")
    if plan is not None and plan.get("command") and mode != "no_environment":
        streams = [x for x in os.environ.get("FAKE_STREAMS", "").split(",") if x]
        if streams:                               # the session's stream, one per call (the last repeats)
            count = jobs_dir / ".fake-calls"
            n = int(count.read_text()) if count.exists() else 0
            count.write_text(str(n + 1))
            text = Path(streams[min(n, len(streams) - 1)]).read_text()
        else:
            text = os.environ["FAKE_DEFAULT_STREAM"] if mode != "silent" else "bash: codex: command not found\\n"
        (trial / "agent" / plan["stream"]).write_text(text)
        if os.environ.get("FAKE_LINK_STREAM_TO"):        # the session left a link in its stream's place
            (trial / "agent" / plan["stream"]).unlink()
            (trial / "agent" / plan["stream"]).symlink_to(os.environ["FAKE_LINK_STREAM_TO"])
        collect = trial / "agent" / "hermit-collect"
        collect.mkdir()
        for item in plan["collect"]:
            if item["to"] == "last_message.txt" and mode != "silent":
                (collect / item["to"]).write_text("all done\\n")
            elif item["to"] == "sessions" and os.environ.get("FAKE_RATE_LIMITS"):   # Codex's session log
                day = collect / "sessions" / "2026" / "11" / "11"
                day.mkdir(parents=True)
                limits = json.loads(os.environ["FAKE_RATE_LIMITS"])
                (day / "rollout-1.jsonl").write_text(json.dumps({
                    "timestamp": datetime.now(timezone.utc).isoformat(), "type": "event_msg",
                    "payload": {"type": "token_count", "rate_limits": limits}}) + "\\n")
        for item in plan["return_files"]:         # what the container hands back: the sign-in Codex refreshed
            refreshed = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()   # (stand-ins), or another file
            Path(item["to"]).write_text(os.environ.get("FAKE_RETURNED") or json.dumps({
                "last_refresh": refreshed, "tokens": {"account_id": "acct-1", "access_token": "at-stand-in",
                                                      "refresh_token": "rt-rotated-stand-in"}}))
    elif plan is not None and plan.get("answer"):   # the answer agent: one call to the endpoint its plan names
        block = plan["answer"]
        body = json.dumps({"model": block["model"], "messages": [{"role": "system", "content": block["system"]},
                                                                  {"role": "user", "content": "What is 6 x 7?"}]})
        request = urllib.request.Request(block["endpoint"] + "/chat/completions", data=body.encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            reply = json.loads(urllib.request.urlopen(
                request, timeout=float(os.environ.get("FAKE_ANSWER_WAIT_S", "30"))).read())
        except TimeoutError:                      # no answer within the agent's time (FAKE_HARBOR_MODE timeout)
            reply = None
        except urllib.error.HTTPError:            # the model failed: the agent exits non-zero (harborside.answer)
            reply, mode = None, "agent_exit"
        if reply is not None:
            said = reply["choices"][0]["message"]["content"]
            (trial / "agent" / "answer.json").write_text(json.dumps({"reply": said, "answer": said.split()[-1],
                                                                     "usage": reply.get("usage"),
                                                                     "model": block["model"]}))
    deploy = (plan or {}).get("deploy") or {}
    result = {"task_name": task.name, "trial_name": trial.name, "started_at": "2026-01-01T00:00:00Z",
              "finished_at": "2026-01-01T00:01:00Z",
              "agent_execution": {"started_at": "2026-01-01T00:00:10Z", "finished_at": "2026-01-01T00:00:50Z"},
              "agent_result": {"n_input_tokens": 100, "n_output_tokens": 20, "n_cache_tokens": 5,
                               "metadata": {"hooks_active": bool(deploy.get("hooks")),
                                            "adapter_deployed": bool(deploy)}},
              "verifier_result": {"rewards": {"reward": 1.0}}, "exception_info": None}
    if mode == "usage_limit":
        result["exception_info"] = {"exception_type": "ApiUsageLimitError", "exception_message": "limit", "exception_traceback": "tb"}
        result["verifier_result"] = None
    elif mode == "hang":
        time.sleep(30)
    elif mode == "no_tools":                      # the verifier's uv download failed (GitHub unreachable): no test ran
        (trial / "verifier" / "test-stdout.txt").write_text(
            "curl: (22) The requested URL returned error: 504\\n"
            "failed to download https://github.com/astral-sh/uv/releases/download/0.9.5/"
            "uv-x86_64-unknown-linux-gnu.tar.gz\\n"
            "/tests/test.sh: line 9: /root/.local/bin/env: No such file or directory\\n"
            "/tests/test.sh: line 18: uvx: command not found\\n")
        result["verifier_result"] = {"rewards": {"reward": 0.0}}
    elif mode in ("silent", "agent_exit"):        # the CLI exited non-zero: Harbor's error, the verifier still ran
        result["exception_info"] = {"exception_type": "NonZeroAgentExitCodeError", "exception_message": "exit 1",
                                    "exception_traceback": "tb"}
        result["verifier_result"] = {"rewards": {"reward": 0.5}}
    elif mode == "no_environment":                # the environment never started: no agent ran, no verifier
        result.pop("agent_execution")
        result["agent_result"] = result["verifier_result"] = None
        result["exception_info"] = {"exception_type": "EnvironmentStartTimeoutError",
                                    "exception_message": "start timed out", "exception_traceback": "tb"}
    elif mode == "timeout_no_tools":              # the agent ran out of its time; the verifier's uv download failed
        (trial / "verifier" / "test-stdout.txt").write_text(
            "curl: (22) The requested URL returned error: 504\\n"
            "failed to download https://github.com/astral-sh/uv/releases/download/0.9.5/"
            "uv-x86_64-unknown-linux-gnu.tar.gz\\n"
            "/tests/test.sh: line 9: /root/.local/bin/env: No such file or directory\\n"
            "/tests/test.sh: line 18: uvx: command not found\\n")
        result["verifier_result"] = {"rewards": {"reward": 0.0}}
        result["exception_info"] = {"exception_type": "AgentTimeoutError", "exception_message": "Agent timed out",
                                    "exception_traceback": "tb"}
    elif mode == "timeout":                       # the agent ran out of its time; the verifier still ran, and passed
        result["exception_info"] = {"exception_type": "AgentTimeoutError", "exception_message": "Agent timed out",
                                    "exception_traceback": "tb"}
    (trial / "result.json").write_text(json.dumps(result))
    ''')


REAL_SANDBOX_FAULT = runner_module.sandbox_fault


@pytest.fixture(autouse=True)
def sandbox_starts(monkeypatch):
    """This machine's strict adapter sandbox taken as starting (sandbox_fault), so that a runner's startup and the
    verdict of a staging that failed need no working bubblewrap, as main's tests skip what needs one; the tests of that
    check run the real one (REAL_SANDBOX_FAULT)."""
    monkeypatch.setattr(runner_module, "sandbox_fault", lambda enforce, *, fresh=True: None)


@pytest.fixture
def fake_harbor(tmp_path, monkeypatch):
    """A `harbor` command and a `python` beside it (the runner derives the driver's interpreter from it); the model's
    key set (a plan holds its name, never its value)."""
    bin_dir = tmp_path / "harbor-venv" / "bin"
    harbor = write(bin_dir / "harbor", FAKE_HARBOR, exe=True)
    python = bin_dir / "python"
    python.symlink_to(sys.executable)
    monkeypatch.setenv(KEY_ENV, KEY)
    monkeypatch.setenv("FAKE_DEFAULT_STREAM", CODEX_STREAM)
    return harbor


def make_agent(tmp_path, fake_harbor, tasks, **params) -> HarborAgent:
    """A runner for HermitCodex on the bound model, web search off, an infra trial not run again (a config's params
    override these)."""
    return HarborAgent({"tasks_dir": str(tasks), "jobs_dir": str(tmp_path / "jobs"), "harbor": str(fake_harbor),
                        "disk_gate_gb": 0, "tmp_gate_gb": 0, "model": BINDING["model"], "binding": BINDING,
                        "web_search": False, "infra_retries": 0, **params}, base=tmp_path)


def cli_answer_agent(tmp_path, fake_harbor, tasks, model: dict, **params) -> HarborAgent:
    """A runner for the answer agent on a model a CLI reaches, given where it must be, in the benchmark file (the
    runner serves it itself; a run config's own is the pipeline's to serve, refused)."""
    bench = write(tmp_path / "bench" / "benchmark.yaml", yaml.safe_dump({"model": model}))
    return HarborAgent({"tasks_dir": str(tasks), "jobs_dir": str(tmp_path / "jobs"), "harbor": str(fake_harbor),
                        "disk_gate_gb": 0, "tmp_gate_gb": 0, "benchmark": str(bench), "acting_agent": {"cli": "answer"},
                        "web_search": None, "infra_retries": 0, **params}, base=tmp_path)


def chatgpt_login(root: Path, account: str = "acct-1") -> Path:
    """The user's Codex sign-in where the test's CODEX_HOME points (tests/conftest.py): a stand-in, no real token,
    refreshed two days ago."""
    from datetime import datetime, timedelta, timezone
    when = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    return write(Path(os.environ["CODEX_HOME"]) / "auth.json",
                 json.dumps({"last_refresh": when, "tokens": {"account_id": account, "refresh_token": "rt-stand-in"}}))


def stream_name(agent: HarborAgent, task: str = "flow", rollout: int = 1) -> str:
    """The name of a multi-step task's stream in `agent`'s runs: its task, its rollout and the runner's own id (one
    runner's streams are its own, never another's on the same jobs directory)."""
    return f"{task}__s{rollout}__{agent.run_id}"


def step_dirs(stream_dir: Path, index: int, name: str) -> list[Path]:
    """The directories of a stream's step `index` (`name`), one per rollout of it, oldest first."""
    return sorted((Path(stream_dir) / "steps").glob(f"{index:03d}-{name}-*"), key=lambda d: d.stat().st_mtime_ns)


def unscored_record(record: dict, cause: str) -> None:
    """A rollout's record (hermit.trainer.rollout.run_one) of a trial that is not the agent's result, for `cause`: the
    judge refused to score it (hermit.harbor.judge.Unscored), so the run records a failed run (judge_error, scored 0,
    as `hermit eval` and the Trainer count one), never a 0 the agent earned."""
    assert (record["status"], record["judge"]["score"]) == ("judge_error", 0.0), record["status"]
    assert record["error"].startswith(f"Unscored: the trial is not the agent's result ({cause}"), record["error"]
    assert record["pipeline_extra"]["unscored"] == cause


def job_files(job: Path) -> str:
    """Every text file of a job directory, joined: what must never hold a secret."""
    return "\n".join(p.read_text(errors="replace") for p in Path(job).rglob("*") if p.is_file() and not p.is_symlink())


# ---- errors ------------------------------------------------------------------------------------------------------

def test_verifier_unreached_needs_the_failed_download_and_the_missing_runner():
    failed = ("curl: (22) The requested URL returned error: 504\n"
              "failed to download https://github.com/astral-sh/uv/releases/download/0.9.5/uv.tar.gz\n"
              "/tests/test.sh: line 9: /root/.local/bin/env: No such file or directory\n"
              "/tests/test.sh: line 18: uvx: command not found\n")
    assert errors.verifier_unreached(failed)
    # A test's own failed request is the agent's; a failed download with the runner already there ran the tests.
    assert not errors.verifier_unreached("curl: (7) Failed to connect to localhost port 8080\nFAILED test_server\n")
    assert not errors.verifier_unreached("failed to download https://github.com/x/y.tar.gz\n=== 3 passed in 1.2s ===\n")
    # The host's disk full while the verifier ran.
    assert errors.verifier_disk_full("  Caused by: No space left on device (os error 28)\n")
    assert not errors.verifier_disk_full("=== 3 passed in 1.2s ===\n")


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
    # the host failing under an unlisted exception: infra (rerun, never kept), not the agent's zero
    full = trial_result(None, exc="RuntimeError")
    full["exception_info"]["exception_message"] = ("Agent install failed: 'OCI runtime exec failed: open "
                                                   "/tmp/runc-process870598827: no space left on device'")
    assert (errors.classify(full)["status"], errors.classify(full)["failure_cause"]) == ("infra", "sandbox_failed")
    # the task's environment never came up (an image build whose dependencies no longer resolve): not the agent's zero
    build = trial_result(None, exc="RuntimeError")
    build["exception_info"]["exception_message"] = (
        "Docker compose command failed for environment animal-reid. Command: docker compose --project-name "
        "animal-reid__weecvwf__env build. Return code: 1. Stdout: Failed to resolve dependencies")
    assert (errors.classify(build)["status"], errors.classify(build)["failure_cause"]) == ("infra", "sandbox_failed")
    own = trial_result(None, exc="NonZeroAgentExitCodeError")
    own["exception_info"]["exception_message"] = "the agent wrote: no space left on device"
    assert errors.classify(own)["status"] == "agent_error"                  # the agent's own exit stays the agent's
    assert errors.classify(None, expired=True)["failure_cause"] == "harbor_timeout"
    assert errors.classify(None, harbor_exit=3)["exception_message"].startswith("harbor run exited 3")
    assert errors.classify(trial_result(0.0), reward_key="f1")["failure_cause"] == "verifier_failed"
    # what the runner reads from the session is infra too
    assert {"session_failed", "tools_failed", "login_failed", "usage_limit"} <= errors.INFRA_CAUSES
    # harborside's launch failing for a reason not the agent's (a key missing, no agent user): infra; the adapter's
    # context too long beside the image's own: the agent's result
    launch_failed = errors.classify(trial_result(None, exc="HermitLaunchError"))
    assert (launch_failed["status"], launch_failed["failure_cause"]) == ("infra", "launch_failed")
    too_long = errors.classify(trial_result(None, exc="HermitContextLimitError"))
    assert (too_long["status"], too_long["failure_cause"]) == ("agent_error", "context_over_limit")


def test_a_verifier_that_could_not_fetch_its_tests_is_infra(tmp_path):
    """Terminal-Bench's tests.sh downloads uv before pytest: when GitHub fails the download (a 5xx), the tests never run
    and the reward is 0. That trial is infra (run again, never kept), not the agent's 0, and so is one whose agent ran
    out of its time or exited with an error (Harbor verifies those too and grades their reward); a test run that failed
    stays the agent's (its timeout too), and so does a pass, and so does a setup that fails the same way every time (a
    mirror's 404)."""
    trial = tmp_path / "t__abc1234"
    said = ("downloading uv 0.9.5 x86_64-unknown-linux-gnu\ncurl: (22) The requested URL returned error: 504\n"
            "failed to download https://github.com/astral-sh/uv/releases/download/0.9.5/uv-x86_64-unknown-linux-gnu"
            ".tar.gz\n/tests/test.sh: line 10: /root/.local/bin/env: No such file or directory\n"
            "/tests/test.sh: line 19: uvx: command not found\n")
    write(trial / "verifier" / "test-stdout.txt", said)
    verdict = HarborAgent.verifier_verdict(errors.classify(trial_result(0.0)), trial)
    assert (verdict["status"], verdict["failure_cause"]) == ("infra", "verifier_failed")
    assert "uvx: command not found" in verdict["exception_message"]
    assert HarborAgent.verifier_verdict(errors.classify(trial_result(1.0)), trial)["status"] == "ok"
    for exc in ("AgentTimeoutError", "NonZeroAgentExitCodeError"):                     # the agent's time ran out,
        failed = errors.classify(trial_result(0.0, exc=exc))                             # or it failed: no test ran
        assert HarborAgent.verifier_verdict(failed, trial)["status"] == "infra", exc
    write(trial / "verifier" / "ctrf.json", "{}")                                        # the tests ran and failed
    assert HarborAgent.verifier_verdict(errors.classify(trial_result(0.0)), trial)["status"] == "ok"
    timeout = errors.classify(trial_result(0.0, exc="AgentTimeoutError"))
    assert HarborAgent.verifier_verdict(timeout, trial)["status"] == "timeout"
    (trial / "verifier" / "ctrf.json").unlink()
    write(trial / "verifier" / "test-stdout.txt", "FAILED test_outputs.py::test_answer - assert 3 == 4\n")
    assert HarborAgent.verifier_verdict(errors.classify(trial_result(0.0)), trial)["status"] == "ok"
    assert HarborAgent.verifier_verdict(errors.classify(trial_result(0.0)), None)["status"] == "ok"
    write(trial / "verifier" / "test-stdout.txt",                                       # GitHub's connection died
          "Setting up curl (8.5.0-2ubuntu10.16) ...\ndownloading uv 0.9.5 x86_64-unknown-linux-gnu\n"
          "curl: (56) Connection died, tried 5 times before giving up\nfailed to download "
          "https://github.com/astral-sh/uv/releases/download/0.9.5/uv-x86_64-unknown-linux-gnu.tar.gz\n"
          "/tests/test.sh: line 19: uvx: command not found\n")
    assert HarborAgent.verifier_verdict(errors.classify(trial_result(0.0)), trial)["status"] == "infra"
    write(trial / "verifier" / "test-stdout.txt",                                       # qemu-startup's, every run
          "Err:6 http://deb.debian.org/debian-security bullseye-security/main amd64 curl amd64 7.74.0-1.3+deb11u16\n"
          "  404  Not Found [IP: 146.75.38.132 80]\n/tests/test.sh: line 8: curl: command not found\n"
          "/tests/test.sh: line 10: /root/.local/bin/env: No such file or directory\n"
          "/tests/test.sh: line 19: uvx: command not found\n")
    assert HarborAgent.verifier_verdict(errors.classify(trial_result(0.0)), trial)["status"] == "ok"


def test_a_verifier_that_left_no_reward_harbor_could_read_is_the_agents_result_as_harbor_grades_it():
    """Harbor grades its verifier group as the agent's result (its error policy: retry=False, grade=True), and its job
    mean counts the trial's missing reward as 0: a verifier that wrote no reward file, an empty one or one that does
    not parse is an agent_error the judge scores 0 and the mean keeps, not infra run again."""
    from hermit.trainer.rollout import run_one
    passed = run_one({"id": "t#1"}, None, lambda task, adapter: RunOutput(output={"status": "ok", "reward": 1.0}),
                     judge.score)
    for exc in ("RewardFileNotFoundError", "RewardFileEmptyError", "VerifierOutputParseError"):
        verdict = errors.classify(trial_result(None, exc=exc))
        assert (verdict["status"], verdict["failure_cause"], verdict["reward"]) == \
            ("agent_error", "verifier_failed", None), exc
        out = RunOutput(output={"status": verdict["status"], "failure_cause": verdict["failure_cause"],
                                "exception_type": exc, "reward": verdict["reward"]})
        record = run_one({"id": "t#2"}, None, lambda task, adapter: out, judge.score)
        assert record["status"] == "ok" and record["judge"]["score"] == 0.0, exc
        assert (passed["judge"]["score"] + record["judge"]["score"]) / 2 == 0.5, exc     # hermit eval's mean


def test_a_request_harbors_own_agent_made_that_the_provider_refused_for_what_it_held_is_the_agents_result():
    """litellm's BadRequestError and UnprocessableEntityError (a request one of Harbor's own agents made that the
    provider refused for what it held: a prompt too long, an invalid request) are the agent's result, its work scored,
    as Harbor grades them and as a Hermit CLI's first request refused for its input is. A provider that could not
    serve (a rate limit, a lost connection, a server error) or that refused the run's configuration (its key, its
    model, a parameter, a budget) stays infra and runs again, which Harbor grades but for the key and the model."""
    for exc in ("BadRequestError", "UnprocessableEntityError"):
        verdict = errors.classify(trial_result(0.0, exc=exc))
        assert (verdict["status"], verdict["failure_cause"], verdict["reward"]) == \
            ("agent_error", "model_provider_failed", 0.0), exc
    for exc in ("ApiRateLimitError", "APIConnectionError", "InternalServerError", "ServiceUnavailableError",
                "APIResponseValidationError", "UnsupportedParamsError", "BudgetExceededError", "AuthenticationError",
                "NotFoundError"):
        verdict = errors.classify(trial_result(None, exc=exc))
        assert (verdict["status"], verdict["failure_cause"]) == ("infra", "model_provider_failed"), exc


def test_a_verifier_the_host_kept_from_its_tests_is_infra_whatever_harbor_raised_for_it(tmp_path, fake_harbor):
    """Hermit's checks of the verifier's output (its tools never arrived; the host's disk was full) find the host behind
    a verifier that left no reward or ran out of its time, as behind one that wrote 0, and behind the verifier of an
    agent that ran out of its own time or exited with an error (Harbor verifies those too): infra, run again. Without
    that evidence such a verifier is the agent's result, and the agent's own timeout or error stays its own."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    agent = make_agent(tmp_path, fake_harbor, tasks)
    trial = tmp_path / "t__abc1234"

    def verdict(exc):
        classified = agent.verifier_timeout_verdict(errors.classify(trial_result(None, exc=exc)))
        return HarborAgent.verifier_verdict(classified, trial)
    write(trial / "verifier" / "test-stdout.txt", "FAILED test_outputs.py::test_answer - assert 3 == 4\n")
    assert verdict("RewardFileNotFoundError")["status"] == "agent_error"
    assert verdict("VerifierTimeoutError")["status"] == "timeout"
    assert verdict("AgentTimeoutError")["status"] == "timeout"                        # the agent's own time ran out
    assert verdict("NonZeroAgentExitCodeError")["status"] == "agent_error"
    write(trial / "verifier" / "test-stdout.txt",
          "failed to download https://github.com/astral-sh/uv/releases/download/0.9.5/uv.tar.gz\n"
          "/tests/test.sh: line 18: uvx: command not found\n")
    for exc in ("RewardFileNotFoundError", "RewardFileEmptyError", "VerifierTimeoutError"):
        assert (verdict(exc)["status"], verdict(exc)["failure_cause"]) == ("infra", "verifier_failed"), exc
    write(trial / "verifier" / "test-stdout.txt", "OSError: [Errno 28] No space left on device: '/logs/verifier'\n")
    for exc in ("RewardFileNotFoundError", "VerifierOutputParseError", "VerifierTimeoutError", "AgentTimeoutError",
                "NonZeroAgentExitCodeError"):
        assert verdict(exc)["status"] == "infra", exc


def test_an_infra_verifier_verdict_quotes_the_verifiers_output_with_verifier_redact_applied(tmp_path):
    """The verifier's output an infra verdict quotes (the tail behind its cause) has `verifier_redact`'s patterns
    removed, as the curated test-stdout.txt has: the test's expected answer reaches neither the verdict's message
    (the rollout's extra, the learning material) nor the log's exception.txt."""
    job = tmp_path / "job"
    trial = job / "t__abc1234"
    write(trial / "verifier" / "test-stdout.txt",
          "FAILED test_outputs.py::test_answer - AssertionError: expected GOLD-ANSWER-42, got 41\n"
          "OSError: [Errno 28] No space left on device: '/tmp/pytest-cache'\n")
    write(trial / "verifier" / "reward.txt", "0\n")
    redact = [r"GOLD-ANSWER-\d+"]
    verdict = HarborAgent.verifier_verdict(errors.classify(trial_result(0.0)), trial, redact)
    assert (verdict["status"], verdict["failure_cause"]) == ("infra", "verifier_failed")
    assert "GOLD-ANSWER-42" not in verdict["exception_message"]
    assert f"expected {curate.REDACTED}, got 41" in verdict["exception_message"]
    log = curate.curate(trial, job, verdict, redact=redact)
    assert "GOLD-ANSWER-42" not in (log / "exception.txt").read_text()
    assert "GOLD-ANSWER-42" not in (log / "verifier" / "test-stdout.txt").read_text()


def test_teardown_takes_the_trials_verifier_projects_down_too(monkeypatch):
    """A killed trial leaves its environment's compose project and each separate verifier's
    (`<trial>__verifier__<key>`, harbor/trial/trial.py): the teardown downs them all, a single-step trial's by its
    exact name (its key "trial", whatever its length: docker.verifier_session) and any other by the prefix, and no
    other project."""
    calls = []

    class Done:
        stdout = "alpha__abc1234__env\nalpha__abc1234__verifier__main\nbeta__xyz9876__verifier__main\n"

    def run(cmd, **kwargs):
        calls.append(cmd)
        return Done()
    monkeypatch.setattr(docker.subprocess, "run", run)
    docker.compose_down("alpha__abc1234")
    downs = [c[c.index("-p") + 1] for c in calls if "down" in c]
    assert downs == ["alpha__abc1234__env", "alpha__abc1234__verifier__trial", "alpha__abc1234__verifier__main"]
    assert docker.verifier_prefix("Mixed.Case__abc1234") == "mixed-case__abc1234__verifier"


# ---- curate ------------------------------------------------------------------------------------------------------

def test_curate_keeps_outputs_and_drops_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    job = tmp_path / "job"
    trial = job / "t__abc1234"
    write(trial / "agent" / "codex.txt", "out\n")
    write(trial / "agent" / "agent-codex.jsonl", '{"type":"thread.started"}\n')
    write(trial / "agent" / "sessions" / "rollout.jsonl", "secret\n")
    write(trial / "agent" / "hook-state" / "state.json", "{}")
    write(trial / "agent" / "hermit-collect" / "last_message.txt", f"done; the key was {KEY}\n")
    write(trial / "agent" / "hermit-collect" / "opencode.db", "SQLite format 3\0")
    write(trial / "agent" / "hermit-copy" / "memory" / "NOTES.md", "- keep the cache warm\n")
    (trial / "agent" / "hook-state" / "linked.txt").symlink_to(tmp_path / "outside.txt")   # the container's link
    write(tmp_path / "outside.txt", "a file of this machine\n")
    write(trial / "verifier" / "test-stdout.txt", "x" * 25_000 + "\nexpected answer: 42\n")
    write(trial / "verifier" / "reward.txt", "1\n")
    write(trial / "result.json", json.dumps(trial_result()))
    write(job / "harbor.log", "harbor\n")
    write(job / "job.yaml", "job_name: j\n")
    write(job / "tool-calls.jsonl", '{"tool": "calc"}\n')
    write(job / "launch" / "home" / "auth.json", "{}")                   # never curated
    log = curate.curate(trial, job, errors.classify(trial_result(None, exc="VerifierTimeoutError")),
                        redact=[r"expected answer: \d+"], audit=job / "tool-calls.jsonl")
    names = sorted(str(p.relative_to(log)) for p in log.rglob("*") if p.is_file() or p.is_symlink())
    assert names == ["agent-codex.jsonl", "agent/codex.txt", "agent/hermit-collect/last_message.txt",
                     "exception.txt", "harbor.log", "hook-state/state.json", "job.yaml", "memory/NOTES.md",
                     "result.json", "tool-calls.jsonl", "verifier/reward.txt", "verifier/test-stdout.txt"]
    text = (log / "verifier" / "test-stdout.txt").read_text()
    assert text.startswith("[... ") and "characters dropped ...]" in text and len(text) < 20_100
    assert "expected answer" not in text and text.rstrip().endswith(curate.REDACTED)
    assert KEY not in (log / "agent" / "hermit-collect" / "last_message.txt").read_text()   # scrubbed: <FAKE_MODEL_KEY>
    assert "VerifierTimeoutError" in (log / "exception.txt").read_text()
    assert curate.seconds(trial_result()["agent_execution"]) == 40.0
    write(trial / "agent" / "trajectory.json", json.dumps({"steps": [{"metrics": {"a": 1}}, {}]}))
    assert curate.model_calls(trial / "agent" / "trajectory.json") == 1


def test_curate_lays_the_log_out_where_hermits_readers_look(tmp_path):
    """The curated log is laid out as a Hermit session's log, where the methods' readers look (the trajectory's
    agent-<cli>.jsonl at the top, AHE's hook-calls.jsonl, prompt.txt and task.md, RRSI's task text): the CLI's stream
    at the top under its Hermit name (Harbor's own codex's codex.txt and claude-code's claude-code.txt too), the
    hooks' audit and state beside it; the task's instruction (task.md), the prompt the session was given
    (prompt.txt: the instruction with the plan's prompt suffix, and a slash command's prefix) and the CLI's reply
    (last_message.txt)."""
    job = tmp_path / "job"
    trial = job / "t__abc1234"
    write(trial / "agent" / "claude-code.txt", '{"type":"system"}\n')
    write(trial / "agent" / "hook-calls.jsonl", '{"hook": "stop"}\n')
    plan = write(job / "launch" / "plan.json", json.dumps({"prompt_suffix": "\n\nKeep notes."}))
    log = curate.curate(trial, job, errors.classify(trial_result()), instruction="Do alpha.\n", plan=plan,
                        reply="all done\n")
    assert (log / "agent-claude.jsonl").read_text() == '{"type":"system"}\n'
    assert not (log / "agent" / "claude-code.txt").exists() and not (log / "agent" / "hook-calls.jsonl").exists()
    assert (log / "hook-calls.jsonl").is_file() and (log / "task.md").read_text() == "Do alpha.\n"
    assert (log / "prompt.txt").read_text().startswith("Do alpha.") and "Keep notes." in (log / "prompt.txt").read_text()
    assert (log / "last_message.txt").read_text() == "all done\n"


def test_a_verifier_output_the_container_left_as_a_link_is_never_read(tmp_path):
    """The container can write the trial's verifier/ folder: a test-stdout.txt that is a link (to a file of this
    machine) is read into neither the curated log, the learner's feedback nor the verdict."""
    host_file = write(tmp_path / "host-only" / "notes.txt", "failed to download https://github.com/x/uv.tar.gz\n"
                                                            "uvx: command not found\ndb_password=not-a-real-one\n")
    job = tmp_path / "job"
    trial = job / "t__abc1234"
    write(trial / "agent" / "agent-codex.jsonl", '{"type":"thread.started"}\n')
    (trial / "verifier").mkdir()
    (trial / "verifier" / "test-stdout.txt").symlink_to(host_file)
    verdict = errors.classify(trial_result(0.0))
    log = curate.curate(trial, job, verdict)
    assert not (log / "verifier" / "test-stdout.txt").exists()
    feedback = judge.score({}, RunOutput(output={"status": "ok", "reward": 0.0}, log=log))["feedback"]
    assert "not-a-real-one" not in feedback and "uvx" not in feedback
    assert HarborAgent.verifier_verdict(verdict, trial) == verdict           # its words are not the verifier's


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

def test_judge_scores_the_reward_whenever_the_verifier_gave_one(tmp_path):
    passed = RunOutput(output={"status": "ok", "reward": 1.0})
    assert judge.score({}, passed) == {"score": 1.0, "feedback": "passed", "status": "ok", "passed": True,
                                       "rewards": None}
    log = tmp_path / "log"
    write(log / "verifier" / "test-stdout.txt", "FAIL: expected 3 got 2\n")
    failed = RunOutput(output={"status": "ok", "reward": 0.0, "rewards": {"reward": 0.0, "f1": 0.3}}, log=log)
    verdict = judge.score({}, failed)
    assert verdict["score"] == 0.0 and "FAIL: expected 3 got 2" in verdict["feedback"]
    assert verdict["rewards"] == {"reward": 0.0, "f1": 0.3}
    # A trial that ran out of the agent's time still has its work scored, as Harbor's job mean scores it
    timeout = RunOutput(output={"status": "timeout", "failure_cause": "agent_exhausted_budget",
                                "exception_type": "AgentTimeoutError", "reward": 1.0}, log=log)
    verdict = judge.score({}, timeout)
    assert verdict["score"] == 1.0 and "time budget" in verdict["feedback"]
    assert "passed the verifier" in verdict["feedback"]
    assert "FAIL" not in verdict["feedback"]                              # a pass needs no tail
    exited = RunOutput(output={"status": "agent_error", "failure_cause": "unclassified", "reward": 0.5,
                               "exception_type": "NonZeroAgentExitCodeError", "rewards": {"reward": 0.5}}, log=log)
    verdict = judge.score({}, exited)
    assert verdict["score"] == 0.5 and "scored its work 0.5" in verdict["feedback"] and "FAIL" in verdict["feedback"]
    assert verdict["rewards"] == {"reward": 0.5}
    slow = RunOutput(output={"status": "timeout", "failure_cause": "solution_timed_out",
                             "exception_type": "VerifierTimeoutError", "reward": None})
    assert judge.score({}, slow)["score"] == 0.0 and "solution" in judge.score({}, slow)["feedback"]
    # Not the agent's result: no score at all, which the run records as a failed run (judge_error), never a 0 it
    # earned
    infra = RunOutput(output={"status": "infra", "failure_cause": "verifier_failed", "reward": 0.0},
                      extra={"unscored": "verifier_failed", "exception_message": "the verifier's tools never arrived"})
    with pytest.raises(judge.Unscored, match=r"\(verifier_failed: the verifier's tools never arrived\)"):
        judge.score({}, infra)
    with pytest.raises(judge.Unscored, match=r"\(infra\)"):
        judge.score({}, RunOutput(output=None))
    # The benchmark's pass reward: a reward that reaches it passed; with none, nothing is said to pass
    half = RunOutput(output={"status": "ok", "reward": 0.8}, extra={"judge": {"pass_reward": 0.75}})
    assert judge.score({}, half)["feedback"] == "passed" and judge.score({}, half)["passed"] is True
    scale = RunOutput(output={"status": "ok", "reward": 1.38}, log=log, extra={"judge": {"pass_reward": None}})
    verdict = judge.score({}, scale)
    assert verdict["score"] == 1.38 and verdict["feedback"].startswith("reward 1.38") and "FAIL" in verdict["feedback"]
    assert verdict["passed"] is None                                       # a score scale: no pass
    assert judge.score({}, failed)["passed"] is False
    # The adapter's context too long beside the task's own: its 0, and the learner is told by how much
    too_long = RunOutput(output={"status": "agent_error", "failure_cause": "context_over_limit", "reward": None,
                                 "exception_type": "HermitContextLimitError"},
                         extra={"exception_message": "hermit: AGENTS.md for Codex would be 300000 bytes (Hermit's "
                                                     "context 290000, then the image's own 10000), over the CLI's "
                                                     "limit of 262144 bytes: shorten the adapter's context"})
    verdict = judge.score({}, too_long)
    assert verdict["score"] == 0.0 and "over the CLI's limit of 262144 bytes" in verdict["feedback"]
    assert "hermit: " not in verdict["feedback"]


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


TOOL_META = """\
template: {id: calcdemo, version: '1', contract: '2'}
structure:
  learnable: [{path: ADAPTER.md, format: {text: true}}]
  fixed: [interface.py, exports.yaml]
"""
TOOL_EXPORTS = """\
context: ADAPTER.md
tools:
  - name: calc
    description: a tool for the agent
    entrypoint: interface.py:calc
    input_schema: {type: object, properties: {x: {type: number}}}
    output_schema: {type: object}
"""
CONTEXT_EXPORTS = TOOL_EXPORTS + """\
  - name: lookup
    description: the harness's per-task context
    entrypoint: interface.py:lookup
    input_schema: {type: object, properties: {query: {type: string}, task: {type: object}}}
    output_schema: {type: object}
context_tool: lookup
"""
TOOL_INTERFACE = '''\
def calc(x=0):
    return {"result": FACTOR * x}


def lookup(query="", task=None):
    return {"text": f"[NOTES FOR {(task or {}).get('id', '?')}]", "retrieved": [(task or {}).get("id")]}


FACTOR = %d
'''


def tool_adapter(root: Path, *, factor: int = 2, context_tool: bool = False) -> Path:
    """A minimal adapter with one agent tool (calc), and with `context_tool` a per-task context (lookup)."""
    write(root / ".hermit" / "adapter.yaml", TOOL_META + ("plugin-portable: false\n" if context_tool else ""))
    write(root / "exports.yaml", CONTEXT_EXPORTS if context_tool else TOOL_EXPORTS)
    write(root / "ADAPTER.md", "# Static instructions\n")
    write(root / "interface.py", TOOL_INTERFACE % factor)
    return root


def test_stage_deploys_the_adapter_for_the_container(tmp_path):
    from hermit.harbor.staging import stage
    adapter = simple_template()
    info = stage(adapter, tmp_path / "job", sys.executable, "best_effort")
    deploy = tmp_path / "job" / "deploy"
    assert info["adapter_id"] and (deploy / "AGENTS.md").is_file() and info["deploy"] == str(deploy)
    assert (deploy / "hermit" / info["adapter_id"]).is_dir() and info["adapter_dir"] == f"hermit/{info['adapter_id']}"
    assert info["hooks"] is False and not (deploy / ".codex" / "hooks.json").exists()
    assert info["tools"] == [] and info["context_tool"] is None
    assert json.loads((tmp_path / "job" / "staging.json").read_text())["adapter_id"] == info["adapter_id"]
    assert stage(None, tmp_path / "none", sys.executable) is None


def test_stage_names_the_tools_and_gives_a_per_task_context(tmp_path):
    """The staging record names the adapter's agent tools (their names: the server is this machine's, not the
    workspace's), and an adapter whose context is per task gets the task's, computed in the adapter sandbox, in its
    host's context file; the answer agent's deployment is that context alone."""
    from hermit.harbor.staging import stage
    adapter = tool_adapter(tmp_path / "adapter", context_tool=True)
    row = {"id": "alpha", "instruction": "Do alpha."}
    info = stage(adapter, tmp_path / "job", sys.executable, "best_effort", task=row)
    assert info["tools"] == ["calc"] and info["context_tool"] == {"text": "[NOTES FOR alpha]", "retrieved": ["alpha"]}
    agents = (tmp_path / "job" / "deploy" / "AGENTS.md").read_text()
    assert "[NOTES FOR alpha]" in agents and "Static instructions" in agents
    answer = stage(adapter, tmp_path / "ajob", sys.executable, "best_effort", "answer", task=row)
    assert answer["deploy"] is None and answer["tools"] == [] and answer["tools_unused"] == ["calc"]
    assert "[NOTES FOR alpha]" in Path(answer["context_file"]).read_text()


def test_stage_rewrites_hooks_relative_to_the_workspace(tmp_path, monkeypatch, fake_harbor):
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
    # Claude Code runs a hook in the shell's current directory: its commands start under $CLAUDE_PROJECT_DIR, and the
    # deployment's record is kept for the container (a hook runner reads its host there)
    info = stage(adapter, tmp_path / "cjob", sys.executable, "best_effort", "claude")
    settings = json.loads((tmp_path / "cjob" / "deploy" / ".claude" / "settings.json").read_text())
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert info["host"] == "claude" and info["hooks"] is True
    assert command.startswith(f'cd "${{CLAUDE_PROJECT_DIR:-.}}"/hermit/{info["adapter_id"]} || exit 1\n')
    assert "python3 scripts/start.py" in command and str(tmp_path) not in command
    record = json.loads((tmp_path / "cjob" / "deploy" / ".hermit-deploy.json").read_text())
    assert record["host"] == "claude-code" and Path(record["adapter_dir"]).name == info["adapter_id"]
    # The launch plan names the deployment the container gets, with its hooks
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    agent = HarborAgent({"tasks_dir": str(tasks), "infra_retries": 0, "jobs_dir": str(tmp_path / "jobs"), "web_search": False,
                         "acting_agent": {"import_path": "harborside.claude:HermitClaude"}, "harbor": str(fake_harbor),
                         "model": "claude-haiku-5-5", "login": "claude"}, base=tmp_path)
    plan_path = agent._launch(row, tmp_path / "cjob", info, None)
    kwargs = agent.agent_config(row, plan_path, info)["kwargs"]
    plan = json.loads(Path(kwargs["plan"]).read_text())
    assert plan["deploy"]["work_dir"] == str((tmp_path / "cjob" / "deploy").resolve())
    assert plan["deploy"]["hooks"] is True
    assert plan["deploy"]["context_file"] == "CLAUDE.md" and "deploy_dir" not in kwargs


# ---- the runner's params, checked at startup -------------------------------------------------------------------------

def test_runner_is_a_builtin_and_validates_params(tmp_path, fake_harbor):
    from hermit.agent.acting.agent import BUILTIN_NAMES, load_agent
    assert "harbor" in BUILTIN_NAMES
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    agent = load_agent("harbor", {"tasks_dir": str(tasks), "harbor": str(fake_harbor), "jobs_dir": str(tmp_path / "j"),
                                  "model": "glm-5", "binding": BINDING, "web_search": False}, tmp_path)
    assert isinstance(agent, HarborAgent) and agent.tasks_dir == tasks and agent.cli == "codex"
    with pytest.raises(ValueError, match="tasks_dir"):
        HarborAgent({}, tmp_path)
    with pytest.raises(ValueError, match="docker_cleanup"):
        make_agent(tmp_path, fake_harbor, tasks, docker_cleanup="prune")
    with pytest.raises(ValueError, match="both a name and an import_path"):
        make_agent(tmp_path, fake_harbor, tasks, acting_agent={"name": "codex", "import_path": "x:Y"})
    with pytest.raises(ValueError, match="one of codex"):
        make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "aider"})
    with pytest.raises(ValueError, match="pass_reward"):
        make_agent(tmp_path, fake_harbor, tasks, pass_reward="high")
    with pytest.raises(ValueError, match="verifier_redact"):
        make_agent(tmp_path, fake_harbor, tasks, verifier_redact=["(unclosed"])


def test_a_cli_without_its_sign_in_is_refused_before_any_trial(tmp_path, fake_harbor, monkeypatch):
    """A HermitXXX runs on a <backend>/<model> reference or on its own CLI's login, never on neither (whatever the
    container finds would pay) nor on both; web search is set either way; a model on this machine's loopback is out
    of a container's reach."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    with pytest.raises(ValueError, match="no <backend>/<model> reference"):
        make_agent(tmp_path, fake_harbor, tasks, model="gpt-5.6-luna", binding=None)
    with pytest.raises(ValueError, match="two bills"):
        make_agent(tmp_path, fake_harbor, tasks, login="chatgpt")
    with pytest.raises(ValueError, match="takes login: claude"):
        make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "claude"}, binding=None, model="haiku",
                   login="chatgpt")
    with pytest.raises(ValueError, match="takes no login"):
        make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "pi"}, binding=None, model="m", login="claude")
    with pytest.raises(ValueError, match="web_search must be set"):
        make_agent(tmp_path, fake_harbor, tasks, web_search=None)
    with pytest.raises(ValueError, match="loopback"):
        make_agent(tmp_path, fake_harbor, tasks, binding={**BINDING, "base_url": "http://127.0.0.1:8000/v1"})
    with pytest.raises(ValueError, match="codex login"):                 # no ChatGPT sign-in on this machine
        make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt")
    with pytest.raises(ValueError, match="setup-token"):                 # nor Claude's token
        make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "claude"}, binding=None, model="haiku",
                   login="claude")
    chatgpt_login(tmp_path)
    assert make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt").cli == "codex"
    # Harbor's own agents keep their own sign-in and need none of this
    assert make_agent(tmp_path, fake_harbor, tasks, acting_agent={"name": "terminus-2"}, binding=None,
                      web_search=None).cli is None
    # The model's key goes to the session by name: unset here, every trial would fail, so none starts; nor in an
    # environment other than Harbor's local Docker one, which cannot pass a variable by name
    with pytest.raises(ValueError, match="docker environment"):
        make_agent(tmp_path, fake_harbor, tasks, environment={"type": "daytona"})
    monkeypatch.delenv(KEY_ENV)
    with pytest.raises(ValueError, match=f"{KEY_ENV} is not set"):
        make_agent(tmp_path, fake_harbor, tasks)


def test_the_answer_agent_without_its_models_key_is_refused_at_startup(tmp_path, fake_harbor, monkeypatch):
    """The answer agent's endpoint on this machine sends each request on with the model's key: without it set, every
    trial's call would fail (each an infra rollout), so none starts."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    monkeypatch.delenv(KEY_ENV)
    with pytest.raises(ValueError, match=f"{KEY_ENV} is not set"):
        make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "answer"}, web_search=None)


# The spec hermit.isolate.agent_pipeline gives the runner for a model a CLI reaches, made by main's own
# endpoint.endpoint_spec (so that a key main adds to it breaks the runner's check of it here, PIPELINE_CLI_SPEC): its
# endpoint's URL and, on a login, a timeout that waits out the plan (3600 s and plan_max_wait_s); it names neither the
# CLI nor its login
def _served() -> dict:
    from types import SimpleNamespace
    from hermit.agent.base.endpoint import endpoint_spec
    endpoint = SimpleNamespace(url="http://127.0.0.1:9/" + "0" * 32,
                               agent=SimpleNamespace(settings={"model": "gpt-5.6-luna", "login": "chatgpt"}))
    return endpoint_spec(endpoint, {"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt"})


SERVED = _served()


def test_the_answer_agents_attachments_on_a_model_a_cli_reaches_are_refused_at_startup(tmp_path, fake_harbor):
    """A session of a model a CLI reaches takes text only: the answer agent's attachments (the task's figures) on such
    a model the runner serves (model: {cli: ...}, the benchmark file's) would be refused in every trial, so the run is
    refused before the first one. Without attachments, or on a <backend>/<model> reference, it starts. A model a CLI
    reaches that the pipeline serves (a run config's own `{cli}`: hermit.isolate.agent_pipeline gives the runner its
    endpoint's spec alone, which names no CLI; hermit check, the config's params as written) is refused with or
    without them: its session waits for its login's plan inside the request, which the runner cannot tell from the
    model's own time (the benchmark file's is the runner's own to serve, and wait for)."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    figures = {"attachments": ["/app/figure*.png"]}
    answer = {"acting_agent": {"cli": "answer"}, "binding": None, "web_search": None}
    reached = {"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt"}
    with pytest.raises(ValueError, match="takes text only"):
        cli_answer_agent(tmp_path, fake_harbor, tasks, reached, answer=figures)
    assert make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "answer"}, web_search=None,
                      answer=figures).cli == "answer"
    from hermit.harbor.runner import PIPELINE_CLI_SPEC
    assert set(SERVED) == PIPELINE_CLI_SPEC and SERVED["timeout"] == 3600.0 + 12 * 3600
    for given in ({}, {"answer": figures}):
        with pytest.raises(ValueError, match="Give it in the benchmark file"):     # as hermit eval hands it
            make_agent(tmp_path, fake_harbor, tasks, **answer, model=SERVED, **given)
        with pytest.raises(ValueError, match="Give it in the benchmark file"):     # as hermit check hands it
            make_agent(tmp_path, fake_harbor, tasks, **answer, model=reached, **given)
    # a model with more than the pipeline endpoint's keys (a loaded config's binding) is the config's own
    assert make_agent(tmp_path, fake_harbor, tasks, **answer, model={**SERVED, "ref": "local/gpt-5.6-luna"}).cli \
        == "answer"


def test_a_task_harbor_runs_for_none_of_hermits_agents_is_infra_and_never_started(tmp_path, fake_harbor):
    """Harbor refuses a task that declares MCP servers or a skills folder, or targets a Windows container, for an
    agent whose capabilities lack them (Trial._validate_agent_capabilities), as harborside's agents' do: its refusal
    leaves no stream, so such a trial would be an agent error, scored 0. For a table CLI and the answer agent its
    rollout is infra (`unsupported_task`, naming why), never handed to Harbor, while the task set's other tasks run (a
    split may leave such tasks out, so the run is not refused); one of Harbor's own agents takes them."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "plain")
    for name, extra in (("mcp", '\n[[environment.mcp_servers]]\nname = "bank"\ntransport = "sse"\n'
                                'url = "http://bank:8000/sse"\n'),
                        ("skills", 'skills_dir = "/skills"\n'), ("windows", 'os = "windows"\n')):
        task = make_task(tasks, name)
        toml = (task / "task.toml").read_text()
        (task / "task.toml").write_text(toml.replace("[environment]\n", "[environment]\n" + extra, 1)
                                        if not extra.startswith("\n[[") else toml + extra)
    rows = {r["harbor_task"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks)
    jobs = tmp_path / "jobs"
    for name, why in (("mcp", "MCP servers"), ("skills", "a skills folder"), ("windows", "a Windows container")):
        out = agent.run(rows[name], None)
        assert out.output["status"] == "infra" and out.extra["unscored"] == "unsupported_task", name
        assert f"it declares {why}" in out.extra["exception_message"] and out.log is None, name
    assert not jobs.exists() or not any(jobs.glob("*/job.yaml"))       # none was handed to Harbor
    assert agent.run(rows["plain"], None).output["status"] == "ok"
    answer = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "answer"}, web_search=None)
    assert answer.run(rows["mcp"], None).extra["unscored"] == "unsupported_task"
    own = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"name": "codex"}, binding=None, model="gpt-5.6-luna")
    assert own.unsupported(rows["mcp"]) == ["MCP servers"] and own.run(rows["mcp"], None).extra.get("unscored") != \
        "unsupported_task"                                             # Harbor's own Codex takes them


def test_the_answer_agents_model_keeps_the_client_timeout_and_retries_its_spec_gives(tmp_path, fake_harbor):
    """A model with a client timeout beside it (one that waits out a slow model) keeps it in each trial's relay, which
    would otherwise cut the request at its client's 600 s and send it again; so does a bound model with a timeout or
    max_retries beside it (the binding's spec keeps them)."""
    from hermit.harbor.tools import ModelEndpoint
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    answer = {"acting_agent": {"cli": "answer"}, "web_search": None}
    served = make_agent(tmp_path, fake_harbor, tasks, **answer, binding=None, model={**SERVED, "ref": "local/luna"})
    bound = make_agent(tmp_path, fake_harbor, tasks, **answer, binding={**BINDING, "timeout": 1200, "max_retries": 1})
    for agent, client in ((served, (SERVED["timeout"], 4)), (bound, (1200.0, 1))):
        relay = ModelEndpoint(agent.answer_model()).start()
        try:
            assert (relay.client.timeout, relay.client.max_retries) == client
        finally:
            relay.close()
    assert served.answer_model()["timeout"] == SERVED["timeout"] and "max_retries" not in served.answer_model()


def test_a_mini_swe_agent_trial_whose_deploying_interpreter_lacks_the_package_is_refused_at_startup(
        tmp_path, fake_harbor, monkeypatch):
    """A mini-swe-agent trial's adapter is deployed on this machine (hermit.hosts.mini_swe_agent, run with `python`),
    which reads the package's own config: where that interpreter has no package, every trial with an adapter would
    fail, so the run is refused before the first one, with the install hint; this interpreter is asked in this
    process, another in a short child of it. Another CLI's deployment needs no such package."""
    import importlib.util
    from types import SimpleNamespace
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    without = write(tmp_path / "bare" / "python", "#!/bin/sh\nexit 1\n", exe=True)      # finds no such package
    within = write(tmp_path / "full" / "python", "#!/bin/sh\nexit 0\n", exe=True)       # finds it
    mini = {"acting_agent": {"cli": "mini-swe-agent"}}
    with pytest.raises(ValueError, match="mini-swe-agent is not installed"):
        make_agent(tmp_path, fake_harbor, tasks, python=str(without), **mini)
    assert make_agent(tmp_path, fake_harbor, tasks, python=str(within), **mini).cli == "mini-swe-agent"
    assert make_agent(tmp_path, fake_harbor, tasks, python=str(without)).cli == "codex"
    real = importlib.util.find_spec
    package = SimpleNamespace(submodule_search_locations=[str(tmp_path / "minisweagent")])
    for found in (None, package):                                            # this interpreter, asked in this process
        monkeypatch.setattr(importlib.util, "find_spec",
                            lambda name, *args, found=found: found if name == "minisweagent" else real(name, *args))
        if found is None:
            with pytest.raises(ValueError, match="mini-swe-agent is not installed"):
                make_agent(tmp_path, fake_harbor, tasks, **mini)
        else:
            assert make_agent(tmp_path, fake_harbor, tasks, **mini).cli == "mini-swe-agent"


def test_a_runner_without_bubblewrap_is_refused_at_startup_unless_its_adapter_sandbox_is_best_effort(
        tmp_path, fake_harbor, monkeypatch):
    """Staging renders every adapter a trial deploys (its exports, its per-task context) in the strict adapter sandbox,
    which, its enforce required (`sandbox_enforce`, the default), refuses to run without bubblewrap: each such trial
    would end in that error, scored 0, so the run is refused before the first, with what to do, the answer agent's as
    a CLI's. The runner is not told whether the run deploys an adapter, so one that deploys none (hermit eval --adapter
    none) is refused too; with the adapter sandbox best_effort it starts."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    real = shutil.which
    with monkeypatch.context() as no_bwrap:
        no_bwrap.setattr(shutil, "which", lambda name, *a, **k: None if name == "bwrap" else real(name, *a, **k))
        for params in ({}, {"acting_agent": {"cli": "answer"}}):
            cli = (params.get("acting_agent") or {}).get("cli", "codex")
            with pytest.raises(ValueError, match=r"bubblewrap \(bwrap\) is not installed: install it, or set "
                                                 r"agent.params.sandbox_enforce: best_effort"):
                make_agent(tmp_path, fake_harbor, tasks, **params)
            assert make_agent(tmp_path, fake_harbor, tasks, sandbox_enforce="best_effort", **params).cli == cli
    assert make_agent(tmp_path, fake_harbor, tasks).cli == "codex"                        # bubblewrap here: as before


def test_a_multi_step_task_set_whose_harbor_has_no_python_beside_it_is_refused_at_startup(tmp_path, fake_harbor):
    """A multi-step task's steps run in a stream whose driver runs under the python of Harbor's venv, beside `harbor`
    (or beside the file a link of that name points to): a `harbor` with none beside it (a wrapper script elsewhere)
    would lose every step of every stream, so a task set with a multi-step task is refused before the first one. One
    without runs every trial through `harbor` alone, and starts."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    wrapper = write(tmp_path / "wrapper" / "harbor", f'#!/bin/sh\nexec "{fake_harbor}" "$@"\n', exe=True)
    assert make_agent(tmp_path, wrapper, tasks).harbor == str(wrapper)
    make_task(tasks, "flow", steps=("s1", "s2"))
    with pytest.raises(ValueError, match="multi-step task's driver runs under"):
        make_agent(tmp_path, wrapper, tasks)
    assert make_agent(tmp_path, fake_harbor, tasks).multi_step() is True


def test_a_harbor_command_that_is_not_there_is_refused_at_startup(tmp_path, fake_harbor):
    """Every rollout starts `harbor`: one that is not there, or not executable, would make each an error scored 0."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    with pytest.raises(ValueError, match="not an executable file"):
        make_agent(tmp_path, tmp_path / "no-such-venv" / "bin" / "harbor", tasks)
    with pytest.raises(ValueError, match="not an executable file"):
        make_agent(tmp_path, write(tmp_path / "plain" / "harbor", "#!/bin/sh\n"), tasks)
    with pytest.raises(ValueError, match="not an executable file"):              # Harbor's own agents run it too
        make_agent(tmp_path, tmp_path / "nowhere", tasks, acting_agent={"name": "terminus-2"}, binding=None)


def test_an_acting_agent_cli_with_no_class_of_the_table_is_refused_at_startup(tmp_path, fake_harbor):
    """`acting_agent.cli` names a CLI of the table: Hermes Agent, a CLI Base Agent of main's (#107), runs in Hermit's
    own workspaces only, and a CLI with no HermitXXX class would make every trial an error."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    for cli in ("hermes", "nope"):
        with pytest.raises(ValueError, match=f"acting_agent.cli is one of codex, .*, not '{cli}'"):
            make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": cli})


def test_a_harbor_named_on_path_or_by_a_relative_path_is_the_one_every_trial_starts(tmp_path, fake_harbor, monkeypatch):
    """`harbor: harbor` is the command on PATH, and a relative path is the working directory's: each is resolved once,
    so the startup check and every trial (whose harbor runs in its job's directory) start the same one."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    monkeypatch.setenv("PATH", f"{fake_harbor.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    on_path = make_agent(tmp_path, fake_harbor, tasks, harbor="harbor")
    assert on_path.harbor == str(fake_harbor) and on_path.run(row, None).output["status"] == "ok"
    monkeypatch.chdir(tmp_path)
    relative = make_agent(tmp_path, fake_harbor, tasks, harbor="harbor-venv/bin/harbor")
    assert os.path.isabs(relative.harbor) and os.path.samefile(relative.harbor, fake_harbor)
    assert relative.run(row, None).output["status"] == "ok"
    with pytest.raises(ValueError, match="nor a command on PATH"):
        make_agent(tmp_path, fake_harbor, tasks, harbor="no-such-harbor")


def test_a_harbor_builtins_login_or_effort_is_refused_at_startup(tmp_path, fake_harbor):
    """What Harbor's own agents would refuse in every trial is refused before the first one: a login (Harbor's own
    agent would put the sign-in on a command line), an effort Claude Code does not take."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    with pytest.raises(ValueError, match="command line"):
        make_agent(tmp_path, fake_harbor, tasks, acting_agent={"name": "codex"}, binding=None, model="m",
                   login="chatgpt")
    with pytest.raises(ValueError, match="command line"):
        make_agent(tmp_path, fake_harbor, tasks, acting_agent={"name": "terminus-2"}, binding=None, login="claude")
    with pytest.raises(ValueError, match="takes effort"):
        make_agent(tmp_path, fake_harbor, tasks, acting_agent={"name": "claude-code"}, binding=None, model="m",
                   effort="extreme")
    assert make_agent(tmp_path, fake_harbor, tasks, acting_agent={"name": "claude-code"}, binding=None, model="m",
                      effort="high").cli is None


def test_harbors_own_claude_code_keeps_harbors_web_rule_per_task_unless_web_search_is_off(tmp_path, fake_harbor):
    """Any `disable_web_search` Harbor's agent is given overrides Harbor's own rule (WebSearch and WebFetch off where
    the task's agent network is not public): `web_search: false` turns them off on every task, and true or unset gives
    Harbor nothing, so a task with no public network never has them; the tools that outlive the session stay denied."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    row = dataset.index(tasks)[0]

    def kwargs(**params):
        return make_agent(tmp_path, fake_harbor, tasks, acting_agent={"name": "claude-code"}, binding=None, model="m",
                          **params).agent_config(row)["kwargs"]
    assert kwargs(web_search=False)["disable_web_search"] is True
    for web in (True, None):
        given = kwargs(web_search=web)
        assert "disable_web_search" not in given and "RemoteTrigger" in given["disallowed_tools"], web
    with pytest.raises(ValueError, match="web_search is true or false"):
        kwargs(web_search="no")


def test_harbors_own_claude_code_is_refused_a_sign_in_it_would_never_be_handed(tmp_path, fake_harbor, monkeypatch):
    """Harbor's own claude-code is never handed the subscription's token (it would put it on a command line):
    CLAUDE_FORCE_OAUTH, which asks for it, would bill the key in every trial, and the token as the only sign-in of a
    model at Anthropic's API would run each one signed out: both refused at startup. A key bills the key, the token
    beside it or not, as Harbor's claude-code always did; a model elsewhere takes no subscription."""
    from hermit.agent.base.cli import CLAUDE_BILLING
    for name in (*CLAUDE_BILLING, "CLAUDE_FORCE_OAUTH", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")

    def builtin(model="claude-haiku-5-5"):
        return make_agent(tmp_path, fake_harbor, tasks, acting_agent={"name": "claude-code"}, binding=None,
                          model=model)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    for model in ("claude-haiku-5-5", "anthropic/claude-haiku-5-5", normalized("anthropic/claude-haiku-5-5", tasks)):
        with pytest.raises(ValueError, match="the only sign-in here.*login: claude"):
            builtin(model)
    builtin("openrouter/anthropic/claude-haiku-5-5")
    make_agent(tmp_path, fake_harbor, tasks, binding=None, model="claude-haiku-5-5",      # a bill in the agent's env
               acting_agent={"name": "claude-code", "env": {"CLAUDE_CODE_USE_BEDROCK": "1"}})
    own = {"model": "claude-haiku-5-5", "base_url": "https://api.anthropic.com/v1", "api_key_env": "MY_ANTHROPIC_KEY",
           "messages_url": "https://api.anthropic.com"}                       # a backend with a key variable of its own
    with pytest.raises(ValueError, match="the only sign-in here"):
        builtin(own)
    monkeypatch.setenv("MY_ANTHROPIC_KEY", "sk-ant-mine-0123456789abcdef")
    builtin(own)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-0123456789abcdef")
    env = builtin().harbor_env()
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-fake-0123456789abcdef" and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    for on in ("1", "true", "YES"):
        monkeypatch.setenv("CLAUDE_FORCE_OAUTH", on)
        with pytest.raises(ValueError, match="CLAUDE_FORCE_OAUTH asks .* bill ANTHROPIC_API_KEY instead"):
            builtin()
    monkeypatch.setenv("CLAUDE_FORCE_OAUTH", "0")
    assert "CLAUDE_FORCE_OAUTH" not in builtin().harbor_env()
    with pytest.raises(ValueError, match="CLAUDE_FORCE_OAUTH asks"):         # the agent's own env, which Harbor reads
        make_agent(tmp_path, fake_harbor, tasks, acting_agent={"name": "claude-code", "env": {"CLAUDE_FORCE_OAUTH": 1}},
                   binding=None, model="claude-haiku-5-5")


def normalized(ref: str, tasks: Path, acting: dict | None = None):
    """The model a loaded config gives one of Harbor's own agents for `ref` (backends.normalize_config): its spec."""
    from hermit.agent.model.backends import normalize_config
    data = {"agent": {"name": "harbor", "params": {"tasks_dir": str(tasks), "web_search": False, "model": ref,
                                                   "acting_agent": acting or {"name": "claude-code"}}}}
    normalize_config(data)
    return data["agent"]["params"]["model"]


def test_harbors_own_claude_code_is_given_the_messages_root_and_anthropics_own_api_by_name(tmp_path, fake_harbor,
                                                                                             monkeypatch):
    """Claude Code appends /v1/messages to its base URL: Harbor's own claude-code on a model a loaded config bound is
    given the backend's Messages API root (Hermit's own Claude sessions are given it too), and a model at Anthropic's
    own API by name alone, which Harbor signs in to with its key (no base URL, so no skip_model_parsing); Harbor's
    own codex is given the endpoint as it is bound."""
    from hermit.agent.base.cli import CLAUDE_BILLING
    for name in (*CLAUDE_BILLING, "CLAUDE_FORCE_OAUTH", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")

    def agent(ref, acting):
        return make_agent(tmp_path, fake_harbor, tasks, acting_agent=acting, binding=None,
                          model=normalized(ref, tasks, acting))
    claude = agent("anthropic/claude-haiku-5-5", {"name": "claude-code"})
    assert claude.model_fields() == {"model_name": "anthropic/claude-haiku-5-5",
                                     "model_api_key_name": "ANTHROPIC_API_KEY"}
    assert "skip_model_parsing" not in claude.agent_config(dataset.index(tasks)[0])
    deepseek = agent("deepseek/deepseek-chat", {"name": "claude-code"}).model_fields()
    assert deepseek["base_url"] == "https://api.deepseek.com/anthropic" and deepseek["model_name"] == "deepseek-chat"
    assert agent("openai/gpt-4o", {"name": "codex"}).model_fields()["base_url"] == "https://api.openai.com/v1"


def test_a_harbor_mini_swe_agent_trial_is_refused_at_startup_what_its_base_agent_refuses(tmp_path, fake_harbor):
    """A Harbor trial's launch plan is rendered without the Base Agent's run, whose checks mini-swe-agent's are: they
    run on the render too, so web search (mini-swe-agent has none) or a model class whose replies Hermit does not
    read is refused at startup, not lost in the container."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    with pytest.raises(ValueError, match="mini-swe-agent has no web search"):
        make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "mini-swe-agent"}, web_search=True)
    with pytest.raises(ValueError, match="model_class 'litellm_response'"):
        make_agent(tmp_path, fake_harbor, tasks,
                   acting_agent={"cli": "mini-swe-agent", "settings": {"model_class": "litellm_response"}})
    make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "mini-swe-agent"})


def test_a_verifier_that_ran_out_of_its_time_is_the_agents_result_unless_verifier_timeout_says_infra(tmp_path,
                                                                                                      fake_harbor):
    """A verifier that ran out of its time (VerifierTimeoutError) is the agent's result by default (`verifier_timeout:
    agent`), as Harbor grades it: a timeout on the agent's solution, scored 0 and kept. `verifier_timeout: infra`, for
    a host whose load makes verifiers time out, makes it infra (run again); any other value is refused at startup."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    ran_out = errors.classify(trial_result(None, exc="VerifierTimeoutError"))
    timed = make_agent(tmp_path, fake_harbor, tasks).verifier_timeout_verdict(ran_out)
    # the session did not run out of its time: timed_out stays false (Continual Harness reads it as the agent's)
    assert (timed["status"], timed["failure_cause"], timed["timed_out"]) == ("timeout", "solution_timed_out", False)
    assert make_agent(tmp_path, fake_harbor, tasks, verifier_timeout="agent").verifier_timeout_verdict(ran_out) == timed
    loaded = make_agent(tmp_path, fake_harbor, tasks, verifier_timeout="infra")
    assert (loaded.verifier_timeout_verdict(ran_out)["status"], loaded.verifier_timeout_verdict(ran_out)["failure_cause"],
            loaded.verifier_timeout_verdict(ran_out)["timed_out"]) == ("infra", "verifier_failed", False)
    other = errors.classify(trial_result(None, exc="RewardFileNotFoundError"))       # only the timeout is the option's
    assert loaded.verifier_timeout_verdict(other) == other and other["status"] == "agent_error"
    with pytest.raises(ValueError, match="verifier_timeout is agent or infra"):
        make_agent(tmp_path, fake_harbor, tasks, verifier_timeout="host")


def test_runner_merges_a_benchmark_file_under_params(tmp_path, fake_harbor):
    tasks = tmp_path / "utils" / "tasks"
    make_task(tasks, "alpha")
    write(tmp_path / "utils" / "benchmark.yaml", yaml.safe_dump({
        "tasks_dir": "tasks", "model": "glm-5", "binding": BINDING, "effort": "medium", "docker_cleanup": "rmi_over_gb",
        "web_search": False, "dataset": {"ref": "local@1"},            # prepare's block, not a param
        "acting_agent": {"kwargs": {"version": "0.164.0"}, "env": {"A": "1"}}}))
    agent = HarborAgent({"benchmark": "../utils/benchmark.yaml", "effort": "high", "harbor": str(fake_harbor),
                         "infra_retries": 0,
                         "jobs_dir": str(tmp_path / "jobs"), "acting_agent": {"env": {"B": "2"}}},
                        base=tmp_path / "method")
    assert agent.tasks_dir == tasks.resolve() and agent.params["model"] == "glm-5" and "dataset" not in agent.params
    assert agent.params["effort"] == "high" and agent.docker_cleanup == "rmi_over_gb"
    assert agent.acting_agent == {"kwargs": {"version": "0.164.0"}, "env": {"B": "2"}, "cli": "codex"}


def test_job_config_runs_the_cli_class_on_its_launch_plan(tmp_path, fake_harbor):
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    chatgpt_login(tmp_path)
    signed_in = {"binding": None, "model": "gpt-5.6-luna", "login": "chatgpt"}
    # Harbor's own retries of a trial would each start from the sign-in copied before the first, which the first may
    # have refreshed (and so spent): refused on login: chatgpt
    with pytest.raises(ValueError, match=r"retry.max_retries > 0 with login: chatgpt"):
        make_agent(tmp_path, fake_harbor, tasks, **signed_in, retry={"max_retries": 1})
    agent = make_agent(tmp_path, fake_harbor, tasks, **signed_in,
                       effort="medium", codex_version="0.164.0", agent_timeout_sec=3600, retry={"max_retries": 0},
                       verifier={"override_timeout_sec": 900}, setup_timeout_sec=900,
                       acting_agent={"cli": "codex", "env": {"OMP_NUM_THREADS": "2"}})
    plan = tmp_path / "job" / "launch" / "plan.json"
    config = agent.job_config(row, "alpha-1", agent.agent_config(row, plan, None))
    harbor_agent = config["agents"][0]
    # The table's class on the plan; no model for Harbor to check credentials for, no sign-in variable
    assert harbor_agent["import_path"] == "harborside.codex:HermitCodex" and "model_name" not in harbor_agent
    assert harbor_agent["kwargs"] == {"plan": str(plan), "version": "0.164.0"}
    assert harbor_agent["env"] == {"OMP_NUM_THREADS": "2"} and "CODEX_FORCE_AUTH_JSON" not in json.dumps(config)
    assert harbor_agent["override_timeout_sec"] == 3600 and harbor_agent["override_setup_timeout_sec"] == 900
    assert "extra_allowed_hosts" not in harbor_agent                     # a public task: Harbor's own rule holds
    assert config["tasks"] == [{"path": str(tasks / "alpha")}] and config["n_concurrent_trials"] == 1
    assert config["retry"] == {"max_retries": 0} and config["verifier"] == {"override_timeout_sec": 900}
    retried = make_agent(tmp_path, fake_harbor, tasks, retry={"max_retries": 1})           # a key's: Harbor's retries
    assert retried.job_config(row, "alpha-1", retried.agent_config(row, plan, None))["retry"] == {"max_retries": 1}
    assert config["environment"] == {"delete": True}
    # A table CLI's own default version, when the config names none
    assert make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "pi"}).agent_config(row)["kwargs"] == \
        {"version": "1.5.0"}
    # A Harbor built-in keeps its model fields: the bound backend's endpoint and the key's variable
    bound = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"name": "terminus-2"}, binding=None,
                       model={"model": "glm", "base_url": "https://x/v1", "api_key_env": "X_KEY"})
    cfg = bound.agent_config(row, None, None)
    assert (cfg["model_name"], cfg["base_url"], cfg["model_api_key_name"]) == ("glm", "https://x/v1", "X_KEY")
    assert "env" not in cfg
    # The wait budget: agent + setup (setup_timeout_sec over Harbor's 360 s) + verifier (its override over the task's
    # 30 s) + teardown, plus the build for a Dockerfile task.
    assert agent.wait_budget(row) == 3600 + 900 + 900 + 600
    make_task(tasks, "built")
    built = dataset.index(tasks, include=["built"])[0]
    assert make_agent(tmp_path, fake_harbor, tasks).wait_budget(built) == 45 + 360 + 30 + 120 + 600
    # Harbor's timeout multipliers: into the job, and into the wait budget (the verifier's 30 s doubled; a pulled
    # image's start gets 2 x its 120 s limit more, a Dockerfile's build 3 x 120 s); unset, Harbor's default.
    assert not {"verifier_timeout_multiplier", "environment_build_timeout_multiplier"} & set(config)
    slow = make_agent(tmp_path, fake_harbor, tasks, verifier_timeout_multiplier=2,
                      environment_build_timeout_multiplier=3)
    scaled = slow.job_config(row, "alpha-1", slow.agent_config(row, None, None))
    assert (scaled["verifier_timeout_multiplier"], scaled["environment_build_timeout_multiplier"]) == (2.0, 3.0)
    assert slow.wait_budget(row) == 45 + 360 + 60 + 240 + 600
    assert slow.wait_budget(built) == 45 + 360 + 60 + 360 + 600
    with pytest.raises(ValueError, match="verifier_timeout_multiplier must be at least 1"):
        make_agent(tmp_path, fake_harbor, tasks, verifier_timeout_multiplier=0.5)
    # Another acting agent, no adapter possible.
    other = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"name": "mini-swe-agent", "kwargs": {"k": 1}},
                       binding=None, model=None)
    assert other.agent_config(row, None, None) == {"name": "mini-swe-agent", "kwargs": {"k": 1}}
    with pytest.raises(ValueError, match="takes no adapter"):
        other.agent_config(row, tmp_path, {"hooks": False})


def test_a_task_without_public_network_names_the_hosts_its_cli_reaches(tmp_path, fake_harbor):
    """Harbor's agent phase without the public network (task.toml): the job names the model's host and the address of
    the adapter's tools (extra_allowed_hosts, which replaces Harbor's own pick), and the CLI's web search is off
    whatever the config says."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "closed", network="no-network")
    make_task(tasks, "open")
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, web_search=True)
    assert agent.restricted(rows["closed"]) and not agent.restricted(rows["open"])
    hosts = agent.agent_config(rows["closed"], None, None, tools_host="172.17.0.1")["extra_allowed_hosts"]
    assert hosts == ["models.example", "172.17.0.1"]
    assert "extra_allowed_hosts" not in agent.agent_config(rows["open"], None, None, tools_host="172.17.0.1")
    assert agent.settings(rows["closed"])["web_search"] is False and agent.settings(rows["open"])["web_search"] is True
    chatgpt_login(tmp_path)
    login = make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt")
    assert login.agent_config(rows["closed"], None, None)["extra_allowed_hosts"] == ["chatgpt.com", "auth.openai.com"]


def test_a_mounted_binary_is_the_containers_program(tmp_path, fake_harbor):
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    release = tmp_path / "releases" / "0.164.0-x86_64-unknown-linux-musl"
    binary = write(release / "bin" / "codex", "#!/bin/sh\necho codex-cli 0.164.0\n", exe=True)
    write(release / "codex-package.json", '{"entrypoint": "bin/codex", "pathDir": "codex-path"}')
    agent = make_agent(tmp_path, fake_harbor, tasks, codex_bin=str(binary), codex_version="0.164.0")
    config = agent.job_config(row, "alpha-1", agent.agent_config(row, None, None))
    assert config["agents"][0]["kwargs"]["codex_bin"] == "/opt/hermit-codex/bin/codex"
    assert {"type": "bind", "source": str(release), "target": "/opt/hermit-codex", "read_only": True} in \
        config["environment"]["mounts"]
    plan = json.loads(agent._launch(row, tmp_path / "job", None, None).read_text())
    assert plan["command"][0] == "/opt/hermit-codex/bin/codex" and plan["path_prepend"] == ["/opt/hermit-codex/bin"]
    lone = write(tmp_path / "pin" / "dsh", "#!/bin/sh\necho 0.2.2\n", exe=True)   # a bare binary, of another CLI
    agent = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "dsh"}, dsh_bin=str(lone))
    config = agent.job_config(row, "alpha-1", agent.agent_config(row, None, None))
    assert config["agents"][0]["kwargs"]["dsh_bin"] == "/opt/hermit-dsh/dsh"
    assert config["environment"]["mounts"][-1] == {"type": "bind", "source": str(lone.parent),
                                                   "target": "/opt/hermit-dsh", "read_only": True}
    with pytest.raises(ValueError, match="not an executable file"):
        make_agent(tmp_path, fake_harbor, tasks, codex_bin=str(tmp_path / "nowhere"))


# ---- the runner: one trial through the fake harbor ------------------------------------------------------------------

def test_a_task_name_longer_than_harbors_trial_name_is_found(tmp_path, fake_harbor):
    # Harbor names the trial directory after the task's first 32 characters, '_' and '-' stripped from the end
    # (harbor/models/trial/config.py generate_trial_name): a longer name (AlgoTune, GSO, tau3-bench, OSWorld) must
    # still find its trial, or every one of its rollouts is an infra zero
    tasks = tmp_path / "tasks"
    name = "algotune-generalized-eigenvalues-complex"      # 41 characters; the 32nd is '-'
    make_task(tasks, name, image="example/long:1")
    row = dataset.index(tasks)[0]
    out = make_agent(tmp_path, fake_harbor, tasks).run(row, None)
    assert out.output["status"] == "ok" and out.output["reward"] == 1.0
    assert out.output["trial"] == f"{name[:32].rstrip('_-')}__abc1234" == "algotune-generalized-eigenvalues__abc1234"


def test_one_trial_through_the_fake_harbor(tmp_path, fake_harbor, monkeypatch):
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks)
    out = agent.run(row, None)
    assert out.output["status"] == "ok" and out.output["reward"] == 1.0 and out.output["trial"] == "alpha__abc1234"
    # Hermit's reading of the CLI's stream: its tokens and calls, priced at the backend's price, and its reply
    assert out.tokens == 120 and out.api_speed == {"model_calls": 1, "status": "ok"} and out.wall_s == 40.0
    assert out.cost_usd == pytest.approx((60 * 1.0 + 40 * 0.5 + 20 * 2.0) / 1e6)
    assert out.output["reply"] == "all done\n" and out.extra["session"]["served"] is True
    assert out.output["hooks_active"] is False and out.extra["harbor_exit"] == 0 and out.extra["deployed"] is None
    assert out.extra["judge"] == {"pass_reward": 1.0} and "unscored" not in out.extra
    log = Path(out.log)
    assert (log / "agent-codex.jsonl").is_file() and not (log / "agent" / "sessions").exists()
    job = Path(out.extra["harbor_job"])
    seen = json.loads(next(job.glob("alpha__*/job-seen.json")).read_text())
    assert seen["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(ROOT / "hermit" / "harbor")
    assert seen["env"]["TMPDIR"] == str(tmp_path / "jobs" / "tmp")
    assert seen["by_name"] == [KEY_ENV] and seen["token"] is False      # the key by its name; no Claude token
    harbor_agent = yaml.safe_load((job / "job.yaml").read_text())["agents"][0]
    plan = json.loads(Path(harbor_agent["kwargs"]["plan"]).read_text())
    assert plan["env_from_host"] == {KEY_ENV: KEY_ENV} and plan["stream"] == "agent-codex.jsonl"
    assert KEY not in job_files(job)                                     # neither the plan, the job nor the logs
    assert judge.score(row, out)["score"] == 1.0
    # With an adapter: staged, and the plan names the deployment
    with_adapter = agent.run(row, simple_template())
    kwargs = yaml.safe_load((Path(with_adapter.extra["harbor_job"]) / "job.yaml").read_text())["agents"][0]["kwargs"]
    deployed = json.loads(Path(kwargs["plan"]).read_text())["deploy"]
    assert Path(deployed["work_dir"], "AGENTS.md").is_file() and deployed["context_file"] == "AGENTS.md"
    assert with_adapter.output["hooks_active"] is False and with_adapter.extra["deployed"]["adapter_id"]
    assert with_adapter.extra["agent_metadata"]["adapter_deployed"] is True
    # A verifier whose tools never arrived: infra (run again), not a zero the agent earned.
    monkeypatch.setenv("FAKE_HARBOR_MODE", "no_tools")
    unreached = agent.run(row, None)
    assert unreached.output["status"] == "infra" and unreached.output["failure_cause"] == "verifier_failed"
    # A CLI that never started (its stream holds no event): infra, whatever the verifier found
    monkeypatch.setenv("FAKE_HARBOR_MODE", "silent")
    silent = agent.run(row, None)
    assert (silent.output["status"], silent.output["failure_cause"]) == ("infra", "session_failed")
    assert silent.extra["unscored"] == "session_failed" and "never started" in silent.extra["exception_message"]
    # A CLI that exited non-zero after it worked: the agent's own error, its work scored
    monkeypatch.setenv("FAKE_HARBOR_MODE", "agent_exit")
    exited = agent.run(row, None)
    assert exited.output["status"] == "agent_error" and exited.output["reward"] == 0.5
    assert judge.score(row, exited)["score"] == 0.5 and exited.tokens == 120
    # The wait budget kills a hung harbor.
    monkeypatch.setenv("FAKE_HARBOR_MODE", "hang")
    monkeypatch.setattr("hermit.harbor.docker.compose_down", lambda name, **k: None)
    hung = make_agent(tmp_path, fake_harbor, tasks, wait_budget_s=2).run(row, None)
    assert hung.output["status"] == "infra" and hung.output["failure_cause"] == "harbor_timeout" and hung.extra["timed_out"]
    # An API account's usage limit ends the run (LoginFailed), the trial's log kept; no later rollout of the runner
    # starts a trial
    monkeypatch.setenv("FAKE_HARBOR_MODE", "usage_limit")
    jobs = set((tmp_path / "jobs").glob("alpha-*"))
    with pytest.raises(LoginFailed, match=r"model_provider_failed \(ApiUsageLimitError\): limit; nothing later"):
        agent.run(row, None)
    [halted] = set((tmp_path / "jobs").glob("alpha-*")) - jobs
    assert (halted / "hermit-log" / "result.json").is_file()
    monkeypatch.setenv("FAKE_HARBOR_MODE", "ok")
    with pytest.raises(LoginFailed, match="nothing later in the run can succeed"):
        agent.run(row, None)
    assert set((tmp_path / "jobs").glob("alpha-*")) - jobs == {halted}           # no job after it


def rewrite_trials(monkeypatch, change) -> None:
    """Each trial the fake harbor leaves changed by `change(trial)` before the runner reads it."""
    run = HarborAgent.harbor_run

    def harbor_run(self, job, config, task, keep=()):
        got = run(self, job, config, task, keep)
        for trial in self.trial_dirs(job, task):
            change(trial)
        return got
    monkeypatch.setattr(HarborAgent, "harbor_run", harbor_run)


def verifier_said(exc: str | None, stdout: str, reward: float | None = None):
    """A change for rewrite_trials: the verifier wrote `stdout` and `reward` (None: no reward file), and Harbor recorded
    `exc` (None: no exception)."""
    def change(trial: Path) -> None:
        result = json.loads((trial / "result.json").read_text())
        result["verifier_result"] = {"rewards": {"reward": reward}} if reward is not None else None
        result["exception_info"] = {"exception_type": exc, "exception_message": f"{exc} happened",
                                    "exception_traceback": "tb"} if exc else None
        (trial / "result.json").write_text(json.dumps(result))
        (trial / "verifier" / "test-stdout.txt").write_text(stdout)
        if reward is None:
            (trial / "verifier" / "reward.txt").unlink()
    return change


def test_a_trial_whose_verifier_failed_on_the_agents_work_is_judged_and_kept_as_harbor_grades_it(tmp_path, fake_harbor,
                                                                                                 monkeypatch):
    """Through the runner: a verifier that left no reward is the agent's result, judged 0 and kept (in the mean); one
    that ran out of its time is the agent's timeout, judged 0, unless `verifier_timeout: infra` makes it infra
    (unscored: run again, and recorded as a failed run when it stays so); and one the host's full disk stopped is
    infra, its quoted output redacted."""
    from hermit.trainer.rollout import run_one
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    row = dataset.index(tasks)[0]
    failing = "FAILED test_outputs.py::test_answer - AssertionError: expected GOLD-ANSWER-42, got 41\n"
    with monkeypatch.context() as m:
        rewrite_trials(m, verifier_said("RewardFileNotFoundError", failing))
        record = run_one(row, None, make_agent(tmp_path, fake_harbor, tasks).run, judge.score)
    assert (record["status"], record["output"]["status"], record["output"]["failure_cause"]) == \
        ("ok", "agent_error", "verifier_failed")
    assert record["judge"]["score"] == 0.0 and "unscored" not in record["pipeline_extra"]
    with monkeypatch.context() as m:
        rewrite_trials(m, verifier_said("VerifierTimeoutError", failing))
        slow = run_one(row, None, make_agent(tmp_path, fake_harbor, tasks).run, judge.score)
        loaded = run_one(row, None, make_agent(tmp_path, fake_harbor, tasks, verifier_timeout="infra").run, judge.score)
    assert (slow["status"], slow["output"]["status"], slow["output"]["failure_cause"]) == \
        ("ok", "timeout", "solution_timed_out")
    assert slow["judge"]["score"] == 0.0 and "solution" in slow["judge"]["feedback"]
    unscored_record(loaded, "verifier_failed")
    with monkeypatch.context() as m:
        rewrite_trials(m, verifier_said(None, failing + "OSError: [Errno 28] No space left on device\n", reward=0.0))
        full = make_agent(tmp_path, fake_harbor, tasks, verifier_redact=[r"GOLD-ANSWER-\d+"]).run(row, None)
    assert (full.output["status"], full.extra["unscored"]) == ("infra", "verifier_failed")
    assert "No space left on device" in full.extra["exception_message"]
    assert "GOLD-ANSWER-42" not in full.extra["exception_message"]
    assert "GOLD-ANSWER-42" not in (Path(full.log) / "exception.txt").read_text()


def test_an_adapter_this_machine_could_not_stage_is_infra_and_one_whose_own_deployment_failed_the_agents_0(
        tmp_path, fake_harbor, monkeypatch):
    """Staging the adapter (its host's deployment, its per-task context) runs on this machine before the trial: an
    OSError there (a full disk) is the host's, infra (run again), not the agent's 0, for a trial and for a stream's step
    alike, and neither a trial nor a stream's driver is started; a RuntimeError, the adapter's deployment or context
    failing in its child process, is the agent's result (what the child printed could be the adapter's own words):
    agent_error (adapter_failed), scored 0 with its message as the feedback, a trial python -m hermit.harbor.report
    counts among the scored ones (its pass@1 a fail), as hermit eval's mean does."""
    from hermit.harbor import report
    from hermit.trainer.rollout import run_one
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    make_task(tasks, "flow", steps=("s1", "s2"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks)
    for failure in (OSError(28, "No space left on device"),
                    RuntimeError("deploying the adapter failed: No space left on device")):   # the adapter could say it
        def stage(*args, **kwargs):
            raise failure
        monkeypatch.setattr("hermit.harbor.runner.stage", stage)
        for row in (rows["alpha"], rows["flow:s1"]):
            record = run_one(row, simple_template(), agent.run, judge.score)
            if isinstance(failure, OSError):
                unscored_record(record, "staging_failed")
                assert record["output"]["failure_cause"] == "staging_failed", row["id"]
                assert str(failure) in record["pipeline_extra"]["exception_message"]
            else:                                    # the agent's result: scored 0, its message the feedback
                assert (record["status"], record["output"]["status"], record["output"]["failure_cause"]) == \
                    ("ok", "agent_error", "adapter_failed"), row["id"]
                assert record["judge"]["score"] == 0.0 and "deploying the adapter failed" in record["judge"]["feedback"]
                assert (report.kind(record), report.passed(record)) == ("scored", False), row["id"]
    assert not list((tmp_path / "jobs").glob("alpha-*/alpha__*"))                     # no trial ran
    assert not list((tmp_path / "jobs" / "streams").glob("flow__s1__*/driver.json"))    # no driver started



def test_this_machines_failure_while_a_trials_job_or_launch_is_written_is_infra_and_starts_no_trial(
        tmp_path, fake_harbor, monkeypatch):
    """Before its container a trial writes its job directory, its launch plan and its job file on this machine: an
    OSError there (a full disk, a job directory that exists) is the host's, infra (run again), never the agent's 0."""
    from types import SimpleNamespace
    from hermit.trainer.rollout import run_one
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks)

    def render(*args, **kwargs):
        raise OSError(28, "No space left on device")
    with monkeypatch.context() as patch:
        patch.setattr("hermit.harbor.launch.render", render)
        record = run_one(row, simple_template(), agent.run, judge.score)
    unscored_record(record, "staging_failed")
    assert "No space left on device" in record["pipeline_extra"]["exception_message"]
    (tmp_path / "jobs" / "alpha-00000000").write_text("")                     # where the job directory goes
    monkeypatch.setattr("hermit.harbor.runner.uuid.uuid4", lambda: SimpleNamespace(hex="0" * 32))
    record = run_one(row, simple_template(), agent.run, judge.score)
    unscored_record(record, "staging_failed")
    assert not list((tmp_path / "jobs").glob("alpha-*/alpha__*"))                     # no trial ran


def test_a_chatgpt_trial_hands_the_refreshed_sign_in_back(tmp_path, fake_harbor):
    """On `login: chatgpt` the shared sign-in goes into the CLI's home as a secret file, comes back from the
    container after the trial and, a later refresh of it, becomes the shared copy; no copy is left in the job."""
    from hermit.agent.base import login
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    chatgpt_login(tmp_path)
    agent = make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt")
    out = agent.run(row, None)
    assert out.output["status"] == "ok" and out.cost_usd is None and out.tokens == 120   # the plan's: no price
    shared = json.loads(login.master(login.ROOT).read_text())
    assert shared["tokens"]["refresh_token"] == "rt-rotated-stand-in"         # the container's later refresh
    job = Path(out.extra["harbor_job"])
    assert not list(job.rglob("auth.json")) and "rt-stand-in" not in job_files(job)
    plan = json.loads(next(job.glob("launch/plan.json")).read_text())
    assert plan["home"]["secret_files"] == ["auth.json"] and plan["return_files"][0]["from"].endswith("/auth.json")
    assert out.extra["session"]["served"] is True


def test_a_chatgpt_trials_sign_in_replaces_the_copy_only_as_a_refresh_stamped_on_its_environments_clock(
        tmp_path, fake_harbor, monkeypatch):
    """The container's session can write the sign-in it hands back (its commands run as the file's owner, with no
    Codex sandbox there): a trial whose container left one without its tokens leaves the shared copy as it was (it
    replaced the copy before, and every session after it started signed out) and no copy in the job. A refresh stamped
    ahead of this machine's clock is refused from Harbor's local Docker environment, whose containers share the clock,
    and taken from another environment, whose clock may run up to an hour ahead (a cloud sandbox's)."""
    from datetime import datetime, timedelta, timezone
    from hermit.agent.base import login
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    chatgpt_login(tmp_path)
    monkeypatch.setenv("FAKE_RETURNED", json.dumps({"last_refresh": datetime.now(timezone.utc).isoformat(),
                                                    "tokens": {"account_id": "acct-1"}}))
    out = make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt").run(row, None)
    assert out.output["status"] == "ok" and not list(Path(out.extra["harbor_job"]).rglob("auth.json"))
    assert json.loads(login.master(login.ROOT).read_text())["tokens"]["refresh_token"] == "rt-stand-in"   # the user's
    ahead = json.dumps({"last_refresh": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
                        "tokens": {"account_id": "acct-1", "access_token": "at-stand-in", "refresh_token": "rt-ahead"}})
    for kind, taken in ((None, False), ("docker", False), ("modal", True)):
        job = tmp_path / "ahead" / str(kind)
        (job / "launch" / "return").mkdir(parents=True)
        (job / "launch" / "return" / "auth.json").write_text(ahead)
        agent = make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt",
                           **({"environment": {"type": kind}} if kind else {}))
        agent.sign_in_back(job)
        shared = json.loads(login.master(login.ROOT).read_text())
        assert (shared["tokens"]["refresh_token"] == "rt-ahead") is taken, kind
        assert not (job / "launch" / "return" / "auth.json").exists(), kind


def test_a_codex_trial_on_chatgpt_notes_the_plans_usage_from_its_session_logs(tmp_path, fake_harbor, monkeypatch):
    """On `login: chatgpt` the launch plan collects the Codex home's session logs, whose rate-limit reports say how
    much of the plan is used: the trial notes the report (as a Hermit session of Codex does), so that the plan's
    MAX_USED holds back the trials after it; the logs (the session's transcripts) stay out of the curated log."""
    from hermit.agent.base import login
    from hermit.harbor import clis
    resets = __import__("time").time() + 3600
    monkeypatch.setenv("FAKE_RATE_LIMITS", json.dumps({"primary": {"used_percent": 97.0, "resets_at": resets}}))
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    chatgpt_login(tmp_path)
    out = make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt").run(row, None)
    plan = json.loads((Path(out.extra["harbor_job"]) / "launch" / "plan.json").read_text())
    assert {"from": f"{clis.CONTAINER_HOME}/sessions", "to": "sessions"} in plan["collect"]
    assert out.output["status"] == "ok" and out.extra["session"]["plan_report"]["primary"]["used_percent"] == 97.0
    (login.PLAN / "MAX_USED").write_text("90")
    assert "97%" in login.held(login.PLAN)                                # the trials after it wait for the window
    assert not (Path(out.log) / "agent" / "hermit-collect" / "sessions").exists()


def test_a_collected_codex_session_log_that_cannot_be_read_gives_no_plan_report(tmp_path, fake_harbor, monkeypatch):
    """The container wrote the collected session logs: one that cannot be read (usage_of raises on it) is no report
    of the plan, and the session is still read (the trial does not fail)."""
    from hermit.agent.base import login
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    row = dataset.index(tasks)[0]
    chatgpt_login(tmp_path)
    agent = make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt")
    job, trial = tmp_path / "job", tmp_path / "trial"
    plan = agent._launch(row, job, None, None)
    write(trial / "agent" / "agent-codex.jsonl", CODEX_STREAM)
    log = write(trial / "agent" / "hermit-collect" / "sessions" / "2026" / "11" / "11" / "rollout-1.jsonl",
                '{"timestamp": "t", "payload": {"rate_limits": {"primary": {"used_percent": 97.0}}}}\n')
    seen = []

    def unreadable(home):
        seen.append(Path(home))
        raise PermissionError(13, "Permission denied", str(log))
    monkeypatch.setattr(login, "usage_of", unreadable)
    facts = agent.session(trial, plan, None, job, row)[0]
    assert seen == [trial / "agent" / "hermit-collect"]                 # the collected home was read, and it raised
    assert facts["served"] is True and "plan_report" not in facts


def test_a_trial_is_priced_at_the_runs_explicit_price(tmp_path, fake_harbor):
    """A price given beside the model (agent.params.price, which the config's loading keeps over the backend's) is
    the one each trial's usage is priced at, and the one the answer agent's endpoint serves the model at."""
    price = {"input": 10.0, "cached_input": 5.0, "output": 20.0}
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    out = make_agent(tmp_path, fake_harbor, tasks, price=price).run(row, None)
    assert out.output["status"] == "ok" and out.cost_usd == pytest.approx((60 * 10.0 + 40 * 5.0 + 20 * 20.0) / 1e6)
    answer = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "answer"}, web_search=None, price=price)
    assert answer.answer_model()["price"] == price


def test_a_stream_the_session_left_as_a_link_is_no_stream(tmp_path, fake_harbor, monkeypatch):
    """The container can write the trial's agent/ folder: a link the session left in its stream's place (to a file
    of this machine) is never read, so nothing of that file reaches the rollout's output or its log."""
    host_file = write(tmp_path / "host-only" / "notes.txt", "db_url=postgres://u:not-a-real-password@db/x\n")
    monkeypatch.setenv("FAKE_LINK_STREAM_TO", str(host_file))
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    out = make_agent(tmp_path, fake_harbor, tasks).run(row, None)
    assert out.extra.get("session") is None and "reply" not in out.output       # no session to read
    assert "not-a-real-password" not in json.dumps([out.output, out.extra], default=str)
    assert "not-a-real-password" not in job_files(Path(out.extra["harbor_job"]))   # its curated log included


def test_json_the_container_wrote_nested_past_the_parsers_depth_is_read_as_json_that_does_not_parse(
        tmp_path, fake_harbor):
    """What the container wrote may be nested past the JSON parser's depth (a RecursionError, not a ValueError): the
    runner reads a line of the CLI's stream so nested as no event, as it reads a line that does not parse; the answer
    agent's answer.json so nested as no reply and no answer; a Harbor agent's trajectory.json as no model call: never
    an error that fails the trial."""
    from hermit.harbor import runner
    nested = '{"a": ' + "[" * 100_000 + "]" * 100_000 + "}"
    init = {"type": "system", "subtype": "init", "mcp_servers": [{"name": "calcdemo", "status": "connected"}]}
    assert list(runner._events(f"{nested}\n{json.dumps(init)}\n")) == [init]
    assert curate.model_calls(write(tmp_path / "trajectory.json", nested)) == 0
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "answer"}, web_search=None)
    plan = write(tmp_path / "job" / "launch" / "plan.json", "{}")
    write(tmp_path / "trial" / "agent" / "answer.json", nested)
    assert agent.session(tmp_path / "trial", plan, None, tmp_path / "job", row) == \
        ({"started": False}, None, {"reply": None, "answer": None})


def test_an_answer_file_that_is_json_but_no_object_is_no_answer(tmp_path, fake_harbor):
    """The answer agent's answer.json is the container's to write: one that is JSON but no object (a list, a string, a
    number, null) is read as no reply and no answer, never an error that fails the trial."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "answer"}, web_search=None)
    plan = write(tmp_path / "job" / "launch" / "plan.json", "{}")
    for text in ('["42"]', '"42"', "42", "null"):
        write(tmp_path / "trial" / "agent" / "answer.json", text)
        assert agent.session(tmp_path / "trial", plan, None, tmp_path / "job", row) == \
            ({"started": False}, None, {"reply": None, "answer": None}), text


def test_a_cli_whose_only_line_holds_a_unicode_line_separator_before_a_brace_never_started(tmp_path, fake_harbor):
    """A trial's stream is read as Hermit's own run reads one: U+0085, U+2028 and U+2029 end no line, so a CLI's own
    line that holds one before a `{` (a value it quotes) starts no event, and a trial whose CLI wrote that line alone
    never started a session: infra (session_failed), not the agent's 0."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks)
    job, trial = tmp_path / "job", tmp_path / "trial"
    plan = agent._launch(row, job, None, None)
    exited = errors.classify(trial_result(0.0, exc="NonZeroAgentExitCodeError"))
    stream = trial / "agent" / "agent-codex.jsonl"
    stream.parent.mkdir(parents=True)
    for sep in ("\x85", "\u2028", "\u2029"):
        stream.write_bytes(f"Error loading config.toml: unknown key{sep}{{ model = 1 }}\n".encode("utf-8"))
        facts = agent.session(trial, plan, None, job, row)[0]
        assert facts["started"] is False, repr(sep)
        verdict = HarborAgent.session_verdict(exited, facts)
        assert (verdict["status"], verdict["failure_cause"]) == ("infra", "session_failed"), repr(sep)


def test_a_stream_whose_progress_line_ends_at_a_carriage_return_before_an_event_reads_the_event(tmp_path, fake_harbor):
    """A trial's stream is read as Hermit's own run reads one (Path.read_text): a "\\r" ends a line, as "\\r\\n" and
    "\\n" do. So the CLI's own progress line ending at "\\r" ("working 50%\\r") before an event on the same line leaves
    the event whole: a command's output that names a lost sign-in is the session's work, not a notice of the CLI's,
    and a turn that completed is read as served."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks)
    job, trial = tmp_path / "job", tmp_path / "trial"
    plan = agent._launch(row, job, None, None)
    command = {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "cat auth.log",
                                                  "aggregated_output": "Not logged in", "exit_code": 0,
                                                  "status": "completed"}}
    done = {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20}}
    start = '{"type":"thread.started","thread_id":"th-1"}\r\n{"type":"turn.started"}\n'
    cut = start + f"working 50%\r{json.dumps(command)}\n"                       # no turn completed: cut off
    stream = trial / "agent" / "agent-codex.jsonl"
    stream.parent.mkdir(parents=True)
    stream.write_bytes(cut.encode("utf-8"))
    facts = agent.session(trial, plan, None, job, row)[0]
    assert (facts["served"], facts["login_failed"]) == (False, False)
    stream.write_bytes((cut + f"working 90%\r{json.dumps(done)}\n").encode("utf-8"))
    facts = agent.session(trial, plan, None, job, row)[0]
    assert (facts["served"], facts["login_failed"], facts["tokens"]) == (True, False, 120)


# A Codex session that ran out of the agent's time: the provider's notice it recovered from, its work, no turn completed
TIMED_OUT = ('{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
             '{"type":"error","message":"Reconnecting... 1/5 (stream disconnected before completion: error sending '
             'request)"}\n'
             '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"working"}}\n')


def test_a_trial_that_ran_out_of_its_time_after_a_notice_it_recovered_from_is_the_agents_result(
        tmp_path, fake_harbor, monkeypatch):
    """A session that ran out of the agent's time has no completed turn: a provider's notice earlier in its stream,
    which the CLI recovered from, does not make it infra. The trial is the agent's result, its work scored, and on a
    login it is not run again, as Hermit's own sessions keep a timed-out session."""
    monkeypatch.setenv("FAKE_STREAMS", str(write(tmp_path / "timed-out.jsonl", TIMED_OUT)))
    monkeypatch.setenv("FAKE_HARBOR_MODE", "timeout")
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    chatgpt_login(tmp_path)
    out = make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt",
                     busy_retries=1, busy_wait_s=0).run(row, None)
    assert (out.output["status"], out.output["failure_cause"], out.output["reward"]) == \
        ("timeout", "agent_exhausted_budget", 1.0)
    assert "unscored" not in out.extra and judge.score(row, out)["score"] == 1.0
    assert (tmp_path / "jobs" / ".fake-calls").read_text() == "1"            # not run again


# A Codex session that met a 401 it reconnected from (Codex retries it), went on working and ran out of the agent's time
RECOVERED_401 = ('{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
                 '{"type":"error","message":"Reconnecting... 1/5 (unexpected status 401 Unauthorized: Missing bearer '
                 'or basic authentication in header, url: https://api.openai.com/v1/responses)"}\n'
                 '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"working"}}\n')


def test_a_codex_trial_that_ran_out_of_its_time_after_a_401_it_reconnected_from_is_the_agents_timeout(
        tmp_path, fake_harbor, monkeypatch):
    """Codex retries a 401 it meets (its reconnect notice), as it does a 403; one that lasts fails the turn. A session
    that recovered from one and then ran out of the agent's time is the agent's timeout, its work scored, never a lost
    sign-in that halts the run."""
    monkeypatch.setenv("FAKE_DEFAULT_STREAM", RECOVERED_401)
    monkeypatch.setenv("FAKE_HARBOR_MODE", "timeout")
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks)
    out = agent.run(row, None)
    assert (out.output["status"], out.output["reward"], out.extra["halt"]) == ("timeout", 1.0, False)
    assert out.extra["session"]["login_failed"] is False and "unscored" not in out.extra
    assert not agent._stop.is_set()


# A Codex session cut off by the agent's time after Codex logged a patch whose lines it did not find (harborside's
# `> stream 2>&1` puts its stderr in the stream): the record, then those lines, the model's own text
QUOTED_SIGN_IN = ('{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
                  '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"fixing the handler"}}\n'
                  '2026-11-12T01:02:03.456789Z ERROR codex_core::tools::router: error=apply_patch verification failed: '
                  'Failed to find expected lines in /app/server/auth.py:\n'
                  '        return jsonify(error="Invalid API key"), 401\n'
                  '{"type":"item.started","item":{"id":"i2","type":"command_execution","command":"pytest -q",'
                  '"aggregated_output":"","exit_code":null,"status":"in_progress"}}\n')


def test_a_codex_trial_that_ran_out_of_its_time_after_its_log_quoted_a_lost_sign_in_is_the_agents_timeout(
        tmp_path, fake_harbor, monkeypatch):
    """What Codex's log quotes of the session's tool calls is the session's own work: a trial that ran out of the
    agent's time after the log quoted the words of a lost sign-in is the agent's timeout, its work scored, never a
    lost sign-in that halts the run (the trial after it runs)."""
    monkeypatch.setenv("FAKE_DEFAULT_STREAM", QUOTED_SIGN_IN)
    monkeypatch.setenv("FAKE_HARBOR_MODE", "timeout")
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    make_task(tasks, "beta", image="example/beta:1")
    rows = dataset.index(tasks)
    agent = make_agent(tmp_path, fake_harbor, tasks)
    for row in rows:
        out = agent.run(row, None)
        assert (out.output["status"], out.output["reward"], out.extra["halt"]) == ("timeout", 1.0, False), row["id"]
        assert out.extra["session"]["login_failed"] is False and "unscored" not in out.extra
    assert not agent._stop.is_set()


# A Codex session the provider refused for its sign-in (a revoked ChatGPT login, a key it does not take)
SIGN_IN_REFUSED = ('{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
                   '{"type":"error","message":"unexpected status 401 Unauthorized: Your authentication token has '
                   'been invalidated. Please try signing in again."}\n'
                   '{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized"}}\n')


def test_a_codex_session_whose_sign_in_the_provider_refused_ends_the_run(tmp_path, fake_harbor, monkeypatch, capsys):
    """A 401 for the session's sign-in is no answer of the agent's, and no wait restores it: the run ends (LoginFailed,
    which hermit.trainer.rollout passes up, as for a Hermit session's lost sign-in; Harbor's own Codex agent's
    AgentAuthenticationError did so too), naming the cause and the exit Harbor kept, the trial's log kept; no later
    rollout of the runner starts a trial."""
    monkeypatch.setenv("FAKE_DEFAULT_STREAM", SIGN_IN_REFUSED)
    monkeypatch.setenv("FAKE_HARBOR_MODE", "agent_exit")                     # the CLI exited 1; the verifier ran
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks)
    lost = r"a Harbor trial \(alpha-\w+\): login_failed \(NonZeroAgentExitCodeError\): "
    with pytest.raises(LoginFailed, match=lost) as ended:
        agent.run(row, None)
    assert "nothing later in the run can succeed" in str(ended.value) and agent._stop.is_set()
    [job] = (tmp_path / "jobs").glob("alpha-*")
    assert (job / "hermit-log" / "result.json").is_file() and f"(the trial's job: {job})" in capsys.readouterr().err
    with pytest.raises(LoginFailed):
        agent.run(row, None)
    assert len(list((tmp_path / "jobs").glob("alpha-*"))) == 1                 # no trial after it
    # through the rollouts of hermit train and hermit eval (hermit.trainer.rollout.run_tasks): the run ends there, the
    # row no record of a failed run
    from hermit.trainer.rollout import run_tasks
    with pytest.raises(LoginFailed, match="login_failed"):
        run_tasks([row], None, make_agent(tmp_path, fake_harbor, tasks).run, judge.score, concurrency=1)


def test_a_codex_session_an_api_account_refused_for_its_bill_ends_the_run_and_for_its_model_is_model_rejected(
        tmp_path, fake_harbor, monkeypatch):
    """Codex on an API account (a key, no login): a session the provider refused for the account's bill (its quota
    spent, a 402) ends the run, as an API account's usage limit does (usage_limit, a halt: LoginFailed), since nothing
    later can be served; one it refused for the run's configuration (a model not there) is infra (model_rejected) and
    never run again, as the other CLIs' are, never the agent's 0; the run goes on. The plan's usage-limit words that
    Codex's log quotes of a tool call (a file the session patched) are no refusal: the trial is the agent's result, and
    the run goes on (the Base Agent's reading of the whole stream takes them for the account's limit, which on an API
    account ends the run)."""
    monkeypatch.setenv("FAKE_HARBOR_MODE", "agent_exit")                     # the CLI exited 1; the verifier ran
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    start = '{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
    failed = '{"type":"turn.failed","error":{"message":"%s"}}\n'
    for n, why in enumerate(("Quota exceeded. Check your plan and billing details.",
                             "unexpected status 402 Payment Required: {\\\"detail\\\":\\\"payment required\\\"}")):
        monkeypatch.setenv("FAKE_DEFAULT_STREAM", start + '{"type":"error","message":"%s"}\n' % why + failed % why)
        jobs = tmp_path / f"jobs-bill-{n}"
        agent = make_agent(tmp_path, fake_harbor, tasks, infra_retries=2, infra_wait_s=0, jobs_dir=str(jobs))
        with pytest.raises(LoginFailed, match="the provider does not bill the account"):
            agent.run(row, None)
        assert agent._stop.is_set(), why
        assert len(list(jobs.glob("alpha-*"))) == 1, why                       # never run again
    why = "unexpected status 404 Not Found: The model `gpt-x` does not exist"
    monkeypatch.setenv("FAKE_DEFAULT_STREAM", start + '{"type":"error","message":"%s"}\n' % why + failed % why)
    jobs = tmp_path / "jobs-model"
    agent = make_agent(tmp_path, fake_harbor, tasks, infra_retries=2, infra_wait_s=0, jobs_dir=str(jobs))
    out = agent.run(row, None)
    assert (out.output["status"], out.output["failure_cause"], out.extra["halt"]) == ("infra", "model_rejected", False)
    assert out.extra["unscored"] == "model_rejected" and not agent._stop.is_set()
    assert len(list(jobs.glob("alpha-*"))) == 1                                # never run again
    quoted = (start + "2026-11-12T01:02:03.456789Z ERROR codex_core::tools::router: error=apply_patch verification "
              "failed: Failed to find expected lines in /app/limits.py:\n"
              '{"note": "a JSON line of the patch"}\n'
              "    raise Exhausted(\"You've hit your usage limit. Try again in 2 hours.\")\n" + failed % "exit")
    monkeypatch.setenv("FAKE_DEFAULT_STREAM", quoted)
    agent = make_agent(tmp_path, fake_harbor, tasks, jobs_dir=str(tmp_path / "jobs-quoted"))
    out = agent.run(row, None)
    assert (out.output["status"], out.extra["halt"], out.extra["session"]["refused"]) == ("agent_error", False, False)
    assert not agent._stop.is_set()


def test_a_login_trial_the_plan_refused_holds_the_plan_with_the_runs_stop(tmp_path, fake_harbor, monkeypatch):
    """On a login, a trial whose session the plan refused for its usage limit (Codex's own words) holds every session
    until the window resets and runs again then; the hold gets the run's stop, so that a hold that cannot be written
    (main's hold then waits five minutes itself, in the trial's slot) ends as soon as the run ends."""
    from hermit.agent.base import login as subscription
    monkeypatch.setenv("FAKE_HARBOR_MODE", "agent_exit")
    limit = "You've hit your usage limit. Upgrade to Pro or try again in 2 hours."
    start = '{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
    events = start + ('{"type":"error","message":"%s"}\n' % limit
                      + '{"type":"turn.failed","error":{"message":"%s"}}\n' % limit)
    monkeypatch.setenv("FAKE_STREAMS", str(write(tmp_path / "limit.jsonl", events)))
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    chatgpt_login(tmp_path)
    agent = make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt",
                       plan_dir=str(tmp_path / "plan"))
    holds = []

    def hold(plan, until=None, why="session_refused", *, cancel=None, max_s=None):
        holds.append(cancel)
        agent._stop.set()                                    # the run ends meanwhile (another rollout's lost sign-in)
    monkeypatch.setattr(subscription, "hold", hold)
    with pytest.raises(LoginFailed):
        agent.run(row, None)
    assert holds == [agent._stop] and (tmp_path / "jobs" / ".fake-calls").read_text() == "1"


# A Codex session that met a provider's notice (a reconnect), then lost its sign-in: the turn failed with a 401
LOST_AFTER_NOTICE = ('{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
                     '{"type":"error","message":"Reconnecting... 1/5 (stream disconnected before completion: error '
                     'sending request)"}\n'
                     '{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized: Your '
                     'authentication token has been invalidated."}}\n')
# ... and one that reported the lost sign-in, then failed its turn on a dropped stream: lost, and unserved too
LOST_THEN_DROPPED = ('{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
                     '{"type":"error","message":"unexpected status 401 Unauthorized: Your authentication token has '
                     'been invalidated."}\n'
                     '{"type":"turn.failed","error":{"message":"stream disconnected before completion: error sending '
                     'request"}}\n')


def test_a_login_trial_that_lost_its_sign_in_after_a_providers_notice_is_never_run_again(tmp_path, fake_harbor,
                                                                                           monkeypatch):
    """On a login, a session whose stream holds a provider's notice and a lost sign-in is the lost sign-in: it ends the
    run (LoginFailed), never run again as one the provider could not serve; whether the notice is one it recovered
    from (a reconnect) or the failure its turn ended with."""
    monkeypatch.setenv("FAKE_HARBOR_MODE", "agent_exit")
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    chatgpt_login(tmp_path)
    for n, events in enumerate((LOST_AFTER_NOTICE, LOST_THEN_DROPPED)):
        monkeypatch.setenv("FAKE_STREAMS", str(write(tmp_path / f"lost-{n}.jsonl", events)))
        jobs = tmp_path / f"jobs-{n}"
        agent = make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt",
                           busy_retries=2, busy_wait_s=0, jobs_dir=str(jobs))
        with pytest.raises(LoginFailed, match="login_failed"):
            agent.run(row, None)
        assert agent._stop.is_set() and (jobs / ".fake-calls").read_text() == "1", n        # not run again

# A Codex 0.164.0 session (the standalone release, Linux; its stderr merged in, as harborside keeps it) on a ChatGPT
# login whose requests to chatgpt.com all failed, from a recorded TB2.0 trial on gpt-5.6-luna (2026-11-08), its ids and
# times replaced and its repeated lines cut, every message as Codex wrote it: its refresh of its models and its MCP
# client failed ("error sending request"), it reconnected over WebSockets, fell back to HTTPS (an `error` item),
# reconnected again, then failed its turn with "workspace routing discovery failed" and exited 1
UNREACHABLE = (
    "Reading additional input from stdin...\n"
    "2026-11-08T05:13:56.000000Z ERROR codex_models_manager::manager: failed to refresh available mod"
    "els: Connection failed: error sending request\n"
    "{\"type\":\"thread.started\",\"thread_id\":\"00000000-0000-7000-8000-000000000001\"}\n"
    "2026-11-08T05:13:57.000000Z ERROR rmcp::transport::worker: worker quit with fatal: Transport cha"
    "nnel closed, when Client(HttpRequest(HttpRequest(\"http/request failed: error sending request for"
    " url (https://chatgpt.com/backend-api/ps/mcp)\")))\n"
    "{\"type\":\"turn.started\"}\n"
    "{\"type\":\"error\",\"message\":\"Reconnecting... 4/5 (workspace routing discovery failed)\"}\n"
    "{\"type\":\"error\",\"message\":\"Reconnecting... 5/5 (workspace routing discovery failed)\"}\n"
    "{\"type\":\"item.completed\",\"item\":{\"id\":\"item_0\",\"type\":\"error\",\"message\":\"Falling back from WebSo"
    "ckets to HTTPS transport. workspace routing discovery failed\"}}\n"
    "{\"type\":\"error\",\"message\":\"Reconnecting... 4/5 (workspace routing discovery failed)\"}\n"
    "{\"type\":\"error\",\"message\":\"Reconnecting... 5/5 (workspace routing discovery failed)\"}\n"
    "{\"type\":\"error\",\"message\":\"workspace routing discovery failed\"}\n"
    "{\"type\":\"turn.failed\",\"error\":{\"message\":\"workspace routing discovery failed\"}}\n"
)


def test_a_login_trial_whose_codex_could_not_reach_its_provider_is_run_again_and_never_the_agents_result(
        tmp_path, fake_harbor, monkeypatch):
    """Codex 0.164 on a ChatGPT login whose requests to chatgpt.com fail retries them, fails its turn with "workspace
    routing discovery failed" and exits 1 (UNREACHABLE): the provider could not serve the trial, which runs again
    after a pause, busy_retries times over the whole trial (infra_retries, here 2, adds none: one count, never their
    product); when every run met the same it is infra (`model_provider_failed`, unscored, no halt), never the agent's
    result scored as Harbor scored it."""
    monkeypatch.setenv("FAKE_STREAMS", str(write(tmp_path / "unreachable.jsonl", UNREACHABLE)))
    monkeypatch.setenv("FAKE_HARBOR_MODE", "agent_exit")                     # the CLI exited 1; the verifier ran
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    chatgpt_login(tmp_path)
    out = make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt",
                     busy_retries=1, busy_wait_s=0, infra_retries=2, infra_wait_s=0).run(row, None)
    assert (out.output["status"], out.output["failure_cause"], out.extra["unscored"], out.extra["halt"]) == \
        ("infra", "model_provider_failed", "model_provider_failed", False)
    assert out.extra["session"]["unavailable"] is True and out.extra["session"]["login_failed"] is False
    assert (tmp_path / "jobs" / ".fake-calls").read_text() == "2"             # run again once (busy_retries) ...
    assert [a["unscored"] for a in out.extra["attempts"]] == ["model_provider_failed"] * 2   # infra_retries: no more


def test_the_answer_agent_asks_the_model_through_an_endpoint_of_this_machine(tmp_path, fake_harbor):
    """`cli: answer`: one model call from Harbor's process to an endpoint the trial serves on 127.0.0.1, which relays
    it to the backend with the key (never in the plan or the job); the answer, the reply and the usage are Hermit's."""
    seen = []

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.append((self.path, self.headers.get("Authorization"),
                         json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            body = json.dumps({"id": "c1", "object": "chat.completion", "model": "glm-5",
                               "choices": [{"index": 0, "message": {"role": "assistant", "content": "The answer is 42"},
                                            "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 30, "completion_tokens": 5}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        tasks = tmp_path / "tasks"
        make_task(tasks, "alpha", image="example/alpha:1")
        row = dataset.index(tasks)[0]
        local = {**BINDING, "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1"}
        agent = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "answer"}, binding=local,
                           web_search=None, answer={"answer_file": "/app/answer.txt"})
        out = agent.run(row, simple_template())
    finally:
        server.shutdown()
    assert out.output["status"] == "ok" and out.output["answer"] == "42" and out.output["reply"] == "The answer is 42"
    assert out.tokens == 35 and out.api_speed["model_calls"] == 1
    assert out.cost_usd == pytest.approx((30 * 1.0 + 5 * 2.0) / 1e6)
    path, authorization, request = seen[0]
    assert path == "/v1/chat/completions" and authorization == f"Bearer {KEY}" and request["model"] == "glm-5"
    context = Path(out.extra["deployed"]["context_file"]).read_text()
    assert context and request["messages"][0] == {"role": "system", "content": context}   # the adapter's context
    job = Path(out.extra["harbor_job"])
    plan = json.loads((job / "launch" / "plan.json").read_text())
    assert plan["command"] is None and plan["answer"]["answer_file"] == "/app/answer.txt"
    assert plan["answer"]["endpoint"].startswith("http://127.0.0.1:") and KEY not in job_files(job)
    assert out.extra["session"]["served"] is True and out.extra["deployed"]["deploy"] is None
    with pytest.raises(ValueError, match="answer_format"):
        make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "answer"}, answer={"answer_format": "x"})
    with pytest.raises(ValueError, match="takes no login"):
        make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "answer"}, binding=None, model="m",
                   login="chatgpt")


def test_the_answer_agents_endpoint_says_how_its_calls_went():
    """The answer agent's session facts are its endpoint's calls, as a CLI's stream gives them: a relay's served,
    unavailable (a retry may help) or rejected; a CLI session's refused (the plan's 429) or lost sign-in; its usage
    from the sessions' replies, unpriced on a login."""
    from hermit.agent.base.interface import LOGIN_FAILED
    from hermit.harbor.tools import ModelEndpoint
    relay = ModelEndpoint(BINDING)
    relay._note((503, b'{"error": {"message": "busy"}}', {}))
    relay._note((200, b'{"choices": []}', {}))
    assert relay.facts()["served"] is True and relay.facts()["calls"] == 2
    relay.statuses, relay.codes = [None], [None]                       # no answer at all
    assert relay.facts()["unavailable"] is True and relay.facts()["started"] is True
    relay.statuses, relay.codes = [400], [None]
    assert relay.facts()["rejected"] is True and not relay.facts()["unavailable"]
    session = ModelEndpoint({"cli": "claude", "model": "claude-haiku-5-5", "login": "claude"})
    session._note((429, b'{"error": {"message": "the plan refused"}}', {}))
    assert session.facts()["refused"] is True and not session.facts()["unavailable"]
    session._note((401, json.dumps({"error": {"code": LOGIN_FAILED, "message": "lost"}}).encode(), {}))
    assert session.facts()["login_failed"] is True and not session.facts()["rejected"]
    session._note((200, b'{"usage": {"prompt_tokens": 10, "completion_tokens": 2}}', {}))
    assert (session.usage.input_tokens, session.usage.output_tokens, session.usage.calls) == (10, 2, 1)
    assert session.usage.cost_usd is None and ModelEndpoint({"cli": "pi"}).facts()["started"] is False


def test_the_answer_agents_endpoint_keeps_what_a_refusal_says_in_a_long_message_or_at_a_bodys_top_level(tmp_path):
    """A failed call's message is kept to its last 1000 characters: the whole of Hermit's endpoint's reply to a CLI's
    failure (endpoint._no_reply: its prefix and the last 600 characters of the failure, as much as the runner reads of
    a container CLI's), so that a Pi failure of up to 600 characters keeps its leading `401 {` behind the prefix; and
    of a longer provider message its end, where the refusal's words are. A body whose error has no message says it at
    its top level, as a `message` (Mistral's) or a `detail` string (a FastAPI server's). Either way, a refusal of the
    configuration is model_rejected."""
    from hermit.agent.base.cli import Pi
    from hermit.agent.base.endpoint import _no_reply
    from hermit.agent.base.interface import Result
    from hermit.harbor.tools import ModelEndpoint
    exited = errors.classify(trial_result(0.5, exc="NonZeroAgentExitCodeError"))
    head = '401 {"error":{"message":"bad key","request":"'
    for n in (500, 598, 599, 600):                          # a Pi failure of n characters (Pi keeps its last 600)
        failure = head + "x" * (n - len(head) - 3) + '"}}'
        said = outcome(Pi({}), pi_failed(failure), tmp_path)
        session = ModelEndpoint({"cli": "pi", "model": "m"})
        session._note(_no_reply(Pi({}), Result(reply="", exit_code=1, extra=said)))
        facts = session.facts()
        assert facts["statuses"] == [400] and facts["failure"] == f"the pi session gave no reply (exit 1): {failure}", n
        assert HarborAgent.session_verdict(exited, facts)["failure_cause"] == "model_rejected", n
    longer = "The request could not be completed: " + "details " * 150 + "The model `gpt-nope` does not exist."
    relay = ModelEndpoint(BINDING)
    relay._note((400, json.dumps({"error": {"message": longer}}).encode(), {}))
    tail = relay.facts()["failure"]
    assert len(longer) > 1000 and longer.endswith(tail) and tail.endswith("does not exist.")
    assert HarborAgent.session_verdict(exited, relay.facts())["failure_cause"] == "model_rejected"
    for body, kept in (({"object": "error", "message": "Invalid model: mistral-nope", "type": "invalid_model",
                         "param": None, "code": "1500"}, "Invalid model: mistral-nope"),
                       ({"detail": "Model not found"}, "Model not found"),
                       ({"error": {"message": "refused"}, "message": "elsewhere"}, "refused"),
                       ({"detail": [{"loc": ["body", "model"], "msg": "field required"}]}, "")):   # no string
        relay = ModelEndpoint(BINDING)
        relay._note((400, json.dumps(body).encode(), {}))
        assert relay.facts()["failure"] == kept, body
    relay = ModelEndpoint(BINDING)
    relay._note((400, b'{"detail": "Model not found"}', {}))
    assert HarborAgent.session_verdict(exited, relay.facts())["failure_cause"] == "model_rejected"


def test_a_model_reply_nested_past_the_parsers_depth_is_read_as_one_that_is_no_json():
    """A reply body nested past the JSON parser's depth is a reply without a body the endpoint can read, as one that
    does not parse: its status counted, no error raised in the call's thread (which would leave it uncounted)."""
    from hermit.harbor.tools import ModelEndpoint
    relay = ModelEndpoint(BINDING)
    relay._note((400, b"[" * 100_000 + b"]" * 100_000, {}))
    facts = relay.facts()
    assert facts["statuses"] == [400] and facts["rejected"] is True and facts["failure"] == ""


@contextlib.contextmanager
def held_model():
    """A model endpoint on 127.0.0.1 that holds each request unanswered until it is released or its caller hangs up:
    (its base URL, an event set once a request arrived, one set once its caller hung up, the release)."""
    arrived, dropped, release = threading.Event(), threading.Event(), threading.Event()

    class Held(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            arrived.set()
            while not release.wait(0.05):
                ready, _, _ = select.select([self.connection], [], [], 0)
                if ready and self.connection.recv(1, socket.MSG_PEEK) == b"":
                    dropped.set()
                    return
    server = ThreadingHTTPServer(("127.0.0.1", 0), Held)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", arrived, dropped, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def ask_in_background(url: str) -> threading.Thread:
    """One chat request to `url` from a thread of its own that keeps its connection until it is answered or closed,
    as the answer agent's request thread does once Harbor's timeout stopped the agent."""
    def ask():
        try:
            urllib.request.urlopen(urllib.request.Request(url + "/chat/completions", data=json.dumps(
                {"model": "glm-5", "messages": [{"role": "user", "content": "What is 6 x 7?"}]}).encode()), timeout=60)
        except OSError:
            pass
    thread = threading.Thread(target=ask, daemon=True)
    thread.start()
    return thread


def test_a_first_request_still_in_flight_when_the_agent_runs_out_of_time_is_the_agents_timeout(monkeypatch):
    """The answer agent's endpoint counts a call from its arrival: a first request still in flight when Harbor
    stopped the agent at its timeout was made, so the trial is the agent's timeout, scored as Harbor scores it, not
    a session that never started (infra, run again); and so it stays once the endpoint has closed."""
    from hermit.harbor.tools import ModelEndpoint
    monkeypatch.setenv(KEY_ENV, KEY)
    timed_out = errors.classify(trial_result(1.0, exc="AgentTimeoutError"))
    with held_model() as (url, arrived, _, _):
        relay = ModelEndpoint({**BINDING, "base_url": url}).start()
        try:
            ask_in_background(relay.url)
            assert arrived.wait(10)
            facts = relay.facts()
        finally:
            relay.close()
    assert (facts["started"], facts["calls"], facts["statuses"]) == (True, 1, [])
    for seen in (facts, relay.facts()):                       # in flight, then given up at the endpoint's close
        verdict = HarborAgent.session_verdict(timed_out, seen)
        assert (verdict["status"], verdict["failure_cause"], verdict["reward"]) == \
            ("timeout", timed_out["failure_cause"], 1.0)


def test_an_answer_trial_whose_agent_sent_no_request_cost_nothing(tmp_path, fake_harbor):
    """The answer agent failed before its request (its instruction file never came, an attachment it could not read):
    the relay saw no call, so the trial's usage is known, none, not unknown, which would make the whole eval's cost
    unknown."""
    from types import SimpleNamespace
    from hermit.agent.model.usage import Usage
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    agent = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "answer"})
    served = SimpleNamespace(facts=lambda: {"started": False, "calls": 0, "statuses": []}, usage=Usage(model="m"))
    facts, usage, _ = agent.session(None, write(tmp_path / "plan.json", "{}"), served, tmp_path)
    assert (usage.calls, usage.input_tokens + usage.output_tokens, usage.cost_usd) == (0, 0, 0.0)
    # a request that reached the relay and had no reply (its upstream dropped it) may have been billed: unknown
    served = SimpleNamespace(facts=lambda: {"started": True, "calls": 1, "statuses": [None]}, usage=Usage(model="m"))
    assert agent.session(None, write(tmp_path / "plan.json", "{}"), served, tmp_path)[1] is None


def test_a_quota_spent_ends_the_run_even_when_the_cli_retried_it_until_the_agents_time_ran_out():
    """A session the provider never served because the account's quota is spent (a 429 insufficient_quota, which a CLI
    such as Pi retries until the agent's time runs out) ends the run (usage_limit, a halt), not the agent's timeout."""
    facts = {"started": True, "served": False, "unavailable": True, "code": runner_module.QUOTA_SPENT}
    verdict = errors.classify(trial_result(None, exc="AgentTimeoutError"))
    out = HarborAgent.session_verdict(verdict, facts)
    assert (out["status"], out["failure_cause"], out["halt"]) == ("infra", "usage_limit", True)


def test_closing_the_answer_agents_endpoint_gives_its_calls_in_flight_up_and_leaves_their_cost_unknown(monkeypatch):
    """Harbor's timeout stops the answer agent, not its request (its thread keeps the connection; a stream's driver
    keeps running): closed after the trial, the endpoint gives each call still in flight up, so the relay drops it
    upstream rather than spend on after the trial's usage was read, and waits for it. What the call used is unknown,
    and so is the trial's cost (None, never the other calls' alone), which its facts say."""
    from hermit.harbor import tools
    monkeypatch.setenv(KEY_ENV, KEY)
    with held_model() as (url, arrived, dropped, _):
        relay = tools.ModelEndpoint({**BINDING, "base_url": url}).start()
        ask_in_background(relay.url)
        assert arrived.wait(10)
        began = time.time()
        relay.close()
        took = time.time() - began
        assert dropped.wait(10)                                    # the upstream's caller, the relay, hung up
    facts, usage = relay.facts(), relay.usage
    assert took < tools.CLOSE_WAIT_S and (facts["statuses"], facts["usage_incomplete"]) == ([None], True)
    assert (usage.calls, usage.unreported_calls, usage.cost_usd) == (1, 1, None)


def test_closing_the_answer_agents_endpoint_cancels_its_sessions_and_waits_for_them_only_so_long(monkeypatch):
    """A model a CLI reaches: each request is one session, which the endpoint's close cancels and waits for, so that
    what it recorded is in the facts read after the close; a session still running past the wait leaves the usage
    incomplete (the cost unknown), and the endpoint is closed all the same."""
    from types import SimpleNamespace
    from hermit import isolate
    from hermit.agent.base import endpoint as endpoint_mod
    from hermit.harbor import tools
    cancelled, release = [], threading.Event()

    def sessions(spec, log_dir=None, *, sandbox=None):        # the CLI's endpoint, over a stand-in of its agent
        served = endpoint_mod.ChatEndpoint(None).start()
        served.agent = SimpleNamespace(settings={"model": spec["model"]})
        return served

    def session(agent, request, abort=None, place=None):      # each request one stand-in session (cli_answer)
        if agent.settings["model"] == "stuck":                 # a session that does not hear its cancel
            release.wait(30)
            return endpoint_mod._DROP
        cancelled.append(abort.wait(30))
        time.sleep(0.3)                                        # its process stopped, its files cleaned up
        return endpoint_mod._DROP                              # as cli_answer gives a cancelled session
    monkeypatch.setattr(endpoint_mod, "cli_endpoint", sessions)
    monkeypatch.setattr(endpoint_mod, "cli_answer", session)
    monkeypatch.setattr(endpoint_mod, "contract_problems", lambda agent, request: ([], []))
    monkeypatch.setattr(isolate, "cli_sandbox", lambda spec, **kwargs: (None, None))
    try:
        for model, wait_s in (("m", 10.0), ("stuck", 0.5)):
            endpoint = tools.ModelEndpoint({"cli": "pi", "model": model}).start()
            ask_in_background(endpoint.url)
            deadline = time.time() + 10
            while not endpoint.facts()["calls"] and time.time() < deadline:
                time.sleep(0.05)
            monkeypatch.setattr(tools, "CLOSE_WAIT_S", wait_s, raising=False)
            began = time.time()
            endpoint.close()
            took, facts, usage = time.time() - began, endpoint.facts(), endpoint.usage
            assert cancelled == [True], model                                       # the first one heard its cancel
            assert facts["statuses"] == ([None] if model == "m" else []), model     # recorded, or still running
            assert (facts["calls"], facts["usage_incomplete"], usage.cost_usd) == (1, True, None), model
            assert took < wait_s + 2 and endpoint.endpoint is None, model
    finally:
        release.set()


def test_an_answer_model_a_cli_reaches_on_a_login_with_no_sign_in_is_refused_at_startup(tmp_path, fake_harbor):
    """The answer agent's model reached through a CLI on a login (the benchmark file's `model: {cli, login}`): with no
    sign-in there (no Codex login, or no Claude token), the runner refuses the run at startup, as it does the acting
    CLI's, instead of every trial going unserved (model_provider_failed), run again and then a failed run."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    spec = {"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt", "plan_dir": str(tmp_path / "plan")}
    with pytest.raises(ValueError, match="login: chatgpt, but there is no Codex login"):
        cli_answer_agent(tmp_path, fake_harbor, tasks, spec)
    chatgpt_login(tmp_path)
    assert cli_answer_agent(tmp_path, fake_harbor, tasks, spec).model_plan() is not None


def test_the_relays_cli_sessions_return_a_refusal_never_wait_for_the_window_inside_the_request(monkeypatch):
    """The answer agent's model reached through a CLI (the benchmark file's `model: {cli, ...}`): each request's session
    runs with plan_rerun off, whatever the spec says, so that a session the plan refused comes back at once (429, the
    relay's `refused`: the trial infra, run again once the plan allows) instead of waiting for the plan's window inside
    the request, as main's CliAgent.run does by default, which would end the trial as the agent's timeout."""
    from types import SimpleNamespace
    from hermit import isolate
    from hermit.agent.base import endpoint as endpoint_mod
    from hermit.harbor import tools
    specs = []

    def sessions(spec, log_dir=None, *, sandbox=None):        # the CLI's endpoint, over a stand-in of its agent
        specs.append(dict(spec))
        served = endpoint_mod.ChatEndpoint(None).start()
        served.agent = SimpleNamespace(settings={"model": spec["model"]})
        return served
    monkeypatch.setattr(endpoint_mod, "cli_endpoint", sessions)
    monkeypatch.setattr(isolate, "cli_sandbox", lambda spec, **kwargs: (None, None))
    for given in ({}, {"plan_rerun": True}):
        endpoint = tools.ModelEndpoint({"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt", **given}).start()
        endpoint.close()
    assert [spec["plan_rerun"] for spec in specs] == [False, False]
    assert all(spec["model"] == "gpt-5.6-luna" and spec["login"] == "chatgpt" for spec in specs)


def test_the_answer_agents_codex_model_has_web_search_off_unless_given(monkeypatch):
    """The answer agent's model reached through Codex (`model: {cli: codex, model, login}`, the docs' minimal form):
    each session gets web_search off unless the spec sets it, since Codex refuses a session without it (main's
    Codex.run), which would make every request the trial's model_rejected."""
    from types import SimpleNamespace
    from hermit import isolate
    from hermit.agent.base import endpoint as endpoint_mod
    from hermit.harbor import tools
    specs = []

    def sessions(spec, log_dir=None, *, sandbox=None):
        specs.append(dict(spec))
        served = endpoint_mod.ChatEndpoint(None).start()
        served.agent = SimpleNamespace(settings={"model": spec["model"]})
        return served
    monkeypatch.setattr(endpoint_mod, "cli_endpoint", sessions)
    monkeypatch.setattr(isolate, "cli_sandbox", lambda spec, **kwargs: (None, None))
    for given in ({}, {"web_search": True}):
        tools.ModelEndpoint({"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt", **given}).start().close()
    assert [spec["web_search"] for spec in specs] == [False, True]


def test_a_rows_context_is_computed_for_harbors_instruction_when_the_row_gives_none(tmp_path, fake_harbor,
                                                                                     monkeypatch):
    """An adapter whose context is per task gets the instruction the agent is given (the task's instruction.md) when
    the row carries none (`{"id", "harbor_task"}`), not the row's JSON."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    staged = []
    monkeypatch.setattr(runner_module, "stage", lambda *args, task, **kwargs: staged.append(task) or {})
    agent = make_agent(tmp_path, fake_harbor, tasks)
    agent._stage({"id": "alpha", "harbor_task": "alpha"}, tmp_path / "adapter", tmp_path)
    agent._stage({"id": "alpha", "harbor_task": "alpha", "instruction": "the row's"}, tmp_path / "adapter", tmp_path)
    assert [t["instruction"] for t in staged] == ["Do alpha.", "the row's"]


def test_an_answer_trial_on_a_login_waits_for_its_plan_and_one_whose_request_the_plan_held_runs_again_once_it_allows(
        tmp_path, fake_harbor, monkeypatch):
    """The answer agent's model reached through a CLI on a login, which the runner serves (model: {cli, login}, the
    benchmark file's): each request is one session on the login, and each trial waits for the plan before it starts,
    so that its request does not wait the plan out in the agent's time. A trial whose request waited for the plan all
    the same (a hold that came once the trial had started) until the agent's time ran out is infra (usage_limit, no
    halt), not the agent's timeout, as the relay recorded its wait; it runs again once the plan allows, as a login's
    own refusal does, not counted among infra_retries (0 here). (A model the pipeline serves, a run config's own
    `{cli}`, reaches the runner as its endpoint's spec alone: neither wait is the runner's.)"""
    out, agent, waits = answer_trial_on_a_login(tmp_path / "own", fake_harbor, monkeypatch, held=True)
    assert [a["unscored"] for a in out.extra["attempts"]] == ["usage_limit", None]      # held, then run again
    assert waits == [(agent.model_plan()[0], False)] * 2      # each run waited for the plan before it started
    assert (out.output["status"], out.output["failure_cause"]) == ("timeout", "agent_exhausted_budget")
    assert "unscored" not in out.extra and not agent._stop.is_set()    # the run again's own result: its session ran


def test_an_answer_request_that_ran_while_another_sessions_report_set_a_hold_is_the_agents_timeout(
        tmp_path, fake_harbor, monkeypatch):
    """A request whose session started (the plan allowed it) and was working when another session's report put the
    plan past MAX_USED never waited for the plan: a trial that ran out of the agent's time with it unanswered is the
    agent's timeout, scored, though the plan holds sessions back when the trial is read."""
    from hermit.agent.base import login
    plan = tmp_path / "trial" / "plan"

    def report():                                    # another session's: its plan at 95%, past MAX_USED 90
        write(plan / "MAX_USED", "90\n")
        login.note_usage(plan, None, {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                      "primary": {"used_percent": 95, "resets_at": time.time() + 3600}})
    out, _, _ = answer_trial_on_a_login(tmp_path / "trial", fake_harbor, monkeypatch, held=False, on_start=report)
    assert (out.output["status"], out.output["failure_cause"], out.output["reward"]) == \
        ("timeout", "agent_exhausted_budget", 1.0)
    row = dataset.index(tmp_path / "trial" / "tasks")[0]
    assert "unscored" not in out.extra and judge.score(row, out)["score"] == 1.0
    facts = out.extra["session"]
    assert (facts["calls"], facts["statuses"], facts["plan_waited"], "plan_held" in facts) == (1, [None], "", False)
    assert login.held(plan).startswith("plan usage 95%")          # the plan holds sessions back as it is read


def test_an_answer_request_that_waited_for_a_hold_that_ended_before_its_trial_was_read_is_infra(
        tmp_path, fake_harbor, monkeypatch):
    """A request whose session waited for its login's plan, and was unanswered when the agent's time ran out, is infra
    (usage_limit), however the plan stands when the trial is read: here its hold ended as the session waited (which
    then started, and was working when the time ran out), or only once Harbor's run had ended (it still waited). The
    trial then runs again once the plan allows (its first run's cause in `attempts`)."""
    from hermit.agent.base import login
    for released in (True, False):
        plan, started = tmp_path / f"released-{released}" / "plan", []
        out, _, _ = answer_trial_on_a_login(plan.parent, fake_harbor, monkeypatch, held=True, released=released,
                                            on_start=lambda: started.append(True),
                                            after_harbor=lambda: (plan / "PAUSE").unlink(missing_ok=True))
        assert [a["unscored"] for a in out.extra["attempts"]] == ["usage_limit", None], released   # then again
        assert started == ([True, True] if released else [True]), released    # the run again's session started
        assert login.held(plan) == "", released                               # no hold as the trials are read


def planned_sessions(plan: Path, on_start=lambda: None):
    """A chat endpoint (started) over Codex sessions on the ChatGPT login whose plan is `plan`, as the pipeline serves a
    model a CLI reaches: each request is one session that waits for the plan as Codex's Base Agent does (CliAgent.run),
    whose own work is a stand-in: `on_start` once the plan let it start, then it works until it is cancelled (no
    reply)."""
    from hermit.agent.base.cli import Codex
    from hermit.agent.base.endpoint import ChatEndpoint
    from hermit.agent.base.interface import Result

    class Session(Codex):
        def _session(self, prompt, **kwargs):
            on_start()
            self.settings["cancel"].wait(30)
            return Result(reply="", exit_code=-1, extra={"served": False, "unavailable": False, "refused": False,
                                                         "started": True, "cancelled": True})
    return ChatEndpoint(Session({"model": "gpt-5.6-luna", "login": "chatgpt", "plan_dir": str(plan),
                                 "web_search": False})).start()


def answer_trial_on_a_login(root: Path, fake_harbor, monkeypatch, *, held: bool, released: bool = False,
                            on_start=lambda: None, after_harbor=lambda: None):
    """One answer trial whose model a CLI reaches on the ChatGPT login (its plan <root>/plan), served by the runner
    (model: {cli, login}), that runs out of the agent's time (1 s) with its one request unanswered: (its output, the
    runner, and the trial's waits for the plan before it started). The plan allows the trial to start, then holds
    sessions back when `held`; with `released` the hold ends as the request waits on it (its session then starts).
    The request is one session of planned_sessions; `after_harbor` runs once Harbor's run ended, before the session is
    read. A trial run again (usage_limit) waited for the plan's window: the hold is gone when it starts."""
    from hermit import isolate
    from hermit.agent.base import endpoint as endpoint_mod
    from hermit.agent.base import login
    tasks, plan = root / "tasks", root / "plan"
    make_task(tasks, "alpha", image="example/alpha:1")
    plan.mkdir(parents=True)
    real_wait, waits, runner = getattr(login.wait, "__wrapped__", login.wait), [], {}   # never an earlier stand-in

    class Ended:                 # the request's cancel, as it waits on the plan with it: the hold ends then
        def __init__(self, cancel):
            self.cancel = cancel

        def wait(self, timeout=None):
            (plan / "PAUSE").unlink(missing_ok=True)
            return self.cancel.is_set()

    def wait(where, cancel=None, log=None, max_s=None, credits=False, **kwargs):
        if cancel is not runner["agent"]._stop:          # the request's (the relay's, its session's): the plan's own
            return real_wait(where, Ended(cancel) if released else cancel, log, max_s, credits, **kwargs)
        waits.append((Path(where), credits))             # the trial's, before it starts: the plan allows it
        if held and len(waits) == 1:
            write(Path(where) / "PAUSE", "held\n")
        else:                                            # a run again: it waited for the window, which came
            (Path(where) / "PAUSE").unlink(missing_ok=True)
        return ""
    wait.__wrapped__ = real_wait
    monkeypatch.setattr(login, "wait", wait)
    monkeypatch.setenv("FAKE_HARBOR_MODE", "timeout")
    monkeypatch.setenv("FAKE_ANSWER_WAIT_S", "1")       # the agent's time, as the fake's request waits it out
    made = []                                            # each trial's relay its own endpoint, as cli_endpoint makes

    def endpoint(spec, log_dir=None, *, sandbox=None):
        made.append(planned_sessions(plan, on_start))
        return made[-1]
    chatgpt_login(root)                                  # the sign-in its startup check looks for
    try:
        monkeypatch.setattr(endpoint_mod, "cli_endpoint", endpoint)
        monkeypatch.setattr(isolate, "cli_sandbox", lambda spec, **kwargs: (None, None))
        agent = cli_answer_agent(root, fake_harbor, tasks,
                                 {"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt", "plan_dir": str(plan)})
        runner["agent"] = agent
        ran = agent.harbor_run
        agent.harbor_run = lambda *a, **k: (ran(*a, **k), after_harbor())[0]
        return agent.run(dataset.index(tasks)[0], None), agent, waits
    finally:
        for sessions in made:
            sessions.close()


def test_the_adapters_tools_are_served_from_this_machine(tmp_path):
    """An adapter's agent tools for a CLI in a container: a Session in the strict adapter sandbox behind a tool server
    on the address the container reaches (here the loopback), each call in the audit log; a stream's next step swaps
    the adapter behind the same URL."""
    from hermit.harbor.tools import AdapterTools

    def call(url, x):
        request = urllib.request.Request(f"{url.removesuffix('/mcp')}/call/calc", data=json.dumps({"x": x}).encode(),
                                         headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(request, timeout=60).read())

    served = AdapterTools(tool_adapter(tmp_path / "one"), audit_log=tmp_path / "a" / "tool-calls.jsonl",
                          host="127.0.0.1", enforce="best_effort").start()
    try:
        assert served.names == ["calc"] and served.mcp_url.startswith("http://127.0.0.1:")
        assert call(served.mcp_url, 4) == {"result": {"result": 8}}
        url = served.mcp_url
        served.swap(tool_adapter(tmp_path / "two", factor=3), tmp_path / "b" / "tool-calls.jsonl")
        assert served.mcp_url == url and call(url, 4) == {"result": {"result": 12}}
    finally:
        served.close()
    first = [json.loads(line) for line in (tmp_path / "a" / "tool-calls.jsonl").read_text().splitlines()]
    second = [json.loads(line) for line in (tmp_path / "b" / "tool-calls.jsonl").read_text().splitlines()]
    assert [(r["tool"], r["arguments"]) for r in first] == [("calc", {"x": 4})] and len(second) == 1
    with pytest.raises(RuntimeError, match="closed"):
        served.swap(tmp_path / "one", tmp_path / "c.jsonl")


def test_a_trial_with_tools_names_their_server_in_the_cli_home(tmp_path, fake_harbor, monkeypatch):
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks, tools_host="127.0.0.1", sandbox_enforce="best_effort")
    out = agent.run(row, tool_adapter(tmp_path / "adapter"))
    assert out.output["status"] == "ok" and out.extra["deployed"]["tools"] == ["calc"]
    plan = json.loads((Path(out.extra["harbor_job"]) / "launch" / "plan.json").read_text())
    config = (Path(plan["home"]["host_dir"]) / "config.toml").read_text()
    assert "calcdemo" in config and "http://127.0.0.1:" in config and plan["metadata"]["tools"] == ["calc"]
    # A server that could not start: no trial, and not the agent's result
    from hermit.harbor import tools
    monkeypatch.setattr(tools.AdapterTools, "start", lambda self: (_ for _ in ()).throw(RuntimeError("no gateway")))
    failed = agent.run(row, tool_adapter(tmp_path / "adapter"))
    assert failed.output["failure_cause"] == "tools_failed" and failed.extra["unscored"] == "tools_failed"


# ---- the runner: one stream step through a fake driver ---------------------------------------------------------------

FAKE_DRIVER = textwrap.dedent('''\
    #!/usr/bin/env python3
    """A stand-in for harborside.driver: the protocol, no Harbor. Steps succeed; step "s2" aborts the stream. Each step
    leaves the CLI's stream as harborside would, from the step's launch plan; FAKE_STEP_VERIFIER makes each other
    step's verifier one whose tools never arrived (no_tools) or one that ran out of its time (timeout). The step
    FAKE_STEP_FAILS names (`<step>:<exception type>`) fails with that exception before its agent ran (no stream, no
    verifier), which aborts the stream; the step FAKE_STEP_HANGS names never answers (a driver hung in a step)."""
    import argparse, json, os, socket, sys, threading, time
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument("-m"); parser.add_argument("--task-dir"); parser.add_argument("--stream-dir")
    parser.add_argument("--name"); parser.add_argument("--socket"); parser.add_argument("--agent-json")
    parser.add_argument("--environment-json"); parser.add_argument("--verifier-json"); parser.add_argument("--idle-s")
    parser.add_argument("--trial-json")
    args, _ = parser.parse_known_args()
    stream_dir, sock_path = Path(args.stream_dir), args.socket
    agent = json.loads(args.agent_json)
    state = {"next": 1, "finished": False, "aborted": None}
    (stream_dir / "seen-agent.json").write_text(json.dumps(agent))
    (stream_dir / "seen-driver.json").write_text(json.dumps({
        "trial": json.loads(args.trial_json) if args.trial_json else None,
        "token": "CLAUDE_CODE_OAUTH_TOKEN" in os.environ}))
    verifier_mode = os.environ.get("FAKE_STEP_VERIFIER", "")
    verifier_out = ("failed to download https://github.com/astral-sh/uv/releases/download/0.9.5/uv.tar.gz\\n"
                    "/tests/test.sh: line 18: uvx: command not found\\n" if verifier_mode == "no_tools" else "PASS\\n")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path); srv.listen(8)
    (stream_dir / "driver.json").write_text(json.dumps({"pid": os.getpid(), "socket": sock_path, "name": args.name,
                                                         "trial_dir": str(stream_dir / "trials" / args.name)}))
    hangs, (fails, failure) = os.environ.get("FAKE_STEP_HANGS"), os.environ.get("FAKE_STEP_FAILS", ":").split(":", 1)
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
            # as harborside.driver ends (_mark_ending): its record says so before the answer, its socket open a moment
            record = json.loads((stream_dir / "driver.json").read_text()); record["ending"] = True
            (stream_dir / "driver.json.part").write_text(json.dumps(record))
            os.replace(stream_dir / "driver.json.part", stream_dir / "driver.json")
        elif req["op"] == "step":
            if req["index"] != state["next"]:
                reply = {"ok": False, "out_of_order": True, "next_index": state["next"]}
            else:
                if req["name"] == hangs:
                    time.sleep(600)
                failed = failure if req["name"] == fails else None
                step_dir = stream_dir / "trials" / args.name / "steps" / req["name"]
                (step_dir / "agent").mkdir(parents=True, exist_ok=True); (step_dir / "verifier").mkdir(exist_ok=True)
                if not failed:
                    (step_dir / "verifier" / "test-stdout.txt").write_text(verifier_out)
                plan_path = (req.get("agent_kwargs") or {}).get("plan")
                (step_dir / "agent" / "plan-seen.txt").write_text(str(plan_path))
                plan = json.loads(Path(plan_path).read_text())
                if not failed:                        # one that failed before its agent ran left no stream
                    (step_dir / "agent" / plan["stream"]).write_text(
                        '{"type":"thread.started","thread_id":"th-%s"}\\n{"type":"turn.completed","usage":'
                        '{"input_tokens":10,"cached_input_tokens":0,"output_tokens":2}}\\n' % req["name"])
                aborted = req["name"] == "s2" or bool(failed)
                why = ({"exception_type": failed, "exception_message": f"{failed} in step {req['name']}"} if failed
                       else {"exception_type": "RuntimeError", "exception_message": "boom"})
                reward = 0.0 if verifier_mode == "no_tools" else 1.0
                result = {"step_name": req["name"], "agent_result": {"n_input_tokens": 10, "n_output_tokens": 2, "metadata": {}},
                          "verifier_result": None if aborted else {"rewards": {"reward": reward}},
                          "exception_info": why if aborted else None,
                          "agent_execution": {"started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:05Z"}}
                if verifier_mode == "timeout" and not aborted:
                    result.update(verifier_result=None, exception_info={"exception_type": "VerifierTimeoutError",
                                                                        "exception_message": "verifier timed out"})
                state["next"] = req["index"] + 1
                if aborted: state["aborted"] = f"step {req['name']} failed"
                reply = {"ok": True, "index": req["index"], "step": result, "aborted": state["aborted"],
                         "paths": {"trial_dir": str(stream_dir / "trials" / args.name), "step_dir": str(step_dir)},
                         "last": req["index"] >= 3}
        else:
            reply = {"ok": False, "error": "unknown op"}
        conn.sendall((json.dumps(reply) + "\\n").encode()); conn.close()
    time.sleep(0.5)                               # as harborside.driver's last answers: its socket there until it exits
    os.unlink(sock_path)
    ''')


def test_stream_steps_through_a_fake_driver(tmp_path, fake_harbor, monkeypatch):
    bin_dir = fake_harbor.parent
    (bin_dir / "python").unlink()
    write(bin_dir / "python", FAKE_DRIVER, exe=True)          # the "interpreter" the driver is started with
    monkeypatch.setattr(stream, "POLL_S", 0.1)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("s1", "s2", "s3"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
    try:
        first = agent.run(rows["flow:s1"], simple_template())
        assert first.output["status"] == "ok" and first.output["reward"] == 1.0 and first.output["step"] == "s1"
        assert first.output["trial"] == stream_name(agent) and first.tokens == 12 and first.wall_s == 5.0
        stream_dir = tmp_path / "jobs" / "streams" / stream_name(agent)
        seen = json.loads((stream_dir / "seen-agent.json").read_text())
        [step] = step_dirs(stream_dir, 1, "s1")
        step_plan = step / "launch" / "plan.json"
        assert seen["kwargs"]["plan"] == str(step_plan) and "model_name" not in seen
        work = json.loads(step_plan.read_text())["deploy"]["work_dir"]
        assert work == str((step / "deploy").resolve())
        assert (Path(first.log) / "verifier" / "test-stdout.txt").read_text() == "PASS\n"
        assert (Path(first.log) / "agent" / "plan-seen.txt").read_text() == str(step_plan)
        client = stream.StreamClient(stream_dir, python=str(bin_dir / "python"))
        assert client.running() is not None

        def stopped():
            deadline = time.time() + 10
            while client.running() is not None and time.time() < deadline:
                time.sleep(0.2)
            return client.running() is None
        # Out of order: step 3 while no rollout runs step 2, whose turn it would wait for, ends at once; the stream's
        # driver, of no use to any step after it, is stopped.
        skipped = agent.run(rows["flow:s3"], None)
        assert skipped.output["status"] == "infra" and skipped.output["failure_cause"] == "stream_order"
        assert "in order" in skipped.extra["exception_message"] and stopped()
        # Run again from step 1 (a new driver), step 2 fails: the stream aborts and the driver is stopped.
        assert agent.run(rows["flow:s1"], None).output["status"] == "ok"
        second = agent.run(rows["flow:s2"], None)
        assert second.output["status"] == "agent_error" and second.extra["aborted"] is True
        assert "RuntimeError" in (Path(second.log) / "exception.txt").read_text()
        assert stopped()
        # A sandboxed worker may not start a driver.
        monkeypatch.setenv("HA_ADAPTER_SANDBOX", "1")
        blocked = agent.run(rows["flow:s1"], None)
        assert blocked.output["failure_cause"] == "stream_lost" and \
            "inside the task sandbox" in blocked.extra["exception_message"]
    finally:
        stop_streams(tmp_path)


def fake_driver(fake_harbor: Path) -> None:
    """FAKE_DRIVER as the interpreter beside `harbor` (the one a stream's driver is started with)."""
    (fake_harbor.parent / "python").unlink()
    write(fake_harbor.parent / "python", FAKE_DRIVER, exe=True)


def stop_streams(tmp_path: Path) -> None:
    """Every fake driver of the test's streams stopped (one a step left running)."""
    for stream_dir in (tmp_path / "jobs" / "streams").glob("*"):
        stream.StreamClient(stream_dir, python="unused", log=lambda m: None).stop(timeout_s=10)


@pytest.fixture
def fake_tools(monkeypatch):
    """The adapter's tools as a stream serves them, without a server or a sandbox (the surface of
    hermit.harbor.tools.AdapterTools): each one made, with the adapter and the log it serves, its swaps, its close."""
    made = []

    class Tools:
        def __init__(self, adapter, *, audit_log, host=None, enforce=None):
            self.adapter, self.audit_log, self.host = Path(adapter), Path(audit_log), host
            self.mcp_url, self.names = "http://127.0.0.1:9/" + "ab" * 16 + "/mcp", ["calc"]
            self.swaps, self.closed = [], False
            made.append(self)

        def start(self):
            return self

        def swap(self, adapter, audit_log):
            self.swaps.append((Path(adapter), Path(audit_log)))
            self.adapter, self.audit_log = Path(adapter), Path(audit_log)

        def close(self):
            self.closed = True
    from hermit.harbor import tools
    monkeypatch.setattr(tools, "AdapterTools", Tools)
    return made


def test_a_stream_steps_verifier_verdicts_are_a_trials(tmp_path, fake_harbor, monkeypatch):
    """A step's result is judged as a trial's: a verifier whose tools never arrived is infra (run again), its quoted
    output with `verifier_redact` applied, and a verifier that ran out of its time on the agent's solution is the
    agent's result (`verifier_timeout`'s default)."""
    fake_driver(fake_harbor)
    tasks = tmp_path / "tasks"
    make_task(tasks, "unreached", steps=("s1", "s2"))
    make_task(tasks, "slow", steps=("s1", "s2"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, verifier_redact=[r"releases/download/\S+"],
                       stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
    try:
        monkeypatch.setenv("FAKE_STEP_VERIFIER", "no_tools")           # each stream's driver: the mode it started with
        unreached = agent.run(rows["unreached:s1"], None)
        monkeypatch.setenv("FAKE_STEP_VERIFIER", "timeout")
        slow = agent.run(rows["slow:s1"], None)
    finally:
        stop_streams(tmp_path)
    assert (unreached.output["status"], unreached.output["failure_cause"]) == ("infra", "verifier_failed")
    assert unreached.extra["unscored"] == "verifier_failed"
    assert "uvx: command not found" in unreached.extra["exception_message"]
    assert "releases/download" not in unreached.extra["exception_message"]
    assert f"github.com/astral-sh/uv/{curate.REDACTED}" in unreached.extra["exception_message"]
    assert (slow.output["status"], slow.output["failure_cause"]) == ("timeout", "solution_timed_out")


def test_a_streams_driver_runs_in_harbors_environment_with_the_timeout_multipliers(tmp_path, fake_harbor,
                                                                                    monkeypatch):
    """A stream's driver is Harbor's process for the stream: it runs with the environment a trial's harbor gets (no
    subscription token outside a Claude login) and gets the job's timeout multipliers for its trial config."""
    fake_driver(fake_harbor)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("s1", "s2"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, verifier_timeout_multiplier=2,
                       stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
    try:
        assert agent.run(rows["flow:s1"], None).output["status"] == "ok"
    finally:
        stop_streams(tmp_path)
    seen = json.loads((tmp_path / "jobs" / "streams" / stream_name(agent) / "seen-driver.json").read_text())
    assert seen == {"trial": {"verifier_timeout_multiplier": 2.0, "environment_build_timeout_multiplier": 1.0},
                    "token": False}


def test_a_streams_settings_given_as_null_take_their_defaults(tmp_path, fake_harbor, monkeypatch):
    """stream.idle_s, start_timeout_s and turn_wait_s left empty (null) are their defaults (the driver stops after
    4 h without a step, its environment has an hour to start), not an error in every step of the task."""
    fake_driver(fake_harbor)
    started = []
    ensure = stream.StreamClient.ensure
    monkeypatch.setattr(stream.StreamClient, "ensure", lambda self, **kw: started.append(kw) or ensure(self, **kw))
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks,
                       stream={"idle_s": None, "start_timeout_s": None, "turn_wait_s": None})
    try:
        assert [agent.run(rows[f"flow:{s}"], None).output["status"] for s in ("one", "two")] == ["ok", "ok"]
    finally:
        stop_streams(tmp_path)
    assert [(kw["idle_s"], kw["start_timeout_s"]) for kw in started] == [(14400.0, 3600.0)] * 2


def test_a_step_whose_driver_is_gone_is_infra_and_closes_the_streams_tools(tmp_path, fake_harbor, monkeypatch,
                                                                            fake_tools):
    """A driver killed while a step waits for its turn (its socket refuses): the step is infra (stream_lost), not a
    pipeline error scored 0, and the stream's tool server is closed. A driver only silent past a request's time is
    infra too; that step was never asked for, so its stream is given up: once none of its rollouts runs (none here),
    its driver is stopped and its tools closed, no later step's turn being able to come."""
    fake_driver(fake_harbor)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("s1", "s2", "s3"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    settings = {"tools_host": "127.0.0.1", "stream": {"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6}}
    agent, other = (make_agent(tmp_path, fake_harbor, tasks, **settings) for _ in range(2))   # a stream each
    adapter = tool_adapter(tmp_path / "adapter")
    try:
        assert agent.run(rows["flow:s1"], adapter).output["status"] == "ok"

        def silent(self, index, *, timeout_s, missing=None):
            raise TimeoutError("timed out")
        with monkeypatch.context() as patched:
            patched.setattr(stream.StreamClient, "wait_turn", silent)
            slow = agent.run(rows["flow:s2"], adapter)
        client = stream.StreamClient(tmp_path / "jobs" / "streams" / stream_name(agent), python="unused")
        given_up = stream_name(agent) not in agent._streams and fake_tools[0].closed and client.running() is None
        assert other.run(rows["flow:s1"], adapter).output["status"] == "ok"

        def gone(self, index, *, timeout_s, missing=None):
            raise ConnectionRefusedError(111, "Connection refused")
        with monkeypatch.context() as patched:
            patched.setattr(stream.StreamClient, "wait_turn", gone)
            lost = other.run(rows["flow:s2"], adapter)
    finally:
        stop_streams(tmp_path)
    assert (slow.output["status"], slow.output["failure_cause"]) == ("infra", "stream_lost") and given_up
    assert (lost.output["status"], lost.output["failure_cause"], lost.extra["unscored"]) == \
        ("infra", "stream_lost", "stream_lost")
    assert len(fake_tools) == 2 and all(served.closed for served in fake_tools)
    assert stream_name(other) not in other._streams


def test_a_driver_that_died_while_it_answered_closes_the_streams_tools(tmp_path, fake_harbor, monkeypatch, fake_tools):
    """A driver killed while it answered closed the connection without an answer (a StreamError) and is gone: no step
    after it reaches it, so the stream's tool server is closed, as for a driver whose socket refuses. A driver still
    running that reports its environment failed has its step never asked for either: its stream is given up, its
    driver stopped and its tools closed once none of its rollouts runs."""
    import signal
    import time
    fake_driver(fake_harbor)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("s1", "s2", "s3"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    settings = {"tools_host": "127.0.0.1", "stream": {"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6}}
    agent, other = (make_agent(tmp_path, fake_harbor, tasks, **settings) for _ in range(2))   # a stream each
    adapter = tool_adapter(tmp_path / "adapter")
    try:
        assert agent.run(rows["flow:s1"], adapter).output["status"] == "ok"

        def failed(self, index, *, timeout_s, missing=None):
            raise stream.StreamError("the stream's environment failed: a step's container exited")
        with monkeypatch.context() as patched:
            patched.setattr(stream.StreamClient, "wait_turn", failed)
            running = agent.run(rows["flow:s2"], adapter)
        client = stream.StreamClient(tmp_path / "jobs" / "streams" / stream_name(agent), python="unused")
        given_up = stream_name(agent) not in agent._streams and fake_tools[0].closed and client.running() is None
        assert other.run(rows["flow:s1"], adapter).output["status"] == "ok"

        def dies(self, index, *, timeout_s, missing=None):  # killed while it answered (its zombie is not reaped)
            pid = int(self.info()["pid"])
            os.kill(pid, signal.SIGKILL)
            state, deadline = Path(f"/proc/{pid}/stat"), time.time() + 10
            while time.time() < deadline and state.exists() and " Z " not in state.read_text():
                time.sleep(0.05)
            raise stream.StreamError("the stream driver closed the connection without an answer")
        with monkeypatch.context() as patched:
            patched.setattr(stream.StreamClient, "wait_turn", dies)
            lost = other.run(rows["flow:s2"], adapter)
    finally:
        stop_streams(tmp_path)
    assert (running.output["status"], running.output["failure_cause"]) == ("infra", "stream_lost") and given_up
    assert (lost.output["status"], lost.output["failure_cause"]) == ("infra", "stream_lost")
    assert len(fake_tools) == 2 and all(served.closed for served in fake_tools)
    assert stream_name(other) not in other._streams


def test_a_step_lost_before_its_driver_was_asked_for_it_costs_0_and_one_lost_after_keeps_its_cost_unknown(
        tmp_path, fake_harbor, monkeypatch):
    """A stream step lost before its driver was asked for it ran no session: it costs 0, as a rollout that never reached
    a container does. One lost after its request was sent may have run its session, whose usage no one read: its cost
    and tokens are unknown, never 0."""
    fake_driver(fake_harbor)
    monkeypatch.setattr(stream, "POLL_S", 0.1)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("s1", "s2", "s3"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    settings = {"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6}
    # two runners, each its own stream of the task: the first's is given up as its step 2 is lost before its driver
    agent, other = (make_agent(tmp_path, fake_harbor, tasks, stream=settings) for _ in range(2))
    try:
        assert agent.run(rows["flow:s1"], simple_template()).output["status"] == "ok"

        def unreached(self, index, *, timeout_s, missing=None):
            raise stream.StreamError("the stream's environment failed: a step's container exited")
        with monkeypatch.context() as patched:
            patched.setattr(stream.StreamClient, "wait_turn", unreached)
            before = agent.run(rows["flow:s2"], simple_template())
        assert other.run(rows["flow:s1"], simple_template()).output["status"] == "ok"

        def unanswered(self, **request):                     # sent, and the connection closed without an answer
            raise stream.StreamError("the stream driver closed the connection without an answer")
        with monkeypatch.context() as patched:
            patched.setattr(stream.StreamClient, "step", unanswered)
            after = other.run(rows["flow:s2"], simple_template())
    finally:
        stop_streams(tmp_path)
    assert (before.output["failure_cause"], before.cost_usd, before.tokens) == ("stream_lost", 0.0, 0)
    assert (after.output["failure_cause"], after.cost_usd, after.tokens) == ("stream_lost", None, None)


def test_a_driver_killed_while_it_wrote_its_answer_is_a_stream_error(tmp_path):
    """A driver killed while it wrote its answer leaves part of one: a StreamError, as for a driver that closed the
    connection without an answer (the runner's lost-driver path: the step is infra, stream_lost), never a ValueError
    that fails the rollout."""
    path = tmp_path / "d.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def answer():                                             # its answer cut off where the driver died
        conn, _ = listener.accept()
        with conn:
            conn.makefile().readline()
            conn.sendall(b'{"ok": true, "index": 1, "step": {"verifier_result": {"rewards"')
    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    write(tmp_path / "stream" / stream.DRIVER_INFO, json.dumps({"pid": os.getpid(), "socket": str(path)}))
    try:
        with pytest.raises(stream.StreamError, match="before its answer was whole"):
            stream.StreamClient(tmp_path / "stream", python="unused").request({"op": "status"}, timeout=10)
    finally:
        thread.join(10)
        listener.close()


def test_each_stream_step_serves_its_own_adapter_once_its_turn_comes(tmp_path, fake_harbor, monkeypatch, fake_tools):
    """A later step's rollout may start the stream's tool server before the step before it has run (that step's rollout
    begun first, as a batch's rows are submitted in order, but slower to stage): each step, once its turn comes, serves
    its own adapter behind the stream's URL and logs its calls in its own log (each rollout of a step has a directory
    of its own)."""
    fake_driver(fake_harbor)
    monkeypatch.setattr(stream, "POLL_S", 0.1)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("s1", "s2", "s3"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, tools_host="127.0.0.1",
                       stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 120})
    one, two = tool_adapter(tmp_path / "one"), tool_adapter(tmp_path / "two", factor=3)
    real_stage, staging, held = HarborAgent._stage, threading.Event(), threading.Event()

    def stage(self, task, adapter, where):                  # step 1's staging held until step 2 served the tools
        if task["step"] == "s1":
            staging.set()
            held.wait(120)
        return real_stage(self, task, adapter, where)
    monkeypatch.setattr(HarborAgent, "_stage", stage)
    earlier, later = {}, {}
    first = threading.Thread(target=lambda: earlier.update(out=agent.run(rows["flow:s1"], one)))
    second = threading.Thread(target=lambda: later.update(out=agent.run(rows["flow:s2"], two)))
    first.start()
    try:
        staging.wait(120)
        second.start()
        deadline = time.time() + 120
        while not fake_tools and time.time() < deadline:            # step 2's rollout started the stream's server
            time.sleep(0.05)
        held.set()
        first.join(180)
        second.join(180)
    finally:
        held.set()
        stop_streams(tmp_path)
    stream_dir = tmp_path / "jobs" / "streams" / stream_name(agent)
    [first_step], [second_step] = step_dirs(stream_dir, 1, "s1"), step_dirs(stream_dir, 2, "s2")
    [served] = fake_tools
    assert served.swaps == [(one, first_step / curate.TOOL_AUDIT), (two, second_step / curate.TOOL_AUDIT)]
    assert earlier["out"].output["status"] == "ok" and later["out"].output["step"] == "s2"


def driver_starts(monkeypatch) -> list[str]:
    """The stream drivers started from now on (StreamClient._start), by their stream's name."""
    started, start = [], stream.StreamClient._start

    def counted(self, **kwargs):
        started.append(self.dir.name)
        return start(self, **kwargs)
    monkeypatch.setattr(stream.StreamClient, "_start", counted)
    return started


def test_a_streams_last_step_that_never_reached_its_driver_stops_it_and_its_next_pass_starts_anew(
        tmp_path, fake_harbor, monkeypatch):
    """A stream's last step that ends before it reaches the driver (this machine could not stage its adapter) gives
    the stream up too, though no step comes after it: its rollout stops the driver as it ends, so that the stream's
    next pass in the same runner (an eval's next repeat, a later epoch) runs from its first step in a new environment.
    Before, the driver stayed up, waiting for that step: the next pass's first step ended stream_order (already run),
    and its last step ran on the environment the pass before had left, scored as the agent's result."""
    fake_driver(fake_harbor)
    monkeypatch.setattr(stream, "POLL_S", 0.1)
    started = driver_starts(monkeypatch)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 60})
    real_stage, failing = HarborAgent._stage, {"two"}

    def stage(self, task, adapter, where):
        if task["step"] in failing:
            raise OSError(28, "No space left on device")
        return real_stage(self, task, adapter, where)
    monkeypatch.setattr(HarborAgent, "_stage", stage)
    client = stream.StreamClient(tmp_path / "jobs" / "streams" / stream_name(agent), python="unused")
    try:
        first = [agent.run(rows[f"flow:{step}"], None) for step in ("one", "two")]
        deadline = time.time() + 10
        while client.running() is not None and time.time() < deadline:
            time.sleep(0.1)
        stopped = client.running() is None
        failing.clear()
        again = [agent.run(rows[f"flow:{step}"], None) for step in ("one", "two")]
    finally:
        stop_streams(tmp_path)
    assert [(out.output["status"], out.output["failure_cause"]) for out in first] == \
        [("ok", None), ("infra", "staging_failed")]
    assert stopped                                           # as its last step's rollout ended
    assert [(out.output["status"], out.output["failure_cause"]) for out in again] == [("ok", None), ("ok", None)]
    assert started == [stream_name(agent)] * 2               # the next pass in a new driver


@pytest.mark.parametrize("end", ["deadline", "swap", "config", "sign_in", "plan"])
def test_a_streams_last_step_that_ends_in_any_way_before_its_driver_is_asked_gives_the_stream_up(
        tmp_path, fake_harbor, monkeypatch, fake_tools, end):
    """Every way a stream's step can end before its driver is asked for it gives the stream up, not only a staging
    that failed: its turn's wait running out (a plain StreamOrderError), the adapter's tools failing to swap in at its
    turn, an exception of its launch's (a ValueError of agent_config), its turn's copy of the ChatGPT sign-in failing
    (staging_failed), its launch plan refused for its adapter (adapter_failed). The driver, waiting for that step, is
    stopped as the last rollout ends, and the stream's next pass runs from its first step in a new driver. Before, each
    of these left the driver up for stream.idle_s, and the next pass's steps met it (already run, or scored on the old
    environment)."""
    from hermit.harbor import launch as launch_mod
    fake_driver(fake_harbor)
    monkeypatch.setattr(stream, "POLL_S", 0.1)
    started = driver_starts(monkeypatch)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    login = {"binding": None, "model": "gpt-5.6-luna", "login": "chatgpt"} if end == "sign_in" else {}
    if login:
        chatgpt_login(tmp_path)
    agent = make_agent(tmp_path, fake_harbor, tasks, tools_host="127.0.0.1",
                       stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 60}, **login)
    adapter = tool_adapter(tmp_path / "adapter")
    client = stream.StreamClient(tmp_path / "jobs" / "streams" / stream_name(agent), python="unused")
    real_wait, real_config = stream.StreamClient.wait_turn, HarborAgent.agent_config
    failing = {"two"}

    def wait_turn(self, index, *, timeout_s, missing=None):
        if index == 2 and failing:
            raise stream.StreamOrderError(f"waited {timeout_s:.0f} s for step {index}'s turn (the driver is at step 1)")
        return real_wait(self, index, timeout_s=timeout_s, missing=missing)

    def agent_config(self, task, *args, **kwargs):
        if task["step"] in failing:
            raise ValueError("an agent config the launch cannot take")
        return real_config(self, task, *args, **kwargs)
    real_sign_in, real_launch, signed = launch_mod.sign_in, HarborAgent._launch, []

    def sign_in(plan, root):
        signed.append(plan)
        if failing and len(signed) == 2:
            raise OSError(28, "No space left on device")
        return real_sign_in(plan, root)

    def plan_of(self, task, *args, **kwargs):
        if task["step"] in failing:
            raise ValueError("dsh takes an MCP server name of 32 characters at most ([A-Za-z0-9_-]), not 'a.b'")
        return real_launch(self, task, *args, **kwargs)
    if end == "deadline":
        monkeypatch.setattr(stream.StreamClient, "wait_turn", wait_turn)
    elif end == "config":
        monkeypatch.setattr(HarborAgent, "agent_config", agent_config)
    elif end == "sign_in":
        monkeypatch.setattr(launch_mod, "sign_in", sign_in)
    elif end == "plan":
        monkeypatch.setattr(HarborAgent, "_launch", plan_of)
    try:
        first = [agent.run(rows["flow:one"], adapter)]
        if end == "swap":
            def swap(adapter, audit_log):
                raise OSError(28, "No space left on device")
            fake_tools[0].swap = swap
        if end == "config":
            with pytest.raises(ValueError, match="cannot take"):
                agent.run(rows["flow:two"], adapter)
        else:
            first.append(agent.run(rows["flow:two"], adapter))
        deadline = time.time() + 10
        while client.running() is not None and time.time() < deadline:
            time.sleep(0.1)
        stopped = client.running() is None and fake_tools[0].closed
        failing.clear()
        again = [agent.run(rows[f"flow:{step}"], adapter) for step in ("one", "two")]
    finally:
        stop_streams(tmp_path)
    causes = {"deadline": ("infra", "stream_order"), "swap": ("infra", "tools_failed"),
              "sign_in": ("infra", "staging_failed"), "plan": ("agent_error", "adapter_failed")}
    assert [(out.output["status"], out.output["failure_cause"]) for out in first] == \
        [("ok", None), *([causes[end]] if end in causes else [])]
    assert stopped                                           # as its last step's rollout ended
    assert [(out.output["status"], out.output["failure_cause"]) for out in again] == [("ok", None), ("ok", None)]
    assert started == [stream_name(agent)] * 2               # the next pass in a new driver
    assert not agent._given_up and not agent._in_flight


def test_a_second_rollout_of_a_step_the_stream_already_ran_gives_nothing_up(tmp_path, fake_harbor, monkeypatch):
    """A rollout of a step its stream's driver is already past (an Evolver's run_agent of a training step whose stream
    went on) ends stream_order and gives nothing up: the stream goes on in the same driver, its next step's turn
    intact."""
    fake_driver(fake_harbor)
    monkeypatch.setattr(stream, "POLL_S", 0.1)
    started = driver_starts(monkeypatch)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two", "three"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 60})
    try:
        outs = [agent.run(rows[f"flow:{step}"], None) for step in ("one", "two", "one", "three")]
    finally:
        stop_streams(tmp_path)
    assert [(out.output["status"], out.output["failure_cause"]) for out in outs] == \
        [("ok", None), ("ok", None), ("infra", "stream_order"), ("ok", None)]
    assert "already run" in outs[2].extra["exception_message"]
    assert started == [stream_name(agent)]                   # one driver, the stream never given up


def test_a_step_after_one_that_never_reached_the_driver_ends_at_once_and_stops_the_stream(tmp_path, fake_harbor,
                                                                                            monkeypatch, fake_tools):
    """A step that ends before it reaches its stream's driver (this machine could not stage its adapter) gives the
    stream up: its driver and tool server, of use to no later step, are stopped and closed as it ends, and a later step
    cannot have its turn: it ends at once, infra (stream_order), starting no driver. Before, each later step held a
    rollout worker for its whole turn's wait (by default over an hour, more for each later step), then ended so."""
    fake_driver(fake_harbor)
    monkeypatch.setattr(stream, "POLL_S", 0.1)
    started = driver_starts(monkeypatch)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two", "three"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, tools_host="127.0.0.1",
                       stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 60})
    real_stage = HarborAgent._stage

    def stage(self, task, adapter, where):
        if task["step"] == "two":
            raise OSError(28, "No space left on device")
        return real_stage(self, task, adapter, where)
    monkeypatch.setattr(HarborAgent, "_stage", stage)
    adapter = tool_adapter(tmp_path / "adapter")
    client = stream.StreamClient(tmp_path / "jobs" / "streams" / stream_name(agent), python="unused")
    try:
        first, second = (agent.run(rows[f"flow:{step}"], adapter) for step in ("one", "two"))
        t0 = time.time()
        third = agent.run(rows["flow:three"], adapter)
        took = time.time() - t0
        deadline = time.time() + 10
        while client.running() is not None and time.time() < deadline:
            time.sleep(0.1)
        stopped = client.running() is None
    finally:
        stop_streams(tmp_path)
    assert (first.output["status"], second.output["failure_cause"]) == ("ok", "staging_failed")
    assert (third.output["status"], third.output["failure_cause"], third.extra["unscored"]) == \
        ("infra", "stream_order", "stream_order")
    assert "step 3's turn cannot come: the stream has no driver" in third.extra["exception_message"]
    assert took < 20                                         # its turn's wait is 60 s
    assert started == [stream_name(agent)] and stopped
    [served] = fake_tools
    assert served.closed and stream_name(agent) not in agent._streams and stream_name(agent) not in agent._drivers


def test_a_later_step_of_a_stream_whose_driver_is_gone_ends_at_once_and_starts_none(tmp_path, fake_harbor,
                                                                                     monkeypatch):
    """A later step whose stream has no driver (it idled out after stream.idle_s, or was stopped; or the run was
    resumed, whose runner names its streams anew) and no rollout of its first step to start one cannot have its turn:
    it ends at once, infra (stream_order), starts no driver, and its curated log holds no tool call. Before, it started
    one (a new environment) at the stream's first step and waited its whole turn's wait for a turn that never came."""
    fake_driver(fake_harbor)
    monkeypatch.setattr(stream, "POLL_S", 0.1)
    started = driver_starts(monkeypatch)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two", "three"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    settings = {"stream": {"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 60}}
    try:
        first = make_agent(tmp_path, fake_harbor, tasks, **settings).run(rows["flow:one"], None)
        client = stream.StreamClient(Path(first.extra["stream_dir"]), python="unused")
        client.stop(timeout_s=10)                            # as at its idle time
        deadline = time.time() + 10
        while client.running() is not None and time.time() < deadline:
            time.sleep(0.1)
        resumed = make_agent(tmp_path, fake_harbor, tasks, **settings)
        t0 = time.time()
        second = resumed.run(rows["flow:two"], None)
        took = time.time() - t0
    finally:
        stop_streams(tmp_path)
    assert first.output["status"] == "ok" and took < 20     # its turn's wait is 60 s
    assert (second.output["status"], second.output["failure_cause"]) == ("infra", "stream_order")
    assert "the stream has no driver" in second.extra["exception_message"]
    assert started == [first.output["trial"]] and not (Path(second.log) / curate.TOOL_AUDIT).exists()


def test_a_steps_halt_stops_its_streams_driver_and_ends_the_run(tmp_path, fake_harbor, monkeypatch):
    """A halt in a step (an API account's usage limit) stops its stream's driver, keeps the step's log and ends the
    run (LoginFailed): no later rollout of the runner starts a step or a driver. Resumed (a new runner, which names its
    streams anew), that stream's later steps end at once, infra (stream_order: its first step is in no rollout), and
    the stream runs anew from its first step."""
    from hermit.trainer.rollout import run_tasks
    fake_driver(fake_harbor)
    monkeypatch.setattr(stream, "POLL_S", 0.1)
    started = driver_starts(monkeypatch)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two", "three"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    settings = {"stream": {"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 60}}
    monkeypatch.setenv("FAKE_STEP_FAILS", "two:ApiUsageLimitError")    # each driver reads it as it starts
    try:
        first = make_agent(tmp_path, fake_harbor, tasks, **settings)
        done = first.run(rows["flow:one"], None)
        with pytest.raises(LoginFailed, match=r"model_provider_failed \(ApiUsageLimitError\)"):
            first.run(rows["flow:two"], None)
        client = stream.StreamClient(tmp_path / "jobs" / "streams" / stream_name(first), python="unused")
        deadline = time.time() + 10
        while client.running() is not None and time.time() < deadline:
            time.sleep(0.1)
        stopped = client.running() is None
        [halted] = step_dirs(client.dir, 2, "two")
        with pytest.raises(LoginFailed):
            first.run(rows["flow:three"], None)
        monkeypatch.delenv("FAKE_STEP_FAILS")
        resumed = make_agent(tmp_path, fake_harbor, tasks, **settings)
        again = run_tasks([rows[t] for t in ("flow:two", "flow:three")], None, resumed.run, judge.score,
                          concurrency=2)
        anew = resumed.run(rows["flow:one"], None)
    finally:
        stop_streams(tmp_path)
    assert done.output["status"] == "ok" and stopped and (halted / "hermit-log").is_dir()
    assert not step_dirs(client.dir, 3, "three")                       # no rollout after the halt
    for record in again:
        unscored_record(record, "stream_order")
    assert anew.output["status"] == "ok" and started == [stream_name(first), stream_name(resumed)]


def test_a_batch_runs_a_streams_steps_in_order_while_the_first_is_still_to_reach_the_driver(tmp_path, fake_harbor,
                                                                                             monkeypatch):
    """run_tasks submits a batch's rows in order, all at once: a stream's later steps wait for their turns while the
    step before is still in flight (here the first, slower to stage than the later steps' grace), none ends for a turn
    taken for lost, and one driver runs them all."""
    from hermit.trainer.rollout import run_tasks
    fake_driver(fake_harbor)
    monkeypatch.setattr(stream, "POLL_S", 0.1)                         # the shortest grace: the strictest check
    started = driver_starts(monkeypatch)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two", "three"))
    agent = make_agent(tmp_path, fake_harbor, tasks, stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 60})
    real_stage = HarborAgent._stage

    def stage(self, task, adapter, where):
        if task["step"] == "one":
            time.sleep(1.0)
        return real_stage(self, task, adapter, where)
    monkeypatch.setattr(HarborAgent, "_stage", stage)
    try:
        records = run_tasks(dataset.index(tasks), None, agent.run, judge.score, concurrency=3)
    finally:
        stop_streams(tmp_path)
    assert [(r["id"], r["status"]) for r in records] == [("flow:one", "ok"), ("flow:two", "ok"), ("flow:three", "ok")]
    assert started == [stream_name(agent)]


def test_a_steps_launch_plan_this_machine_could_not_write_is_infra_not_the_agents_0(tmp_path, fake_harbor,
                                                                                      monkeypatch):
    """A host OSError while a step's launch plan is rendered (a full disk) is this machine's, before the step: infra
    (staging_failed), as one while its adapter is staged, and no driver is started. Before, it failed the rollout,
    scored 0 as the agent's."""
    fake_driver(fake_harbor)
    started = driver_starts(monkeypatch)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two"))
    row = {r["id"]: r for r in dataset.index(tasks)}["flow:one"]
    agent = make_agent(tmp_path, fake_harbor, tasks, stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
    monkeypatch.setattr(HarborAgent, "_launch",
                        lambda self, *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    try:
        out = agent.run(row, None)
    finally:
        stop_streams(tmp_path)
    assert (out.output["status"], out.output["failure_cause"], out.extra["unscored"]) == \
        ("infra", "staging_failed", "staging_failed")
    assert "No space left on device" in out.extra["exception_message"] and started == []


@pytest.mark.skipif(not Path("/proc/self/cmdline").is_file(), reason="a driver is told by its command line in /proc")
def test_a_driver_that_answers_no_stop_is_killed_and_its_environment_taken_down(tmp_path, fake_harbor, monkeypatch):
    """A driver hung in a step never answers it (the step expires), nor the stop then asked of it, which queues
    behind the step; neither its idle stop nor the pipeline's close would end it: it is killed with its process group,
    and its trial's compose projects are taken down by name, as an expired trial's are. Before, it stayed up with its
    environment."""
    import signal
    fake_driver(fake_harbor)
    monkeypatch.setattr(stream, "STOP_TIMEOUT_S", 1.0)
    downs = []
    monkeypatch.setattr(docker, "compose_down", lambda name, **kwargs: downs.append(name))
    monkeypatch.setenv("FAKE_STEP_HANGS", "one")
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two"))
    row = {r["id"]: r for r in dataset.index(tasks)}["flow:one"]
    agent = make_agent(tmp_path, fake_harbor, tasks, wait_budget_s=1,
                       stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
    client = stream.StreamClient(tmp_path / "jobs" / "streams" / stream_name(agent), python="unused")
    try:
        t0 = time.time()
        out = agent.run(row, None)
        took = time.time() - t0
        gone, socket_left = client.gone(), stream.socket_path(client.dir).exists()
    finally:
        pid = client._driver_pid()                           # this test's own driver, were it not killed
        if pid:
            os.killpg(pid, signal.SIGKILL)
        stop_streams(tmp_path)
    assert (out.output["status"], out.output["failure_cause"]) == ("infra", "harbor_timeout")
    assert gone and not socket_left and downs == [stream_name(agent)] and took < 30


def test_each_repeat_of_a_multi_step_task_runs_its_stream_anew_and_keeps_its_step_logs(tmp_path, fake_harbor,
                                                                                        monkeypatch):
    """`hermit eval` runs its repeats one after the other, each over every row: a multi-step task's stream finishes in
    one repeat, and the next runs it anew in a driver of its own (its environment new), in the same stream directory,
    each rollout of a step in a directory of its own, so that no repeat overwrites another's step logs."""
    from hermit.trainer.rollout import run_tasks
    fake_driver(fake_harbor)
    started = driver_starts(monkeypatch)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two"))            # not "s2": the fake driver aborts the stream there
    agent = make_agent(tmp_path, fake_harbor, tasks, stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
    try:
        repeats = [run_tasks(dataset.index(tasks), None, agent.run, judge.score, concurrency=1) for _ in (1, 2)]
    finally:
        stop_streams(tmp_path)
    assert [[r["status"] for r in records] for records in repeats] == [["ok", "ok"]] * 2
    assert started == [stream_name(agent)] * 2
    logs = [r["log"] for records in repeats for r in records]
    assert len(set(logs)) == 4 and all((Path(log) / "verifier" / "test-stdout.txt").is_file() for log in logs)


def test_each_runners_streams_are_its_own_and_a_resumed_run_starts_its_streams_anew(tmp_path, fake_harbor):
    """A multi-step task's stream is named for its runner too (HarborAgent.run_id, made as the runner is): two runs of
    one task on one jobs directory, at once or one after the other, never share its driver, its environment or its
    step logs. A resumed run is another runner: a stream its earlier steps ran in is not met again, so its later step
    ends at once (stream_order: its first step is in no rollout)."""
    fake_driver(fake_harbor)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two"))          # not "s2": the fake driver aborts the stream there
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agents = [make_agent(tmp_path, fake_harbor, tasks, stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
              for _ in range(3)]
    try:
        first, apart = (agent.run(rows["flow:one"], None) for agent in agents[:2])
        resumed = agents[2].run(rows["flow:two"], None)
    finally:
        stop_streams(tmp_path)
    assert len({agent.run_id for agent in agents}) == 3
    assert [o.output["status"] for o in (first, apart)] == ["ok", "ok"]
    assert [o.output["trial"] for o in (first, apart)] == [stream_name(agents[0]), stream_name(agents[1])]
    assert first.log != apart.log and first.extra["stream_dir"] != apart.extra["stream_dir"]
    assert (resumed.output["status"], resumed.output["failure_cause"]) == ("infra", "stream_order")


def test_a_stream_run_again_after_its_last_step_keeps_its_step_logs_and_one_still_going_refuses_its_steps(
        tmp_path, fake_harbor):
    """A stream is its runner's. Run again after its last step (a second epoch of a split that lists it whole), it runs
    in a new driver and environment in the same stream directory, each step's rollout in a directory of its own (the
    first run's step logs kept); run again while its driver waits for its next step (a split that lists it in part),
    its steps' turns are taken (stream_order: infra, which the run records as failed rollouts)."""
    fake_driver(fake_harbor)
    tasks = tmp_path / "tasks"
    make_task(tasks, "whole", steps=("one", "two"))           # not "s2": the fake driver aborts the stream there
    make_task(tasks, "part", steps=("one", "two"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
    try:
        first, second = ([agent.run(rows[t], None) for t in ("whole:one", "whole:two", "part:one")] for _ in (1, 2))
    finally:
        stop_streams(tmp_path)
    assert [o.output["status"] for o in first] == ["ok"] * 3 and [o.output["status"] for o in second[:2]] == ["ok"] * 2
    assert [o.extra["stream_dir"] for o in second[:2]] == [o.extra["stream_dir"] for o in first[:2]]
    assert not {o.log for o in second[:2]} & {o.log for o in first[:2]}
    assert all(Path(o.log).is_dir() for o in first + second[:2])
    assert (second[2].output["status"], second[2].output["failure_cause"]) == ("infra", "stream_order")


def test_a_stream_run_again_after_its_last_step_reads_none_of_the_first_runs_outputs(tmp_path, fake_harbor,
                                                                                      monkeypatch):
    """A stream run again after its last step (a validation task run_agent runs in each call) starts a new driver in
    its directory, whose trial Harbor names as the first one's and would reuse step by step: the first run's trial
    directory and replies are set aside first (earlier/1), so a step of the second run that leaves no output of its own
    (its setup failed before its agent ran) is read with none of the first run's. Before, it was billed the first run's
    usage again and curated with its stream."""
    fake_driver(fake_harbor)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
    try:
        call = [agent.run(rows[t], None) for t in ("flow:one", "flow:two")]       # the stream finished
        monkeypatch.setenv("FAKE_STEP_FAILS", "one:RuntimeError")               # the next driver's step one
        again = agent.run(rows["flow:one"], None)
    finally:
        stop_streams(tmp_path)
    stream_dir, trial = Path(again.extra["stream_dir"]), again.output["trial"]
    name = json.loads((step_dirs(stream_dir, 1, "one")[-1] / "launch" / "plan.json").read_text())["stream"]
    assert [o.output["status"] for o in call] == ["ok", "ok"] and call[0].tokens == 12 and call[0].cost_usd
    assert (again.output["status"], again.tokens, again.cost_usd) == ("agent_error", None, None)
    assert not (Path(again.log) / "agent" / name).exists()
    assert (stream_dir / "earlier" / "1" / "trials" / trial / "steps" / "one" / "agent" / name).is_file()


def test_closing_the_runner_stops_the_drivers_of_its_unfinished_streams(tmp_path, fake_harbor, monkeypatch):
    """A runner's streams are met by no later runner (a resumed run names its own anew): its close stops the driver of
    each stream it stepped that did not finish, which would otherwise hold its task's environment for stream.idle_s;
    so does its finalizer for a runner never closed (hermit.isolate.agent_pipeline's close does not close it): at exit
    for one still held then, and as soon as one is freed. Before, a weak set of the runners was closed at exit, and a
    runner freed before then (`hermit check`'s probe, `hermit eval` once it returned) left its drivers up. A stream that
    finished was stopped by its last step, and is not stopped again; nor is a driver that ended by itself (its idle
    time) asked to stop."""
    import gc
    fake_driver(fake_harbor)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two"))          # not "s2": the fake driver aborts the stream there
    rows = {r["id"]: r for r in dataset.index(tasks)}
    settings = {"stream": {"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6}}
    stopped = []                                            # each stop asked of a driver, by its stream's name
    real_stop = stream.StreamClient.stop

    timeouts = []                                           # ... and the time each was given before its kill

    def counted(self, **kwargs):
        stopped.append(self.dir.name)
        timeouts.append(kwargs.get("timeout_s"))
        return real_stop(self, **kwargs)
    monkeypatch.setattr(stream.StreamClient, "stop", counted)

    def driver(out, *, stopping=False):
        """The live driver of the stream a step ran in, or None; one being stopped is waited for (the driver removes
        its socket as it ends)."""
        client = stream.StreamClient(Path(out.extra["stream_dir"]), python="unused")
        deadline = time.time() + 10
        while stopping and client.running() is not None and time.time() < deadline:
            time.sleep(0.1)
        return client.running()
    try:
        agent = make_agent(tmp_path, fake_harbor, tasks, **settings)
        done = [agent.run(rows[f"flow:{step}"], None) for step in ("one", "two")]       # the stream finished
        assert [o.output["status"] for o in done] == ["ok", "ok"] and stopped == [done[1].output["trial"]]
        assert done[1].output["trial"] not in agent._drivers                            # no longer its to stop
        agent.close()
        assert stopped == [done[1].output["trial"]]
        other = make_agent(tmp_path, fake_harbor, tasks, **settings)
        left = other.run(rows["flow:one"], None)                                         # this one did not finish
        assert driver(left) is not None
        other.close()
        assert stopped[1:] == [left.output["trial"]] and driver(left, stopping=True) is None
        interrupted = make_agent(tmp_path, fake_harbor, tasks, **settings)               # never closed, held at exit
        cut = interrupted.run(rows["flow:one"], None)
        assert interrupted._finalizer.atexit and interrupted._finalizer.alive
        interrupted._finalizer()                                                        # as at exit
        assert stopped[2:] == [cut.output["trial"]] and driver(cut, stopping=True) is None
        freed = make_agent(tmp_path, fake_harbor, tasks, **settings)                    # never closed, and let go
        dropped = freed.run(rows["flow:one"], None)
        assert driver(dropped) is not None
        del freed
        gc.collect()
        assert stopped[3:] == [dropped.output["trial"]] and driver(dropped, stopping=True) is None
        ended = make_agent(tmp_path, fake_harbor, tasks, **settings)
        idle = ended.run(rows["flow:one"], None)
        real_stop(stream.StreamClient(Path(idle.extra["stream_dir"]), python="unused"), timeout_s=10)   # by itself
        assert driver(idle, stopping=True) is None                                      # (as at its idle time)
        ended.close()
        assert stopped[4:] == []
        # the last step's own stop waits as long as a stop does; a close's, its finalizer's, CLOSE_STOP_S each
        assert timeouts == [None] + [runner_module.CLOSE_STOP_S] * 3
    finally:
        stop_streams(tmp_path)


def test_a_harbor_command_that_is_a_link_starts_a_streams_driver_with_its_venvs_python(tmp_path, fake_harbor):
    """`harbor` on PATH may be a link into its tool's venv (uv tool's and pipx's ~/.local/bin/harbor): a stream's
    driver runs under that venv's interpreter, beside the link's target, not a `python` beside the link."""
    fake_driver(fake_harbor)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("s1", "s2"))
    row = {r["id"]: r for r in dataset.index(tasks)}["flow:s1"]
    link = tmp_path / "local" / "bin" / "harbor"
    link.parent.mkdir(parents=True)
    link.symlink_to(fake_harbor)
    agent = make_agent(tmp_path, fake_harbor, tasks, harbor=str(link),
                       stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
    assert agent.harbor == str(link) and os.path.samefile(Path(agent.harbor_python).parent, fake_harbor.parent)
    try:
        out = agent.run(row, None)
    finally:
        stop_streams(tmp_path)
    assert (out.output["status"], out.output["trial"]) == ("ok", stream_name(agent))


def test_socket_path_is_short(tmp_path, monkeypatch):
    short = tmp_path / "r"
    short.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
    path = stream.socket_path(tmp_path / ("x" * 200))
    assert len(str(path)) <= 100 and path.parent.is_dir()
    fits = len(str(short / "hermit-harbor-sockets" / path.name).encode()) <= 100   # tmp_path's length varies
    assert path.is_relative_to(short) == fits
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / ("y" * 120)))     # too long: another base is taken
    (tmp_path / ("y" * 120)).mkdir()
    assert len(str(stream.socket_path(tmp_path))) <= 100


# ---- Claude Code on the subscription -------------------------------------------------------------------------------

STREAMS = ROOT / "tests" / "fixtures" / "claude_streams"
TOKEN = "fake-setup-token-never-real"


def claude_agent(tmp_path, fake_harbor, tasks, **params) -> HarborAgent:
    return make_agent(tmp_path, fake_harbor, tasks,
                      **{"acting_agent": {"import_path": "harborside.claude:HermitClaude"},
                         "model": "claude-haiku-5-5", "binding": None, "effort": "medium", "login": "claude",
                         "claude_version": "2.1.318", **params})


def test_a_compose_overlay_is_found_beside_the_benchmark_file(tmp_path, fake_harbor):
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    utils = tmp_path / "utils"
    overlay = write(utils / "no-core-dumps.yaml", "services:\n  main:\n    ulimits:\n      core: 0\n")
    write(utils / "benchmark.yaml", f"tasks_dir: {tasks}\nenvironment:\n  env: {{OMP_NUM_THREADS: '8'}}\n"
                                    "  extra_docker_compose: [no-core-dumps.yaml]\n")
    agent = make_agent(tmp_path, fake_harbor, tasks, benchmark=str(utils / "benchmark.yaml"))
    environment = agent.job_config(row, "alpha-1", agent.agent_config(row, None, None))["environment"]
    assert environment["extra_docker_compose"] == [str(overlay)]       # absolute: Harbor resolves it from its own cwd
    assert environment["env"] == {"OMP_NUM_THREADS": "8"} and environment["delete"] is True


def test_claude_plan_carries_the_login_rules_and_never_the_token(tmp_path, fake_harbor, monkeypatch):
    from hermit.agent.base import cli
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    for name in ("ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.setenv(name, "from-the-parent")
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    binary = write(tmp_path / "pin" / "claude", "#!/bin/sh\necho 2.1.318\n", exe=True)
    agent = claude_agent(tmp_path, fake_harbor, tasks, claude_bin=str(binary))
    job = tmp_path / "job"
    plan_path = agent._launch(row, job, None, None)
    config = agent.job_config(row, "alpha-1", agent.agent_config(row, plan_path, None))
    kwargs = config["agents"][0]["kwargs"]
    assert kwargs == {"plan": str(plan_path), "version": "2.1.318", "claude_bin": "/opt/hermit-claude/claude"}
    assert {"type": "bind", "source": str(binary.parent), "target": "/opt/hermit-claude", "read_only": True} in \
        config["environment"]["mounts"]
    plan = json.loads(plan_path.read_text())
    # The token on a descriptor, the login's lock last on the command line, the tools that outlive a session denied
    assert plan["env_by_fd"] == {"CLAUDE_CODE_OAUTH_TOKEN": "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"}
    assert plan["command"][-2:] == ["--settings", cli.CLAUDE_LOCK] and plan["stdin"] is True
    denied = plan["command"][plan["command"].index("--disallowedTools") + 1].split(",")
    assert {"WebSearch", "WebFetch", *cli.CLAUDE_BEYOND_SESSION} <= set(denied)
    assert {k: plan["env"][k] for k in cli.CLAUDE_LOGIN_ENV} == cli.CLAUDE_LOGIN_ENV
    assert plan["env"]["IS_SANDBOX"] == "1"
    assert TOKEN not in yaml.safe_dump(config) and TOKEN not in job_files(job)   # the job and the plan never hold it
    env = agent.harbor_env(set(plan["env_from_host"].values()))
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN                    # Harbor's environment does, by name
    assert not {"ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"} & set(env)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    with pytest.raises(ValueError, match="setup-token"):
        agent.harbor_env()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    with pytest.raises(ValueError, match="takes effort"):
        claude_agent(tmp_path, fake_harbor, tasks, effort="extreme")
    with pytest.raises(ValueError, match="command line"):            # Harbor's own agent puts the token in argv
        make_agent(tmp_path, fake_harbor, tasks, acting_agent={"name": "claude-code"}, binding=None, model="m",
                   login="claude")
    with pytest.raises(ValueError, match="model name"):
        claude_agent(tmp_path, fake_harbor, tasks, model=None)
    # A Codex trial's Harbor never gets the token at all
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in make_agent(tmp_path, fake_harbor, tasks).harbor_env()


def test_harbors_own_claude_code_keeps_the_key_it_signs_in_with(tmp_path, fake_harbor, monkeypatch):
    """Harbor's own Claude Code signs in with this process's key and endpoint: its harbor gets them, and nothing of a
    parent Claude Code session; HermitClaude's harbor gets no bill but the one its plan names."""
    parent = {"ANTHROPIC_API_KEY": "sk-ant-fake-0123456789abcdef", "ANTHROPIC_BASE_URL": "https://api.example",
              "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "the-parents"}
    for name, value in parent.items():
        monkeypatch.setenv(name, value)
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    env = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"name": "claude-code"}, binding=None,
                     model="claude-haiku-5-5").harbor_env()
    assert {k: env.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")} == \
        {k: parent[k] for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")}
    assert not {"CLAUDECODE", "CLAUDE_CODE_SESSION_ID"} & set(env)
    hermits = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "claude"}).harbor_env()
    assert not set(parent) & set(hermits)


def test_harbor_never_gets_the_switches_that_sign_its_own_codex_in_with_the_users_chatgpt_file(tmp_path, fake_harbor,
                                                                                                monkeypatch):
    """CODEX_FORCE_AUTH_JSON or CODEX_AUTH_JSON_PATH in this process's environment has Harbor's own codex agent upload
    the user's ChatGPT sign-in (~/.codex/auth.json, or the file named) into every trial's container: the plan used
    outside its slots and MAX_USED. Neither reaches Harbor, for its own codex or for a trial of Hermit's table."""
    monkeypatch.setenv("CODEX_FORCE_AUTH_JSON", "1")
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(tmp_path / "auth.json"))
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    for spec in ({"name": "codex"}, {"cli": "codex"}):
        env = make_agent(tmp_path, fake_harbor, tasks, acting_agent=spec).harbor_env()
        assert not {"CODEX_FORCE_AUTH_JSON", "CODEX_AUTH_JSON_PATH"} & set(env), spec
        assert env[KEY_ENV] == KEY                                    # the rest of this process's environment


def test_a_session_is_read_from_its_stream(tmp_path, fake_harbor, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    row = dataset.index(tasks)[0]
    agent = claude_agent(tmp_path, fake_harbor, tasks)
    job = tmp_path / "job"
    plan = agent._launch(row, job, None, None)
    trial = tmp_path / "trial"

    def facts(case):
        write(trial / "agent" / "agent-claude.jsonl", (STREAMS / f"{case}.jsonl").read_text())
        return agent.session(trial, plan, None, job, row)[0]

    assert facts("reply")["served"] is True and facts("reply")["refused"] is False and facts("reply")["started"]
    limit = facts("limit_seven_day")
    assert limit["refused"] is True and limit["held_until"] == 1793856468 and limit["plan_report"]["windows"]
    assert facts("overloaded")["unavailable"] is True and facts("not_logged_in")["login_failed"] is True
    assert agent.session(tmp_path / "nowhere", plan, None, job, row) == (None, None, {})   # the CLI never ran
    _, usage, said = agent.session(trial, plan, None, job, row)
    assert usage is None or usage.cost_usd is None                       # a subscription: no price
    ok = errors.classify(trial_result())
    verdict = HarborAgent.session_verdict(ok, limit, "claude")
    assert verdict["status"] == "infra" and verdict["failure_cause"] == "usage_limit" and not verdict["halt"]
    assert HarborAgent.session_verdict(ok, limit)["halt"] is True        # an API account's limit: nothing later runs
    lost = HarborAgent.session_verdict(ok, facts("not_logged_in"), "claude")
    assert lost["halt"] is True and lost["failure_cause"] == "login_failed"
    usage_limit = errors.classify(trial_result(None, exc="ApiUsageLimitError"))
    assert usage_limit["halt"] is True
    assert HarborAgent.session_verdict(usage_limit, facts("reply"), "claude")["halt"] is False
    # the model's safeguards ended the session: Harbor raises UnknownApiError (a provider failure, infra), but it is
    # the agent's own end (agent_refused_task), kept as its 0 and not run again
    refusal = facts("model_refusal")
    assert refusal["model_refusal"] is True
    flagged = errors.classify(trial_result(None, exc="UnknownApiError"))
    assert flagged["status"] == "infra"
    kept = HarborAgent.session_verdict(flagged, refusal, "claude")
    assert (kept["status"], kept["failure_cause"], kept["halt"]) == ("agent_error", "agent_refused_task", False)
    assert HarborAgent.session_verdict(ok, refusal, "claude")["status"] == "ok"   # a scored trial stays scored
    # the adapter's tools never came up (Claude Code's init names the server failed): infra, not the agent's 0
    init = {"type": "system", "subtype": "init", "session_id": "s", "mcp_servers": [{"name": "calcdemo",
                                                                                     "status": "failed"}]}
    write(trial / "agent" / "agent-claude.jsonl", json.dumps(init) + "\n" +
          (STREAMS / "reply.jsonl").read_text().split("\n", 1)[1])
    data = json.loads(plan.read_text())
    data["metadata"].update(adapter_id="calcdemo", tools=["calc"])
    plan.write_text(json.dumps(data))
    down = agent.session(trial, plan, None, job, row)[0]
    assert down["tools_down"] is True and "calcdemo: failed" in down["failure"]
    assert HarborAgent.session_verdict(ok, down, "claude")["failure_cause"] == "session_failed"


def test_claude_codes_init_event_whose_strings_hold_a_unicode_line_separator_still_names_its_tools_down(
        tmp_path, fake_harbor, monkeypatch):
    """Claude Code's init event names each tool server's status, and JSON.stringify leaves U+0085, U+2028 and U+2029
    unescaped in its strings: the event is read whole, so the adapter's tools that never came up are still infra."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    row = dataset.index(tasks)[0]
    agent = claude_agent(tmp_path, fake_harbor, tasks)
    job, trial = tmp_path / "job", tmp_path / "trial"
    plan = agent._launch(row, job, None, None)
    data = json.loads(plan.read_text())
    data["metadata"].update(adapter_id="calcdemo", tools=["calc"])
    plan.write_text(json.dumps(data))
    stream = trial / "agent" / "agent-claude.jsonl"
    stream.parent.mkdir(parents=True)
    for sep in ("\x85", "\u2028", "\u2029"):
        init = {"type": "system", "subtype": "init", "session_id": "s", "cwd": f"/app/notes{sep}2026",
                "mcp_servers": [{"name": "calcdemo", "status": "failed"}]}
        rest = (STREAMS / "reply.jsonl").read_text(encoding="utf-8").split("\n", 1)[1]
        stream.write_bytes((json.dumps(init, ensure_ascii=False) + "\n" + rest).encode("utf-8"))
        down = agent.session(trial, plan, None, job, row)[0]
        assert down["tools_down"] is True and "calcdemo: failed" in down["failure"], repr(sep)


def test_a_session_whose_key_the_provider_refused_is_infra_without_a_halt(tmp_path, fake_harbor):
    """A provider that refused the session's requests before it served any (Claude Code on a key the provider does
    not take: `rejected`) is not the agent's result: infra (model_rejected), and the run goes on (there is no login
    to restore). A session the provider served before its last request was refused stays the agent's."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "claude"})
    job, trial = tmp_path / "job", tmp_path / "trial"
    plan = agent._launch(row, job, None, None)
    write(trial / "agent" / "agent-claude.jsonl", (STREAMS / "bad_token.jsonl").read_text())
    facts = agent.session(trial, plan, None, job, row)[0]
    assert facts["rejected"] is True and facts["login_failed"] is False
    ok = errors.classify(trial_result())
    verdict = HarborAgent.session_verdict(ok, facts)
    assert (verdict["status"], verdict["failure_cause"], verdict["halt"]) == ("infra", "model_rejected", False)
    assert "model_rejected" in errors.INFRA_CAUSES and facts["tokens"] == 0
    assert HarborAgent.session_verdict(ok, {**facts, "tokens": 1200})["status"] == "ok"   # it had been served


def test_a_first_request_refused_for_what_the_input_made_it_is_the_agents_result(tmp_path, fake_harbor):
    """Only a refusal of the run's configuration (a key or token, a permission, a model the provider does not have)
    is model_rejected: a first request the adapter's or the task's input made too long or invalid is the agent's
    result, its work scored as Harbor scores it. The answer agent's 400 is its result too, unless its message names a
    configuration refusal; its 401, the configuration's."""
    from hermit.harbor.tools import ModelEndpoint
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "claude"})
    job, trial = tmp_path / "job", tmp_path / "trial"
    plan = agent._launch(row, job, None, None)
    exited = errors.classify(trial_result(0.5, exc="NonZeroAgentExitCodeError"))   # the CLI exited 1; verified

    def verdict(case):
        write(trial / "agent" / "agent-claude.jsonl", (STREAMS / f"{case}.jsonl").read_text())
        facts = agent.session(trial, plan, None, job, row)[0]
        assert facts["rejected"] is True and not facts["served"] and facts["tokens"] == 0, case
        return HarborAgent.session_verdict(exited, facts)
    too_long = verdict("too_long")                                       # "Prompt is too long", a 400
    assert (too_long["status"], too_long["failure_cause"], too_long["reward"]) == ("agent_error", "unclassified", 0.5)
    assert judge.score(row, RunOutput(output={"status": too_long["status"], "reward": too_long["reward"]}))["score"] \
        == 0.5
    for case in ("bad_token", "no_model"):                               # a 401, a model that may not exist (404)
        assert verdict(case)["failure_cause"] == "model_rejected", case
    for status, cause in ((400, "unclassified"), (401, "model_rejected")):
        relay = ModelEndpoint(BINDING)
        relay._note((status, b'{"error": {"message": "refused"}}', {}))
        assert HarborAgent.session_verdict(exited, relay.facts())["failure_cause"] == cause, status


def test_the_answer_agents_cli_session_refused_for_its_key_is_model_rejected_by_what_its_400_says():
    """Hermit's endpoint answers every session of a model a CLI reaches that the provider rejected with a 400 (the
    refusal named only in its message, endpoint._no_reply): one refused for its key (Claude Code's 401 for a bad
    token) is the configuration's, model_rejected, its message the session's failure where the trial has none; one
    whose prompt was too long stays the agent's result."""
    from hermit.agent.base.cli import Claude
    from hermit.agent.base.endpoint import _no_reply
    from hermit.agent.base.interface import Result
    from hermit.harbor.tools import ModelEndpoint
    exited = errors.classify(trial_result(0.5, exc="NonZeroAgentExitCodeError"))

    def facts(case):
        claude = Claude({"model": "claude-haiku-5-5"})
        said = claude.outcome((STREAMS / f"{case}.jsonl").read_text(), None, None)
        endpoint = ModelEndpoint({"cli": "claude", "model": "claude-haiku-5-5"})
        endpoint._note(_no_reply(claude, Result(reply="", exit_code=1, extra=said)))     # the endpoint's own reply
        out = endpoint.facts()
        assert out["statuses"] == [400] and out["rejected"] is True, case
        return out
    bad_key = facts("bad_token")
    refused = HarborAgent.session_verdict(exited, bad_key)
    assert (refused["status"], refused["failure_cause"], refused["halt"]) == ("infra", "model_rejected", False)
    assert bad_key["failure"].endswith("Failed to authenticate. API Error: 401 Invalid bearer token")
    unexplained = HarborAgent.session_verdict(errors.classify(trial_result(0.0)), bad_key)   # no exception of its own
    assert unexplained["failure_cause"] == "model_rejected" and unexplained["exception_message"] == bad_key["failure"]
    too_long = HarborAgent.session_verdict(exited, facts("too_long"))
    assert (too_long["status"], too_long["failure_cause"], too_long["reward"]) == ("agent_error", "unclassified", 0.5)


def pi_failed(error_message: str) -> str:
    """A Pi session whose one request failed with `error_message` (as Pi writes the provider's status and body)."""
    events = [{"type": "session", "version": 3, "id": "s"}, {"type": "agent_start"}, {"type": "turn_start"},
              {"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
                                                  "errorMessage": error_message, "usage": {"input": 0, "output": 0}}},
              {"type": "agent_settled"}]
    return "".join(json.dumps(e) + "\n" for e in events)


def outcome(agent, raw: str, where: Path) -> dict:
    """What `agent` (a CLI Base Agent) reads of a session whose stream is `raw`, given as a file, as the runner gives a
    trial's stream to it (its own copy under launch/read/)."""
    where.mkdir(parents=True, exist_ok=True)
    stream = where / f"stream-{uuid.uuid4().hex[:8]}.jsonl"
    stream.write_text(raw, encoding="utf-8")
    return agent.outcome(raw, stream, None)


def test_a_refusal_of_the_key_or_model_in_a_providers_own_wording_is_model_rejected_one_of_the_bill_ends_the_run(
        tmp_path, fake_harbor):
    """A provider's refusal of the run's configuration in its own wording (DeepSeek's model that does not exist,
    Zhipu GLM's unknown model, Mistral's invalid model, OpenRouter's model that is not a valid model ID, Gemini's key
    not valid) is model_rejected. Its refusal of the account's bill (a 402, a balance or credit too low, an
    insufficient quota, billing; the answer agent's relay's 402) ends the run (usage_limit, a halt), as an API
    account's usage limit does: nothing later can be served. A rate limit is the provider's, `unavailable` before any
    rejection is read, and its advice (billing details, a link to the billing page) is no such wording; nor is a prompt
    too long, a content filter or an overload. (An OpenCode error whose words name none of it is the agent's result:
    OpenCode's Base Agent gives its failure without its status.)"""
    from hermit.agent.base.cli import Claude, OpenCode, Pi
    from hermit.harbor.runner import CONFIG_REFUSED
    exited = errors.classify(trial_result(0.5, exc="NonZeroAgentExitCodeError"))

    def verdict(raw, cli=Pi):
        said = outcome(cli({}), raw, tmp_path / "read")
        facts = {k: said.get(k) for k in ("served", "unavailable", "refused", "login_failed", "rejected",
                                          "model_refusal")}
        return HarborAgent.session_verdict(exited, {**facts, "failure": said["failure"], "started": True, "tokens": 0})
    refusals = ['400 {"error":{"message":"Model Not Exist","type":"invalid_request_error","param":null,'
                '"code":"invalid_request_error"}}',
                '400 {"error":{"code":"1211","message":"Unknown Model, please check the model code."}}',
                '400 {"object":"error","message":"Invalid model: mistral-nope","type":"invalid_model","param":null,'
                '"code":"1500"}',
                '400 {"error":{"message":"openai/gpt-nope is not a valid model ID","code":400}}',
                '400 {"error":{"code":400,"message":"API key not valid. Please pass a valid API key.",'
                '"status":"INVALID_ARGUMENT"}}']
    for text in refusals:
        assert (verdict(pi_failed(text))["failure_cause"], verdict(pi_failed(text))["halt"]) == \
            ("model_rejected", False), text
    bills = ['402 {"error":{"message":"Insufficient Balance","type":"unknown_error"}}',
             '400 {"type":"error","error":{"type":"invalid_request_error","message":"Your credit balance is too low '
             'to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."}}',
             '400 {"error":{"message":"You exceeded your current quota.","type":"insufficient_quota"}}',
             '400 {"error":{"code":400,"message":"Gemini API free tier is not available in your country. Please '
             'enable billing on your project in Google AI Studio.","status":"FAILED_PRECONDITION"}}']
    for text in bills:
        assert (verdict(pi_failed(text))["failure_cause"], verdict(pi_failed(text))["halt"]) == \
            ("usage_limit", True), text
    credit = verdict((STREAMS / "credit_balance.jsonl").read_text(), Claude)
    assert (credit["failure_cause"], credit["halt"]) == ("usage_limit", True)
    from hermit.harbor.tools import ModelEndpoint
    relay = ModelEndpoint(BINDING)                                     # the answer agent's relay: the bill's own status
    relay._note((402, b'{"error": {"message": "Insufficient Balance"}}', {}))
    relayed = HarborAgent.session_verdict(exited, relay.facts())
    assert (relayed["failure_cause"], relayed["halt"]) == ("usage_limit", True)
    relay = ModelEndpoint(BINDING)                                     # ... its 402 whatever its words
    relay._note((402, b'{"error": {"message": "This account is not active"}}', {}))
    relayed = HarborAgent.session_verdict(exited, relay.facts())
    assert (relayed["failure_cause"], relayed["halt"]) == ("usage_limit", True)
    # a 429 for a spent quota (OpenAI's insufficient_quota, its code or its type alone), which no pause ends, ends the
    # run too: the relay's, and a CLI's that quotes the reply; any other 429 is the provider's, a pause away
    spent = b'{"error": {"message": "You exceeded your current quota, please check your plan and billing details.", '
    for body in (spent + b'"type": "insufficient_quota", "code": "insufficient_quota"}}', spent + b'"type": '
                 b'"insufficient_quota"}}'):
        relay = ModelEndpoint(BINDING)
        relay._note((429, body, {}))
        relayed = HarborAgent.session_verdict(exited, relay.facts())
        assert (relayed["failure_cause"], relayed["halt"]) == ("usage_limit", True), body
    relay = ModelEndpoint(BINDING)
    relay._note((429, b'{"error": {"message": "Rate limit reached", "code": "rate_limit_exceeded"}}', {}))
    assert HarborAgent.session_verdict(exited, relay.facts())["failure_cause"] == "model_provider_failed"
    quoted = verdict(pi_failed('429 {"error":{"message":"You exceeded your current quota, please check your plan and '
                               'billing details.","type":"insufficient_quota","code":"insufficient_quota"}}'))
    assert (quoted["failure_cause"], quoted["halt"]) == ("usage_limit", True)
    relay = ModelEndpoint(BINDING)                                     # ... and its refusal of the model
    relay._note((404, b'{"error": {"message": "The model does not exist"}}', {}))
    relayed = HarborAgent.session_verdict(exited, relay.facts())
    assert (relayed["failure_cause"], relayed["halt"]) == ("model_rejected", False)
    rate_limits = ['429 {"error":{"message":"Rate limit reached for gpt-5 in organization org-x on requests per min '
                   '(RPM): Limit 3, Used 3, Requested 1. Please try again in 20s. Visit '
                   'https://platform.openai.com/account/billing to add a payment method.",'
                   '"code":"rate_limit_exceeded"}}',
                   '429 {"error":{"code":429,"message":"You exceeded your current quota, please check your plan and '
                   'billing details.","status":"RESOURCE_EXHAUSTED"}}']
    for text in rate_limits:
        assert verdict(pi_failed(text))["failure_cause"] == "model_provider_failed", text
        assert not CONFIG_REFUSED.search(text), text                  # nor in a rejection's earlier notices
    others = [outcome(Claude({}), (STREAMS / f"{case}.jsonl").read_text(), tmp_path / "read")["failure"]
              for case in ("too_long", "model_refusal", "throttled", "overloaded", "server_error")]
    others += [outcome(OpenCode({}), (OPENCODE / f"{case}.jsonl").read_text(), tmp_path / "read")["failure"]
               for case in ("failure_429", "failure_500")]
    others.append("400 The response was filtered due to the prompt triggering Azure OpenAI's content management "
                  "policy. Please modify your prompt and retry.")
    others.append('400 {"error":{"message":"Invalid prompt: your prompt was flagged as potentially violating our usage '
                  'policy."}}')
    assert [text for text in others if CONFIG_REFUSED.search(text)] == []


def test_a_rejections_status_is_read_only_where_one_is_written_never_from_a_number_in_its_words(tmp_path):
    """A refusal's status is read where a status is written: leading the text or a line of it (Pi's `401 {...}`,
    `403: {...}`), after the prefix Hermit's endpoint gives a CLI's failure in its reply (`...(exit 1): 401 {...}`,
    `(timed out): `), after `API Error: ` (Claude Code's) or `Error code: ` (OpenAI's SDK), in `(HTTP 402)`
    (OpenCode's) or `API error (404)` (Pi's), after `status` or a JSON `"code"`. A number in the words is none:
    OpenAI's refusal of a prompt over the context length names token counts (`you requested 4402 tokens (402 in the
    messages, ...)`), a refused image's message the image host's status (`(HTTP 403 from the image host)`), and a
    provider's own `(...): 403` is no endpoint's prefix, each the agent's result however it is read: a Pi session's
    stream, the answer agent's relay's 400, a Pi session the answer agent's model is."""
    from hermit.agent.base.cli import Pi
    from hermit.agent.base.endpoint import _no_reply
    from hermit.agent.base.interface import Result
    from hermit.harbor.runner import CONFIG_REFUSED
    from hermit.harbor.tools import ModelEndpoint
    exited = errors.classify(trial_result(0.5, exc="NonZeroAgentExitCodeError"))

    def read(text):
        """(as a Pi trial's stream is read, as the answer agent's relay reads a provider's 400 saying `text`, as the
        answer agent's endpoint reads a Pi session that failed with it): each verdict's failure cause."""
        said = outcome(Pi({}), pi_failed(text), tmp_path)
        facts = {k: said.get(k) for k in ("served", "unavailable", "refused", "login_failed", "rejected")}
        relay = ModelEndpoint(BINDING)
        relay._note((400, json.dumps({"error": {"message": text}}).encode(), {}))
        session = ModelEndpoint({"cli": "pi", "model": "m"})
        session._note(_no_reply(Pi({}), Result(reply="", exit_code=1, extra=said)))
        return [HarborAgent.session_verdict(exited, f)["failure_cause"] for f in
                ({**facts, "failure": said["failure"], "started": True, "tokens": 0}, relay.facts(), session.facts())]
    for text in ('401 {"error":{"message":"bad key"}}', '403: {"message":"Your account has been suspended."}',
                 'hermit API error (404): {"error":{"message":"no such model"}}'):          # Pi's own forms
        assert read(text) == ["model_rejected"] * 3, text
    written = ["API Error: 402 Insufficient credits", "Error code: 401 - {'error': {'message': 'bad key'}}",
               "provider.quota: the model is down (HTTP 402)", "request failed with status code 403",
               '{"error": {"code": 402, "message": "Insufficient credits"}}', "the model is down\n404 no such model",
               "the codex session gave no reply (exit -9): 401 {}", "the pi session gave no reply (timed out): 404 {}"]
    assert [text for text in written if not CONFIG_REFUSED.search(text)] == []
    numbers = ["400 This model's maximum context length is 4097 tokens. However, you requested 4402 tokens (402 in the "
               "messages, 4000 in the completion). Please reduce the length of the messages or completion.",
               '400 {"error":{"message":"This model\'s maximum context length is 131072 tokens. However, you requested '
               '402 tokens (1 in the messages, 401 in the completion)."}}',
               '400 {"error":{"message":"The input (403 tokens) is longer than the model\'s context length (402 '
               'tokens)."}}',
               '400 {"type":"error","error":{"type":"invalid_request_error","message":"messages.0.content.0.image.'
               'source: Unable to download the file. Please verify the URL and try again (HTTP 403 from the image '
               'host)."}}',
               '400 {"error":{"message":"input tokens (402): 403 over the limit"}}',
               '400 {"error":{"message":"(1 of 2): 403 tokens exceed the limit"}}']
    for text in numbers:
        assert not CONFIG_REFUSED.search(text), text
        assert read(text) == ["unclassified"] * 3, text


def test_a_request_the_models_session_or_its_provider_cannot_take_refused_the_configuration(tmp_path):
    """What Hermit's endpoint refuses a session of a model a CLI reaches cannot take (a figure, tool calls, another
    model, more than one reply; settings the session cannot start with: a 400 with Hermit's own code,
    endpoint.UNSUPPORTED), and a provider's refusal of a request setting its model does not take (OpenAI's unsupported
    parameter or value, Azure's unrecognized request argument, OpenCode's unsupported operation) refused the run's
    configuration, which no input changes: model_rejected (unscored, no halt), never the agent's 0 in every trial. A 400
    without that code or such words stays the agent's. (The code is the runner's relay's, hermit.harbor.tools: Hermit's
    endpoint's own 400 names the problem in its words alone.)"""
    from hermit.agent.base.cli import OpenCode
    from hermit.agent.base.interface import make
    from hermit.harbor.tools import ModelEndpoint
    exited = errors.classify(trial_result(0.0, exc="NonZeroAgentExitCodeError"))
    codex = make("codex", {"model": "gpt-5.6-luna", "login": "chatgpt", "web_search": False})
    figure = {"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "What does the figure show?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]}
    text = {"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "What is 6 x 7?"}]}
    for request in (figure, {**text, "model": "gpt-other"}, {**text, "n": 2},
                    {**text, "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}]}):
        session = ModelEndpoint({"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt"})
        session._note(session._cli_answer(codex)(request, None, None, {}))     # refused before any session
        verdict = HarborAgent.session_verdict(exited, session.facts())
        assert (session.facts()["statuses"], verdict["failure_cause"], verdict["halt"]) == \
            ([400], "model_rejected", False), request
    session = ModelEndpoint({"cli": "codex", "model": "gpt-5.6-luna"})       # web_search unset: it never starts
    unset = session._cli_answer(make("codex", {"model": "gpt-5.6-luna"}))
    session._note(unset(text, None, None, {}))                          # the session's settings refused, by its code
    assert HarborAgent.session_verdict(exited, session.facts())["failure_cause"] == "model_rejected"
    for said in ("Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' "
                 "instead.", "Unsupported value: 'reasoning_effort' does not support 'minimal' with this model.",
                 "Unrecognized request argument supplied: reasoning_effort"):
        relay = ModelEndpoint(BINDING)
        relay._note((400, json.dumps({"error": {"message": said, "type": "invalid_request_error", "param": None,
                                                "code": None}}).encode(), {}))
        assert HarborAgent.session_verdict(exited, relay.facts())["failure_cause"] == "model_rejected", said
    unsupported = outcome(OpenCode({}), '{"type":"error","error":{"type":"provider.unsupported-operation","message":'
                                        '"the model takes no images","status":400}}\n', tmp_path)
    facts = {k: unsupported.get(k) for k in ("served", "unavailable", "refused", "login_failed", "rejected")}
    assert HarborAgent.session_verdict(exited, {**facts, "failure": unsupported["failure"], "started": True,
                                                "tokens": 0})["failure_cause"] == "model_rejected"
    relay = ModelEndpoint(BINDING)                     # another code, and words that name no setting: the agent's
    relay._note((400, b'{"error": {"code": "invalid_request", "message": "Invalid prompt"}}', {}))
    assert HarborAgent.session_verdict(exited, relay.facts())["failure_cause"] == "unclassified"


OPENCODE = ROOT / "tests" / "fixtures" / "opencode"
OPENCODE_SESSION = "ses_e40d3f481ffePQb8uaj39iK60v"                     # success.jsonl's session


def opencode_db(path: Path, *, wal: bool = False, compactions: int = 1, **tokens):
    """OpenCode's session database as 2.2.6 keeps a session's totals (tests/core/test_opencode_agent.py's
    keep_totals), at `path`; with `wal` in WAL mode with its rows still in the write-ahead log, as a session cut off
    leaves them. The connection is returned open (closing it would merge them in)."""
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    if wal:
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA wal_autocheckpoint = 0")
    c.execute("CREATE TABLE session_v2 (id text PRIMARY KEY, tokens_input integer, tokens_output integer, "
              "tokens_reasoning integer, tokens_cache_read integer, tokens_cache_write integer)")
    c.execute("CREATE TABLE session_message (id text PRIMARY KEY, session_id text, type text)")
    c.execute("INSERT INTO session_v2 VALUES (?, ?, ?, ?, ?, ?)", (OPENCODE_SESSION, *(
        tokens.get(k, 0) for k in ("input", "output", "reasoning", "cache_read", "cache_write"))))
    c.executemany("INSERT INTO session_message VALUES (?, ?, 'compaction')",
                  [(f"msg_{i}", OPENCODE_SESSION) for i in range(compactions)])
    c.commit()
    return c


def opencode_trial(tmp_path: Path, fake_harbor: Path):
    """An OpenCode runner, its launch plan, and a trial whose agent/ holds the recorded session's stream (four requests
    of 1000 prompt tokens, 400 cached, and 20 output tokens each)."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "opencode"})
    job, trial = tmp_path / "job", tmp_path / "trial"
    plan = agent._launch(row, job, None, None)
    write(trial / "agent" / "agent-opencode.jsonl", (OPENCODE / "success.jsonl").read_text())
    (trial / "agent" / "hermit-collect").mkdir()
    return agent, row, job, trial, plan


def test_an_opencode_sessions_last_rows_in_its_write_ahead_log_are_counted(tmp_path, fake_harbor):
    """OpenCode keeps its database in WAL mode: the plan collects its -wal and -shm beside it, and what a session cut
    off left only in the -wal (its compaction's model call here) counts in its usage."""
    agent, row, job, trial, plan = opencode_trial(tmp_path, fake_harbor)
    assert [c["to"] for c in json.loads(plan.read_text())["collect"]] == \
        ["opencode.db", "opencode.db-wal", "opencode.db-shm"]
    live = opencode_db(tmp_path / "live" / "opencode.db", wal=True, input=4400, cache_read=2600, output=90,
                       reasoning=30)
    try:
        for name in ("opencode.db", "opencode.db-wal"):             # as harborside collects them, the session cut off
            shutil.copy(tmp_path / "live" / name, trial / "agent" / "hermit-collect" / name)
    finally:
        live.close()
    usage = agent.session(trial, plan, None, job, row)[1]
    assert (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens, usage.calls) == (7000, 2600, 120, 5)


def test_an_opencode_database_the_container_left_as_a_link_is_never_copied(tmp_path, fake_harbor):
    """A collected opencode.db that is a link (to a database of this machine) is not read: the usage is the
    stream's."""
    agent, row, job, trial, plan = opencode_trial(tmp_path, fake_harbor)
    opencode_db(tmp_path / "host-only" / "opencode.db", input=999_000).close()
    (trial / "agent" / "hermit-collect" / "opencode.db").symlink_to(tmp_path / "host-only" / "opencode.db")
    usage = agent.session(trial, plan, None, job, row)[1]
    assert (usage.input_tokens, usage.calls) == (4000, 4)
    assert not list((job / "launch" / "read").rglob("opencode.db"))


def test_a_refused_trial_waits_for_the_plan_and_runs_again(tmp_path, fake_harbor, monkeypatch):
    from hermit.agent.base import login
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    monkeypatch.setenv("FAKE_STREAMS", f"{STREAMS / 'limit_seven_day.jsonl'},{STREAMS / 'reply.jsonl'}")
    waits, holds = [], []
    monkeypatch.setattr(login, "wait", lambda plan, cancel, log, max_s, credits: waits.append(max_s) or "")
    monkeypatch.setattr(login, "hold", lambda plan, until=None, why="session_refused", **kw: holds.append((until, why)))
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    agent = claude_agent(tmp_path, fake_harbor, tasks)
    out = agent.run(row, None)
    assert out.output["status"] == "ok" and out.extra["session"]["served"] is True
    assert holds == [(1793856468, "session_refused")] and waits == [None, None]   # no limit on the wait
    assert (tmp_path / "jobs" / ".fake-calls").read_text() == "2" and not agent._stop.is_set()
    noted = (login.CLAUDE_PLAN / "plan.jsonl").read_text().splitlines()          # both sessions' reports are kept
    assert len(noted) == 2 and json.loads(noted[0])["used_percent"] == 100.0
    # a lost sign-in ends the run instead
    monkeypatch.setenv("FAKE_STREAMS", str(STREAMS / "not_logged_in.jsonl"))
    lost = claude_agent(tmp_path, fake_harbor, tasks)
    with pytest.raises(LoginFailed, match=r"login_failed \(AgentAuthenticationError\): Not logged in"):
        lost.run(row, None)
    assert lost._stop.is_set()


def test_a_login_trials_report_and_holds_are_written_before_its_slot_is_given_back(tmp_path, fake_harbor,
                                                                                   monkeypatch):
    """A trial waiting for one of the plan's slots reads the plan the moment it has one: what the session before it
    said of the plan (its report, a refusal's hold) is written while that session still holds its slot, as
    CliAgent.run writes it, so that with SLOTS: 1 no trial starts on a plan the one before it found exhausted."""
    from contextlib import contextmanager
    from hermit.agent.base import login
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    monkeypatch.setenv("FAKE_STREAMS", f"{STREAMS / 'limit_seven_day.jsonl'},{STREAMS / 'reply.jsonl'}")
    seen = []

    @contextmanager
    def slot(plan, cancel=None, log=None, deadline=None):
        seen.append("slot")
        try:
            yield True
        finally:
            seen.append("given back")
    monkeypatch.setattr(login, "slot", slot)
    monkeypatch.setattr(login, "wait", lambda *a, **k: "")
    monkeypatch.setattr(login, "note_usage", lambda plan, home, seen_=None: seen.append("report"))
    monkeypatch.setattr(login, "hold", lambda plan, until=None, why="session_refused", **kw: seen.append(f"hold {why}"))
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    assert claude_agent(tmp_path, fake_harbor, tasks).run(row, None).output["status"] == "ok"
    assert seen == ["slot", "report", "hold session_refused", "given back", "slot", "report", "given back"]


def claude_stream(case: str, path: Path, input_tokens: int, output_tokens: int) -> Path:
    """A Claude Code stream of the fixtures whose result reports this usage (none of it cached), written to `path`."""
    lines = []
    for line in (STREAMS / f"{case}.jsonl").read_text().splitlines():
        event = json.loads(line) if line.lstrip().startswith("{") else None
        if isinstance(event, dict) and event.get("type") == "result":
            event.pop("model_usage_since_last_result", None)
            event["usage"] = {"input_tokens": input_tokens, "output_tokens": output_tokens,
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
            line = json.dumps(event)
        lines.append(line)
    return write(path, "\n".join(lines) + "\n")


def test_a_login_trials_usage_is_what_every_run_of_it_used(tmp_path, fake_harbor, monkeypatch):
    """A login trial run again (its provider could not serve the first session) used what each run used: a first
    session that worked on, used 5,100 tokens and then met an overload is in the trial's 5,300 tokens, in the
    rollout and in the run's budget, each run's own in `extra.attempts`; its cost is unknown when any run's is."""
    from dataclasses import replace
    from hermit.trainer.budget import Budget
    from hermit.trainer.rollout import run_tasks
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    busy = claude_stream("overloaded", tmp_path / "busy.jsonl", 5000, 100)
    served = claude_stream("reply", tmp_path / "served.jsonl", 150, 50)
    monkeypatch.setenv("FAKE_STREAMS", f"{busy},{served}")
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    budget = Budget()
    [record] = run_tasks([row], None, claude_agent(tmp_path, fake_harbor, tasks, busy_wait_s=0).run, judge.score,
                         concurrency=1, budget=budget)
    assert (record["status"], record["tokens"], budget.tokens["rollout"]) == ("ok", 5300, 5300)
    assert [a["tokens"] for a in record["pipeline_extra"]["attempts"]] == [5100, 200]
    assert record["api_speed"]["model_calls"] == sum(a["model_calls"] for a in record["pipeline_extra"]["attempts"])
    assert (tmp_path / "jobs" / ".fake-calls").read_text() == "2"
    runs = [RunOutput(output={"status": "infra"}, cost_usd=0.5, tokens=10, api_speed={"model_calls": 1}),
            RunOutput(output={"status": "ok"}, cost_usd=0.25, tokens=20, api_speed={"model_calls": 2})]
    total = HarborAgent._attempts(runs)
    assert (total.tokens, total.cost_usd, total.api_speed["model_calls"], len(total.extra["attempts"])) == \
        (30, 0.75, 3, 2)
    assert HarborAgent._attempts([runs[0], replace(runs[1], cost_usd=None)]).cost_usd is None
    assert HarborAgent._attempts(runs[1:]) is runs[1]                        # one run: as it is


def relayed_sessions(*streams: Path):
    """A chat endpoint (started) over Claude Code sessions, as hermit.isolate.agent_pipeline serves the answer agent's
    model a CLI reaches (cli_endpoint: each request one session): each session plays the next of `streams` (the last
    one repeats), read as Claude Code's Base Agent reads a session's stream; (the endpoint, the streams played)."""
    from hermit.agent.base.cli import Claude
    from hermit.agent.base.endpoint import ChatEndpoint
    from hermit.agent.base.interface import Result
    played: list[Path] = []

    class Session(Claude):
        def _session(self, prompt, **kwargs):
            stream = streams[min(len(played), len(streams) - 1)]
            played.append(stream)
            said = self.outcome(stream.read_text(), stream, None)
            said.pop("started", None)
            usage = self.parse(stream, self.settings["model"])[1]
            return Result(reply=said.pop("reply"), finish_reason=said.pop("finish_reason", None), usage=usage,
                          exit_code=0 if said.get("served") else 1,
                          extra={"failed": False, "login_failed": False, "rejected": False, **said})
    return ChatEndpoint(Session({"model": "claude-haiku-5-5", "web_search": False})).start(), played


def test_a_relayed_cli_session_that_failed_gives_the_trial_the_tokens_it_used_and_the_budget_their_cost(
        tmp_path, fake_harbor, monkeypatch):
    """The answer agent's model behind an endpoint of CLI sessions (as endpoint.cli_answer serves them), which the
    trial's endpoint relays each request to (a client of its own): a session that failed after it used tokens (Claude Code's model refusal, whose
    400 reports them: endpoint._no_reply) gives the trial those tokens, priced at the spec's price, and the run's budget
    is charged them. Of a session the relay's client ran again (the provider could not serve it: a 503), the trial has
    the served one's alone: the failed attempt's usage is in the error reply the client read and did not keep."""
    from types import SimpleNamespace
    from hermit.agent.model import llm
    from hermit.trainer.budget import Budget
    from hermit.trainer.rollout import run_tasks
    waits = []
    monkeypatch.setattr(llm, "time", SimpleNamespace(sleep=waits.append))         # the relay's client: no wait
    price = {"input": 1.0, "cached_input": 0.5, "output": 2.0}
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    busy = claude_stream("overloaded", tmp_path / "busy.jsonl", 5000, 100)
    served = claude_stream("text_session", tmp_path / "served.jsonl", 150, 50)      # a session with no tools
    seen = {}
    for case, streams in (("refused", [STREAMS / "model_refusal.jsonl"]), ("retried", [busy, served])):
        sessions, played = relayed_sessions(*streams)
        (tmp_path / case).mkdir()
        try:
            agent = make_agent(tmp_path / case, fake_harbor, tasks, acting_agent={"cli": "answer"}, binding=None,
                               web_search=None,
                               model={**SERVED, "model": "claude-haiku-5-5", "base_url": sessions.url, "price": price,
                                      "ref": "local/claude-haiku-5-5"})
            budget = Budget()
            [record] = run_tasks([row], None, agent.run, judge.score, concurrency=1, budget=budget)
        finally:
            sessions.close()
        seen[case] = (record, budget, played)
    record, budget, played = seen["refused"]
    cost = ((558 - 178) * 1.0 + 178 * 0.5 + 90 * 2.0) / 1e6                  # the refusal's tokens, 178 of them cached
    assert record["pipeline_extra"]["session"]["statuses"] == [400] and len(played) == 1
    assert (record["tokens"], record["api_speed"]["model_calls"], budget.tokens["rollout"]) == (648, 1, 648)
    assert record["cost_usd"] == pytest.approx(cost) and budget.usd["rollout"] == pytest.approx(cost)
    assert record["pipeline_extra"]["n_cache_tokens"] == 178 and budget.unknown_cost_calls == 0
    record, budget, played = seen["retried"]
    assert (record["status"], record["pipeline_extra"]["session"]["statuses"], played) == ("ok", [200], [busy, served])
    assert waits == [2.0]                                                    # the relay's client ran the session again
    assert (record["tokens"], record["api_speed"]["model_calls"], budget.tokens["rollout"]) == (200, 1, 200)
    cost = (150 * 1.0 + 50 * 2.0) / 1e6
    assert record["cost_usd"] == pytest.approx(cost) and budget.usd["rollout"] == pytest.approx(cost)


# A Codex session whose turn failed on a dropped stream: a provider that could not serve it
UNSERVED = ('{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
            '{"type":"turn.failed","error":{"message":"stream disconnected before completion: error sending '
            'request"}}\n')


def test_a_relayed_codex_session_that_lost_its_sign_in_is_a_401_whose_login_failed_ends_the_run(tmp_path, monkeypatch):
    """The answer agent's model reached through Codex (`{cli: codex}`): a session that gave no reply and whose stream,
    kept in the agent's log_dir, says its sign-in was refused (signin.codex_signin_lost, which the Base Agent's Codex
    reading does not report) is a 401 with LOGIN_FAILED, as the endpoint says a Claude session's, so the trial's facts
    say login_failed (the runner ends the run); one the provider refused for good (signin.codex_rejected) is a 400
    ending with Codex's words, read as a trial's Codex session is; one that failed otherwise keeps its reply (a 502:
    unavailable)."""
    import threading
    from types import SimpleNamespace
    from hermit.agent.base import endpoint
    from hermit.agent.base.interface import LOGIN_FAILED
    from hermit.harbor.tools import ModelEndpoint
    logs = tmp_path / "model-sessions"
    logs.mkdir()
    refused = ('{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
               '{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized: Your token has been '
               'invalidated."}}\n')
    streams = iter([refused, UNSERVED])

    def answer(agent, request, abort=None, place=None):
        (logs / f"t{len(list(logs.iterdir()))}-codex.jsonl").write_text(next(streams))
        return 502, json.dumps({"error": {"message": "the codex session gave no reply (exit 1)"}}).encode(), {}
    monkeypatch.setattr(endpoint, "cli_answer", answer)
    monkeypatch.setattr(endpoint, "contract_problems", lambda agent, request: ([], []))
    relay = ModelEndpoint({"cli": "codex", "model": "gpt-5.6-luna"})
    ask = relay._cli_answer(SimpleNamespace(backend="codex", settings={"log_dir": str(logs)}))
    request = {"messages": [{"role": "user", "content": "2+2?"}]}
    status, body, _ = reply = ask(request, None, threading.Event(), {})
    assert (status, json.loads(body)["error"]["code"]) == (401, LOGIN_FAILED)
    relay._note(reply)
    assert relay.facts()["login_failed"] is True
    status, body, _ = reply = ask(request, None, threading.Event(), {})
    assert (status, json.loads(body)["error"].get("code")) == (502, None)
    relay._note(reply)
    facts = relay.facts()
    assert (facts["login_failed"], facts["unavailable"]) == (False, True)
    # a session the provider refused for good (signin.codex_rejected), which the Base Agent's Codex reading leaves a
    # 502, is a 400 ending with Codex's words: the run's configuration (model_rejected), its bill (a halt), or the
    # agent's result (a prompt too long), never the provider's outage run again
    exited = errors.classify(trial_result(0.0, exc="NonZeroAgentExitCodeError"))
    start = '{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
    for said, want in (("unexpected status 400 Bad Request: Unsupported parameter: 'max_tokens'", "model_rejected"),
                       ("Your workspace is out of credits. Add credits to continue.", "usage_limit"),
                       ('{"error":{"message":"Your input exceeds the context window of this model.",'
                        '"type":"invalid_request_error","code":"context_length_exceeded"}}', None)):
        streams = iter([start + json.dumps({"type": "turn.failed", "error": {"message": said}}) + "\n"])
        relay = ModelEndpoint({"cli": "codex", "model": "gpt-5.6-luna"})
        ask = relay._cli_answer(SimpleNamespace(backend="codex", settings={"log_dir": str(logs)}))
        status, body, _ = reply = ask(request, None, threading.Event(), {})
        assert status == 400 and json.loads(body)["error"]["message"].endswith(said), (said, body)
        relay._note(reply)
        facts = relay.facts()
        assert (facts["rejected"], facts["unavailable"]) == (True, False), said
        got = HarborAgent.session_verdict(exited, facts)
        if want is None:
            assert got["status"] == "agent_error" and got["failure_cause"] != "model_rejected", (said, got)
        else:
            assert (got["failure_cause"], bool(got.get("halt"))) == (want, want == "usage_limit"), (said, got)


def test_a_login_trial_starts_no_container_once_another_trial_ended_the_run_while_it_waited(tmp_path, fake_harbor,
                                                                                            monkeypatch):
    """A login trial may wait long for the plan, or pause before it runs again: another rollout that ended the run
    meanwhile (a lost sign-in: HarborAgent.end_run) ends it too (LoginFailed) before it starts a container, as it ends
    every rollout after it."""
    from hermit.agent.base import login
    monkeypatch.setenv("FAKE_STREAMS", str(write(tmp_path / "unserved.jsonl", UNSERVED)))   # each run's, counted
    monkeypatch.setenv("FAKE_HARBOR_MODE", "agent_exit")
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    chatgpt_login(tmp_path)
    lost = {"failure_cause": "login_failed", "exception_type": "AgentAuthenticationError",
            "exception_message": "the sign-in was lost"}
    for ended_at in (1, 2):                     # the wait before the first run, the one after the pause before another
        jobs, waits = tmp_path / f"jobs-{ended_at}", []
        agent = make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt",
                           busy_retries=3, busy_wait_s=0, jobs_dir=str(jobs))

        def wait(plan, cancel, log, max_s, credits, agent=agent, waits=waits, ended_at=ended_at):
            waits.append(plan)
            if len(waits) == ended_at:                   # another rollout's lost sign-in, as this one waits
                with contextlib.suppress(LoginFailed):
                    agent.end_run(lost, jobs / "another")
            return ""
        monkeypatch.setattr(login, "wait", wait)
        with pytest.raises(LoginFailed, match="the sign-in was lost"):
            agent.run(row, None)
        ran = (jobs / ".fake-calls").read_text() if (jobs / ".fake-calls").exists() else "0"
        assert (ran, len(waits)) == (str(ended_at - 1), ended_at), ended_at


def test_a_login_trials_busy_settings_given_as_null_take_their_defaults(tmp_path, fake_harbor, monkeypatch):
    """busy_retries and busy_wait_s left empty (null) are their defaults (the session runs again, 120 s later), not an
    error in every rollout."""
    slept = []
    monkeypatch.setattr(HarborAgent, "_pause", lambda self, seconds: slept.append(seconds))
    monkeypatch.setenv("FAKE_STREAMS", f"{write(tmp_path / 'unserved.jsonl', UNSERVED)},"
                                       f"{write(tmp_path / 'served.jsonl', CODEX_STREAM)}")
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    chatgpt_login(tmp_path)
    out = make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt",
                     busy_retries=None, busy_wait_s=None).run(row, None)
    assert out.output["status"] == "ok" and slept == [120.0]
    assert (tmp_path / "jobs" / ".fake-calls").read_text() == "2"


def test_the_disk_gate_waits_for_the_hosts_tmp_too(tmp_path, monkeypatch):
    from hermit.harbor import runner as runner_mod
    free = {runner_mod.HOST_TMP: 0.5, tmp_path: 100.0}
    monkeypatch.setattr(docker, "free_gb", lambda root: free[root])
    monkeypatch.setattr(docker.time, "sleep", lambda s: None)
    r = runner_mod.HarborAgent.__new__(runner_mod.HarborAgent)
    r.params, r._root, r.pulls, r.image_sizes = {"disk_gate_wait_s": 0.01}, tmp_path, docker.Pulls(), {}
    r._stop, r._stop_why = threading.Event(), ""
    assert not r.wait_for_disk()                                  # /tmp under 1 GB: no trial starts
    assert r._disk_held() == "the host's /tmp stayed under 1 GB free"     # the trial's verdict says which gate
    free[runner_mod.HOST_TMP] = 2.0
    assert r.wait_for_disk()
    free[tmp_path] = 5.0                                          # the data root under its 20 GB
    assert not r.wait_for_disk() and r._disk_held() == f"the Docker data root {tmp_path} stayed under 20 GB free"
    free[tmp_path] = 100.0
    r.params["tmp_gate_gb"] = 0                                   # off
    free[runner_mod.HOST_TMP] = 0.0
    assert r.wait_for_disk()
    r._stop_why = "a Harbor trial (alpha-1): login_failed"           # another rollout ended the run
    r._stop.set()
    with pytest.raises(LoginFailed, match="login_failed"):
        r.wait_for_disk()


def test_the_disk_gate_keeps_room_for_images_still_to_be_pulled(monkeypatch):
    present = set()
    monkeypatch.setattr(docker, "free_gb", lambda root: 30.0)
    monkeypatch.setattr(docker, "image_size", lambda image: 1 if image in present else None)
    pulls, first, second = docker.Pulls(), [], []
    gate = dict(gate_gb=5, wait_s=0, poll_s=0, log=lambda m: None, pulls=pulls)
    assert docker.wait_for_disk(Path("/"), image="big:1", image_gb=20, admitted=first, **gate)
    assert pulls.taken_gb() == 20 and len(first) == 1
    assert not docker.wait_for_disk(Path("/"), image="big:2", image_gb=20, admitted=second, **gate)   # 10 left
    assert docker.wait_for_disk(Path("/"), image="small:1", image_gb=2, admitted=second, **gate)      # 10 >= 7
    present.add("big:1")                                   # pulled: the free space shows it now, not the book
    assert pulls.taken_gb() == 2
    pulls.done(first[0])
    pulls.done(second[0])
    assert pulls.taken_gb() == 0 and docker.wait_for_disk(Path("/"), image="big:2", image_gb=20, **gate)
    assert docker.wait_for_disk(Path("/"), image="big:1", image_gb=20, **gate)    # here already: the gate alone


def test_a_trial_whose_agent_ran_out_of_its_time_and_whose_verifier_ran_no_test_is_infra(tmp_path, fake_harbor,
                                                                                        monkeypatch):
    """Harbor verifies a trial whose agent ran out of its time and grades its reward: when the verifier's tools never
    arrived (GitHub's 504), its tests never ran, and the trial is infra (run again), as one the agent finished is; not
    the agent's 0."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    monkeypatch.setenv("FAKE_HARBOR_MODE", "timeout_no_tools")
    out = make_agent(tmp_path, fake_harbor, tasks).run(row, None)
    assert (out.output["status"], out.output["failure_cause"], out.extra["unscored"]) == \
        ("infra", "verifier_failed", "verifier_failed")


def test_a_trial_whose_environment_never_started_spent_nothing(tmp_path, fake_harbor, monkeypatch):
    """Harbor records no agent start (`agent_execution`) for a trial whose environment or agent setup failed first: no
    session ran, so the trial costs 0, never an unknown cost that would leave an eval's total unknown."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    monkeypatch.setenv("FAKE_HARBOR_MODE", "no_environment")
    out = make_agent(tmp_path, fake_harbor, tasks).run(row, None)
    assert (out.output["status"], out.output["failure_cause"]) == ("infra", "sandbox_failed")
    assert (out.cost_usd, out.tokens) == (0.0, 0)


def test_a_host_failure_is_infra(tmp_path, fake_harbor, monkeypatch):
    """This machine's failure past the steps that guard it (no process left to start `harbor`: EAGAIN) is infra
    (host_failed: run again, its cost unknown), not the agent's error scored 0."""
    import subprocess
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks)

    def eagain(*args, **kwargs):
        raise BlockingIOError(11, "Resource temporarily unavailable")
    with monkeypatch.context() as patched:
        patched.setattr(subprocess, "Popen", eagain)
        out = agent.run(row, None)
    assert (out.output["status"], out.output["failure_cause"], out.cost_usd) == ("infra", "host_failed", None)
    assert out.extra.get("unscored") and agent.run(row, None).output["status"] == "ok"


def test_a_step_whose_stream_aborted_before_its_turn_is_never_asked_for(tmp_path, monkeypatch):
    """An aborted stream's driver refuses every step, the one at its turn too: the wait ends before the step is asked
    for (a step never asked for ran no session: it costs 0), never on the turn it would only be refused at."""
    client = stream.StreamClient(tmp_path / "flow__s1", python="unused")
    monkeypatch.setattr(stream.StreamClient, "status",
                        lambda self: {"next_index": 2, "running": False, "aborted": "step 1 missed min_reward"})
    with pytest.raises(stream.StreamError, match="aborted"):
        client.wait_turn(2, timeout_s=5)


def test_a_halt_the_rollout_met_ends_the_run_when_this_machines_failure_follows_it(tmp_path, fake_harbor,
                                                                                  monkeypatch):
    """A rollout that met a halt (an API account's limit) and then this machine's OSError (its curated log not
    written: a full disk) still ends the run (LoginFailed, through hermit.trainer.rollout.run_one), rather than every
    later trial starting on the dead account. A jobs directory this machine cannot make is infra (host_failed) before
    anything ran: it costs 0."""
    from hermit.harbor import runner as runner_mod
    from hermit.trainer.rollout import run_one
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    row = dataset.index(tasks)[0]
    monkeypatch.setenv("FAKE_HARBOR_MODE", "usage_limit")

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")
    with monkeypatch.context() as patched:
        patched.setattr(runner_mod.curate_mod, "curate", full)
        agent = make_agent(tmp_path, fake_harbor, tasks)
        with pytest.raises(LoginFailed, match=r"model_provider_failed \(ApiUsageLimitError\)"):
            run_one(row, None, agent.run, judge.score)
    assert agent._stop.is_set()
    blocked = tmp_path / "a-file"
    blocked.write_text("")
    agent = make_agent(tmp_path, fake_harbor, tasks)
    agent.params["jobs_dir"] = str(blocked / "jobs")
    out = agent.run(row, None)
    assert (out.output["status"], out.output["failure_cause"], out.cost_usd) == ("infra", "host_failed", 0.0)


def test_the_words_of_a_full_disk_in_tests_that_ran_are_their_output_not_the_hosts(tmp_path):
    """A verifier's output that says the disk was full is the host's failure (infra) only before its tests wrote a
    report: with ctrf.json there the tests ran, and the words are a test's (an agent's program that printed them, a
    task about a full disk), so the trial stays the agent's result, timed out or failed alike."""
    assert errors.verifier_disk_full("OSError: [Errno 28] No space left on device\n")
    assert not errors.verifier_disk_full("OSError: [Errno 28] No space left on device\n", report_exists=True)
    trial = tmp_path / "t__abc1234"
    write(trial / "verifier" / "test-stdout.txt", "FAILED test_writes - OSError: [Errno 28] No space left on device\n")
    write(trial / "verifier" / "reward.txt", "0\n")
    for verdict in (errors.classify(trial_result(0.0)), errors.classify(trial_result(0.5, exc="AgentTimeoutError"))):
        assert HarborAgent.verifier_verdict(verdict, trial)["status"] == "infra"
        write(trial / "verifier" / "ctrf.json", '{"results": {"summary": {"tests": 1, "failed": 1}}}')
        assert HarborAgent.verifier_verdict(verdict, trial) == verdict
        (trial / "verifier" / "ctrf.json").unlink()


def test_the_task_sets_link_is_followed_once_so_a_prepare_mid_run_changes_no_trials_set(tmp_path, fake_harbor):
    """`tasks_dir` names the link prepare writes (utils/tasks): the runner follows it once, so a prepare that points it
    at another set mid-run changes none of the tasks the later trials run."""
    first, second = tmp_path / "set-1", tmp_path / "set-2"
    for root, image in ((first, "example/alpha:1"), (second, "example/alpha:2")):
        make_task(root, "alpha", image=image)
    link = tmp_path / "utils" / "tasks"
    link.parent.mkdir()
    link.symlink_to(first, target_is_directory=True)
    row = dataset.index(link)[0]
    agent = make_agent(tmp_path, fake_harbor, link)
    link.unlink()
    link.symlink_to(second, target_is_directory=True)                     # another prepare, mid-run
    assert agent.tasks_dir == first.resolve() and agent.task_dir(row) == first.resolve() / "alpha"
    assert make_agent(tmp_path, fake_harbor, link).task_dir(row) == second.resolve() / "alpha"


def test_a_secret_in_the_acting_agents_env_is_refused_at_startup(tmp_path, fake_harbor):
    """acting_agent.env is written into each trial's job and onto `docker compose exec`'s command line: a variable of
    a secret's name, or a value naming one, is refused at startup, for a table CLI and Harbor's own agents alike: as
    `${NAME}` or `${NAME:-default}`, which Harbor fills in from this process's environment, or as `$NAME`, which it
    hands on as that text. A secret's name is one Harbor treats as one (KEY, SECRET, TOKEN but not TOKENS, PASSWORD,
    CREDENTIAL or AUTH anywhere in it: TOKENIZERS_PARALLELISM and GIT_AUTHOR_NAME too) or one Hermit does (DB_PASSWD);
    a count named like one (MAX_OUTPUT_TOKENS) is none, nor is a value naming one."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    for spec in ({"cli": "codex", "env": {"OPENAI_API_KEY": "sk-stand-in"}},
                 {"cli": "codex", "env": {"GITHUB": "${GH_TOKEN}"}},
                 {"cli": "codex", "env": {"GITHUB": "${GH_TOKEN:-}"}},
                 {"cli": "codex", "env": {"GITHUB": "$GH_TOKEN"}},
                 {"cli": "codex", "env": {"AUTH": "Bearer ${HF_TOKEN:-none}"}},
                 {"cli": "codex", "env": {"PARALLEL": "${TOKENIZERS_PARALLELISM:-false}"}},
                 {"cli": "codex", "env": {"GIT_AUTHOR_NAME": "me"}},
                 {"cli": "codex", "env": {"DB": "${DB_PASSWD}"}},
                 {"name": "claude-code", "env": {"ANTHROPIC_API_KEY": "stand-in"}}):
        with pytest.raises(ValueError, match=r"acting_agent.env \w+ holds a secret"):
            make_agent(tmp_path, fake_harbor, tasks, acting_agent=spec)
    env = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8000", "A": "1", "LIMIT": "$MAX_OUTPUT_TOKENS"}
    agent = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "codex", "env": env})
    assert agent.agent_config(dataset.index(tasks)[0])["env"] == env


def test_a_table_clis_trial_of_a_model_a_cli_reaches_is_refused_for_what_it_is(tmp_path, fake_harbor):
    """A model a CLI reaches is the answer agent's: a codex trial of one the runner is given (model: {cli: ...}) is
    refused saying so; one a run config's loading served (its model the endpoint's spec, on the loopback) is refused
    for the loopback, which a container cannot reach, the message naming such a model too."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    with pytest.raises(ValueError, match="is a model the answer agent reaches through a CLI"):
        make_agent(tmp_path, fake_harbor, tasks, binding=None, model={"cli": "codex", "model": "gpt-5.6-luna"})
    with pytest.raises(ValueError, match=r"loopback.*a model a CLI reaches \(model: \{cli: \.\.\.\}"):
        make_agent(tmp_path, fake_harbor, tasks, binding={**BINDING, "base_url": "http://127.0.0.1:8123/v1"})


def test_a_sign_in_that_cannot_be_taken_back_leaves_the_trial_and_no_copy_in_the_job(tmp_path, monkeypatch, capsys):
    """The ChatGPT sign-in a trial's Codex refreshed, when the shared copy's folder cannot take it (an OSError: a full
    disk), is lost with a warning: the trial stands, and no copy of a sign-in is left in its job."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from hermit.agent.base import login as subscription
    from hermit.harbor import launch
    job = tmp_path / "job"
    when = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    refreshed = json.dumps({"last_refresh": when, "tokens": {"refresh_token": "rt-stand-in", "access_token": "at"}})
    for folder in (launch.RETURN_DIR, launch.HOME_DIR):
        write(job / launch.LAUNCH_DIR / folder / subscription.FILE, refreshed)

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(subscription, "take_back", full)
    agent = SimpleNamespace(login="chatgpt", login_root=tmp_path / "login", params={})
    HarborAgent.sign_in_back(agent, job)
    assert "could not be taken back" in capsys.readouterr().err
    assert not list(job.rglob(subscription.FILE))


# ---- a trial's sign-in: what Hermit's side reads of it (hermit.harbor.signin) ------------------------------------

def test_a_codex_streams_lost_sign_in_is_codexs_own_word_since_the_session_last_worked():
    """signin.codex_signin_lost: Codex's report of a sign-in or a key the provider refused (CODEX_AUTH) in a line of
    its own or an error since the session last worked, or a 403 in the error its turn failed with; never a recovery
    notice Codex went on from (its reconnect), a 403 anywhere else, a refusal the session worked after, what Codex's
    log quotes of the session's tool calls, or a line another program's log record that follows a record of Codex's."""
    from hermit.harbor import signin
    start = '{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
    error = '{"type":"error","message":"%s"}\n'
    failed = '{"type":"turn.failed","error":{"message":"%s"}}\n'
    worked = '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"working"}}\n'
    lost = (start + "Error: Not logged in\n",                                       # codex exec's own line
            start + failed % "unexpected status 401 Unauthorized: Your token has been invalidated.",
            start + error % "Your access token could not be refreshed. Please log out and sign in again."
            + failed % "stream disconnected before completion",
            start + failed % "unexpected status 403 Forbidden",
            start + "2026-11-12T01:02:03.456789Z ERROR codex_core::client: Invalid API key\n" + failed % "exit")
    kept = (start + error % "Reconnecting... 1/5 (unexpected status 401 Unauthorized: Missing bearer)" + worked,
            start + error % "Reconnecting... 2/5 (unexpected status 403 Forbidden)" + failed % "stream disconnected",
            start + error % "unexpected status 403 Forbidden" + failed % "stream disconnected",
            start + error % "unexpected status 401 Unauthorized" + worked + failed % "the context window ran out",
            start + "2026-11-12T01:02:03.456789Z ERROR codex_core::tools::router: error=apply_patch verification "
            "failed: Failed to find expected lines in /app/auth.py:\n    return jsonify(error=\"Invalid API key\"), 401\n"
            + failed % "exit",
            start + "2026-11-12T01:02:03.456789Z ERROR codex_exec: a step failed\n"
            "2026-11-12T01:02:03Z ERROR server::auth: Invalid API key\n" + failed % "exit")
    assert [signin.codex_signin_lost(raw) for raw in lost] == [True] * len(lost)
    assert [signin.codex_signin_lost(raw) for raw in kept] == [False] * len(kept)


def test_a_codex_streams_provider_outage_is_its_last_failure_or_a_retry_it_was_still_making(tmp_path):
    """signin.codex_unavailable: the provider could not serve a Codex session that served no turn when its last
    failure says so (the error its turn failed with, else its last error since it last worked), when its turn failed
    with the error Codex was retrying (its retries run out) or the stream ended while it retried, or when no session
    began and a line says so (docker's own error); never a reconnect Codex went on from before the session failed on
    its own (its retried request got through: the session's own error came after it), which the Base Agent's reading
    would take for an outage, nor a response the provider's content filter ended (Codex writes it as a disconnected
    stream): the agent's result."""
    from hermit.harbor import signin
    start = '{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
    error = '{"type":"error","message":"%s"}\n'
    failed = '{"type":"turn.failed","error":{"message":"%s"}}\n'
    worked = '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"working"}}\n'
    routing = "workspace routing discovery failed"
    out = (UNREACHABLE,
           start + failed % "stream disconnected before completion: error sending request",
           start + worked + error % "Reconnecting... 3/5 (stream disconnected before completion)",
           start + error % f"Reconnecting... 5/5 ({routing})" + error % routing + failed % routing,
           start + error % "Reconnecting... 2/5 (unexpected status 401 Unauthorized)",    # it ended as Codex retried
           start + error % "Reconnecting... 2/5" + failed % "exit",                      # no word of what it retried
           "docker: Error response from daemon: no such image.\nSee 'docker run --help'.\n")
    own = (start + error % "Reconnecting... 1/5 (stream disconnected before completion)" + worked
           + failed % "Your input exceeds the context window of this model.",
           start + error % "Reconnecting... 1/5 (stream disconnected before completion)"
           + error % "Your input exceeds the context window of this model."
           + failed % "Your input exceeds the context window of this model.",
           start + error % f"Reconnecting... 5/5 ({routing})" + failed % "the agent stopped",
           start + failed % ("stream disconnected before completion: Incomplete response returned, reason: "
                             "content_filter"),
           start + error % "Reconnecting... 1/5 (stream disconnected before completion: Incomplete response returned, "
           "reason: content_filter)" + failed % "stream disconnected before completion: Incomplete response returned, "
           "reason: content_filter",
           start + worked + failed % "the agent stopped")
    assert [signin.codex_unavailable(raw) for raw in out] == [True] * len(out)
    assert [signin.codex_unavailable(raw) for raw in own] == [False] * len(own)
    # the Base Agent's own reading takes the first of `own` for an outage: why the runner reads it here
    from hermit.agent.base.cli import Codex
    stream = write(tmp_path / "agent-codex.jsonl", own[0])
    assert Codex({}).outcome(own[0], stream, None)["unavailable"] is True


def test_a_codex_streams_usage_limit_and_refused_configuration_are_codexs_own_words(tmp_path):
    """signin.codex_refused and signin.codex_rejected read Codex's own notices only: the plan's usage limit (the Base
    Agent's words) in a line of Codex's or an error since the session last worked, and a refusal of the run's
    configuration or bill in its last failure (a 400, 402 or 404 Codex does not retry, an API account's quota); never
    the words in what Codex's log quotes of the session's tool calls, a line of which may be a JSON object, which the
    Base Agent's reading takes for its notices (and its JSON line for an event that ends the record)."""
    from hermit.harbor import signin
    start = '{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
    error = '{"type":"error","message":"%s"}\n'
    failed = '{"type":"turn.failed","error":{"message":"%s"}}\n'
    worked = '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"working"}}\n'
    record = "2026-11-12T01:02:03.456789Z ERROR codex_core::tools::router: error=apply_patch verification failed:\n"
    limit = "You've hit your usage limit. Upgrade to Pro or try again in 2 hours."
    quota = "Quota exceeded. Check your plan and billing details."
    assert signin.codex_refused(start + error % limit + failed % limit)
    assert signin.codex_refused(start + "Error: " + limit + "\n")
    assert not signin.codex_refused(start + error % limit + worked + failed % "the context window ran out")
    quoted = start + record + '{"note": "a JSON line of the patch"}\n' + f"    say('{limit}')\n" + failed % "exit"
    assert not signin.codex_refused(quoted)
    assert signin.codex_rejected(start + failed % quota) == quota
    for status in ("400 Bad Request: Unsupported parameter: 'max_tokens'", "402 Payment Required",
                   "404 Not Found: The model `gpt-x` does not exist"):
        raw = start + error % f"unexpected status {status}" + failed % f"unexpected status {status}"
        assert signin.codex_rejected(raw) == f"unexpected status {status}"
    for raw in (start + failed % "unexpected status 401 Unauthorized",
                start + failed % "unexpected status 429 Too Many",
                start + error % "unexpected status 404 Not Found" + worked + failed % "the agent stopped",
                start + record + f"    raise SystemExit('{quota}')\n" + failed % "exit"):
        assert signin.codex_rejected(raw) is None
    # a provider's error body Codex gives as it is (a 400 it reads as an invalid request), and its words for a plan
    # that refuses every session until someone acts: refusals too, of the configuration (model_rejected) or of the
    # bill, which end the run (usage_limit, a halt); a body that refused the request's input is the agent's result
    exited = errors.classify(trial_result(0.0, exc="NonZeroAgentExitCodeError"))
    cases = {'{"error":{"message":"Unsupported value: \'xhigh\' is not supported with the \'gpt-5-mini\' model.",'
             '"type":"invalid_request_error","param":"reasoning.effort","code":"unsupported_value"}}': "model_rejected",
             '{"detail":"The \'gpt-x\' model is not supported when using Codex with a ChatGPT account."}':
                 "model_rejected",
             "Your workspace is out of credits. Add credits to continue.": "usage_limit",
             "You hit your spend cap set in your workspace. Increase your spend cap to continue.": "usage_limit",
             "To use Codex with your ChatGPT plan, upgrade to Plus: https://chatgpt.com/explore/plus.": "usage_limit",
             '{"error":{"message":"Your input exceeds the context window of this model.",'
             '"type":"invalid_request_error","code":"context_length_exceeded"}}': "agent"}
    for said, want in cases.items():
        raw = start + json.dumps({"type": "turn.failed", "error": {"message": said}}) + "\n"
        refusal = signin.codex_rejected(raw)
        assert refusal == said, said
        got = HarborAgent.session_verdict(exited, {"served": False, "rejected": True, "failure": refusal,
                                                   "started": True, "tokens": 0})
        if want == "agent":
            assert got["status"] == "agent_error" and got["failure_cause"] != "model_rejected", (said, got)
        else:
            assert (got["failure_cause"], got["halt"]) == (want, want == "usage_limit"), (said, got)
    assert not signin.codex_refused(start + failed % "Your workspace is out of credits. Add credits to continue.")
    # the Base Agent's own reading takes the quoted limit for the plan's refusal
    from hermit.agent.base.cli import Codex
    stream = write(tmp_path / "agent-codex.jsonl", quoted)
    assert Codex({}).outcome(quoted, stream, None)["refused"] is True


def test_a_returned_sign_in_becomes_the_shared_copy_only_as_a_small_regular_chatgpt_sign_in_on_a_clock_it_trusts(
        tmp_path, monkeypatch):
    """signin.take_back offers the sign-in a container handed back to login.take_back only as a regular file (never
    through a link) of at most MAX_BYTES holding a refresh and an access token, stamped at most SKEW past this
    machine's clock in Harbor's local Docker environment (OTHER_CLOCK in another), read once into a private copy that
    is removed after."""
    from datetime import datetime, timedelta, timezone
    from hermit.agent.base import login
    from hermit.harbor import signin
    offered = []
    monkeypatch.setattr(login, "take_back", lambda where, path, within=None: offered.append(
        json.loads(Path(path).read_text())["tokens"]["refresh_token"]))
    now = datetime.now(timezone.utc)

    def sign_in(when, refresh, access="at-stand-in"):
        return json.dumps({"last_refresh": when.isoformat(), "tokens": {"refresh_token": refresh,
                                                                        "access_token": access}})

    def back(name, text=None, link=None, local=True):
        folder = tmp_path / name / "launch" / "return"
        folder.mkdir(parents=True)
        if link is not None:
            (folder / signin.FILE).symlink_to(link)
        else:
            (folder / signin.FILE).write_text(text)
        return signin.take_back(tmp_path / "login", folder, local=local)
    assert back("refreshed", sign_in(now - timedelta(hours=1), "rt-refreshed"))
    assert not back("ahead", sign_in(now + timedelta(minutes=10), "rt-ahead"))
    assert back("elsewhere", sign_in(now + timedelta(minutes=10), "rt-elsewhere"), local=False)
    assert not back("no-access", sign_in(now, "rt-no-access", access=""))
    assert not back("large", sign_in(now, "rt-" + "x" * signin.MAX_BYTES))
    assert not back("link", link=write(tmp_path / "host" / signin.FILE, sign_in(now, "rt-host")))
    assert offered == ["rt-refreshed", "rt-elsewhere"] and not list(tmp_path.glob("*/launch/" + signin.TAKEN_DIR))


# ---- a trial that is not the agent's result ------------------------------------------------------------------------

def test_a_trial_that_ended_infra_runs_again_and_one_that_stays_so_is_a_failed_run(tmp_path, fake_harbor, monkeypatch):
    """A trial that is not the agent's result runs again, infra_retries times at most, each after infra_wait_s, what
    every run used the trial's (extra.attempts). One that ends so every time is returned so, and the judge refuses to
    score it: the run records a failed run (judge_error), never a 0 the agent earned. A run again that is the agent's
    result stands; a cause no run again helps (a task Harbor runs for none of Hermit's agents) is not run again."""
    from hermit.trainer.rollout import run_one
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    mcp = make_task(tasks, "mcp")
    (mcp / "task.toml").write_text((mcp / "task.toml").read_text() + '\n[[environment.mcp_servers]]\nname = "bank"\n'
                                   'transport = "sse"\nurl = "http://bank:8000/sse"\n')
    rows = {r["harbor_task"]: r for r in dataset.index(tasks)}
    paused = []
    monkeypatch.setattr(HarborAgent, "_pause", lambda self, seconds: paused.append(seconds))
    monkeypatch.setenv("FAKE_HARBOR_MODE", "no_tools")                # every verifier's tools never arrive
    agent = make_agent(tmp_path, fake_harbor, tasks, infra_retries=2, infra_wait_s=7)
    record = run_one(rows["alpha"], None, agent.run, judge.score)
    unscored_record(record, "verifier_failed")
    assert paused == [7.0, 7.0] and len(record["pipeline_extra"]["attempts"]) == 3
    assert len(list((tmp_path / "jobs").glob("alpha-*"))) == 3

    def recovered(self, seconds):                                     # the verifier's tools arrive the second time
        monkeypatch.setenv("FAKE_HARBOR_MODE", "ok")
    monkeypatch.setattr(HarborAgent, "_pause", recovered)
    again = run_one(rows["alpha"], None, make_agent(tmp_path / "again", fake_harbor, tasks, infra_retries=2,
                                                    infra_wait_s=0).run, judge.score)
    assert (again["status"], again["judge"]["score"], len(again["pipeline_extra"]["attempts"])) == ("ok", 1.0, 2)
    paused.clear()
    monkeypatch.setattr(HarborAgent, "_pause", lambda self, seconds: paused.append(seconds))
    once = make_agent(tmp_path / "once", fake_harbor, tasks, infra_retries=2).run(rows["mcp"], None)
    assert once.extra["unscored"] == "unsupported_task" and paused == [] and "attempts" not in once.extra


def test_a_cause_no_run_again_helps_ends_at_its_first_run(tmp_path, fake_harbor, monkeypatch):
    """With runs again allowed (infra_retries 2), a trial whose cause runs no better again is not run again
    (runner.NO_RETRY): a disk gate that waited its time out, and a request the provider refused for the run's
    configuration (model_rejected: its key, its permission, its model, its bill); nor is a host failure past the
    trial's staging (host_failed, whose session may have run: HarborAgent.run catches it outside the trial's runs).
    Each is a failed run at once, its first run's; another infra cause (a session that never started) runs again."""
    from hermit.trainer.rollout import run_one
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    row = dataset.index(tasks)[0]
    paused = []
    monkeypatch.setattr(HarborAgent, "_pause", lambda self, seconds: paused.append(seconds))
    agent = make_agent(tmp_path, fake_harbor, tasks, infra_retries=2, infra_wait_s=0)
    with monkeypatch.context() as patch:
        patch.setattr(HarborAgent, "wait_for_disk", lambda self, *args, **kwargs: False)
        record = run_one(row, None, agent.run, judge.score)
    unscored_record(record, "disk")
    assert paused == [] and "attempts" not in record["pipeline_extra"]
    runs = []

    def once(self, task, adapter, started):                 # a trial's verdict, as the runner reads its session
        runs.append(cause)
        return self._output(task, errors.no_trial(cause, f"{cause}: a stand-in"), None, None, started)
    monkeypatch.setattr(HarborAgent, "_trial_once", once)
    for cause, times in (("model_rejected", 1), ("session_failed", 3)):
        runs.clear()
        record = run_one(row, None, agent.run, judge.score)
        unscored_record(record, cause)
        assert len(runs) == times, cause
    assert paused == [0.0, 0.0]                              # session_failed's two runs again
    launched = []

    def no_fork(self, task, adapter, started):              # a host failure past the staging: its session may have run
        launched.append(task["id"])
        raise BlockingIOError(11, "Resource temporarily unavailable")
    monkeypatch.setattr(HarborAgent, "_trial_once", no_fork)
    record = run_one(row, None, agent.run, judge.score)
    unscored_record(record, "host_failed")
    assert launched == [row["id"]] and paused == [0.0, 0.0]  # never run again


def test_the_runner_refuses_a_trial_cache_and_infra_settings_it_cannot_take(tmp_path, fake_harbor):
    """There is no trial cache (no trial is taken from an earlier run's): `trial_cache` is refused, saying what a
    resumed run runs instead, as is an infra retry count or wait that is no count or no number of seconds."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    with pytest.raises(ValueError, match="there is no trial cache any more"):
        make_agent(tmp_path, fake_harbor, tasks, trial_cache=True)
    for bad, match in (({"infra_retries": -1}, "infra_retries is a count"), ({"infra_retries": "two"}, "a count"),
                       ({"infra_wait_s": -5}, "infra_wait_s is a number of seconds")):
        with pytest.raises(ValueError, match=match):
            make_agent(tmp_path, fake_harbor, tasks, **bad)
    agent = make_agent(tmp_path, fake_harbor, tasks, infra_retries=None, infra_wait_s=None)
    assert (agent.infra_retries, agent.infra_wait_s) == (2, 300.0)


def test_a_benchmark_files_model_is_a_name_or_one_a_cli_reaches(tmp_path, fake_harbor):
    """The benchmark file names the methods' configs' model by name (the CLI's own, with a login; one of Harbor's own
    agents') or as one a CLI reaches (model: {cli: ...}, which the runner serves itself, its login's plan the trials'
    to wait for: plan_login); a <backend>/<model> reference or another model mapping there is refused, as it belongs
    in the run config's agent.params.model, which the config's loading binds and hermit check sees. A run config that
    gives its own model never reads the file's."""
    tasks = tmp_path / "utils" / "tasks"
    make_task(tasks, "alpha")

    def bench(model):
        write(tmp_path / "utils" / "benchmark.yaml", yaml.safe_dump({"tasks_dir": "tasks", "web_search": False,
                                                                       "model": model}))
        return {"benchmark": "../utils/benchmark.yaml", "harbor": str(fake_harbor), "jobs_dir": str(tmp_path / "jobs"),
                "infra_retries": 0, "disk_gate_gb": 0, "tmp_gate_gb": 0}
    for model in ("openai/gpt-5.6-luna", {"model": "glm-5", "base_url": "https://models.example/v1"}):
        with pytest.raises(ValueError, match="give it in the run config's own agent.params.model"):
            HarborAgent(bench(model), base=tmp_path / "method")
    given = HarborAgent({**bench("openai/gpt-5.6-luna"), "model": BINDING["model"],
                         "binding": BINDING}, base=tmp_path / "method")
    assert given.params["model"] == BINDING["model"] and given.plan_login() is None
    chatgpt_login(tmp_path)
    signed_in = HarborAgent({**bench("gpt-5.6-luna"), "login": "chatgpt"}, base=tmp_path / "method")
    assert signed_in.plan_login() == "chatgpt"
    reached = {"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt", "plan_dir": str(tmp_path / "plan")}
    served = HarborAgent({**bench(reached), "acting_agent": {"cli": "answer"}, "web_search": None},
                         base=tmp_path / "method")
    assert served.cli_model["cli"] == "codex" and served.plan_login() == "chatgpt"
    assert served.model_plan() == (tmp_path / "plan", False)


# ---- the report: pass@1 over the agent's results --------------------------------------------------------------------

def test_the_report_gives_pass_at_1_over_the_trials_that_are_the_agents_result(tmp_path, capsys):
    """python -m hermit.harbor.report reads an eval's results.jsonl: of its trials, the ones the judge scored (the
    agent's results), the ones that are not the agent's result (infra: the judge refused to score them) and the other
    failed runs; pass@1 and the mean of the scored ones, and hermit eval's mean (every trial, the others as 0); by
    repeat too."""
    from dataclasses import asdict
    from hermit.harbor import report
    from hermit.task import RunOutput, normalise_judge

    def judged(reward, status="ok"):
        # the judge's record as hermit eval keeps it (hermit.trainer.rollout: asdict of normalise_judge)
        output = RunOutput(output={"status": status, "reward": reward},
                           extra={"judge": {"pass_reward": 1.0}})
        return asdict(normalise_judge(judge.score({"id": "t"}, output)))
    failed = {"score": 0.0, "feedback": "the judge failed: Unscored: ..."}
    rows = [{"repeat": 1, "id": "a", "status": "ok", "output": {"status": "ok"}, "judge": judged(1.0)},
            {"repeat": 1, "id": "b", "status": "ok", "output": {"status": "agent_error"},
             "judge": judged(0.0, "agent_error")},
            {"repeat": 1, "id": "c", "status": "judge_error", "output": {"status": "infra"}, "judge": failed},
            {"repeat": 2, "id": "a", "status": "error", "output": None, "judge": {"score": 0.0}},
            {"repeat": 2, "id": "b", "status": "ok", "output": {"status": "ok"}, "judge": judged(1.0)}]
    assert [report.passed(r) for r in rows] == [True, False, None, None, True]
    results = write(tmp_path / "eval" / "final" / "results.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
    figures = report.report(results.parent)
    assert [figures[k] for k in ("trials", "scored", "infra", "failed")] == [5, 3, 1, 1]
    assert figures["pass_at_1"] == pytest.approx(2 / 3) and figures["mean_scored"] == pytest.approx(2 / 3)
    assert figures["mean_all"] == pytest.approx(2 / 5)
    assert figures["by_repeat"]["1"]["pass_at_1"] == 0.5 and figures["by_repeat"]["2"]["failed"] == 1
    assert report.main([str(results), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {**figures, "results": str(results)}
    assert report.main([str(results.parent)]) == 0
    out = capsys.readouterr().out
    assert "5 trials: 3 scored, 1 infra, 1 failed; pass@1 66.7% of the scored" in out and "\n  repeat 2: " in out
    assert report.main([str(tmp_path / "none")]) == 1


def test_the_report_says_when_an_eval_did_not_finish(tmp_path, monkeypatch, capsys):
    """Through main's own `hermit eval` (hermit.evaluate.evaluate, on the toy task): a finished eval reads as finished,
    and so does its copy whatever the files' times (a copy changes them). An eval of the same folder that a lost
    sign-in stopped (LoginFailed) wrote no row of the repeat it stopped in, run_tasks handing back a repeat's records
    once all are done, beside the summary of the eval before: the report says it did not finish (`finished` false,
    `unfinished` why), as it does for rows with no summary beside them, for a summary that is not of its rows, and for
    rows whose last one is cut off (an eval stopped as it wrote it), which is left out instead of failing the report."""
    import shutil
    from hermit.agent.base.interface import LoginFailed
    from hermit.config import Config
    from hermit.evaluate import evaluate
    from hermit.harbor import report
    from hermit.trainer import rollout
    from toy_task import make_task
    cfg = Config.load(make_task(tmp_path), [])
    evaluate(cfg, "none", log=lambda *_: None)
    folder = cfg.run_dir() / "eval" / "none"
    rows, summary = report.rows_of(folder), (folder / "summary.json").read_text()
    finished = report.report(folder)
    assert (finished["finished"], finished["trials"]) == (True, 3) and "unfinished" not in finished
    assert "note" not in finished                                 # in place: the summary written after the rows
    copy = shutil.copytree(folder, tmp_path / "handed-back")
    os.utime(copy / "summary.json", (time.time() - 3600, time.time() - 3600))   # an hour older than the rows
    handed = report.report(copy)
    assert handed["finished"] is True and "unless the folder was copied" in handed["note"]   # noted, never unfinished
    assert report.main([str(copy)]) == 0 and "\n  note: its results.jsonl was written" in capsys.readouterr().out
    (copy / "summary.json").unlink()
    assert "no summary.json beside its rows" in report.report(copy)["unfinished"]
    other = write(tmp_path / "other" / "results.jsonl", "".join(
        json.dumps({**r, "judge": {**r["judge"], "score": 1.0 - r["judge"]["score"]}}) + "\n" for r in rows))
    write(other.parent / "summary.json", summary)                 # three trials, but another mean score
    assert "is not of its rows" in report.report(other)["unfinished"]
    failed = write(tmp_path / "failed" / "results.jsonl", "".join(  # the same scores, but a failed run among them
        json.dumps({**r, "status": "error" if n == 0 else r["status"]}) + "\n" for n, r in enumerate(rows)))
    write(failed.parent / "summary.json", summary)
    assert "another count of failed runs" in report.report(failed)["unfinished"]
    text = (folder / "results.jsonl").read_text()                 # an eval stopped as it wrote its last row
    cut = write(tmp_path / "cut" / "results.jsonl", text[:len(text) - 40])
    write(cut.parent / "summary.json", summary)
    figures = report.report(cut.parent)
    assert (figures["finished"], figures["trials"]) == (False, 2) and "is cut off" in figures["unfinished"]
    assert report.main([str(cut.parent)]) == 0 and "  unfinished: the last row" in capsys.readouterr().out
    broken = write(tmp_path / "broken" / "results.jsonl", text.replace("\n", "\n{not json\n", 1))
    with pytest.raises(ValueError, match="line 2 of .* is no JSON row"):    # a row of its own that is none: no cut
        report.read_rows(broken)

    def lost(task, adapter, pipeline, judge):
        raise LoginFailed("the sign-in was lost (a stand-in)")
    monkeypatch.setattr(rollout, "run_one", lost)
    with pytest.raises(LoginFailed):
        evaluate(cfg, "none", log=lambda *_: None)
    stopped = report.report(folder)
    assert (stopped["finished"], stopped["trials"]) == (False, 0)               # beside the eval before's summary
    assert "counts 3 trials and its results.jsonl holds 0" in stopped["unfinished"]
    assert report.main([str(folder)]) == 0
    assert "\n  unfinished: its summary.json counts 3 trials" in capsys.readouterr().out


# ---- staging's child: the host's deployment with the tools the runner serves ---------------------------------------

@pytest.mark.parametrize("host", ["pi", "dsh"])
def test_staging_deploys_pi_and_dsh_without_the_tools_the_runner_serves(tmp_path, host, capsys):
    """`python -m hermit.harbor.staging` (prepare_workspace's child, in the strict adapter sandbox) deploys the adapter
    as the host's own deployment does, but for its tools, which the runner serves to the container itself: Pi's
    .pi/mcp.json names no server, dsh's patch layer holds no server row (serve_tools=False), and its report is JSON."""
    from hermit.harbor import staging
    adapter = tool_adapter(tmp_path / "adapter")
    workspace = tmp_path / "w"
    assert staging.main(["--host", host, "--adapter", str(adapter), "--workspace", str(workspace),
                         "--python", sys.executable]) == 0
    reported = json.loads(capsys.readouterr().out)
    assert reported["context_injected_via"] == "AGENTS.md" and (workspace / "AGENTS.md").is_file()
    if host == "pi":
        assert json.loads((workspace / ".pi" / "mcp.json").read_text()) == {"mcpServers": {}}
    else:
        assert not [r for r in json.loads((workspace / ".dsh" / "hermit.patch.yml").read_text()) if "insert" in r]


# ---- the round-10 review: what tells the host's failure from the adapter's, and Codex's readings ---------------------

def test_a_staging_child_that_failed_beside_this_machines_own_failure_is_infra_never_the_adapters_0(
        tmp_path, fake_harbor, monkeypatch):
    """A staging child's failure (its RuntimeError) is the adapter's unless this process sees this machine fail any
    adapter's staging there: a job folder it cannot write, less than STAGING_FREE_MB free beside the trial's job, or the
    strict adapter sandbox not starting (bubblewrap without its namespaces: sandbox_fault). Then the trial, and a stream's step, are infra
    (staging_failed: run again), the child's own words kept in the message; the same failure beside room and a sandbox
    that starts is the adapter's (adapter_failed, scored 0)."""
    from collections import namedtuple
    import shutil as shutil_mod
    from hermit.harbor import runner as runner_mod
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    make_task(tasks, "flow", steps=("s1", "s2"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks)

    def stage(*args, **kwargs):
        raise RuntimeError("deploying the adapter failed: OSError: [Errno 28] No space left on device")
    monkeypatch.setattr("hermit.harbor.runner.stage", stage)
    usage, real_usage, real_access = namedtuple("usage", "total used free"), shutil_mod.disk_usage, os.access
    jobs = tmp_path / "jobs"

    def access(path, mode, **kwargs):                       # the job folder this process may not write
        return False if Path(path).is_relative_to(jobs) and mode & os.W_OK else real_access(path, mode, **kwargs)
    bwrap = "the strict adapter sandbox did not start (exit 1): bwrap: setting up uid map: Permission denied"
    says = {"full": "10 MB free", "sandbox": "bwrap: setting up uid map", "unwritable": "cannot be written"}
    for case, low, fault in (("full", True, None), ("sandbox", False, bwrap), ("unwritable", False, None),
                             ("adapter", False, None)):
        monkeypatch.setattr(runner_mod.shutil, "disk_usage",
                            (lambda path: usage(1 << 40, 1 << 40, 10 << 20)) if low else real_usage)
        monkeypatch.setattr(runner_mod, "sandbox_fault", lambda enforce, fresh=True, fault=fault: fault)
        monkeypatch.setattr(runner_mod.os, "access", access if case == "unwritable" else real_access)
        for row in (rows["alpha"], rows["flow:s1"]):
            out = agent.run(row, simple_template())
            want = ("agent_error", "adapter_failed") if case == "adapter" else ("infra", "staging_failed")
            assert (out.output["status"], out.output["failure_cause"]) == want, (case, row["id"])
            if case != "adapter":
                message = out.extra["exception_message"]
                assert says[case] in message, (case, message)
                assert "No space left on device" in message, (case, message)       # the child's own words, kept
    assert not agent._in_flight


def test_a_runner_on_a_machine_whose_bubblewrap_cannot_start_its_sandbox_is_refused_at_startup(
        tmp_path, fake_harbor, monkeypatch):
    """bubblewrap installed but unable to start the strict adapter sandbox (no user namespaces, an AppArmor rule)
    would fail every trial's staging: the runner is refused at startup, saying why and the way out (sandbox_enforce:
    best_effort, with which it starts); once a sandbox started in this process, a later runner does not try again."""
    import shutil as shutil_mod
    import subprocess as subprocess_mod
    from hermit import sandbox as sandbox_mod
    from hermit.harbor import runner as runner_mod
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    real_which, tries = shutil_mod.which, []
    monkeypatch.setattr(runner_mod.shutil, "which",
                        lambda name, *a, **k: "/usr/bin/bwrap" if name == "bwrap" else real_which(name, *a, **k))
    monkeypatch.setattr(runner_mod, "_SANDBOX_STARTED", [])
    monkeypatch.setattr(runner_mod, "sandbox_fault", REAL_SANDBOX_FAULT)

    def refused(command, **kwargs):
        tries.append(command)
        return subprocess_mod.CompletedProcess(command, 1, "", "bwrap: setting up uid map: Permission denied\n")
    monkeypatch.setattr(sandbox_mod, "run_in_strict_sandbox", refused)
    with pytest.raises(ValueError, match=r"the strict adapter sandbox did not start \(exit 1\): bwrap: setting up uid "
                                         r"map: Permission denied: every trial's staging would fail"):
        make_agent(tmp_path, fake_harbor, tasks)
    assert make_agent(tmp_path, fake_harbor, tasks, sandbox_enforce="best_effort").cli == "codex"
    monkeypatch.setattr(sandbox_mod, "run_in_strict_sandbox",
                        lambda command, **kwargs: (tries.append(command), subprocess_mod.CompletedProcess(
                            command, 0, "", ""))[1])
    make_agent(tmp_path, fake_harbor, tasks)
    make_agent(tmp_path, fake_harbor, tasks)
    assert len(tries) == 2                                   # the refused try, then one that started: no more


def test_a_trials_launch_plan_refused_for_its_adapter_is_the_adapters_0_and_with_no_adapter_the_configs_error(
        tmp_path, fake_harbor, monkeypatch):
    """A launch plan refused at a trial for what its adapter is (dsh's MCP server name made of the adapter's id, over 32
    characters or with a '.') is the adapter's result, as a deployment of it that failed: adapter_failed, scored 0 with
    the message as its feedback, for a trial and for a stream's step alike. With no adapter, the same plan rendered at
    startup (check_agent), so its refusal then is the config's: a pipeline error, as before."""
    from hermit.trainer.rollout import run_one
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    make_task(tasks, "flow", steps=("s1", "s2"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks)

    def refuse(self, *args, **kwargs):
        raise ValueError("dsh takes an MCP server name of 32 characters at most ([A-Za-z0-9_-]), not 'a.b'")
    monkeypatch.setattr(HarborAgent, "_launch", refuse)
    for row in (rows["alpha"], rows["flow:s1"]):
        record = run_one(row, simple_template(), agent.run, judge.score)
        assert (record["status"], record["output"]["status"], record["output"]["failure_cause"]) == \
            ("ok", "agent_error", "adapter_failed"), row["id"]
        assert record["judge"]["score"] == 0.0 and "32 characters at most" in record["judge"]["feedback"]
    assert not agent._in_flight and not list((tmp_path / "jobs").glob("alpha-*/alpha__*"))      # no trial ran
    with pytest.raises(ValueError, match="32 characters at most"):
        agent.run(rows["alpha"], None)


def test_a_codex_session_that_failed_on_its_own_after_a_reconnect_it_went_on_from_is_the_agents_result(
        tmp_path, fake_harbor, monkeypatch):
    """A Codex session that met a provider's notice, reconnected (Codex went on: an item after it) and then failed on
    its own (its turn failed, not on the provider) is the agent's result, scored and never run again: the runner reads
    Codex's last failure (signin.codex_unavailable), where the Base Agent's reading of the whole stream (main's Codex
    outcome) takes the reconnect's notice for an outage."""
    from hermit.agent.base.cli import Codex
    raw = TIMED_OUT + '{"type":"turn.failed","error":{"message":"exit"}}\n'
    assert outcome(Codex({}), raw, tmp_path / "read")["unavailable"] is True           # main's reading: an outage
    monkeypatch.setenv("FAKE_DEFAULT_STREAM", raw)
    monkeypatch.setenv("FAKE_HARBOR_MODE", "agent_exit")                     # the CLI exited 1; the verifier ran
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    out = make_agent(tmp_path, fake_harbor, tasks, infra_retries=1, infra_wait_s=0).run(row, None)
    assert (out.output["status"], "unscored" in out.extra, out.extra["session"]["unavailable"]) == \
        ("agent_error", False, False)
    assert len(list((tmp_path / "jobs").glob("alpha-*"))) == 1                  # not run again


def test_a_stream_given_up_twice_keeps_the_first_reason(tmp_path, fake_harbor):
    """A stream two of its steps gave up (one ending before its driver, another as it waited) keeps the first reason,
    which the steps after it report."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("s1", "s2", "s3"))
    agent = make_agent(tmp_path, fake_harbor, tasks)
    agent._give_up("flow__s1__x", 2, "staging_failed")
    agent._give_up("flow__s1__x", 3, "tools_failed")
    assert agent._given_up["flow__s1__x"] == \
        "its step 2 ended before it reached the stream's driver (staging_failed)"


def test_curate_puts_at_the_top_the_stream_the_plan_names_never_another_clis_the_agent_wrote(tmp_path):
    """The log's top stream is the one the trial's launch plan names (a Pi trial's agent-pi.jsonl): a file of another
    CLI's name the session wrote in its agent folder (an agent-codex.jsonl) is never taken for it; a plan of the answer
    agent, whose answer.json is no CLI's stream, puts none at the top."""
    trial = tmp_path / "trial"
    write(trial / "agent" / "agent-pi.jsonl", '{"type":"session"}\n')
    write(trial / "agent" / "agent-codex.jsonl", '{"type":"thread.started"}\n')       # the session's own file
    plan = write(tmp_path / "job" / "launch" / "plan.json", json.dumps({"stream": "agent-pi.jsonl"}))
    log = curate.curate(trial, tmp_path / "job", errors.classify(trial_result()), plan=plan)
    assert (log / "agent-pi.jsonl").read_text() == '{"type":"session"}\n' and not (log / "agent-codex.jsonl").exists()
    answer = write(tmp_path / "answer" / "launch" / "plan.json", json.dumps({"stream": "answer.json"}))
    log = curate.curate(trial, tmp_path / "answer", errors.classify(trial_result()), plan=answer)
    assert not (log / "agent-pi.jsonl").exists() and not (log / "agent-codex.jsonl").exists()


# ---- the round-11 review: which disk gate held a trial back ---------------------------------------------------------

def test_a_trial_or_step_the_disk_gate_held_back_names_the_gate_that_held_it(tmp_path, fake_harbor, monkeypatch):
    """The verdict of a trial, or of a stream's first step, that a disk gate held back past its wait (disk: infra, not
    run again) names the gate that held it: the host's /tmp (its own gate, waited for first) or Docker's data root.
    Before, it named the data root whichever gate held it."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    make_task(tasks, "flow", steps=("one", "two"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    root = tmp_path / "docker-root"
    free = {}
    monkeypatch.setattr(docker, "free_gb", lambda path: free.get(path, 100.0))
    monkeypatch.setattr(docker.time, "sleep", lambda s: None)
    for case, (short, says) in enumerate(((runner_module.HOST_TMP, "the host's /tmp stayed under 1 GB free"),
                                          (root, f"the Docker data root {root} stayed under 20 GB free"))):
        free.clear()
        free[short] = 0.5
        agent = make_agent(tmp_path / f"case-{case}", fake_harbor, tasks, disk_gate_gb=20, tmp_gate_gb=1,
                           disk_gate_wait_s=0.01)
        agent._root = root
        for row, then in ((rows["alpha"], "no container started"),
                          (rows["flow:one"], "the stream's environment was not started")):
            out = agent.run(row, None)
            assert (out.output["status"], out.extra["unscored"]) == ("infra", "disk"), row["id"]
            assert out.extra["exception_message"] == f"{says}: {then}", (row["id"], out.extra["exception_message"])


# ---- the round-12 review: a stream's next pass, a curated log's links, the providers' refusals ----------------------

def test_a_driver_that_is_ending_or_died_unreaped_is_no_live_driver_and_the_next_waits_for_its_exit(tmp_path,
                                                                                                  monkeypatch):
    """StreamClient.running takes no driver whose record says it is ending (harborside.driver's _mark_ending: its
    step loop over, its socket open a moment more), nor one killed and not yet reaped (a zombie, which `alive` still
    finds, beside the socket it left): the stream's next step starts a new driver instead of reusing it (a repeat's
    whole pass lost: stream_order) or failing on it (stream_lost). Before a new driver starts, an earlier one still
    there is waited for until it has exited (ENDING_WAIT_S), then killed."""
    import subprocess as subprocess_mod
    if not Path("/proc/self/stat").exists():
        pytest.skip("no /proc to tell a zombie or a driver's command line")
    sock = write(tmp_path / "d.sock", "")
    # a process named as this directory's driver would be, leading its own process group
    proc = subprocess_mod.Popen([sys.executable, "-c", "import time; time.sleep(60)", "harborside.driver",
                                 "--stream-dir", str(tmp_path)], start_new_session=True)
    try:
        client = stream.StreamClient(tmp_path, python="unused", log=lambda m: None)
        write(tmp_path / "driver.json", json.dumps({"pid": proc.pid, "socket": str(sock)}))
        assert client.running() is not None                          # alive, its socket there: live
        write(tmp_path / "driver.json", json.dumps({"pid": proc.pid, "socket": str(sock), "ending": True}))
        assert client.running() is None and not client.gone()        # ending: no live driver, its process still there
        monkeypatch.setattr(stream, "ENDING_WAIT_S", 0.5)
        client._wait_out()                                            # never exits by itself: killed
        assert proc.wait(timeout=30) is not None and client.gone()
    finally:
        proc.kill()
        proc.wait()
    zombie = subprocess_mod.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        write(tmp_path / "driver.json", json.dumps({"pid": zombie.pid, "socket": str(sock)}))
        assert client.running() is not None
        zombie.kill()                                                # not reaped: a zombie
        deadline = time.time() + 10
        while not client.gone() and time.time() < deadline:
            time.sleep(0.05)
        assert client.gone() and client.running() is None
    finally:
        zombie.wait()


def test_a_step_whose_folder_cannot_be_made_gives_its_stream_up_and_the_next_pass_starts_anew(tmp_path, fake_harbor,
                                                                                              monkeypatch):
    """A step whose own folder this machine cannot make (a full disk) ends host_failed before it reaches the driver,
    which waits for it: the stream is given up and its driver stopped, so the stream's next pass (an eval's next
    repeat) starts anew in an environment of its own, never running that step on the earlier pass's. Before, the
    folder was made outside what gives a stream up: the next pass's first step met the old driver (stream_order) and
    its last ran on the old environment, scored as the agent's."""
    import errno
    fake_driver(fake_harbor)
    started = driver_starts(monkeypatch)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
    real_mkdir = Path.mkdir

    def full(self, *args, **kwargs):
        if self.parent.name == "steps" and "-two-" in self.name:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), str(self))
        return real_mkdir(self, *args, **kwargs)
    try:
        assert agent.run(rows["flow:one"], None).output["status"] == "ok"
        with monkeypatch.context() as patch:
            patch.setattr(Path, "mkdir", full)
            failed = agent.run(rows["flow:two"], None)
        assert (failed.output["status"], failed.output["failure_cause"]) == ("infra", "host_failed")
        again = [agent.run(rows[f"flow:{step}"], None) for step in ("one", "two")]       # the stream's next pass
        assert [o.output["status"] for o in again] == ["ok", "ok"]
        assert started == [stream_name(agent)] * 2                                      # in a driver of its own
    finally:
        stop_streams(tmp_path)


def test_a_reward_or_the_copys_folder_swapped_for_a_link_after_its_checks_is_never_copied_through(tmp_path, monkeypatch):
    """A verifier's reward file, and the agent's hermit-copy folder above its memory/, swapped for links to this
    machine's files after the curation's checks (a process the agent left running): neither link is followed, the
    reward opened at its own name (open_regular) and memory/ through the copy's folder (curate._subdir)."""
    secret = write(tmp_path / "host" / "auth.json", "the machine's own secret\n")
    write(tmp_path / "host" / "memory" / "notes.md", "the machine's own notes\n")
    outputs = tmp_path / "step"
    (outputs / "verifier").mkdir(parents=True)
    os.symlink(secret, outputs / "verifier" / "reward.txt")
    (outputs / "agent").mkdir()
    os.symlink(tmp_path / "host", outputs / "agent" / "hermit-copy")
    job = tmp_path / "job"
    job.mkdir()
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)      # the checks, made before the swap
    log = Path(curate.curate(outputs, job, errors.classify(trial_result())))
    monkeypatch.undo()
    assert not [p for p in log.rglob("*") if p.is_file() and "machine's own" in p.read_text()]


@pytest.mark.parametrize("swap,after", [("link", 1), ("gone", 1), ("gone", 2), ("gone", 3)])
def test_a_curated_log_never_copies_through_a_link_swapped_in_while_it_walks(tmp_path, monkeypatch, swap, after):
    """A process the agent left running (a stream's environment stays up between steps) may swap a file of the step's
    agent/ folders for a link to a file of this machine, or remove it, while the step's log is curated: each entry is
    opened through its folder, never through a link at its name (curate._files, open_regular), so a link swapped in
    after the walk's checks is left out, not copied through, and a file gone meanwhile (after any of the checks made of
    it) is left out, the curation going on (no OSError that would make the step this machine's failure). Before, the
    checks of each file were followed by a copy that followed a link, and a file gone between them raised."""
    secret = write(tmp_path / "host" / "auth.json", "the machine's own secret\n")
    outputs = tmp_path / "step"
    victims = [write(outputs / "agent" / folder / f"a{n}.json", '{"ok": 1}\n')
               for n, folder in enumerate(("hook-state", "hermit-collect", ".hermit", "hermit-copy/memory"))]
    victims.append(write(outputs / "agent" / "hook-calls.jsonl", "{}\n"))
    for folder in ("hook-state", "hermit-collect", ".hermit", "hermit-copy/memory"):
        write(outputs / "agent" / folder / "kept.json", '{"kept": 1}\n')
    real_stat, checks = os.stat, {str(v): 0 for v in victims}

    def stat_then_move(path, *args, **kwargs):              # the container's move, right after a check of the file
        got = real_stat(path, *args, **kwargs)
        if isinstance(path, int):
            return got
        name = os.fsdecode(path)
        if kwargs.get("dir_fd") is not None:
            name = os.path.join(os.readlink(f"/proc/self/fd/{kwargs['dir_fd']}"), name)
        name = os.path.abspath(name)
        if name in checks:
            checks[name] += 1
            if checks[name] == after:
                os.unlink(name)
                if swap == "link":
                    os.symlink(secret, name)
        return got
    if not Path("/proc/self/fd").is_dir():
        pytest.skip("no /proc to name a folder by its descriptor")
    monkeypatch.setattr(os, "stat", stat_then_move)
    try:
        log = curate.curate(outputs, tmp_path / "job", errors.classify(trial_result()))
    finally:
        monkeypatch.setattr(os, "stat", real_stat)
    assert any(checks.values())                             # the curation checked the files (moved after that)
    copied = {p.relative_to(log).as_posix(): p.read_text() for p in log.rglob("*") if p.is_file()}
    assert not any("own secret" in text for text in copied.values()), copied
    assert {"hook-state/kept.json", "agent/hermit-collect/kept.json", "agent/.hermit/kept.json",
            "memory/kept.json"} <= set(copied)              # the walk went on


def test_a_benchmark_files_cli_model_without_a_login_is_refused_at_startup(tmp_path, fake_harbor):
    """The benchmark file's `model: {cli: ...}` is served as it is written (tools.ModelEndpoint): no config's loading
    binds a `<backend>/<model>` reference there, as it binds `llm`'s, so one without a login (Pi's, which has none,
    among them) would run sessions with no key, endpoint or host they may reach; it is refused at startup. On the
    CLI's own login it runs."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    for model in ({"cli": "codex", "model": "openai/gpt-5.6-luna"}, {"cli": "pi", "model": "openai/gpt-5.6-luna"},
                  {"cli": "claude", "model": "anthropic/claude-haiku-5-5"}):
        with pytest.raises(ValueError, match="with no login"):
            cli_answer_agent(tmp_path, fake_harbor, tasks, model)
    chatgpt_login(tmp_path)
    assert cli_answer_agent(tmp_path, fake_harbor, tasks, {"cli": "codex", "model": "gpt-5.6-luna",
                                                           "login": "chatgpt"}).cli_model["login"] == "chatgpt"


def test_a_login_in_the_acting_agents_settings_is_refused_and_a_relative_config_is_the_params_path(tmp_path,
                                                                                                     fake_harbor):
    """`acting_agent.settings.login` would sign the session in (launch.render_plan) where the runner, which reads
    agent.params.login alone, neither checks it nor hands the trial its sign-in, plan and refusals: refused at startup.
    mini-swe-agent's settings.config, when relative, is read beside the params (the runner's other paths), as main
    reads agent.params.config beside the run config, never from the working directory."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    with pytest.raises(ValueError, match="acting_agent.settings.login"):
        make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "codex", "settings": {"login": "chatgpt"}},
                   model="gpt-5.6-luna", binding=None).check_agent()
    write(tmp_path / "mini.yaml", "agent: {}\n")                    # beside the params, not in the working directory
    agent = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "mini-swe-agent",
                                                                   "settings": {"config": "mini.yaml"}})
    assert agent.settings()["config"] == str((tmp_path / "mini.yaml").resolve())
    agent.check_agent()                                               # its dry render reads it there
    absolute = write(tmp_path / "elsewhere" / "m.yaml", "agent: {}\n")
    agent = make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "mini-swe-agent",
                                                                   "settings": {"config": str(absolute)}})
    assert agent.settings()["config"] == str(absolute)


def test_a_held_trials_verdict_names_the_room_its_image_and_other_pulls_took(tmp_path, fake_harbor, monkeypatch):
    """_disk_held names what held a trial back: the data root's gate, with the room the task's image takes when it is
    not here yet (`image_sizes`) and the room other trials hold for their pulls, which the gate counts as taken."""
    from hermit.harbor import docker as docker_mod
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks, disk_gate_gb=5)
    agent._root = tmp_path
    monkeypatch.setattr(docker_mod, "image_size", lambda image: None)
    assert agent._disk_held(row) == f"the Docker data root {tmp_path} stayed under 5 GB free"
    agent.image_sizes = {"example/alpha:1": 2 * docker_mod.GB}
    agent.pulls.add("example/beta:1", 3.0)
    held = agent._disk_held(row)
    assert "GB more for the task's image example/alpha:1, not here yet" in held, held
    assert held.endswith("beside 3.0 GB held for the images other trials are still to pull"), held


def test_a_streams_first_step_holds_room_for_its_image_until_its_environment_is_up(tmp_path, fake_harbor,
                                                                                   monkeypatch):
    """The disk gate before a stream's environment starts is a single trial's: it keeps room for the task's image
    (wait_for_disk with the task), held for the image's pull until the driver is up, then released."""
    fake_driver(fake_harbor)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
    calls = []

    def gate(task=None, admitted=None):
        calls.append(task["id"] if task else None)
        if admitted is not None:
            admitted.append(agent.pulls.add("example/flow:1", 1.0))
        return True
    monkeypatch.setattr(agent, "wait_for_disk", gate)
    try:
        assert [agent.run(rows[f"flow:{s}"], None).output["status"] for s in ("one", "two")] == ["ok", "ok"]
    finally:
        stop_streams(tmp_path)
    assert calls == ["flow:one"] and not agent.pulls.pending      # the first step's, its room released


def test_a_streams_step_waits_for_its_turn_over_the_budgets_of_the_steps_before_it(tmp_path, fake_harbor):
    """A step's wait for its turn (stream.turn_wait_s left empty) is the budgets of the steps before it, each its
    own (a step may set its own agent and verifier timeouts), and the driver's start; never this step's budget times
    their number."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two", "three"))
    toml = tasks / "flow" / "task.toml"
    text = toml.read_text().replace('name = "one"\n', 'name = "one"\n[steps.agent]\ntimeout_sec = 100.0\n') \
        .replace('name = "two"\n', 'name = "two"\n[steps.agent]\ntimeout_sec = 900.0\n')
    toml.write_text(text)
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks)
    settings = {"start_timeout_s": 30, "turn_wait_s": None}
    budget = {s: agent.wait_budget(rows[f"flow:{s}"], build=False) for s in ("one", "two", "three")}
    assert len(set(budget.values())) == 3
    assert agent._turn_wait(rows["flow:three"], 3, settings) == budget["one"] + budget["two"] + 30
    assert agent._turn_wait(rows["flow:one"], 1, settings) == budget["one"] + 30
    assert agent._turn_wait(rows["flow:three"], 3, {**settings, "turn_wait_s": 7}) == 7
