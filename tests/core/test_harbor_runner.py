"""The Harbor runner's host side without Docker or Harbor: the verdicts of a trial's result and of the CLI's stream,
the curated log, the dataset rows, lock and splits, the judge, the adapter's staging, the startup checks, one trial
through a fake `harbor` command that carries out the launch plan as harborside would, the answer agent's model on
this machine, the adapter's tools served for a container, and stream steps through a fake driver."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
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

import pytest
import yaml

from hermit.harbor import curate, dataset, docker, errors, judge, stream
from hermit.harbor.runner import HarborAgent
from hermit.task import RunOutput

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "hermit" / "adapter" / "templates"
KEY_ENV = "FAKE_MODEL_KEY"
KEY = "fake-model-key-0123456789abcdef"                 # never a real key: what a plan must never hold
# A model bound as the config's loading binds it (hermit.agent.model.backends.bind_harbor_params): its id, and the
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
    import json, os, sys, time, urllib.request, yaml
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
    if plan is not None and plan.get("command"):
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
            refreshed = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            Path(item["to"]).write_text(json.dumps({"last_refresh": refreshed,
                                                    "tokens": {"account_id": "acct-1", "refreshed": True}}))
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
    elif mode == "timeout":                       # the agent ran out of its time; the verifier still ran, and passed
        result["exception_info"] = {"exception_type": "AgentTimeoutError", "exception_message": "Agent timed out",
                                    "exception_traceback": "tb"}
    (trial / "result.json").write_text(json.dumps(result))
    ''')


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
    """A runner for HermitCodex on the bound model, web search off (a config's params override these)."""
    return HarborAgent({"tasks_dir": str(tasks), "jobs_dir": str(tmp_path / "jobs"), "harbor": str(fake_harbor),
                        "disk_gate_gb": 0, "tmp_gate_gb": 0, "model": BINDING["model"], "binding": BINDING,
                        "web_search": False, **params}, base=tmp_path)


def chatgpt_login(root: Path, account: str = "acct-1") -> Path:
    """The user's Codex sign-in where the test's CODEX_HOME points (tests/conftest.py): a stand-in, no real token,
    refreshed two days ago."""
    from datetime import datetime, timedelta, timezone
    when = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    return write(Path(os.environ["CODEX_HOME"]) / "auth.json",
                 json.dumps({"last_refresh": when, "tokens": {"account_id": account, "refresh_token": "rt-stand-in"}}))


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
    and the reward is 0. That trial is infra (run again, never kept), not the agent's 0; a test run that failed stays
    the agent's, and so does a pass, and so does a setup that fails the same way every time (a mirror's 404)."""
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
    timeout = errors.classify(trial_result(0.0, exc="AgentTimeoutError"))
    assert HarborAgent.verifier_verdict(timeout, trial)["status"] == "timeout"             # the agent's time ran out
    write(trial / "verifier" / "ctrf.json", "{}")                                        # the tests ran and failed
    assert HarborAgent.verifier_verdict(errors.classify(trial_result(0.0)), trial)["status"] == "ok"
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


def test_teardown_takes_the_trials_verifier_projects_down_too(monkeypatch):
    """A killed trial leaves its environment's compose project and each separate verifier's
    (`<trial>__verifier__<key>`, harbor/trial/trial.py): the teardown downs them all, and no other project."""
    calls = []

    class Done:
        stdout = "alpha__abc1234__env\nalpha__abc1234__verifier__main\nbeta__xyz9876__verifier__main\n"

    def run(cmd, **kwargs):
        calls.append(cmd)
        return Done()
    monkeypatch.setattr(docker.subprocess, "run", run)
    docker.compose_down("alpha__abc1234")
    downs = [c[c.index("-p") + 1] for c in calls if "down" in c]
    assert downs == ["alpha__abc1234__env", "alpha__abc1234__verifier__main"]
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
    assert names == ["agent/agent-codex.jsonl", "agent/codex.txt", "agent/hermit-collect/last_message.txt",
                     "agent/hook-state/state.json", "exception.txt", "harbor.log", "job.yaml", "memory/NOTES.md",
                     "result.json", "tool-calls.jsonl", "verifier/reward.txt", "verifier/test-stdout.txt"]
    text = (log / "verifier" / "test-stdout.txt").read_text()
    assert text.startswith("[... ") and "characters dropped ...]" in text and len(text) < 20_100
    assert "expected answer" not in text and text.rstrip().endswith(curate.REDACTED)
    assert KEY not in (log / "agent" / "hermit-collect" / "last_message.txt").read_text()   # scrubbed: <FAKE_MODEL_KEY>
    assert "VerifierTimeoutError" in (log / "exception.txt").read_text()
    assert curate.seconds(trial_result()["agent_execution"]) == 40.0
    write(trial / "agent" / "trajectory.json", json.dumps({"steps": [{"metrics": {"a": 1}}, {}]}))
    assert curate.model_calls(trial / "agent" / "trajectory.json") == 1


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
    # Not the agent's result: not scored from its reward
    infra = RunOutput(output={"status": "infra", "failure_cause": "verifier_failed", "reward": 0.0})
    assert judge.score({}, infra)["score"] == 0.0 and judge.score({}, infra)["feedback"].startswith("infra")
    assert judge.score({}, RunOutput(output=None))["feedback"].startswith("infra")
    # The benchmark's pass reward: a reward that reaches it passed; with none, nothing is said to pass
    half = RunOutput(output={"status": "ok", "reward": 0.8}, extra={"judge": {"pass_reward": 0.75}})
    assert judge.score({}, half)["feedback"] == "passed" and judge.score({}, half)["passed"] is True
    scale = RunOutput(output={"status": "ok", "reward": 1.38}, log=log, extra={"judge": {"pass_reward": None}})
    verdict = judge.score({}, scale)
    assert verdict["score"] == 1.38 and verdict["feedback"].startswith("reward 1.38") and "FAIL" in verdict["feedback"]
    assert verdict["passed"] is None                                       # a score scale: hermit eval counts no pass
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
    agent = HarborAgent({"tasks_dir": str(tasks), "jobs_dir": str(tmp_path / "jobs"), "web_search": False,
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


# The spec hermit.isolate.agent_pipeline gives the runner for a model a CLI reaches (endpoint.endpoint_spec): its
# endpoint's URL, and on a login a timeout that waits out the plan (3600 s and plan_max_wait_s)
SERVED = {"base_url": "http://127.0.0.1:9/" + "0" * 32, "model": "gpt-5.6-luna", "api_key_env": None,
          "key_required": False, "price": None, "timeout": 3600.0 + 12 * 3600}


def test_the_answer_agents_attachments_on_a_model_a_cli_reaches_are_refused_at_startup(tmp_path, fake_harbor,
                                                                                       monkeypatch):
    """A session of a model a CLI reaches takes text only: the answer agent's attachments (the task's figures) on such
    a model would be refused in every trial, so the run is refused before the first one, whether the runner is given
    the model (model: {cli: ...}) or the pipeline serves it (hermit.isolate.agent_pipeline gives an endpoint's spec
    and names the CLI in model_cli). Without attachments, or on a <backend>/<model> reference, it starts."""
    from types import SimpleNamespace
    from hermit import isolate
    from hermit import sandbox as sandbox_module
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    figures = {"attachments": ["/app/figure*.png"]}
    answer = {"acting_agent": {"cli": "answer"}, "binding": None, "web_search": None}
    reached = {"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt"}
    with pytest.raises(ValueError, match="takes text only"):
        make_agent(tmp_path, fake_harbor, tasks, **answer, model=reached, answer=figures)
    with pytest.raises(ValueError, match="takes text only"):
        make_agent(tmp_path, fake_harbor, tasks, **answer, model=SERVED, model_cli="codex", answer=figures)
    assert make_agent(tmp_path, fake_harbor, tasks, **answer, model=SERVED, model_cli="codex").cli == "answer"
    assert make_agent(tmp_path, fake_harbor, tasks, acting_agent={"cli": "answer"}, web_search=None,
                      answer=figures).cli == "answer"
    # The pipeline serves the model itself (here a stand-in endpoint) and names its CLI to the runner
    monkeypatch.delenv(sandbox_module.ENV_HIDE, raising=False)          # the pipeline sets the run's (empty) hide list
    served = SimpleNamespace(agent=SimpleNamespace(settings={"model": "gpt-5.6-luna"}), url=SERVED["base_url"],
                             closing=[], close=lambda: None)
    monkeypatch.setattr(isolate, "cli_sandbox", lambda spec, **kwargs: (None, None))
    monkeypatch.setattr(isolate, "cli_endpoint", lambda spec, log_dir=None, *, sandbox=None: served)
    monkeypatch.setattr(isolate, "run_secrets", lambda config: [])
    harbor = {"tasks_dir": str(tasks), "jobs_dir": str(tmp_path / "jobs"), "harbor": str(fake_harbor),
              "disk_gate_gb": 0, "tmp_gate_gb": 0, "acting_agent": {"cli": "answer"}, "model": reached}

    def pipeline(given):
        config = SimpleNamespace(base=tmp_path, data={"sandbox": {"enabled": True}, "environment": {},
                                                      "agent": {"name": "harbor", "params": given}})
        run, close = isolate.agent_pipeline(config, "eval", records=None)
        close()
        return run.__self__.params
    params = pipeline(harbor)
    assert params["model_cli"] == "codex" and params["model"]["base_url"] == SERVED["base_url"]
    with pytest.raises(ValueError, match="takes text only"):
        pipeline({**harbor, "answer": figures})


def test_the_answer_agents_model_keeps_the_client_timeout_and_retries_its_spec_gives(tmp_path, fake_harbor):
    """A model a CLI reaches through the pipeline's endpoint keeps that endpoint spec's timeout (one that waits out a
    session and a login's plan) in each trial's relay, which would otherwise cut the request at its client's 600 s
    and send it again, another session each time; so does a bound model with a timeout or max_retries beside it
    (bind_harbor_params keeps them in the binding)."""
    from hermit.harbor.tools import ModelEndpoint
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    answer = {"acting_agent": {"cli": "answer"}, "web_search": None}
    served = make_agent(tmp_path, fake_harbor, tasks, **answer, binding=None, model=SERVED, model_cli="codex")
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


def test_a_mini_swe_agent_eval_that_deploys_no_adapter_starts_without_the_package(tmp_path, fake_harbor, monkeypatch):
    """hermit eval --adapter none deploys no adapter, so nothing on this machine reads the minisweagent package: its
    pipeline says so (hermit eval gives isolate.agent_pipeline deploys_adapters false for no adapter, and true for an
    adapter), and the startup check lets the run start where the deploying interpreter lacks the package; its trials
    run. A pipeline that deploys adapters is refused as before."""
    from types import SimpleNamespace
    from hermit import evaluate as evaluate_mod
    from hermit import isolate
    from hermit import sandbox as sandbox_module
    monkeypatch.delenv(sandbox_module.ENV_HIDE, raising=False)          # the pipeline sets the run's (empty) hide list
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    without = write(tmp_path / "bare" / "python", "#!/bin/sh\nexit 1\n", exe=True)      # finds no such package
    config = SimpleNamespace(base=tmp_path, data={"sandbox": {"enabled": True}, "environment": {}, "agent": {
        "name": "harbor", "params": {"tasks_dir": str(tasks), "jobs_dir": str(tmp_path / "jobs"),
                                     "harbor": str(fake_harbor), "disk_gate_gb": 0, "tmp_gate_gb": 0,
                                     "model": BINDING["model"], "binding": BINDING, "web_search": False,
                                     "acting_agent": {"cli": "mini-swe-agent"}, "python": str(without)}}})
    with pytest.raises(ValueError, match="mini-swe-agent is not installed"):
        isolate.agent_pipeline(config, "eval", records=None)
    run, close = isolate.agent_pipeline(config, "eval", records=None, deploys_adapters=False)
    try:
        out = run(dataset.index(tasks)[0], None)
    finally:
        close()
    assert run.__self__.params["deploys_adapters"] is False and out.output["status"] == "ok"
    given = []

    def pipeline(config, role, **kwargs):                       # hermit eval's pipeline, as _run asks for it
        given.append(kwargs.get("deploys_adapters"))
        raise RuntimeError("asked")
    monkeypatch.setattr(evaluate_mod, "agent_pipeline", pipeline)
    asked = SimpleNamespace(data={"eval": {"repeats": 1}, "agent": {"concurrency": 1}})
    for target in (None, tmp_path / "adapter"):
        with pytest.raises(RuntimeError, match="asked"):
            evaluate_mod._run(asked, target, [], tmp_path / "eval", label="none", log=lambda m: None)
    assert given == [False, True]


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


def test_a_verifier_timeout_is_infra_unless_the_verifier_times_the_solution(tmp_path, fake_harbor):
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    ran_out = errors.classify(trial_result(None, exc="VerifierTimeoutError"))
    assert make_agent(tmp_path, fake_harbor, tasks).verifier_timeout_verdict(ran_out)["status"] == "infra"
    timed = make_agent(tmp_path, fake_harbor, tasks, verifier_timeout="agent").verifier_timeout_verdict(ran_out)
    assert (timed["status"], timed["failure_cause"], timed["timed_out"]) == ("timeout", "solution_timed_out", True)
    other = errors.classify(trial_result(None, exc="RewardFileNotFoundError"))       # only the timeout is the agent's
    assert make_agent(tmp_path, fake_harbor, tasks, verifier_timeout="agent").verifier_timeout_verdict(other) == other


def test_runner_merges_a_benchmark_file_under_params(tmp_path, fake_harbor):
    tasks = tmp_path / "utils" / "tasks"
    make_task(tasks, "alpha")
    write(tmp_path / "utils" / "benchmark.yaml", yaml.safe_dump({
        "tasks_dir": "tasks", "model": "glm-5", "binding": BINDING, "effort": "medium", "docker_cleanup": "rmi_over_gb",
        "web_search": False, "dataset": {"ref": "local@1"},            # prepare's block, not a param
        "acting_agent": {"kwargs": {"version": "0.164.0"}, "env": {"A": "1"}}}))
    agent = HarborAgent({"benchmark": "../utils/benchmark.yaml", "effort": "high", "harbor": str(fake_harbor),
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
    agent = make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt",
                       effort="medium", codex_version="0.164.0", agent_timeout_sec=3600, retry={"max_retries": 1},
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
    assert config["retry"] == {"max_retries": 1} and config["verifier"] == {"override_timeout_sec": 900}
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
    assert (log / "agent" / "agent-codex.jsonl").is_file() and not (log / "agent" / "sessions").exists()
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
    # An API account's usage limit halts the run: the next rollout returns without a trial.
    monkeypatch.setenv("FAKE_HARBOR_MODE", "usage_limit")
    halted = agent.run(row, None)
    assert halted.output["status"] == "infra" and halted.extra["halt"] is True and agent.halt_file().is_file()
    assert halted.extra["unscored"] == "model_provider_failed"
    monkeypatch.setenv("FAKE_HARBOR_MODE", "ok")
    after = agent.run(row, None)
    assert after.output["failure_cause"] == "halted" and after.log is None and after.extra["unscored"] == "halted"
    agent.halt_file().unlink()
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
    assert shared["tokens"] == {"account_id": "acct-1", "refreshed": True}     # the container's later refresh
    job = Path(out.extra["harbor_job"])
    assert not list(job.rglob("auth.json")) and "rt-stand-in" not in job_files(job)
    plan = json.loads(next(job.glob("launch/plan.json")).read_text())
    assert plan["home"]["secret_files"] == ["auth.json"] and plan["return_files"][0]["from"].endswith("/auth.json")
    assert out.extra["session"]["served"] is True


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
    assert not agent.halt_file().exists()


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
    assert not agent.halt_file().exists()


# A Codex session the provider refused for its sign-in (a revoked ChatGPT login, a key it does not take)
SIGN_IN_REFUSED = ('{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
                   '{"type":"error","message":"unexpected status 401 Unauthorized: Your authentication token has '
                   'been invalidated. Please try signing in again."}\n'
                   '{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized"}}\n')


def test_a_codex_session_whose_sign_in_the_provider_refused_halts_the_run(tmp_path, fake_harbor, monkeypatch):
    """A 401 for the session's sign-in is no answer of the agent's, and no wait restores it: infra, and the run
    halts (as Harbor's own Codex agent's AgentAuthenticationError did)."""
    monkeypatch.setenv("FAKE_DEFAULT_STREAM", SIGN_IN_REFUSED)
    monkeypatch.setenv("FAKE_HARBOR_MODE", "agent_exit")                     # the CLI exited 1; the verifier ran
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks)
    out = agent.run(row, None)
    assert (out.output["status"], out.output["failure_cause"], out.extra.get("unscored")) == \
        ("infra", "login_failed", "login_failed")
    assert out.extra["halt"] is True and agent.halt_file().is_file()


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
    """On a login, a session whose stream holds a provider's notice and a lost sign-in is the lost sign-in: returned
    (it halts), never run again as one the provider could not serve; whether the notice is one it recovered from (a
    reconnect, no failure: not `unavailable`) or the failure its turn ended with (`unavailable` as well)."""
    monkeypatch.setenv("FAKE_HARBOR_MODE", "agent_exit")
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    chatgpt_login(tmp_path)
    for n, (events, unavailable) in enumerate(((LOST_AFTER_NOTICE, False), (LOST_THEN_DROPPED, True))):
        monkeypatch.setenv("FAKE_STREAMS", str(write(tmp_path / f"lost-{n}.jsonl", events)))
        jobs = tmp_path / f"jobs-{n}"
        agent = make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt",
                           busy_retries=2, busy_wait_s=0, jobs_dir=str(jobs))
        out = agent.run(row, None)
        assert (out.output["status"], out.output["failure_cause"]) == ("infra", "login_failed"), n
        assert out.extra["session"]["unavailable"] is unavailable and agent.halt_file().is_file(), n
        assert (jobs / ".fake-calls").read_text() == "1", n                  # not run again


# A Codex session that recovered from a dropped stream (its reconnect), worked on, then failed its turn for its own
# reason: the model's context window ran out
RECOVERED_THEN_FAILED = ('{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
                         '{"type":"error","message":"Reconnecting... 1/5 (stream disconnected before completion: error '
                         'sending request for url (https://models.example/v1/responses))"}\n'
                         '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"working"}}\n'
                         '{"type":"error","message":"Codex ran out of room in the model\'s context window."}\n'
                         '{"type":"turn.failed","error":{"message":"Codex ran out of room in the model\'s context '
                         'window."}}\n')


def test_a_codex_trial_that_recovered_from_a_notice_and_then_failed_for_its_own_reason_is_the_agents_result(
        tmp_path, fake_harbor, monkeypatch):
    """A provider's notice the session recovered from (a reconnect after a dropped stream) is no failure: a session
    that went on and then failed its turn for its own reason (the context window) is the agent's result, its work
    scored as Harbor scores it (the CLI exited non-zero; the verifier gave 0.5), never infra run again."""
    monkeypatch.setenv("FAKE_DEFAULT_STREAM", RECOVERED_THEN_FAILED)
    monkeypatch.setenv("FAKE_HARBOR_MODE", "agent_exit")
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    out = make_agent(tmp_path, fake_harbor, tasks).run(row, None)
    assert (out.output["status"], out.output["failure_cause"], out.output["reward"]) == \
        ("agent_error", "unclassified", 0.5)
    assert out.extra["session"]["unavailable"] is False and "unscored" not in out.extra
    assert judge.score(row, out)["score"] == 0.5


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


def test_the_answer_agents_endpoint_keeps_what_a_refusal_says_in_a_long_message_or_at_a_bodys_top_level():
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
        said = Pi({}).outcome(pi_failed(failure), None, None)
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
    from hermit import isolate
    from hermit.agent.base import endpoint as endpoint_mod
    from hermit.harbor import tools
    cancelled, release = [], threading.Event()

    def sessions(spec, log_dir=None, *, sandbox=None):        # each request one stand-in session of the CLI
        served = endpoint_mod.ChatEndpoint(None).start()

        def session(request, authorization, abort=None, note=None):
            if spec["model"] == "stuck":                       # a session that does not hear its cancel
                release.wait(30)
                return endpoint_mod._DROP
            cancelled.append(abort.wait(30))
            time.sleep(0.3)                                    # its process stopped, its files cleaned up
            return endpoint_mod._DROP                          # as cli_answer gives a cancelled session
        served.answer = session
        return served
    monkeypatch.setattr(endpoint_mod, "cli_endpoint", sessions)
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


def test_an_answer_trial_on_a_login_waits_for_its_plan_and_one_whose_request_the_plan_held_is_not_the_agents_timeout(
        tmp_path, fake_harbor, monkeypatch):
    """The answer agent's model reached through a CLI on a login: each request is one session on the login, which
    waits inside the request while the plan holds sessions back (CliAgent.run). hermit.isolate.agent_pipeline tells the
    runner that login (model_login, model_plan_dir), and each trial waits for the plan before it starts, so that its
    request does not wait the plan out in the agent's time. A trial whose request's session waited for the plan all
    the same (a hold that came once the trial had started) until the agent's time ran out is infra (usage_limit, no
    halt), not the agent's timeout: as the session recorded its wait, behind the pipeline's endpoint (the relay
    carries the record to it) and behind the runner's own."""
    from types import SimpleNamespace
    from hermit import isolate
    from hermit import sandbox as sandbox_module
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    plan = tmp_path / "plan"
    plan.mkdir()
    reached = {"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt", "plan_dir": str(plan)}
    # The pipeline serves the model itself (here a stand-in endpoint), and names its CLI and its login to the runner
    monkeypatch.delenv(sandbox_module.ENV_HIDE, raising=False)          # the pipeline sets the run's (empty) hide list
    served = SimpleNamespace(agent=SimpleNamespace(settings={"model": "gpt-5.6-luna", "login": "chatgpt"}),
                             url=SERVED["base_url"], closing=[], close=lambda: None)
    monkeypatch.setattr(isolate, "cli_sandbox", lambda spec, **kwargs: (None, None))
    monkeypatch.setattr(isolate, "cli_endpoint", lambda spec, log_dir=None, *, sandbox=None: served)
    monkeypatch.setattr(isolate, "run_secrets", lambda config: [])
    config = SimpleNamespace(base=tmp_path, data={"sandbox": {"enabled": True}, "environment": {}, "agent": {
        "name": "harbor", "params": {"tasks_dir": str(tasks), "jobs_dir": str(tmp_path / "jobs"),
                                     "harbor": str(fake_harbor), "disk_gate_gb": 0, "tmp_gate_gb": 0,
                                     "acting_agent": {"cli": "answer"}, "model": reached}}})
    run, close = isolate.agent_pipeline(config, "eval", records=None)
    close()
    params = run.__self__.params
    assert (params["model_cli"], params["model_login"], params["model_plan_dir"]) == ("codex", "chatgpt", str(plan))
    assert run.__self__.model_plan() == (plan, False) and "model_usage_credits" not in params
    # The plan allows each trial to start, and then holds sessions back: the request's session waits for it until the
    # agent's time runs out, behind the pipeline's endpoint, and behind the runner's own (model: {cli, login})
    for own in (False, True):
        out, agent, waits = answer_trial_on_a_login(tmp_path / f"own-{own}", fake_harbor, monkeypatch, held=True,
                                                    own=own)
        facts = out.extra["session"]
        assert (facts["calls"], facts["statuses"], facts["plan_held"].startswith("paused")) == (1, [None], True)
        assert facts["plan_waiting"] is True                       # still waiting when the endpoint gave it up
        assert (out.output["status"], out.output["failure_cause"], out.extra["halt"]) == \
            ("infra", "usage_limit", False), own
        assert out.extra["unscored"] == "usage_limit" and not agent.halt_file().exists()
        assert waits == [(agent.model_plan()[0], False)]          # the trial waited for the plan before it started


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
    then started, and was working when the time ran out), or only once Harbor's run had ended (it still waited)."""
    from hermit.agent.base import login
    for released in (True, False):
        plan, started = tmp_path / f"released-{released}" / "plan", []
        out, _, _ = answer_trial_on_a_login(plan.parent, fake_harbor, monkeypatch, held=True, released=released,
                                            on_start=lambda: started.append(True),
                                            after_harbor=lambda: (plan / "PAUSE").unlink(missing_ok=True))
        facts = out.extra["session"]
        assert facts["plan_held"].startswith("paused"), released
        assert (facts["plan_waiting"], started) == ((False, [True]) if released else (True, [])), released
        assert (out.output["status"], out.output["failure_cause"], out.extra["halt"]) == \
            ("infra", "usage_limit", False), released
        assert out.extra["unscored"] == "usage_limit" and login.held(plan) == ""    # no hold as the trial is read


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
                            on_start=lambda: None, after_harbor=lambda: None, own: bool = False):
    """One answer trial whose model a CLI reaches on the ChatGPT login (its plan <root>/plan), that runs out of the
    agent's time (1 s) with its one request unanswered: (its output, the runner, and the trial's waits for the plan
    before it started). The plan allows the trial to start, then holds sessions back when `held`; with `released`
    the hold ends as the request's session waits on it (the session then starts). The request is one session of
    planned_sessions, behind the pipeline's endpoint (the runner told its login) or, `own`, behind the runner's own
    (model: {cli, login}); `after_harbor` runs once Harbor's run ended, before the session is read."""
    from hermit import isolate
    from hermit.agent.base import endpoint as endpoint_mod
    from hermit.agent.base import login
    tasks, plan = root / "tasks", root / "plan"
    make_task(tasks, "alpha", image="example/alpha:1")
    plan.mkdir(parents=True)
    real_wait, waits = getattr(login.wait, "__wrapped__", login.wait), []    # never an earlier call's stand-in

    class Ended:                 # the session's cancel, as the session waits on the plan with it: the hold ends then
        def __init__(self, cancel):
            self.cancel = cancel

        def wait(self, timeout=None):
            (plan / "PAUSE").unlink(missing_ok=True)
            return self.cancel.is_set()

    def wait(where, cancel=None, log=None, max_s=None, credits=False, **kwargs):
        if cancel is not None:                           # the request's session's (its call's cancel): the plan's own
            return real_wait(where, Ended(cancel) if released else cancel, log, max_s, credits, **kwargs)
        waits.append((Path(where), credits))             # the trial's, before it starts: the plan allows it
        if held:
            write(Path(where) / "PAUSE", "held\n")
        return ""
    wait.__wrapped__ = real_wait
    monkeypatch.setattr(login, "wait", wait)
    monkeypatch.setenv("FAKE_HARBOR_MODE", "timeout")
    monkeypatch.setenv("FAKE_ANSWER_WAIT_S", "1")       # the agent's time, as the fake's request waits it out
    sessions = planned_sessions(plan, on_start)
    try:
        if own:
            monkeypatch.setattr(endpoint_mod, "cli_endpoint", lambda spec, log_dir=None, *, sandbox=None: sessions)
            monkeypatch.setattr(isolate, "cli_sandbox", lambda spec, **kwargs: (None, None))
            model = {"model": {"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt", "plan_dir": str(plan)}}
        else:
            model = {"model": {**SERVED, "base_url": sessions.url}, "model_cli": "codex", "model_login": "chatgpt",
                     "model_plan_dir": str(plan)}
        agent = make_agent(root, fake_harbor, tasks, acting_agent={"cli": "answer"}, binding=None, web_search=None,
                           **model)
        ran = agent.harbor_run
        agent.harbor_run = lambda *a, **k: (ran(*a, **k), after_harbor())[0]
        return agent.run(dataset.index(tasks)[0], None), agent, waits
    finally:
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
    step's verifier one whose tools never arrived (no_tools) or one that ran out of its time (timeout)."""
    import argparse, json, os, socket, sys, threading
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
                (step_dir / "verifier" / "test-stdout.txt").write_text(
                    "failed to download https://github.com/astral-sh/uv/releases/download/0.9.5/uv.tar.gz\\n"
                    "/tests/test.sh: line 18: uvx: command not found\\n" if verifier_mode == "no_tools" else "PASS\\n")
                plan_path = (req.get("agent_kwargs") or {}).get("plan")
                (step_dir / "agent" / "plan-seen.txt").write_text(str(plan_path))
                plan = json.loads(Path(plan_path).read_text())
                (step_dir / "agent" / plan["stream"]).write_text(
                    '{"type":"thread.started","thread_id":"th-%s"}\\n'
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":2}}\\n'
                    % req["name"])
                aborted = req["name"] == "s2"
                reward = 0.0 if verifier_mode == "no_tools" else 1.0
                result = {"step_name": req["name"], "agent_result": {"n_input_tokens": 10, "n_output_tokens": 2, "metadata": {}},
                          "verifier_result": None if aborted else {"rewards": {"reward": reward}},
                          "exception_info": {"exception_type": "RuntimeError", "exception_message": "boom"} if aborted else None,
                          "agent_execution": {"started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:05Z"}}
                if verifier_mode == "timeout" and not aborted:
                    result.update(verifier_result=None, exception_info={"exception_type": "VerifierTimeoutError",
                                                                        "exception_message": "verifier timed out"})
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
    agent = make_agent(tmp_path, fake_harbor, tasks, stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
    first = agent.run(rows["flow:s1"], simple_template())
    assert first.output["status"] == "ok" and first.output["reward"] == 1.0 and first.output["step"] == "s1"
    assert first.output["trial"] == "flow__s1" and first.tokens == 12 and first.wall_s == 5.0
    stream_dir = tmp_path / "jobs" / "streams" / "flow__s1"
    seen = json.loads((stream_dir / "seen-agent.json").read_text())
    step_plan = stream_dir / "steps" / "001-s1" / "launch" / "plan.json"
    assert seen["kwargs"]["plan"] == str(step_plan) and "model_name" not in seen
    work = json.loads(step_plan.read_text())["deploy"]["work_dir"]
    assert work == str((stream_dir / "steps" / "001-s1" / "deploy").resolve())
    assert (Path(first.log) / "verifier" / "test-stdout.txt").read_text() == "PASS\n"
    assert (Path(first.log) / "agent" / "plan-seen.txt").read_text() == str(step_plan)
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
    assert blocked.output["failure_cause"] == "stream_lost" and \
        "inside the task sandbox" in blocked.extra["exception_message"]


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
    """A step's result is judged as a trial's: a verifier whose tools never arrived is infra (run again), and with
    `verifier_timeout: agent` a verifier that ran out of its time on the agent's solution is the agent's result."""
    fake_driver(fake_harbor)
    tasks = tmp_path / "tasks"
    make_task(tasks, "unreached", steps=("s1", "s2"))
    make_task(tasks, "slow", steps=("s1", "s2"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, verifier_timeout="agent",
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
    seen = json.loads((tmp_path / "jobs" / "streams" / "flow__s1" / "seen-driver.json").read_text())
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
    infra too, its tools kept (it may still be running the step before on them)."""
    fake_driver(fake_harbor)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("s1", "s2", "s3"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, tools_host="127.0.0.1",
                       stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
    adapter = tool_adapter(tmp_path / "adapter")
    try:
        assert agent.run(rows["flow:s1"], adapter).output["status"] == "ok"

        def silent(self, index, *, timeout_s):
            raise TimeoutError("timed out")
        monkeypatch.setattr(stream.StreamClient, "wait_turn", silent)
        slow = agent.run(rows["flow:s2"], adapter)
        kept = "flow__s1" in agent._streams and not fake_tools[0].closed

        def gone(self, index, *, timeout_s):
            raise ConnectionRefusedError(111, "Connection refused")
        monkeypatch.setattr(stream.StreamClient, "wait_turn", gone)
        lost = agent.run(rows["flow:s3"], adapter)
    finally:
        stop_streams(tmp_path)
    assert (slow.output["status"], slow.output["failure_cause"]) == ("infra", "stream_lost") and kept
    assert (lost.output["status"], lost.output["failure_cause"], lost.extra["unscored"]) == \
        ("infra", "stream_lost", "stream_lost")
    [served] = fake_tools
    assert served.closed and "flow__s1" not in agent._streams


def test_a_driver_that_died_while_it_answered_closes_the_streams_tools(tmp_path, fake_harbor, monkeypatch, fake_tools):
    """A driver killed while it answered closed the connection without an answer (a StreamError) and is gone: no step
    after it reaches it, so the stream's tool server is closed, as for a driver whose socket refuses. A driver still
    running that reports an error keeps them."""
    import signal
    import time
    fake_driver(fake_harbor)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("s1", "s2", "s3"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, tools_host="127.0.0.1",
                       stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
    adapter = tool_adapter(tmp_path / "adapter")
    try:
        assert agent.run(rows["flow:s1"], adapter).output["status"] == "ok"

        def failed(self, index, *, timeout_s):
            raise stream.StreamError("the stream's environment failed: a step's container exited")
        monkeypatch.setattr(stream.StreamClient, "wait_turn", failed)
        running = agent.run(rows["flow:s2"], adapter)
        kept = "flow__s1" in agent._streams and not fake_tools[0].closed

        def dies(self, index, *, timeout_s):                # killed while it answered (its zombie is not reaped)
            pid = int(self.info()["pid"])
            os.kill(pid, signal.SIGKILL)
            state, deadline = Path(f"/proc/{pid}/stat"), time.time() + 10
            while time.time() < deadline and state.exists() and " Z " not in state.read_text():
                time.sleep(0.05)
            raise stream.StreamError("the stream driver closed the connection without an answer")
        monkeypatch.setattr(stream.StreamClient, "wait_turn", dies)
        lost = agent.run(rows["flow:s3"], adapter)
    finally:
        stop_streams(tmp_path)
    assert (running.output["status"], running.output["failure_cause"]) == ("infra", "stream_lost") and kept
    assert (lost.output["status"], lost.output["failure_cause"]) == ("infra", "stream_lost")
    [served] = fake_tools
    assert served.closed and "flow__s1" not in agent._streams


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
    """A later step's rollout may start the stream's tool server before the step before it has run: each step, once
    its turn comes, serves its own adapter behind the stream's URL and logs its calls in its own log."""
    import time
    fake_driver(fake_harbor)
    monkeypatch.setattr(stream, "POLL_S", 0.1)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("s1", "s2", "s3"))
    rows = {r["id"]: r for r in dataset.index(tasks)}
    agent = make_agent(tmp_path, fake_harbor, tasks, tools_host="127.0.0.1",
                       stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 120})
    one, two = tool_adapter(tmp_path / "one"), tool_adapter(tmp_path / "two", factor=3)
    later: dict = {}
    second = threading.Thread(target=lambda: later.update(out=agent.run(rows["flow:s2"], two)))
    second.start()
    try:
        deadline = time.time() + 120
        while not fake_tools and time.time() < deadline:            # step 2's rollout started the stream's server
            time.sleep(0.05)
        first = agent.run(rows["flow:s1"], one)
        second.join(180)
    finally:
        stop_streams(tmp_path)
    steps = tmp_path / "jobs" / "streams" / "flow__s1" / "steps"
    [served] = fake_tools
    assert served.swaps == [(one, steps / "001-s1" / "tool-calls.jsonl"), (two, steps / "002-s2" / "tool-calls.jsonl")]
    assert first.output["status"] == "ok" and later["out"].output["step"] == "s2"


def test_each_repeat_of_a_multi_step_task_runs_a_stream_of_its_own(tmp_path, fake_harbor):
    """`hermit eval` with eval.repeats > 1 names each repeat in its rows: a multi-step task's repeats are streams of
    their own, so a later repeat neither meets the earlier one's driver past its step nor overwrites its step logs."""
    fake_driver(fake_harbor)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("s1", "s2"))
    row = {r["id"]: r for r in dataset.index(tasks)}["flow:s1"]
    agent = make_agent(tmp_path, fake_harbor, tasks, stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6})
    try:
        first, second = [agent.run({**row, "repeat": r}, None) for r in (1, 2)]
        plain = agent.run(row, None)                                        # a row without a repeat: as before
    finally:
        stop_streams(tmp_path)
    assert [o.output["status"] for o in (first, second, plain)] == ["ok", "ok", "ok"]
    assert [o.output["trial"] for o in (first, second, plain)] == ["flow__s1__r1", "flow__s1__r2", "flow__s1"]
    assert first.log != second.log and (Path(first.log) / "verifier" / "test-stdout.txt").is_file()


