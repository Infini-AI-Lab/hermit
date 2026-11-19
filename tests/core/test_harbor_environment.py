"""A task's Harbor environment (hermit/environment/harbor.py), the environment served to an agent's worker
(hermit/environment/remote.py), a CLI Base Agent in a remote environment, the answer-only agent, and the run that
starts each task's environment, hands it to the agent and verifies with it (hermit/trainer/rollout.py).

Stand-ins only at the edge: Harbor's driver process is a small script speaking the driver's protocol (JSON lines),
which plays the task's container with a directory of this machine; a remote environment is an Environment whose exec
records the commands and answers with a CLI's stream; Docker's daemon is not asked (the disk gate and the image
cleanup are recorded). The last test runs the real driver against Docker and Harbor's venv, and skips where either is
absent."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import textwrap

import pytest

from hermit.common import RunOutput
from hermit.environment import harbor as harbor_mod
from hermit.environment.base import Environment, ExecResult, Limits, Unsupported
from hermit.environment.harbor import EnvironmentUnavailable, HarborEnvironments

# The driver's stand-in: the container is the folder FAKE_ROOT, its workdir FAKE_ROOT/app; `verify` writes the reward
# a file /app/reward-to-give names (0 when there is none), or fails as FAKE_VERIFY says.
FAKE_DRIVER = textwrap.dedent('''
    import json, os, shutil, subprocess, sys
    from pathlib import Path
    root = Path(os.environ["FAKE_ROOT"])
    out = os.fdopen(os.dup(1), "w"); os.dup2(2, 1)
    def local(p): return root / str(p).lstrip("/")
    seen = root.parent / "requests.jsonl"
    for line in sys.stdin:
        req = json.loads(line)
        with open(seen, "a") as f:
            f.write(json.dumps({k: v for k, v in req.items() if k != "stdin"}) + "\\n")
        op, rid = req["op"], req["id"]
        reply = {"id": rid, "ok": True}
        if op == "start":
            (root / "app").mkdir(parents=True, exist_ok=True)
            if os.environ.get("FAKE_START") == "fail":
                reply = {"id": rid, "ok": False, "error": "RuntimeError: the image did not pull"}
            else:
                reply.update(workdir="/app", agent_timeout_sec=900.0, trial_dir=str(root.parent / "trial"))
        elif op == "exec":
            env = {**os.environ, **req.get("env", {})}
            done = subprocess.run(["bash", "-c", req["command"]], cwd=local(req["cwd"]), env=env, input=None,
                                  capture_output=True, text=True)
            reply.update(code=done.returncode, output=done.stdout + done.stderr, timed_out=False)
        elif op == "upload":
            src, dst = Path(req["source"]), local(req["target"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst, dirs_exist_ok=True) if src.is_dir() else shutil.copy2(src, dst)
        elif op == "download":
            src, dst = local(req["source"]), Path(req["target"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst, dirs_exist_ok=True) if src.is_dir() else shutil.copy2(src, dst)
        elif op == "verify":
            logs = root.parent / "verifier"; logs.mkdir(exist_ok=True)
            how = os.environ.get("FAKE_VERIFY", "")
            if how:
                reply.update(rewards=None, exception={"type": how, "message": "it failed"}, verifier_dir=str(logs))
            else:
                given = local("/app/reward-to-give")
                reward = float(given.read_text()) if given.is_file() else 0.0
                (logs / "test-stdout.txt").write_text("1 failed" if reward < 1 else "all passed")
                reply.update(rewards={"reward": reward}, exception=None, verifier_dir=str(logs))
        elif op == "stop":
            reply.update(result=str(root.parent / "result.json"))
            out.write(json.dumps(reply) + "\\n"); out.flush()
            break
        out.write(json.dumps(reply) + "\\n"); out.flush()
''')


@pytest.fixture
def harbor(tmp_path, monkeypatch):
    """HarborEnvironments over the stand-in driver: (environments, the container's folder, the gate's and cleanup's
    calls)."""
    driver = tmp_path / "fake_driver.py"
    driver.write_text(FAKE_DRIVER)
    monkeypatch.setattr(harbor_mod, "DRIVER", driver)
    root = tmp_path / "container"
    monkeypatch.setenv("FAKE_ROOT", str(root))
    calls = {"gate": [], "cleanup": [], "down": []}
    monkeypatch.setattr(harbor_mod.docker, "wait_for_disk", lambda where, gate, wait, **kw:
                        calls["gate"].append((str(where), gate, kw.get("image"))) or True)
    monkeypatch.setattr(harbor_mod.docker, "data_root", lambda: Path("/var/lib/docker"))
    monkeypatch.setattr(harbor_mod.docker, "cleanup_images", lambda mode, task_dir, lock_dir, threshold_gb=1.0:
                        calls["cleanup"].append((mode, task_dir.name)))
    monkeypatch.setattr(harbor_mod.docker, "compose_down", lambda name, **kw: calls["down"].append(name))
    tasks = tmp_path / "tasks" / "demo"
    tasks.mkdir(parents=True)
    (tasks / "task.toml").write_text('[environment]\ndocker_image = "demo:1"\n')
    environments = HarborEnvironments({"tasks_dir": str(tmp_path / "tasks"), "harbor": sys.executable,
                                       "jobs_dir": str(tmp_path / "jobs"), "docker_cleanup": "rmi_task_image",
                                       "disk_gate_gb": 7}, base=tmp_path)
    return environments, root, calls


def test_an_environment_starts_behind_the_disk_gate_serves_the_agent_verifies_and_stops(harbor, tmp_path):
    environments, root, calls = harbor
    with environments.session(environments.task_dir({"id": "demo#1", "harbor_task": "demo"}), "demo#1") as env:
        assert env.workspace == PurePosixPath("/app") and env.limits.timeout_s == 900.0   # the task's budget
        assert env.exec(["sh", "-c", "echo $WHO > who.txt; cat who.txt"], env={"WHO": "agent"}).output == "agent\n"
        (tmp_path / "local.txt").write_text("1\n")
        env.upload(tmp_path / "local.txt", "reward-to-give")                   # relative: in the workspace
        assert (root / "app" / "reward-to-give").read_text() == "1\n"
        env.download("who.txt", tmp_path / "back" / "who.txt")
        assert (tmp_path / "back" / "who.txt").read_text() == "agent\n"
        with pytest.raises(ValueError, match="outside the workspace"):
            env.path("../etc/passwd")
        assert env.path("/logs/agent") == PurePosixPath("/logs/agent")         # absolute: anywhere there
        verdict = env.verify()
        assert (verdict["reward"], verdict["status"], verdict["passed"]) == (1.0, "ok", True)
        assert verdict["feedback"] == "all passed"
        with pytest.raises(Unsupported, match="verified"):                     # the agent's phase is over
            env.exec(["true"])
    # the warm start: the host's /tmp, then Docker's data root with the task's image; the image removed after
    assert calls["gate"] == [("/tmp", 1.0, None), ("/var/lib/docker", 7.0, "demo:1")]
    assert calls["cleanup"] == [("rmi_task_image", "demo")] and calls["down"] == []
    sent = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    start = sent[0]
    assert start["op"] == "start" and start["environment"] == {"delete": True}
    assert start["trial_name"].startswith("demo_1__") and Path(start["task_dir"]).name == "demo"
    assert sent[-1]["op"] == "stop" and not environments.live


def test_an_environment_that_does_not_start_is_unavailable_and_taken_down(harbor, monkeypatch):
    environments, _, calls = harbor
    monkeypatch.setenv("FAKE_START", "fail")
    with pytest.raises(EnvironmentUnavailable, match="did not start.*did not pull"):
        with environments.session(environments.task_dir({"harbor_task": "demo"})):
            pass
    assert calls["cleanup"] == [("rmi_task_image", "demo")] and not environments.live


def test_a_disk_gate_that_waits_its_time_out_starts_nothing(harbor, monkeypatch):
    environments, _, calls = harbor
    monkeypatch.setattr(harbor_mod.docker, "wait_for_disk", lambda *a, **k: False)
    monkeypatch.setattr(harbor_mod.docker, "free_gb", lambda root: 0.5)
    with pytest.raises(EnvironmentUnavailable, match="/tmp stayed under 1 GB free: no environment started"):
        environments.open(environments.task_dir({"harbor_task": "demo"})).start()


@pytest.mark.parametrize("raised, status, cause", [
    ("VerifierTimeoutError", "timeout", "solution_timed_out"),       # Harbor's grading: the agent's solution, 0
    ("RewardFileNotFoundError", "agent_error", "verifier_failed"),   # no reward: 0, as Harbor grades it
    ("EnvironmentStartTimeoutError", "infra", "verifier_failed"),    # not the agent's result
])
def test_a_verifier_that_fails_is_read_as_harbor_grades_it(harbor, monkeypatch, raised, status, cause):
    environments, _, _ = harbor
    monkeypatch.setenv("FAKE_VERIFY", raised)
    with environments.session(environments.task_dir({"harbor_task": "demo"})) as env:
        verdict = env.verify()
    assert (verdict["reward"], verdict["status"], verdict["failure_cause"]) == (None, status, cause)


def test_the_settings_merge_over_the_benchmark_file_and_refuse_what_they_do_not_know(tmp_path):
    (tmp_path / "tasks").mkdir()
    (tmp_path / "benchmark.yaml").write_text("dataset: {ref: x@1}\ntasks_dir: tasks\nenvironment: {type: docker, "
                                             "env: {A: '1'}}\ndocker_cleanup: images_keep\n")
    environments = HarborEnvironments({"benchmark": "benchmark.yaml", "harbor": sys.executable,
                                       "environment": {"force_build": True}}, base=tmp_path)
    assert environments.environment_config() == {"delete": True, "type": "docker", "env": {"A": "1"},
                                                  "force_build": True}
    for bad, match in (({"web_search": False}, "unknown keys"), ({"docker_cleanup": "prune"}, "docker_cleanup"),
                       ({"verifier_timeout": "never"}, "verifier_timeout"),
                       ({"verifier_timeout_multiplier": 0.5}, "at least 1")):
        with pytest.raises(ValueError, match=match):
            HarborEnvironments({"benchmark": "benchmark.yaml", "harbor": sys.executable, **bad}, base=tmp_path)


class Recorded(Environment):
    """A remote environment of the test's: its exec records each command and answers with `answer(command)`."""
    isolation = "remote"

    def __init__(self, tmp: Path, answer=None):
        super().__init__(PurePosixPath("/"), Limits(timeout_s=600))
        self.workspace = PurePosixPath("/app")
        self.tmp, self.answer, self.calls, self.uploads = tmp, answer or (lambda c: ExecResult(0, "")), [], {}

    def path(self, relative="", *, follow=True):
        rel = PurePosixPath(str(relative))
        return rel if rel.is_absolute() else self.workspace / rel

    def exec(self, command, *, cwd=None, env=None, timeout=None, stdin=None):
        self.calls.append({"command": list(command), "cwd": cwd, "env": dict(env or {}), "timeout": timeout,
                           "stdin": stdin})
        return self.answer(list(command))

    def upload(self, source, target):
        source = Path(source)
        self.uploads[str(self.path(target))] = sorted(p.name for p in source.iterdir()) if source.is_dir() else \
            source.read_text()
        return self.path(target)

    def download(self, source, target):
        raise FileNotFoundError(source)

    def install(self, cli, version=None):
        self.calls.append({"install": cli, "version": version})
        return version


