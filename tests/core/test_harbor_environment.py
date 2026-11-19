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
    with pytest.raises(EnvironmentUnavailable, match="did not start.*did not pull") as raised:
        with environments.session(environments.task_dir({"harbor_task": "demo"})):
            pass
    assert raised.value.cause == "sandbox_failed"                          # infra: run again
    assert calls["cleanup"] == [("rmi_task_image", "demo")] and not environments.live


def test_a_disk_gate_that_waits_its_time_out_starts_nothing(harbor, monkeypatch):
    environments, _, calls = harbor
    monkeypatch.setattr(harbor_mod.docker, "wait_for_disk", lambda *a, **k: False)
    monkeypatch.setattr(harbor_mod.docker, "free_gb", lambda root: 0.5)
    with pytest.raises(EnvironmentUnavailable, match="/tmp stayed under 1 GB free: no environment started") as raised:
        environments.open(environments.task_dir({"harbor_task": "demo"})).start()
    assert raised.value.cause == "disk"                                    # one that runs no better again


@pytest.mark.parametrize("raised, status, cause", [
    ("VerifierTimeoutError", "timeout", "solution_timed_out"),       # Harbor's grading: the agent's solution, 0
    ("RewardFileNotFoundError", "agent_error", "verifier_failed"),   # no reward: 0, as Harbor grades it
    ("EnvironmentStartTimeoutError", "infra", "sandbox_failed"),     # not the agent's result
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
        self.uploads[str(self.path(target))] = {p.name: p.read_text() if p.is_file() else None
                                                for p in sorted(source.iterdir())} if source.is_dir() else \
            source.read_text()
        return self.path(target)

    def download(self, source, target):
        raise FileNotFoundError(source)

    def install(self, cli, version=None):
        self.calls.append({"install": cli, "version": version})
        return version


def session_of(command) -> bool:
    """Whether a command an environment ran is a CLI's session (the CLI after the remote prologue), not a probe."""
    return command[:2] == ["bash", "-c"] and len(command) > 4 and command[3] == "bash"


def probe_of(command) -> bool:
    return command[:2] == ["bash", "-c"] and len(command) == 3 and "command -v" in command[2]


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
    env = Recorded(tmp_path, lambda c: ExecResult(0, CODEX_STREAM) if session_of(c) else
                   ExecResult(1, "") if probe_of(c) else ExecResult(0, ""))
    agent = make("codex", {"web_search": False, "binding": binding, "model": "m",
                           "log": str(tmp_path / "log" / "agent-codex.jsonl")})
    result = agent.run("fix the repository", environment=env)
    assert result.reply == "fixed it" and result.extra["served"] and result.extra["isolation"] == "remote"
    assert (tmp_path / "log" / "agent-codex.jsonl").read_text() == CODEX_STREAM      # the stream is its output
    install = next(c for c in env.calls if "install" in c)
    assert install == {"install": "codex", "version": None}                          # not there: Harbor installs it
    session = next(c for c in env.calls if session_of(c.get("command", [])))
    prologue, command = session["command"][2], session["command"][4:]
    assert 'nvm.sh' in prologue and '$HOME/.local/bin' in prologue                  # the installs' places on PATH
    assert probe_of(next(c["command"] for c in env.calls if probe_of(c.get("command", []))))
    assert command[-1] == "-" and session["stdin"] == "fix the repository"          # the prompt on stdin
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
    env = Recorded(tmp_path, lambda c: ExecResult(0, stream) if session_of(c) else ExecResult(0, "/usr/bin/claude"))
    agent = make("claude", {"web_search": False, "login": "claude", "model": "claude-haiku-5-5",
                            "plan_dir": str(tmp_path / "plan"), "bin": "claude"})
    result = agent.run("hi", environment=env)
    assert result.reply == "done"
    session = next(c for c in env.calls if session_of(c["command"]))
    assert "unset CLAUDE_CODE_OAUTH_TOKEN" in session["command"][2] and session["command"][4] == "claude"
    assert session["env"][login.TOKEN_ENV] == "the-token" and session["env"]["IS_SANDBOX"] == "1"
    assert session["stdin"] == "hi"


def test_the_cli_agent_uploads_its_staging_into_a_remote_workspace_and_refuses_mounts(tmp_path, monkeypatch):
    from hermit.agent.acting.workspace import CliAgent
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    env = Recorded(tmp_path, lambda c: ExecResult(0, CODEX_STREAM) if session_of(c) else ExecResult(0, ""))
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "a.txt").write_text("a")
    agent = CliAgent({"backend": "codex", "web_search": False, "bin": "/opt/codex", "files": ["fixtures"],
                      "workspaces": str(tmp_path / "ws")}, tmp_path)
    out = agent.run({"id": "t#1", "instruction": "do it"}, None, environment=env)
    assert out.output == "fixed it" and out.extra["isolation"] == "remote" and out.extra["workspace"] == "/app"
    assert env.uploads["/app/fixtures"] == {"a.txt": "a"}
    session = next(c for c in env.calls if session_of(c["command"]))
    assert session["command"][4] == "/opt/codex" and session["stdin"] == "do it"
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
    """The run's task environments, the test's: each task's environment records what happened to it. `fails` names
    what each start (in turn) does: None starts, a cause raises EnvironmentUnavailable with it (the last one repeats);
    `verdicts` the verifier's verdict of each run in turn (the last one repeats)."""
    infra_wait_s = 7.0

    def __init__(self, tmp: Path, fails=(None,), verdicts=None, infra_retries: int = 2):
        self.tmp, self.fails, self.events, self.infra_retries = tmp, list(fails), [], infra_retries
        self.verdicts = list(verdicts or [{"reward": 1.0, "rewards": {"reward": 1.0}, "status": "ok",
                                           "passed": True, "feedback": ""}])
        self.starts = 0

    def task_dir(self, task):
        return self.tmp / str(task.public["harbor_task"])

    def session(self, task_dir, name=None):
        import contextlib
        outer = self

        class Env(Recorded):
            def verify(self, agent_logs=None):
                outer.events.append(("verify", agent_logs))
                done = sum(event[0] == "verify" for event in outer.events)
                return dict(outer.verdicts[min(done, len(outer.verdicts)) - 1])

        @contextlib.contextmanager
        def cm():
            fail = outer.fails[min(outer.starts, len(outer.fails) - 1)]
            outer.starts += 1
            if fail is not None:
                raise EnvironmentUnavailable(f"the image did not pull ({fail})", fail)
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
    assert "attempts" not in record["pipeline_extra"]                      # one run: no attempts recorded


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
    Harbor's verifier scores, 1 for the right answer and 0 for none, the tests uploaded only for the verifier. A command
    of the agent's still running when the verifier starts is ended first; what an ended command left running (a server
    the task asked for) stays."""
    import threading
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
    (task / "tests" / "test.sh").write_text(
        '#!/bin/bash\nrunning() { for p in /proc/[0-9]*; do tr "\\0" " " < $p/cmdline 2>/dev/null; echo; done | '
        'grep -q "^sleep $1"; }\n'
        'if [ "$(cat /app/answer.txt 2>/dev/null)" = 42 ] && ! running 777 && running 888; then '
        'echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi\n')
    environments = HarborEnvironments({"tasks_dir": str(tmp_path / "tasks"), "jobs_dir": str(tmp_path / "jobs"),
                                       "disk_gate_gb": 0, "tmp_gate_gb": 0}, base=tmp_path)
    rewards = []
    for script in ("mkdir -p /app && echo 42 > /app/answer.txt && (nohup sleep 888 >/dev/null 2>&1 &)", "true"):
        with environments.session(task) as env:
            assert env.instruction == "Write 42 to /app/answer.txt.\n"          # Harbor's, byte for byte
            assert env.exec(["sh", "-c", "test -e /tests/test.sh"]).code != 0     # no tests while the agent works
            assert env.exec(["sh", "-c", script], timeout=60).code == 0
            ended = []

            def still_running():
                try:
                    env.exec(["sleep", "777"], timeout=100)
                except RuntimeError as exc:                                   # ended by the verifier's start
                    ended.append(str(exc))

            late = threading.Thread(target=still_running, daemon=True)
            late.start()                                                         # still running at the verifier
            import time
            time.sleep(3)
            rewards.append(env.verify()["reward"])
            late.join(timeout=30)
            assert ended and "the verifier started" in ended[0]
    assert rewards == [1.0, 0.0]


def test_a_react_agent_works_in_the_tasks_environment_its_tools_run_there(tmp_path, monkeypatch):
    """A ReactAgent subclass's tools use the task's environment (`self.environment`, the run's by default); the loop,
    which cannot stop a model call at the environment's budget, is given it without that budget."""
    from hermit.agent.acting.react import ReactAgent, tool
    from hermit.agent.model import llm
    from hermit.agent.model.usage import Usage
    replies = iter([{"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "ls"})}}]},
        {"role": "assistant", "content": "listed"}])

    class Model:
        model = "m"
        usage = []

        def chat(self, messages, **kwargs):
            return next(replies), Usage(model="m", input_tokens=1, output_tokens=1, calls=1, cost_usd=0.0)

    monkeypatch.setattr(llm.ChatClient, "from_spec", staticmethod(lambda spec: Model()))

    class Terminal(ReactAgent):
        @tool
        def bash(self, command: str) -> str:
            """Run a shell command in the task's environment."""
            return self.environment.exec(["bash", "-c", command]).output

    env = Recorded(tmp_path, lambda c: ExecResult(0, "README.md\n"))
    out = Terminal({"model": {"base_url": "https://x", "model": "m"}, "logs": str(tmp_path / "logs")},
                   tmp_path).run({"id": "t", "instruction": "list the files"}, None, environment=env)
    assert out.output == "listed" and env.calls[0]["command"] == ["bash", "-c", "ls"]


def _infra_run(tmp_path, environments, pauses, monkeypatch):
    """A task run through run_one with the test's environments, its pauses recorded instead of slept."""
    from hermit.task import Task
    from hermit.task.verify import environment as judge
    from hermit.trainer import rollout
    calls = []

    def pipeline(task, adapter, environment=None):
        calls.append(task["id"])
        return RunOutput(output="done", cost_usd=0.01, tokens=10, api_speed={"model_calls": 1})

    monkeypatch.setattr(rollout, "_sleep", pauses.append)
    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo"})
    return rollout.run_one(task, None, pipeline, judge, environments), calls


def test_an_environment_that_fails_to_start_once_runs_again_and_is_scored(tmp_path, monkeypatch):
    """The old runner's infra semantics: a rollout that is not the agent's result runs again in a fresh environment,
    after infra_wait_s; the run that works is the one scored, and its record carries every run (attempts)."""
    environments, pauses = Environments(tmp_path, fails=["sandbox_failed", None]), []
    record, calls = _infra_run(tmp_path, environments, pauses, monkeypatch)
    assert pauses == [7.0] and calls == ["demo#1"] and environments.starts == 2
    assert record["status"] == "ok" and record["judge"]["score"] == 1.0
    attempts = record["pipeline_extra"]["attempts"]
    assert [a["unscored"] for a in attempts] == ["sandbox_failed", None]
    assert attempts[0]["cost_usd"] == 0.0 and record["cost_usd"] == 0.01     # the environment that never came up: 0


def test_a_rollout_that_stays_infra_is_not_scored_as_the_agents_zero(tmp_path, monkeypatch):
    """Retried infra_retries times; then the judge refuses to score it (Unscored): a failed run, judge_error, which
    `hermit eval` counts among its errors and the report as infra, never a verdict of the agent's 0."""
    from hermit.harbor.report import kind
    infra = {"reward": None, "rewards": None, "status": "infra", "failure_cause": "verifier_failed", "passed": None,
             "exception_message": "the verifier's tools never arrived", "feedback": ""}
    environments, pauses = Environments(tmp_path, verdicts=[infra]), []
    record, calls = _infra_run(tmp_path, environments, pauses, monkeypatch)
    assert calls == ["demo#1"] * 3 and pauses == [7.0, 7.0]                 # 1 + infra_retries runs
    assert record["status"] == "judge_error" and "Unscored" in record["error"]
    assert "not the agent's result (verifier_failed" in record["judge"]["feedback"]
    assert record["pipeline_extra"]["unscored"] == "verifier_failed" and len(record["pipeline_extra"]["attempts"]) == 3
    assert record["cost_usd"] == pytest.approx(0.03) and record["tokens"] == 30   # every run's usage is the rollout's
    assert kind(record) == "infra"
    never = Environments(tmp_path, fails=["harbor_failed"])
    record, calls = _infra_run(tmp_path, never, [], monkeypatch)
    assert calls == [] and never.starts == 3 and record["status"] == "judge_error"
    assert record["output"]["status"] == "infra" and kind(record) == "infra" and record["cost_usd"] == 0.0


def test_a_cause_that_runs_no_better_again_is_not_run_again(tmp_path, monkeypatch):
    for cause in ("disk", "model_rejected"):                                # NO_RETRY, the runner's
        environments, pauses = Environments(tmp_path, fails=[cause, None]), []
        record, calls = _infra_run(tmp_path, environments, pauses, monkeypatch)
        assert environments.starts == 1 and pauses == [] and calls == []
        assert record["status"] == "judge_error" and f"({cause}" in record["judge"]["feedback"]


def test_a_session_the_provider_could_not_serve_is_infra_and_any_other_pipeline_failure_the_pipelines(tmp_path):
    from hermit.task import Task
    from hermit.task.verify import environment as judge
    from hermit.trainer import rollout
    seen = []

    def pipeline(task, adapter, environment=None):
        seen.append(1)
        raise RuntimeError("agent codex could not be served after 3 attempts: 503")

    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo"})
    out = rollout.environment_rollout(task, task.given(), None, pipeline, Environments(tmp_path), 0.0,
                                      pause=lambda s: None)
    # the CLI agent's own runs again are the rollout's one provider allowance (the runner's busy_retries): spent
    assert len(seen) == 1 and out.extra["unscored"] == "model_provider_failed" and out.cost_usd is None
    for said, cause, runs in (("chat/completions HTTP 503: overloaded", "model_provider_failed", 3),
                              ("EnvironmentUnavailable: the Harbor driver exited (-9)", "harbor_failed", 3),
                              ("agent codex: model_rejected: unexpected status 404 Not Found", "model_rejected", 1),
                              ("chat/completions HTTP 401: invalid key", "model_rejected", 1)):
        seen.clear()

        def failing(task, adapter, environment=None, said=said):
            seen.append(1)
            raise RuntimeError(said)

        out = rollout.environment_rollout(task, task.given(), None, failing, Environments(tmp_path), 0.0,
                                          pause=lambda s: None)
        assert (len(seen), out.extra["unscored"]) == (runs, cause), said

    def broken(task, adapter, environment=None):
        raise KeyError("a bug of the agent's own")

    record = rollout.run_one(task, None, broken, judge, Environments(tmp_path))
    assert record["status"] == "error" and "the pipeline failed" in record["judge"]["feedback"]


@pytest.mark.parametrize("verified, status, cause", [
    ({"rewards": {"reward": 0.0}, "exception": None, "stdout": "failed to download https://github.com/astral-sh/uv/"
      "releases/download/x\nuvx: command not found"}, "infra", "verifier_failed"),    # its tools never arrived
    ({"rewards": {"reward": 0.0}, "exception": None, "stdout": "1 failed"}, "ok", None),   # the agent's 0
    ({"rewards": None, "exception": None}, "infra", "verifier_failed"),                  # no exception and no reward
    ({"rewards": None, "exception": {"type": "RuntimeError", "message": "Docker compose command failed for "
                                     "environment x"}}, "infra", "sandbox_failed"),     # the host's (HOST_FAILURE)
    ({"rewards": None, "exception": {"type": "HarborDriverError", "message": "gone"}}, "infra", "harbor_failed"),
])
def test_the_verifiers_answer_is_classified_as_the_runner_classified_a_trial(harbor, tmp_path, verified, status,
                                                                             cause):
    environments, _, _ = harbor
    logs = tmp_path / "verifier"
    logs.mkdir()
    if "stdout" in verified:
        (logs / "test-stdout.txt").write_text(verified.pop("stdout"))
    verdict = environments.verdict({**verified, "verifier_dir": str(logs)})
    assert (verdict["status"], verdict["failure_cause"]) == (status, cause)


def test_infra_retries_and_infra_wait_s_are_read_and_checked(tmp_path):
    (tmp_path / "tasks").mkdir()
    base = {"tasks_dir": "tasks", "harbor": sys.executable}
    environments = HarborEnvironments(base, base=tmp_path)
    assert (environments.infra_retries, environments.infra_wait_s) == (2, 300.0)       # the runner's defaults
    assert HarborEnvironments({**base, "infra_retries": 0, "infra_wait_s": 1}, base=tmp_path).infra_retries == 0
    with pytest.raises(ValueError, match="infra_retries"):
        HarborEnvironments({**base, "infra_retries": -1}, base=tmp_path)


# --- review round 1: the old runner's behavior, each restored ---------------------------------------------------------

def test_a_remote_session_refuses_to_resume_and_turns_web_search_off_on_a_restricted_network(tmp_path, monkeypatch):
    from hermit.agent.base.interface import make
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    env = Recorded(tmp_path, lambda c: ExecResult(0, CODEX_STREAM) if session_of(c) else ExecResult(0, "/x"))
    with pytest.raises(Unsupported, match="resume"):
        make("codex", {"web_search": True, "bin": "codex"}).run("x", environment=env, session="t-1")
    env.network_public = False                          # the task's agent phase has no public network
    make("codex", {"web_search": True, "bin": "codex"}).run("x", environment=env)
    config = env.uploads["/installed-agent/hermit-home"]["config.toml"]
    assert 'web_search = "disabled"' in config


def test_every_sign_in_file_leaves_the_environment_and_an_unconfirmed_removal_fails(tmp_path, monkeypatch):
    from hermit.agent.base.interface import make
    user = tmp_path / "user-codex"
    user.mkdir()
    (user / "auth.json").write_text('{"OPENAI_API_KEY": "sk-user"}')         # the user's own CLI home's sign-in
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    from hermit.agent.base import cli
    monkeypatch.setattr(cli.Codex, "user_home", classmethod(lambda cls: user))
    env = Recorded(tmp_path, lambda c: ExecResult(0, CODEX_STREAM) if session_of(c) else ExecResult(0, ""))
    make("codex", {"web_search": False, "bin": "codex"}).run("x", environment=env)
    removal = next(c for c in env.calls if c["command"][:2] == ["rm", "-f"])
    assert removal["command"][2:] == ["/installed-agent/hermit-home/auth.json"] and removal["cwd"] == "/"
    stuck = Recorded(tmp_path, lambda c: ExecResult(1, "busy") if c[:2] == ["rm", "-f"] else
                     ExecResult(0, CODEX_STREAM) if session_of(c) else ExecResult(0, ""))
    with pytest.raises(RuntimeError, match="sign-in could not be removed"):
        make("codex", {"web_search": False, "bin": "codex"}).run("x", environment=stuck)


def test_codexs_lost_sign_in_and_refused_configuration_are_read_from_its_notices(tmp_path, monkeypatch):
    from hermit.agent.acting.workspace import CliAgent
    from hermit.agent.base.interface import LoginFailed
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    lost = json.dumps({"type": "turn.failed", "error": {"message": "unexpected status 401 Unauthorized: bad"}}) + "\n"
    model = json.dumps({"type": "turn.failed", "error": {"message": "unexpected status 404 Not Found: model "
                                                                    "gpt-x does not exist"}}) + "\n"
    params = {"backend": "codex", "web_search": False, "bin": "codex", "workspaces": str(tmp_path / "ws"),
              "retries": 1}
    for stream, raised, match in ((lost, LoginFailed, "401"), (model, RuntimeError, "model_rejected")):
        env = Recorded(tmp_path, lambda c, stream=stream: ExecResult(1, stream) if session_of(c) else ExecResult(0, ""))
        with pytest.raises(raised, match=match):
            CliAgent(params, tmp_path).run({"id": "t", "instruction": "x"}, None, environment=env)


def test_the_session_takes_harbors_instruction_and_the_tasks_budget_and_counts_every_attempt(tmp_path, monkeypatch):
    """Harbor's instruction byte for byte (what the runner gave the CLI), the task's own agent budget (8 hours for
    Terminal-Bench-Science) unless the agent's timeout_s says less, and the usage of a run again counted too."""
    from hermit.agent.acting.workspace import CliAgent
    from hermit.agent.base import cli
    from hermit.agent.base.interface import Result
    from hermit.agent.model.usage import Usage
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    monkeypatch.setattr("time.sleep", lambda s: None)
    seen = []

    def run(self, prompt, *, environment=None, tools=(), session=None, roles=()):
        seen.append((prompt, environment.limits.timeout_s))
        served = len(seen) > 1                          # the first: the provider could not serve it, after 100 tokens
        return Result(reply="done" if served else "", usage=[Usage(model="m", input_tokens=100 if not served else 10,
                                                                   output_tokens=5, calls=1, cost_usd=0.5)],
                      exit_code=0, extra={"served": served, "unavailable": not served, "started": True,
                                          "failed": False, "output_tail": ""})

    monkeypatch.setattr(cli.Codex, "run", run)
    env = Recorded(tmp_path)
    env.limits = Limits(timeout_s=28800.0)
    env.instruction = "Fix the repo.\n\n  keep this  \n"
    out = CliAgent({"backend": "codex", "web_search": False, "bin": "codex", "workspaces": str(tmp_path / "ws")},
                   tmp_path).run({"id": "t", "question": "Fix?", "instruction": "Fix the repo."}, None,
                                 environment=env)
    assert seen == [("Fix the repo.\n\n  keep this  \n", 28800.0)] * 2
    assert out.extra["attempts"] == 2 and out.tokens == 120 and out.cost_usd == 1.0       # both attempts' usage
    capped = CliAgent({"backend": "codex", "web_search": False, "bin": "codex", "timeout_s": 600,
                       "workspaces": str(tmp_path / "ws")}, tmp_path)
    seen.clear()
    capped.run({"id": "t"}, None, environment=env)
    assert seen[0][1] == 600.0                                                   # the agent's own cap, when less


def test_hooks_get_python3_their_audit_comes_back_and_the_image_context_is_checked(tmp_path, monkeypatch):
    from hermit.agent.acting import workspace
    log = tmp_path / "log"
    log.mkdir()
    calls = []

    class Env(Recorded):
        def ensure(self, packages):
            calls.append(("ensure", list(packages)))

        def download(self, source, target):
            if source == ".hermit-hooks":
                (Path(target) / "hook-state").mkdir(parents=True)
                (Path(target) / "hook-calls.jsonl").write_text("{}\n")
                return Path(target)
            raise OSError("docker cp failed")

    env = Env(tmp_path, lambda c: ExecResult(1, "") if c[:2] == ["sh", "-c"] else ExecResult(0, ""))
    assert workspace._ensure_python3(env) is False and calls == [("ensure", ["python3"])]
    workspace._collect_hooks(env, log)
    assert (log / "hook-calls.jsonl").is_file() and (log / "hook-state").is_dir()
    work = tmp_path / "work"
    work.mkdir()
    (work / "AGENTS.md").write_text("adapter")
    with pytest.raises(RuntimeError, match="could not be read"):          # there, but unread: never overwritten
        workspace._upload_staging(env, work, "codex", {"adapter_id": "a"}, {})
    from hermit.environment.harbor_errors import pipeline_cause
    assert pipeline_cause(f"RuntimeError: {workspace.IMAGE_CONTEXT_UNREAD} (AGENTS.md)") == ("harbor_failed", True)

    class Big(Recorded):
        def download(self, source, target):
            Path(target).write_text("x" * 40000)
            return Path(target)

    with pytest.raises(ValueError, match="over Codex's limit"):            # the merged text within Codex's cap
        workspace._upload_staging(Big(tmp_path), work, "codex", {"adapter_id": "a"}, {})


def test_the_answer_agent_writes_to_harbors_default_file_unless_told_none(tmp_path, monkeypatch):
    from hermit.agent.acting import answer as answer_mod
    from hermit.agent.model.usage import Usage

    class Model:
        model = "m"

        def chat(self, messages, **kwargs):
            return {"role": "assistant", "content": "C"}, Usage(model="m", calls=1)

    monkeypatch.setattr(answer_mod.ChatClient, "from_spec", staticmethod(lambda spec: Model()))
    for given, written in (({}, {"/app/answer.txt": "C"}), ({"answer_file": None}, {})):
        env = Recorded(tmp_path)
        env.instruction = "Which? A, B or C"
        answer_mod.AnswerAgent({"model": {"base_url": "https://x", "model": "m"}, "logs": str(tmp_path / "logs"),
                                **given}, tmp_path).run({"id": "q"}, None, environment=env)
        assert env.uploads == written


def test_the_rollouts_status_is_the_runners_and_a_login_plan_is_waited_for_before_the_environment(tmp_path):
    """`status` beside the verdict as the runner wrote it (AHE classifies by it): a CLI that exited with an error is
    agent_error, a session that ran out of its time a timeout, whatever the verifier then scored."""
    from hermit.task import Task
    from hermit.trainer import rollout
    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo"})
    environments = Environments(tmp_path)
    environments.before_start = lambda: environments.events.append(("plan",))
    for extra, status in (({"exit_code": 1}, "agent_error"), ({"timed_out": True}, "timeout"), ({}, "ok")):
        out = rollout.environment_rollout(task, task.given(), None, lambda t, a, environment=None, extra=extra:
                                          RunOutput(output="x", extra=dict(extra)), environments, 0.0)
        assert out.extra["status"] == status
    assert environments.events[0] == ("plan",) and environments.events[1][0] == "start"


def test_a_run_that_ends_stops_the_pause_before_a_run_again(tmp_path):
    import threading
    from hermit.task import Task
    from hermit.trainer import rollout
    environments = Environments(tmp_path, fails=["sandbox_failed"])
    environments.stopping = threading.Event()
    environments.stopping.set()
    environments.infra_wait_s = 3600.0
    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo"})
    out = rollout.environment_rollout(task, task.given(), None, lambda *a, **k: None, environments, 0.0)
    assert environments.starts == 1 and out.extra["unscored"] == "sandbox_failed"


def test_the_harbor_task_directories_are_run_secrets_and_a_login_reaches_its_hosts(tmp_path):
    from types import SimpleNamespace
    from hermit import isolate
    (tmp_path / "tasks").mkdir()
    data = {"data": {"tasks": None}, "judge": {}, "environment": {"harbor": {"tasks_dir": "tasks",
                                                                            "harbor": sys.executable}},
            "agent": {"params": {"model": "gpt-5.6-luna", "login": "chatgpt"}}}
    config = SimpleNamespace(data=data, base=tmp_path, path=lambda v: None if v is None else tmp_path / v,
                             run_dir=lambda: (_ for _ in ()).throw(ValueError("none")))
    secrets, _ = isolate._secrets(config)
    assert (tmp_path / "tasks").resolve() in secrets
    environments = isolate.task_environments(config)
    assert "chatgpt.com" in environments.allowed_hosts and environments.before_start is not None


def test_the_worker_of_a_task_in_its_own_environment_has_that_environments_budget(tmp_path, monkeypatch):
    from hermit import isolate
    env = Recorded(tmp_path)
    env.limits = Limits(timeout_s=3600.0)
    env.setup_budget_s = 360.0
    assert isolate._call_deadline(None, env, {"agent_timeout_sec": 900}) == 3600.0 + 360.0 + isolate.STARTUP_S
    assert isolate._call_deadline(600.0, env, {}) == 600.0                     # a stricter run limit stays
    assert isolate._call_deadline(None, None, {"agent_timeout_sec": 900}) == 900 + isolate.STARTUP_S


def test_a_request_to_a_driver_that_reads_nothing_times_out(tmp_path):
    """The request's write is the writer thread's: a driver whose pipe is full holds no caller past its timeout."""
    import time
    from hermit.environment.harbor import _Driver
    script = tmp_path / "deaf.py"
    script.write_text("import time\ntime.sleep(60)\n")
    original = harbor_mod.DRIVER
    harbor_mod.DRIVER = script
    try:
        driver = _Driver(sys.executable, tmp_path / "driver.log", dict(os.environ))
        started = time.time()
        with pytest.raises(TimeoutError):
            driver.request("exec", 0.5, command="x" * (4 << 20))
        assert time.time() - started < 5
        driver.close(1)
    finally:
        harbor_mod.DRIVER = original


def test_the_agents_logs_are_copied_for_the_verifier_never_through_a_link_left_there(tmp_path):
    """The driver's safe_copy (loaded alone: the driver imports harbor, which this interpreter has not): a link the
    agent put in /logs/agent, a folder it shares with this machine, is replaced, not written through."""
    import ast
    import shutil as shutil_mod
    source = (Path(harbor_mod.__file__).with_name("harbor_driver.py")).read_text()
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "safe_copy")
    space = {"os": os, "shutil": shutil_mod, "Path": Path}
    exec(compile(ast.Module([node], []), "safe_copy", "exec"), space)
    victim = tmp_path / "host-file"
    victim.write_text("precious")
    logs, shared = tmp_path / "logs", tmp_path / "agent-dir"
    logs.mkdir()
    shared.mkdir()
    (logs / "agent-codex.jsonl").write_text("stream")
    (shared / "agent-codex.jsonl").symlink_to(victim)
    space["safe_copy"](logs, shared)
    assert victim.read_text() == "precious" and (shared / "agent-codex.jsonl").read_text() == "stream"
    assert not (shared / "agent-codex.jsonl").is_symlink()