def test_each_runs_streams_are_its_own_and_a_resumed_run_meets_its_own_again(tmp_path, fake_harbor):
    """A multi-step task's stream is named for the run too (`run_identity`, which isolate.agent_pipeline gives): two
    runs of one task, at once or one after the other (a run started over: another record directory at the same path),
    never share its driver, its environment or its step logs; a resumed run (the same identity) goes on with its own;
    a runner given none (not started by isolate.agent_pipeline) names it as before. A trial's cache key is the same in
    every run."""
    fake_driver(fake_harbor)
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two"))          # not "s2": the fake driver aborts the stream there
    make_task(tasks, "alpha")
    rows = {r["id"]: r for r in dataset.index(tasks)}
    records = (tmp_path / "runs" / "a" / "sandbox").resolve()
    one, other = f"{records}:101", f"{records}:202"         # a run's record directory, and the one made there anew
    agents = [make_agent(tmp_path, fake_harbor, tasks, stream={"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6},
                         **({"run_identity": run} if run else {})) for run in (one, other, one, None)]
    try:
        first, apart = (agent.run(rows["flow:one"], None) for agent in agents[:2])
        resumed = agents[2].run(rows["flow:two"], None)
        plain = agents[3].run(rows["flow:one"], None)
    finally:
        stop_streams(tmp_path)
    digest = {r: hashlib.sha256(r.encode()).hexdigest()[:8] for r in (one, other)}
    assert [o.output["status"] for o in (first, apart, resumed, plain)] == ["ok", "ok", "ok", "ok"]
    assert [o.output["trial"] for o in (first, apart, resumed, plain)] == \
        [f"flow__s1__{digest[one]}", f"flow__s1__{digest[other]}", f"flow__s1__{digest[one]}", "flow__s1"]
    assert first.log != apart.log and first.extra["stream_dir"] == resumed.extra["stream_dir"]
    assert len({agent.cache_key(rows["alpha"], None) for agent in agents}) == 1


def test_a_resumed_train_meets_its_streams_again_and_a_train_started_over_or_any_other_pipeline_gets_new_ones(
        tmp_path, fake_harbor, monkeypatch):
    """isolate.agent_pipeline gives a Harbor task set's agent what its multi-step tasks' streams are named for
    (`run_identity`), never writing it to the config: the Trainer's pipeline (role rollout) its record directory, by
    its resolved path and its inode (made if absent), so that a resumed train (the same directory) meets its own
    streams and one started over (trainer.resume_mode: disable renames the run's directory and makes another at the
    same path) gets new ones; any other pipeline, which never resumes (hermit eval's, on any record directory; hermit
    check's probe, with none), one of its own. Another agent on the host pipeline gets its params as they are."""
    from types import SimpleNamespace
    from hermit import sandbox as sandbox_module
    from hermit.isolate import agent_pipeline
    monkeypatch.delenv(sandbox_module.ENV_HIDE, raising=False)          # the pipeline sets the run's (empty) hide list
    monkeypatch.syspath_prepend(str(tmp_path.resolve()))                # where the function agent's file is loaded from
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha")
    write(tmp_path / "pipe.py", "def run(task, adapter, params):\n    return None\n")
    harbor = {"tasks_dir": str(tasks), "jobs_dir": str(tmp_path / "jobs"), "harbor": str(fake_harbor),
              "disk_gate_gb": 0, "tmp_gate_gb": 0, "model": BINDING["model"], "binding": BINDING, "web_search": False}

    def params(role, records, name="harbor", given=harbor, sandbox=True):
        config = SimpleNamespace(base=tmp_path, data={"sandbox": {"enabled": sandbox}, "environment": {},
                                                      "agent": {"name": name, "params": dict(given)}})
        pipeline, close = agent_pipeline(config, role, records=records)
        close()
        assert config.data["agent"]["params"] == given                 # never written to the config
        return pipeline.__self__.params
    run_dir = tmp_path / "runs" / "r"
    records, where = run_dir / "eval" / ".." / "sandbox", (run_dir / "sandbox").resolve()
    first = params("rollout", records)["run_identity"]
    assert first == f"{where}:{where.stat().st_ino}"                    # the directory, made
    assert params("rollout", records)["run_identity"] == first          # a resumed train: the same directory
    run_dir.rename(run_dir.with_name("r.replaced-20261111-120000"))     # started over: another one at the same path
    run_dir.mkdir()
    over = params("rollout", records)["run_identity"]
    assert over == f"{where}:{where.stat().st_ino}" and over != first
    evals = [params("eval", records)["run_identity"] for _ in range(2)]
    probes = [params("check", None)["run_identity"] for _ in range(2)]
    assert len({first, over, *evals, *probes}) == 6 and all(evals + probes)
    assert params("rollout", records, "pipe.py:run", {"x": 1}, sandbox=False) == {"x": 1}


def test_a_pipeline_that_never_resumes_stops_the_drivers_of_its_unfinished_streams_when_it_closes(
        tmp_path, fake_harbor, monkeypatch):
    """A pipeline that never resumes never meets its streams again: hermit check's probe (one row, which may be a
    multi-step task's first step) and hermit eval's or a checkpoint test's pipeline stop, when closed, the driver of
    each stream they stepped that did not finish, which would otherwise hold its task's environment for
    stream.idle_s; so does the runner at exit for a pipeline whose close never ran or was cut short. A train's pipeline
    keeps its streams for a resume. A stream that finished was stopped by its last step, and is not stopped again; nor
    is a driver that ended by itself (its idle time) asked to stop."""
    import time
    import weakref
    from types import SimpleNamespace
    from hermit import sandbox as sandbox_module
    from hermit.harbor import runner
    from hermit.isolate import agent_pipeline
    fake_driver(fake_harbor)
    monkeypatch.delenv(sandbox_module.ENV_HIDE, raising=False)          # the pipeline sets the run's (empty) hide list
    tasks = tmp_path / "tasks"
    make_task(tasks, "flow", steps=("one", "two"))          # not "s2": the fake driver aborts the stream there
    rows = {r["id"]: r for r in dataset.index(tasks)}
    harbor = {"tasks_dir": str(tasks), "jobs_dir": str(tmp_path / "jobs"), "harbor": str(fake_harbor),
              "disk_gate_gb": 0, "tmp_gate_gb": 0, "model": BINDING["model"], "binding": BINDING, "web_search": False,
              "stream": {"idle_s": 60, "start_timeout_s": 30, "turn_wait_s": 6}}
    stopped = []                                            # each stop asked of a driver, by its stream's name
    real_stop = stream.StreamClient.stop

    def counted(self, **kwargs):
        stopped.append(self.dir.name)
        return real_stop(self, **kwargs)
    monkeypatch.setattr(stream.StreamClient, "stop", counted)

    def pipeline(role, records):
        config = SimpleNamespace(base=tmp_path, data={"sandbox": {"enabled": True}, "environment": {},
                                                      "agent": {"name": "harbor", "params": dict(harbor)}})
        return agent_pipeline(config, role, records=records)

    def driver(out, *, stopping=False):
        """The live driver of the stream a step ran in, or None; one being stopped is waited for (the driver removes
        its socket as it ends)."""
        client = stream.StreamClient(Path(out.extra["stream_dir"]), python="unused")
        deadline = time.time() + 10
        while stopping and client.running() is not None and time.time() < deadline:
            time.sleep(0.1)
        return client.running()
    try:
        probe, close = pipeline("check", None)
        first = probe(rows["flow:one"], None)
        assert first.output["status"] == "ok" and driver(first) is not None and stopped == []
        close()
        assert stopped == [first.output["trial"]] and driver(first, stopping=True) is None
        evaluate, close = pipeline("eval", tmp_path / "eval" / "sandbox")
        done = [evaluate(rows[f"flow:{step}"], None) for step in ("one", "two")]       # the stream finished
        assert [o.output["status"] for o in done] == ["ok", "ok"] and stopped[1:] == [done[1].output["trial"]]
        assert done[1].output["trial"] not in evaluate.__self__._drivers               # no longer its to stop
        left = evaluate({**rows["flow:one"], "repeat": 2}, None)                         # this one did not
        close()
        assert stopped[1:] == [done[1].output["trial"], left.output["trial"]]
        assert driver(left, stopping=True) is None
        interrupted, _ = pipeline("eval", tmp_path / "again" / "sandbox")               # never closed
        cut = interrupted(rows["flow:one"], None)
        monkeypatch.setattr(runner, "_LIVE", weakref.WeakSet([interrupted.__self__]))
        runner._close_all()                                                             # at exit
        assert stopped[3:] == [cut.output["trial"]] and driver(cut, stopping=True) is None
        ended, close = pipeline("check", None)
        idle = ended(rows["flow:one"], None)
        real_stop(stream.StreamClient(Path(idle.extra["stream_dir"]), python="unused"), timeout_s=10)   # by itself
        assert driver(idle, stopping=True) is None                                      # (as at its idle time)
        close()
        assert stopped[4:] == []
        train, close = pipeline("rollout", tmp_path / "runs" / "r" / "sandbox")
        kept = train(rows["flow:one"], None)
        close()
        assert stopped[4:] == [] and driver(kept) is not None                           # for a resume
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
    assert (out.output["status"], out.output["trial"]) == ("ok", "flow__s1")


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


def test_a_refusal_of_the_key_model_or_bill_in_a_providers_own_wording_is_model_rejected_but_a_rate_limit_is_not(
        tmp_path, fake_harbor):
    """A provider's refusal of the run's configuration in its own wording (DeepSeek's model that does not exist,
    Zhipu GLM's unknown model, Mistral's invalid model, OpenRouter's model that is not a valid model ID, Gemini's key
    not valid, the account's bill: a 402, a credit balance too low, an insufficient quota, billing; an OpenCode error
    whose words name none of it, by its status; the answer agent's relay's 402) is model_rejected. A rate limit is the
    provider's, `unavailable` before any rejection is read, and its advice (billing details, a link to the billing
    page) is no such wording; nor is a prompt too long, a content filter or an overload."""
    from hermit.agent.base.cli import Claude, OpenCode, Pi
    from hermit.harbor.runner import CONFIG_REFUSED
    exited = errors.classify(trial_result(0.5, exc="NonZeroAgentExitCodeError"))

    def verdict(raw, cli=Pi):
        said = cli({}).outcome(raw, None, None)
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
                '"status":"INVALID_ARGUMENT"}}',
                '402 {"error":{"message":"Insufficient Balance","type":"unknown_error"}}',
                '400 {"type":"error","error":{"type":"invalid_request_error","message":"Your credit balance is too low '
                'to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."}}',
                '400 {"error":{"message":"You exceeded your current quota.","type":"insufficient_quota"}}',
                '400 {"error":{"code":400,"message":"Gemini API free tier is not available in your country. Please '
                'enable billing on your project in Google AI Studio.","status":"FAILED_PRECONDITION"}}']
    for text in refusals:
        assert verdict(pi_failed(text))["failure_cause"] == "model_rejected", text
    assert verdict((STREAMS / "credit_balance.jsonl").read_text(), Claude)["failure_cause"] == "model_rejected"
    bland = '{"type":"error","error":{"type":"provider.unknown","message":"the model is down","status":401}}\n'
    assert verdict(bland, OpenCode)["failure_cause"] == "model_rejected"
    agent, row, job, trial, plan = opencode_trial(tmp_path, fake_harbor)       # as a trial's stream is read
    write(trial / "agent" / "agent-opencode.jsonl", bland)
    assert HarborAgent.session_verdict(exited, agent.session(trial, plan, None, job, row)[0])["failure_cause"] == \
        "model_rejected"
    from hermit.harbor.tools import ModelEndpoint
    relay = ModelEndpoint(BINDING)                                     # the answer agent's relay: the bill's own status
    relay._note((402, b'{"error": {"message": "Insufficient Balance"}}', {}))
    assert HarborAgent.session_verdict(exited, relay.facts())["failure_cause"] == "model_rejected"
    rate_limits = ['429 {"error":{"message":"Rate limit reached for gpt-5 in organization org-x on requests per min '
                   '(RPM): Limit 3, Used 3, Requested 1. Please try again in 20s. Visit '
                   'https://platform.openai.com/account/billing to add a payment method.",'
                   '"code":"rate_limit_exceeded"}}',
                   '429 {"error":{"code":429,"message":"You exceeded your current quota, please check your plan and '
                   'billing details.","status":"RESOURCE_EXHAUSTED"}}']
    for text in rate_limits:
        assert verdict(pi_failed(text))["failure_cause"] == "model_provider_failed", text
        assert not CONFIG_REFUSED.search(text), text                  # nor in a rejection's earlier notices
    others = [Claude({}).outcome((STREAMS / f"{case}.jsonl").read_text(), None, None)["failure"]
              for case in ("too_long", "model_refusal", "throttled", "overloaded", "server_error")]
    others += [OpenCode({}).outcome((OPENCODE / f"{case}.jsonl").read_text(), None, None)["failure"]
               for case in ("failure_429", "failure_500")]
    others.append("400 The response was filtered due to the prompt triggering Azure OpenAI's content management "
                  "policy. Please modify your prompt and retry.")
    others.append('400 {"error":{"message":"Invalid prompt: your prompt was flagged as potentially violating our usage '
                  'policy."}}')
    assert [text for text in others if CONFIG_REFUSED.search(text)] == []