CODEX_STREAM = "\n".join(json.dumps(e) for e in [
    {"type": "thread.started", "thread_id": "t-1"}, {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": "fixed it"}},
    {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}}]) + "\n"


def test_codex_runs_where_a_remote_environment_runs_its_commands(tmp_path, monkeypatch):
    from hermit.agent.base.interface import make
    monkeypatch.setenv("FAKE_KEY", "the-key")
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    binding = {"ref": "fake/m", "backend": "fake", "model": "m", "base_url": "https://models.example/v1",
               "api_key_env": "FAKE_KEY", "key_required": True, "apis": ["responses"]}
    env = Recorded(tmp_path, lambda c: ExecResult(0, CODEX_STREAM) if c[1:2] == ["exec"] else
                   ExecResult(1, "") if c[:2] == ["sh", "-c"] else ExecResult(0, ""))
    agent = make("codex", {"web_search": False, "binding": binding, "model": "m",
                           "log": str(tmp_path / "log" / "agent-codex.jsonl")})
    result = agent.run("fix the repository", environment=env)
    assert result.reply == "fixed it" and result.extra["served"] and result.extra["isolation"] == "remote"
    assert (tmp_path / "log" / "agent-codex.jsonl").read_text() == CODEX_STREAM      # the stream is its output
    install = next(c for c in env.calls if "install" in c)
    assert install == {"install": "codex", "version": None}                          # not there: Harbor installs it
    session = next(c for c in env.calls if c.get("command", [""])[1:2] == ["exec"])
    command = session["command"]
    assert command[command.index("-C") + 1] == "/app" and command[command.index("-s") + 1] == "danger-full-access"
    assert command[command.index("-o") + 1] == "/installed-agent/hermit-home/last_message.txt"
    assert session["cwd"] == "/app" and session["timeout"] == 600
    assert session["env"]["FAKE_KEY"] == "the-key"                                   # by name, never in a file
    assert session["env"]["CODEX_HOME"] == "/installed-agent/hermit-home"
    assert "config.toml" in env.uploads["/installed-agent/hermit-home"]
    assert "the-key" not in json.dumps(env.uploads)


def test_a_remote_session_refuses_what_the_environment_cannot_keep(tmp_path):
    from hermit.agent.base.interface import make
    from hermit.agent.base.tool import Tool
    env = Recorded(tmp_path)
    agent = make("codex", {"web_search": False})
    with pytest.raises(Unsupported, match="tool server"):
        agent.run("x", environment=env, tools=[Tool("t", "d", lambda: 1)])
    with pytest.raises(Unsupported, match="takes isolation"):
        make("pi", {"web_search": False}).run("x", environment=env)


def test_claude_codes_token_reaches_a_remote_session_on_a_descriptor(tmp_path, monkeypatch):
    from hermit.agent.base import login
    from hermit.agent.base.interface import make
    monkeypatch.setenv(login.TOKEN_ENV, "the-token")
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    stream = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done",
                         "usage": {"input_tokens": 1, "output_tokens": 1}}) + "\n"
    env = Recorded(tmp_path, lambda c: ExecResult(0, stream) if c[:2] == ["sh", "-c"] and "claude" in c else
                   ExecResult(0, "/usr/bin/claude"))
    agent = make("claude", {"web_search": False, "login": "claude", "model": "claude-haiku-5-5",
                            "plan_dir": str(tmp_path / "plan"), "bin": "claude"})
    result = agent.run("hi", environment=env)
    assert result.reply == "done"
    session = next(c for c in env.calls if "claude" in c["command"])
    assert session["command"][:2] == ["sh", "-c"] and "unset CLAUDE_CODE_OAUTH_TOKEN" in session["command"][2]
    assert session["env"][login.TOKEN_ENV] == "the-token" and session["env"]["IS_SANDBOX"] == "1"
    assert session["stdin"] == "hi"