def test_a_rejections_status_is_read_only_where_one_is_written_never_from_a_number_in_its_words():
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
        said = Pi({}).outcome(pi_failed(text), None, None)
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


def test_a_request_the_models_session_or_its_provider_cannot_take_refused_the_configuration():
    """What Hermit's endpoint refuses a session of a model a CLI reaches cannot take (a figure, tool calls, another
    model, more than one reply; settings the session cannot start with: a 400 with Hermit's own code,
    endpoint.UNSUPPORTED), and a provider's refusal of a request setting its model does not take (OpenAI's unsupported
    parameter or value, Azure's unrecognized request argument, OpenCode's unsupported operation) refused the run's
    configuration, which no input changes: model_rejected (unscored, no halt), never the agent's 0 in every trial. A 400
    without that code or such words stays the agent's."""
    from hermit.agent.base.cli import OpenCode
    from hermit.agent.base.endpoint import cli_answer
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
        session._note(cli_answer(codex, request))                       # refused before any session
        verdict = HarborAgent.session_verdict(exited, session.facts())
        assert (session.facts()["statuses"], verdict["failure_cause"], verdict["halt"]) == \
            ([400], "model_rejected", False), request
    from hermit.agent.base.endpoint import ChatEndpoint
    with ChatEndpoint(make("codex", {"model": "gpt-5.6-luna"})) as unset:     # web_search unset: it never starts
        session = ModelEndpoint({"cli": "codex", "model": "gpt-5.6-luna"})
        session._note(unset.answer(text, None))                         # the session's settings refused, by its code
    assert HarborAgent.session_verdict(exited, session.facts())["failure_cause"] == "model_rejected"
    for said in ("Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' "
                 "instead.", "Unsupported value: 'reasoning_effort' does not support 'minimal' with this model.",
                 "Unrecognized request argument supplied: reasoning_effort"):
        relay = ModelEndpoint(BINDING)
        relay._note((400, json.dumps({"error": {"message": said, "type": "invalid_request_error", "param": None,
                                                "code": None}}).encode(), {}))
        assert HarborAgent.session_verdict(exited, relay.facts())["failure_cause"] == "model_rejected", said
    unsupported = OpenCode({}).outcome('{"type":"error","error":{"type":"provider.unsupported-operation","message":'
                                       '"the model takes no images","status":400}}\n', None, None)
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
    out = claude_agent(tmp_path, fake_harbor, tasks).run(row, None)
    assert out.output["status"] == "ok" and out.extra["session"]["served"] is True
    assert holds == [(1793856468, "session_refused")] and waits == [None, None]   # no limit on the wait
    assert (tmp_path / "jobs" / ".fake-calls").read_text() == "2" and not (tmp_path / "jobs" / "HALT").exists()
    noted = (login.CLAUDE_PLAN / "plan.jsonl").read_text().splitlines()          # both sessions' reports are kept
    assert len(noted) == 2 and json.loads(noted[0])["used_percent"] == 100.0
    # a lost sign-in halts instead
    monkeypatch.setenv("FAKE_STREAMS", str(STREAMS / "not_logged_in.jsonl"))
    lost = claude_agent(tmp_path, fake_harbor, tasks).run(row, None)
    assert lost.output["status"] == "infra" and lost.output["failure_cause"] == "login_failed"
    assert (tmp_path / "jobs" / "HALT").exists() and lost.extra["unscored"] == "login_failed"


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