def test_the_cli_agent_uploads_its_staging_into_a_remote_workspace_and_refuses_mounts(tmp_path, monkeypatch):
    from hermit.agent.acting.workspace import CliAgent
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    env = Recorded(tmp_path, lambda c: ExecResult(0, CODEX_STREAM) if c[1:2] == ["exec"] else ExecResult(0, ""))
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "a.txt").write_text("a")
    agent = CliAgent({"backend": "codex", "web_search": False, "bin": "/opt/codex", "files": ["fixtures"],
                      "workspaces": str(tmp_path / "ws")}, tmp_path)
    out = agent.run({"id": "t#1", "instruction": "do it"}, None, environment=env)
    assert out.output == "fixed it" and out.extra["isolation"] == "remote" and out.extra["workspace"] == "/app"
    assert env.uploads["/app/fixtures"] == ["a.txt"]
    session = next(c for c in env.calls if c["command"][1:2] == ["exec"])
    assert session["command"][0] == "/opt/codex" and session["command"][-1] == "do it"
    assert session["timeout"] == 600                                       # the environment's budget is the cap
    with pytest.raises(Unsupported, match="mounts"):
        CliAgent({"backend": "codex", "web_search": False, "mounts": [{"source": ".", "at": "d"}]},
                 tmp_path).run({"id": "t"}, None, environment=env)


def test_the_answer_agent_answers_once_and_writes_the_answer_where_the_verifier_reads_it(tmp_path, monkeypatch):
    from hermit.agent.acting import answer as answer_mod
    from hermit.agent.model.usage import Usage
    asked = []

    class Model:
        model = "m"

        def chat(self, messages, **kwargs):
            asked.append(messages)
            return {"role": "assistant", "content": "Thinking... <answer>B</answer>"}, Usage(model="m", input_tokens=3,
                                                                                             output_tokens=2, calls=1,
                                                                                             cost_usd=0.001)

    monkeypatch.setattr(answer_mod.ChatClient, "from_spec", staticmethod(lambda spec: Model()))
    env = Recorded(tmp_path, lambda c: ExecResult(0, "Which panel? A or B\n") if c[0] == "cat" else ExecResult(0, ""))
    agent = answer_mod.AnswerAgent({"model": {"base_url": "https://x", "model": "m"}, "instruction_file":
                                    "/app/instruction.md", "answer_file": "/app/answer.txt",
                                    "answer_pattern": "<answer>(.*?)</answer>", "logs": str(tmp_path / "logs")},
                                   tmp_path)
    out = agent.run({"id": "q1", "instruction": "unused"}, None, environment=env)
    assert out.output == {"answer": "B", "reply": "Thinking... <answer>B</answer>"} and out.cost_usd == 0.001
    assert asked == [[{"role": "user", "content": "Which panel? A or B"}]]           # no adapter: no system message
    assert env.uploads["/app/answer.txt"] == "B"


def test_the_environment_server_gives_the_agent_exec_upload_and_download_but_never_verify(tmp_path):
    from hermit.environment import Local
    from hermit.environment.remote import EnvironmentServer, ProxyEnvironment
    work = tmp_path / "work"
    work.mkdir()
    local = Local(work, Limits(timeout_s=30))
    local.verify = lambda **kw: {"reward": 1.0}                            # the host's to call, never the agent's
    server = EnvironmentServer(local).start()
    try:
        proxy = ProxyEnvironment.from_spec(server.spec())
        assert proxy.limits.timeout_s == 30 and proxy.isolation == "remote"
        assert proxy.exec(["sh", "-c", "echo hi > a.txt; cat a.txt"]).output == "hi\n"
        (tmp_path / "up").mkdir()
        (tmp_path / "up" / "b.txt").write_text("b")
        proxy.upload(tmp_path / "up", "dir")
        assert (work / "dir" / "b.txt").read_text() == "b"
        proxy.download("a.txt", tmp_path / "down" / "a.txt")
        assert (tmp_path / "down" / "a.txt").read_text() == "hi\n"
        import urllib.error
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for url, body in ((f"{server.url}/verify", b"{}"), (f"http://127.0.0.1:{server.port}/wrong-token/info", None)):
            with pytest.raises(urllib.error.HTTPError, match="404"):       # no such operation; no other token's
                opener.open(urllib.request.Request(url, data=body))
    finally:
        server.close()


class Environments:
    """The run's task environments, the test's: each task's environment records what happened to it."""

    def __init__(self, tmp: Path, fail_start: bool = False):
        self.tmp, self.fail_start, self.events = tmp, fail_start, []

    def task_dir(self, task):
        return self.tmp / str(task.public["harbor_task"])

    def session(self, task_dir, name=None):
        import contextlib
        outer = self

        class Env(Recorded):
            def verify(self, agent_logs=None):
                outer.events.append(("verify", agent_logs))
                return {"reward": 1.0, "rewards": {"reward": 1.0}, "status": "ok", "passed": True, "feedback": ""}

        @contextlib.contextmanager
        def cm():
            if outer.fail_start:
                raise EnvironmentUnavailable("the image did not pull")
            outer.events.append(("start", Path(task_dir).name))
            try:
                yield Env(outer.tmp)
            finally:
                outer.events.append(("stop", Path(task_dir).name))
        return cm()