# A Codex session whose turn failed on a dropped stream: a provider that could not serve it
UNSERVED = ('{"type":"thread.started","thread_id":"th-1"}\n{"type":"turn.started"}\n'
            '{"type":"turn.failed","error":{"message":"stream disconnected before completion: error sending '
            'request"}}\n')


def test_a_login_trial_starts_no_container_once_another_trial_halted_the_run_while_it_waited(tmp_path, fake_harbor,
                                                                                              monkeypatch):
    """A login trial may wait long for the plan, or sleep before it runs again: a halt another trial wrote meanwhile
    (a lost sign-in) ends it before it starts a container, as it ends every rollout after it; what its runs before
    used stays its own."""
    from hermit.agent.base import login
    monkeypatch.setenv("FAKE_STREAMS", str(write(tmp_path / "unserved.jsonl", UNSERVED)))   # each run's, counted
    monkeypatch.setenv("FAKE_HARBOR_MODE", "agent_exit")
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    chatgpt_login(tmp_path)
    for halted_at in (1, 2):                    # the wait before the first run, the one after the pause before another
        jobs, waits = tmp_path / f"jobs-{halted_at}", []

        def wait(plan, cancel, log, max_s, credits, jobs=jobs, waits=waits, halted_at=halted_at):
            waits.append(plan)
            if len(waits) == halted_at:
                write(jobs / "HALT", "AgentAuthenticationError: the sign-in was lost\n")
            return ""
        monkeypatch.setattr(login, "wait", wait)
        out = make_agent(tmp_path, fake_harbor, tasks, binding=None, model="gpt-5.6-luna", login="chatgpt",
                         busy_retries=3, busy_wait_s=0, jobs_dir=str(jobs)).run(row, None)
        assert (out.output["status"], out.output["failure_cause"], out.extra["halt"]) == ("infra", "halted", True)
        ran = (jobs / ".fake-calls").read_text() if (jobs / ".fake-calls").exists() else "0"
        assert (ran, len(waits)) == (str(halted_at - 1), halted_at), halted_at
        assert len(out.extra.get("attempts", [])) == halted_at - 1, halted_at      # the run before, its usage


def test_a_login_trials_busy_settings_given_as_null_take_their_defaults(tmp_path, fake_harbor, monkeypatch):
    """busy_retries and busy_wait_s left empty (null) are their defaults (the session runs again, 120 s later), not an
    error in every rollout."""
    from types import SimpleNamespace
    from hermit.harbor import runner as runner_mod
    slept = []
    monkeypatch.setattr(runner_mod, "time", SimpleNamespace(time=time.time, sleep=slept.append))
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


def test_a_finished_trial_is_kept_and_a_resumed_run_takes_it(tmp_path, fake_harbor, monkeypatch):
    from hermit.harbor.runner import adapter_digest
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks, trial_cache=True)
    first = agent.run(row, None)
    calls = len(list((tmp_path / "jobs").glob("alpha*")))
    again = make_agent(tmp_path, fake_harbor, tasks, trial_cache=True).run(row, None)
    assert len(list((tmp_path / "jobs").glob("alpha*"))) == calls      # no second trial
    assert again.output == first.output and again.extra["from_trial_cache"] and again.log == first.log
    other = make_agent(tmp_path, fake_harbor, tasks, trial_cache=True,
                       binding={**BINDING, "ref": "fake/glm-6", "model": "glm-6"}).run(row, None)
    assert "from_trial_cache" not in other.extra                       # another model: another trial
    for change in ({"pass_reward": 0.5}, {"verifier_redact": ["secret"]}, {"acting_agent": {"cli": "codex",
                                                                           "settings": {"args": ["-c", "x=1"]}}}):
        changed = make_agent(tmp_path, fake_harbor, tasks, trial_cache=True, **change).run(row, None)
        assert "from_trial_cache" not in changed.extra, change         # another launch or judge: another trial
    monkeypatch.setenv("FAKE_HARBOR_MODE", "usage_limit")
    row2 = {**row, "id": "alpha#2"}
    make_agent(tmp_path, fake_harbor, tasks, trial_cache=True).run(row2, None)
    kept = (tmp_path / "jobs" / "trial-cache").glob("*.json")
    assert not any(json.loads(p.read_text())["row"] == "alpha#2" for p in kept)
    a = write(tmp_path / "ad" / "ADAPTER.md", "one\n").parent
    before = adapter_digest(a)
    write(a / "__pycache__" / "x.pyc", "cache")
    assert adapter_digest(a) == before and adapter_digest(None) == "none"
    write(a / "ADAPTER.md", "two\n")
    assert adapter_digest(a) != before