def test_the_run_hands_the_agent_the_tasks_environment_and_judges_its_verifiers_verdict(tmp_path):
    from hermit.task import Task
    from hermit.task.verify import environment as judge
    from hermit.trainer.rollout import run_one
    environments, given = Environments(tmp_path), []
    logs = tmp_path / "log"
    logs.mkdir()

    def pipeline(task, adapter, environment=None):
        given.append((task, environment.workspace))
        return RunOutput(output="done", log=logs, extra={"session_failed": "the CLI failed"})   # verified all the same

    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo"}, private={"task_dir": "/secret/demo"})
    record = run_one(task, None, pipeline, judge, environments)
    assert given == [({"id": "demo#1", "harbor_task": "demo"}, PurePosixPath("/app"))]   # the public half only
    assert environments.events == [("start", "demo"), ("verify", logs), ("stop", "demo")]
    assert record["status"] == "ok" and record["judge"]["score"] == 1.0 and record["judge"]["feedback"] == "passed"
    failed = run_one(task, None, pipeline, judge, Environments(tmp_path, fail_start=True))
    assert failed["status"] == "error" and "could not be given" in failed["judge"]["feedback"]


def test_the_environment_judge_scores_the_reward_and_refuses_an_environment_that_failed():
    from hermit.task.verify import Unscored, environment as judge
    out = lambda verdict, **extra: RunOutput(output=None, extra={"verified": verdict, **extra})
    timed = judge({}, out({"reward": 0.0, "status": "timeout", "failure_cause": "solution_timed_out",
                           "exception": {"type": "VerifierTimeoutError"}, "passed": False, "feedback": "slow"}))
    assert timed.score == 0.0 and "solution_timed_out" in timed.feedback and "slow" in timed.feedback
    late = judge({}, out({"reward": 1.0, "status": "ok", "passed": True}, timed_out=True))
    assert late.score == 1.0 and "ran out of its time" in late.feedback       # its work still scored
    with pytest.raises(Unscored):
        judge({}, out({"reward": None, "status": "infra", "failure_cause": "verifier_failed"}))
    with pytest.raises(RuntimeError, match="environment.harbor"):
        judge({}, RunOutput(output="x"))


HARBOR_VENV = Path(os.path.expanduser("~/.cache/hermit/harbor-venv/bin/harbor"))


def _docker_up() -> bool:
    try:
        return shutil.which("docker") is not None and \
            subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not HARBOR_VENV.exists() or not _docker_up(), reason="needs Docker and Harbor's venv "
                    "(~/.cache/hermit/harbor-venv)")
def test_a_real_harbor_environment_runs_the_agents_commands_and_harbors_verifier(tmp_path):
    """A task of the test's own (an image Docker already has, no pull): what the agent writes in the environment is what
    Harbor's verifier scores, 1 for the right answer and 0 for none, the tests uploaded only for the verifier."""
    image = next((i for i in ("python:3.12-slim", "python:3.11-slim", "python:3.10-slim")
                  if subprocess.run(["docker", "image", "inspect", i], capture_output=True).returncode == 0), None)
    if image is None:
        pytest.skip("no python slim image on this machine (the test pulls none)")
    task = tmp_path / "tasks" / "answer-42"
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "task.toml").write_text(f'[agent]\ntimeout_sec = 120\n[verifier]\ntimeout_sec = 120\n'
                                    f'[environment]\ndocker_image = "{image}"\n')
    (task / "instruction.md").write_text("Write 42 to /app/answer.txt.\n")
    (task / "tests" / "test.sh").write_text('#!/bin/bash\nif [ "$(cat /app/answer.txt 2>/dev/null)" = 42 ]; then '
                                            'echo 1 > /logs/verifier/reward.txt; else echo 0 > '
                                            '/logs/verifier/reward.txt; fi\n')
    environments = HarborEnvironments({"tasks_dir": str(tmp_path / "tasks"), "jobs_dir": str(tmp_path / "jobs"),
                                       "disk_gate_gb": 0, "tmp_gate_gb": 0}, base=tmp_path)
    rewards = []
    for script in ("mkdir -p /app && echo 42 > /app/answer.txt", "true"):
        with environments.session(task) as env:
            assert env.exec(["sh", "-c", "test -e /tests/test.sh"]).code != 0     # no tests while the agent works
            assert env.exec(["sh", "-c", script], timeout=60).code == 0
            rewards.append(env.verify()["reward"])
    assert rewards == [1.0, 0.0]