def test_a_trial_taken_from_the_cache_spends_nothing_again(tmp_path, fake_harbor):
    """A resumed run's trial from the cache is no new spending: the run's budget adds 0 for it, and what it spent when
    it ran is kept beside (cached_cost_usd, cached_tokens)."""
    from hermit.trainer.budget import Budget
    from hermit.trainer.rollout import run_tasks
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    first = make_agent(tmp_path, fake_harbor, tasks, trial_cache=True).run(row, None)
    assert first.cost_usd > 0 and first.tokens == 120
    budget = Budget()
    [record] = run_tasks([row], None, make_agent(tmp_path, fake_harbor, tasks, trial_cache=True).run, judge.score,
                         concurrency=1, budget=budget)
    extra = record["pipeline_extra"]
    assert extra["from_trial_cache"] and (budget.usd["rollout"], budget.tokens["rollout"]) == (0.0, 0)
    assert (extra["cached_cost_usd"], extra["cached_tokens"]) == (first.cost_usd, first.tokens)


def test_the_trial_cache_keys_the_whole_row_and_the_tasks_contents(tmp_path, fake_harbor):
    """Two repeats of a row are two trials (`hermit eval` numbers each repeat in its row), and a task edited in place
    is another trial; the same row of the same task is the same trial, in a resumed run too."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    agent = make_agent(tmp_path, fake_harbor, tasks, trial_cache=True)
    key = agent.cache_key(row, None)
    assert agent.cache_key({**row, "repeat": 1}, None) != agent.cache_key({**row, "repeat": 2}, None)
    assert make_agent(tmp_path, fake_harbor, tasks, trial_cache=True).cache_key(dict(row), None) == key
    write(tasks / "alpha" / "tests" / "test.sh", "#!/bin/bash\necho 0 > /logs/verifier/reward.txt\n", exe=True)
    assert make_agent(tmp_path, fake_harbor, tasks, trial_cache=True).cache_key(row, None) != key


def test_the_trial_cache_keys_the_adapter_as_deployed_its_files_modes_and_the_files_its_links_lead_to(
        tmp_path, fake_harbor):
    """A deployment copies each of the adapter's files with its permission bits, and a link inside the adapter as the
    file it leads to (hermit.hosts.inputs.copy_adapter): a script made executable, or the context's link led to another
    file, is another adapter, so another trial, which a resumed run does not take from the cache; the same adapter
    copied elsewhere is the same one."""
    from hermit.harbor.runner import adapter_digest
    adapter = tmp_path / "adapter"
    shutil.copytree(simple_template(), adapter)
    write(adapter / "prompts" / "a.md", "# Answer in French\n")
    write(adapter / "prompts" / "b.md", "# Answer in German\n")
    script = write(adapter / "scripts" / "check.sh", "#!/bin/sh\necho checked\n")
    mode = stat.S_IMODE(script.stat().st_mode)
    (adapter / "ADAPTER.md").unlink()
    (adapter / "ADAPTER.md").symlink_to(Path("prompts") / "a.md")
    digest = adapter_digest(adapter)
    shutil.copytree(adapter, tmp_path / "elsewhere", symlinks=True)
    assert adapter_digest(tmp_path / "elsewhere") == digest              # its files, not where it is
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    run = lambda: make_agent(tmp_path, fake_harbor, tasks, trial_cache=True).run(row, adapter)
    assert "from_trial_cache" not in run().extra and run().extra["from_trial_cache"]
    script.chmod(mode | 0o111)                                           # executable, as the copy will be
    assert adapter_digest(adapter) != digest and "from_trial_cache" not in run().extra
    script.chmod(mode)
    assert adapter_digest(adapter) == digest
    (adapter / "ADAPTER.md").unlink()
    (adapter / "ADAPTER.md").symlink_to(Path("prompts") / "b.md")        # the context from another file
    assert adapter_digest(adapter) != digest and "from_trial_cache" not in run().extra


def test_a_link_a_deployment_refuses_keys_the_adapter_by_its_own_text_so_no_run_takes_its_deployable_twins_trial(
        tmp_path, fake_harbor):
    """A link that leads to no file (a loop's too), outside the adapter or to a directory is refused by a deployment
    (hermit.hosts.inputs.adapter_files: the trial fails at staging), named as Python's caches are too (a deployment
    skips a __pycache__ folder and a .pyc file, not such a link), so an adapter with one is never the adapter without
    it, whose trial is in the cache: the link counts by its own text (another text, another adapter), and a resumed run
    stages the adapter and fails, as a run without the cache does."""
    from hermit.harbor.runner import adapter_digest
    adapter = tmp_path / "adapter"
    shutil.copytree(simple_template(), adapter)
    write(adapter / "prompts" / "a.md", "# Answer in French\n")
    outside = write(tmp_path / "outside.md", "# Answer in German\n")
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    run = lambda cache: make_agent(tmp_path, fake_harbor, tasks, trial_cache=cache).run(row, adapter)
    assert "from_trial_cache" not in run(True).extra and run(True).extra["from_trial_cache"]   # the twin's, kept
    digests = {adapter_digest(adapter)}
    for name, target in (("notes.md", "missing.md"), ("notes.md", "gone.md"), ("notes.md", "notes.md"),
                         ("notes.md", outside), ("docs", "prompts"),
                         ("__pycache__", "missing.md"), ("docs.pyc", "prompts")):   # no cache's folder, no .pyc file
        (adapter / name).symlink_to(target)
        digests.add(adapter_digest(adapter))
        for cache in (True, False):
            with pytest.raises(RuntimeError, match="deploying the adapter failed"):
                run(cache)
        (adapter / name).unlink()
    assert len(digests) == 8                                             # the twin, and each link of its own
    assert "from_trial_cache" in run(True).extra                        # the twin again: its trial


def test_the_trial_cache_keys_the_field_a_per_task_context_retrieves_for(tmp_path, fake_harbor):
    """An adapter's per-task context is computed for the row's `context_query` field (unset: the default query):
    another field is another context, so another trial."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = {**dataset.index(tasks)[0], "question": "What is 6 x 7?"}
    adapter = tool_adapter(tmp_path / "adapter", context_tool=True)
    keys = [make_agent(tmp_path, fake_harbor, tasks, trial_cache=True, **query).cache_key(row, adapter)
            for query in ({}, {"context_query": None}, {"context_query": "question"})]
    assert keys[0] == keys[1] != keys[2]


def test_the_trial_cache_keys_the_answer_agents_model_with_its_clients_timeout_and_retries(tmp_path, fake_harbor):
    """The answer agent's model is served with the client settings its spec gives (answer_model: its timeout and
    retries, which bind_harbor_params keeps beside a bound model): another timeout or retries is another trial."""
    tasks = tmp_path / "tasks"
    make_task(tasks, "alpha", image="example/alpha:1")
    row = dataset.index(tasks)[0]
    answer = {"acting_agent": {"cli": "answer"}, "web_search": None, "trial_cache": True}
    keys = [make_agent(tmp_path, fake_harbor, tasks, **answer, binding={**BINDING, **client}).cache_key(row, None)
            for client in ({}, {"timeout": 1200}, {"timeout": 1200}, {"timeout": 1200, "max_retries": 1})]
    assert keys[1] == keys[2] and len({keys[0], keys[1], keys[3]}) == 3


def test_the_disk_gate_waits_for_the_hosts_tmp_too(tmp_path, monkeypatch):
    from hermit.harbor import runner as runner_mod
    free = {runner_mod.HOST_TMP: 0.5, tmp_path: 100.0}
    monkeypatch.setattr(docker, "free_gb", lambda root: free[root])
    monkeypatch.setattr(docker.time, "sleep", lambda s: None)
    r = runner_mod.HarborAgent.__new__(runner_mod.HarborAgent)
    r.params, r._root, r.pulls, r.image_sizes = {"disk_gate_wait_s": 0.01}, tmp_path, docker.Pulls(), {}
    assert not r.wait_for_disk()                                  # /tmp under 1 GB: no trial starts
    free[runner_mod.HOST_TMP] = 2.0
    assert r.wait_for_disk()
    r.params["tmp_gate_gb"] = 0                                   # off
    free[runner_mod.HOST_TMP] = 0.0
    assert r.wait_for_disk()


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
