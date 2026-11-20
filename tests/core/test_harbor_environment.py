"""A task's Harbor environment (hermit/environment/harbor.py) and its driver, a CLI Base Agent in a remote
environment, the answer-only agent, and the run that
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
import shlex
import shutil
import subprocess
import sys
import textwrap

import pytest

from hermit.common import RunOutput
from hermit.environment import harbor as harbor_mod
from hermit.environment.base import Environment, ExecResult, Limits, Unsupported
from hermit.environment.harbor import EnvironmentUnavailable, HarborEnvironments

# The driver's stand-in: the container is the folder FAKE_ROOT, its workdir FAKE_ROOT/app; `done` writes the reward
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
                if os.environ.get("FAKE_ELAPSED"):       # the phase began this long before the reply
                    import time
                    reply["deadline_epoch"] = time.time() + 900.0 - float(os.environ["FAKE_ELAPSED"])
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
        elif op == "done":
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
        assert env.workspace == PurePosixPath("/app") and env.limits.timeout_s == 840.0   # the task's, less the margin
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

    def exec(self, command, *, cwd=None, env=None, timeout=None, stdin=None, by_fd=None):
        self.calls.append({"command": list(command), "cwd": cwd, "env": dict(env or {}), "timeout": timeout,
                           "stdin": stdin, "by_fd": dict(by_fd or {})})
        if list(command[:2]) == ["mv", "-f"] and command[2] in self.uploads:      # an upload put in place
            self.uploads[command[3]] = self.uploads.pop(command[2])
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


ROTATED = json.dumps({"tokens": {"access_token": "a" * 20, "refresh_token": "r" * 20, "account_id": "acc"},
                      "last_refresh": "2026-01-01T00:00:00Z"})      # a ChatGPT sign-in Codex refreshed
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
    assert session["by_fd"] == {login.TOKEN_ENV: "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"}   # the environment's move
    assert session["command"][4] == "claude"
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

    def session(self, task_dir, name=None, installs=None):
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
AGENT_KEY = "sk-agent-synthetic-0123456789abcdef"     # an agent's model credential, the test's own


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
        'echo "grader key: $GRADER_API_KEY"\n'
        'test -e /tmp/hermit-sign-in && echo "secret-present"\n'
        'running 777 && echo "777-alive"; stat -c "home-owner:%u" /installed-agent/hermit-home 2>/dev/null\n'
        'echo "stream: $(cat /logs/agent/agent-codex.jsonl 2>&1)"\n'
        'if [ "$(cat /app/answer.txt 2>/dev/null)" = 42 ] && ! running 777 && ! running 666 && running 888 && '
        '! grep -rqs sk-agent-synthetic /logs/agent; then '
        'echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi\n')
    environments = HarborEnvironments({"tasks_dir": str(tmp_path / "tasks"), "jobs_dir": str(tmp_path / "jobs"),
                                       "disk_gate_gb": 0, "tmp_gate_gb": 0,
                                       "verifier": {"env": {"GRADER_API_KEY": "grader-literal-credential"}}},
                       base=tmp_path)
    rewards = []
    for script in ("mkdir -p /app && echo 42 > /app/answer.txt && (nohup sleep 888 >/dev/null 2>&1 &)", "true"):
        with environments.session(task) as env:
            assert env.instruction == "Write 42 to /app/answer.txt.\n"          # Harbor's, byte for byte
            assert env.exec(["sh", "-c", "test -e /tests/test.sh"]).code != 0     # no tests while the agent works
            assert env.exec(["sh", "-c", script], timeout=60).code == 0
            # the agent's variables reach the container only: a DOCKER_HOST of its own leaves this machine's compose
            # client as it was (the next command still runs), and a secret on a descriptor is in no environment there
            said = env.exec(["sh", "-c", 'echo "host=$DOCKER_HOST"; cat <&"$SECRET_FD"; echo; '
                             'grep -l s3cr3t-value /proc/[0-9]*/environ 2>/dev/null | wc -l'],
                            env={"DOCKER_HOST": "tcp://127.0.0.1:9", "SECRET": "s3cr3t-value"},
                            by_fd={"SECRET": "SECRET_FD"}, timeout=60)
            assert said.output.split() == ["host=tcp://127.0.0.1:9", "s3cr3t-value", "0"], said.output
            assert env.exec(["true"], env={"FAKE_AGENT_API_KEY": AGENT_KEY}, timeout=60).code == 0
            if script != "true":
                # an image whose AGENTS.md is a link to its CLAUDE.md: the adapter's text goes first in a file of its
                # own there, the image's text (read through the link there) after it, CLAUDE.md untouched
                from hermit.agent.acting import workspace
                assert env.exec(["sh", "-c", "echo IMAGE-CLAUDE > CLAUDE.md && ln -sf CLAUDE.md AGENTS.md"]).code == 0
                staging = tmp_path / "staging"
                staging.mkdir(exist_ok=True)
                (staging / "AGENTS.md").write_text("ADAPTER")
                workspace._upload_staging(env, staging, "codex", {"adapter_id": "a"}, {}, {})
                said = env.exec(["sh", "-c", "test -L AGENTS.md && echo link; cat AGENTS.md; echo ---; cat CLAUDE.md"])
                assert said.output == "ADAPTER\n\nIMAGE-CLAUDE\n---\nIMAGE-CLAUDE\n", said.output
            ended = []

            def still_running():
                try:   # one marked, one below it with a cleared environment (no mark): both ended
                    env.exec(["bash", "-c", "env -i /bin/sleep 666 & sleep 777"], timeout=100)
                except RuntimeError as exc:                                   # ended by the verifier's start
                    ended.append(str(exc))

            late = threading.Thread(target=still_running, daemon=True)
            late.start()                                                         # still running at the verifier
            import time
            time.sleep(3)
            logs = tmp_path / "agent-logs"                  # the session's log holding the agent's credential
            logs.mkdir(exist_ok=True)
            (logs / "agent-codex.jsonl").write_text(f"error: bad key {AGENT_KEY}\n")
            verdict = env.verify(agent_logs=logs)
            rewards.append(verdict["reward"])
            # Harbor's scrub of the verifier's credential: neither the feedback nor the trial's files hold it
            assert "grader-literal-credential" not in verdict["feedback"] and "grader key" in verdict["feedback"]
            late.join(timeout=30)
            assert ended and "the agent's phase ended" in ended[0]
    assert rewards == [1.0, 0.0]                        # the agent's credential never reached the verifier
    kept = [p for p in (tmp_path / "jobs").rglob("*") if p.is_file() and not p.is_symlink()]
    assert not [p for p in kept if b"grader-literal-credential" in p.read_bytes()]
    assert not [p for p in kept if AGENT_KEY.encode() in p.read_bytes()]          # nor stays in the trial's files
    # a task whose agent user is not root: what Hermit provisions is that user's (its uid:gid, as the runner's
    # _owner), its home among it
    user_task = tmp_path / "tasks" / "as-nobody"
    shutil.copytree(task, user_task)
    (user_task / "task.toml").write_text(f'[agent]\ntimeout_sec = 120\nuser = "nobody"\n[verifier]\ntimeout_sec = 120\n'
                                         f'[environment]\ndocker_image = "{image}"\n')
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("x = 1\n")
    with environments.session(user_task) as env:
        assert env.exec(["id", "-u"]).output.strip() == "65534"
        env.upload(home, "/installed-agent/hermit-home")                         # Hermit's own: given to the user
        assert env.exec(["stat", "-c", "%u", "/installed-agent/hermit-home/config.toml"]).output.strip() == "65534"
        def sleeps():
            try:
                env.exec(["sleep", "777"], timeout=100)
            except RuntimeError:                       # ended at the phase's end
                pass

        late = threading.Thread(target=sleeps, daemon=True)
        late.start()                                   # a command of the non-root agent's, running at the phase's end
        import time
        time.sleep(3)
        verdict = env.verify()
        late.join(timeout=30)
    # its process group ended at the phase's end (killed as root), its home its own
    assert "777-alive" not in verdict["feedback"] and "home-owner:65534" in verdict["feedback"], verdict["feedback"]
    with environments.session(user_task) as env:       # a credential path the agent cannot remove: no verifier
        env.secret(["/etc/hostname"])
        verdict = env.verify()
    assert verdict["status"] == "infra" and (verdict["exception"] or {}).get("type") == "HarborDriverError"
    # Harbor's own agent timeout ends the phase: the command still running is ended (AgentPhaseOver), and the trial
    # runs on to its verifier, its verdict the agent's timeout
    timed = HarborEnvironments({"tasks_dir": str(tmp_path / "tasks"), "jobs_dir": str(tmp_path / "jobs2"),
                                "disk_gate_gb": 0, "tmp_gate_gb": 0, "agent_timeout_sec": 15}, base=tmp_path)
    with timed.session(task) as env:
        assert env.limits.timeout_s == 13.5                                      # its own budget ends first
        env.deadline_at = None             # this side's own cap off: Harbor's timeout first, as on an abnormal path
        env.secret(["/tmp/hermit-sign-in"])                                      # a sign-in, registered first
        assert env.exec(["sh", "-c", "echo token > /tmp/hermit-sign-in"]).code == 0
        with pytest.raises(RuntimeError, match="AgentPhaseOver"):                # a CLI still streaming at the end
            env.exec(["sh", "-c", "echo '{\"type\": \"answer\"}'; sleep 100"], timeout=100,
                     stream="agent-codex.jsonl")
        verdict = env.verify()
    assert (verdict["status"], (verdict["exception"] or {}).get("type")) == ("timeout", "AgentTimeoutError")
    # before the verifier: the sign-in removed, the stream so far kept in the agent logs under its name
    assert "secret-present" not in verdict["feedback"] and 'stream: {"type": "answer"}' in verdict["feedback"]


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
    def phase_over(task, adapter, environment=None):              # Harbor's agent timeout ended the phase first
        raise RuntimeError("AgentPhaseOver: exec: the agent's phase ended")

    environments = Environments(tmp_path, verdicts=[{"reward": 0.0, "rewards": {"reward": 0.0}, "status": "timeout",
                                                     "failure_cause": "agent_exhausted_budget", "passed": False,
                                                     "feedback": ""}])
    out = rollout.environment_rollout(task, task.given(), None, phase_over, environments, 0.0, pause=lambda s: None)
    assert out.extra["verified"]["status"] == "timeout" and out.extra["timed_out"] is True   # the trial's verdict

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
    from hermit.agent.base import login
    from hermit.agent.base.interface import make
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    monkeypatch.setattr(login, "prepare", lambda root, home: (Path(home) / login.FILE).write_text("{}"))
    monkeypatch.setattr(login, "keep_refreshed", lambda *a: None)
    monkeypatch.setattr(login, "note_usage", lambda *a: None)
    params = {"web_search": False, "bin": "codex", "login": "chatgpt", "model": "m", "plan_dir": str(tmp_path / "p")}
    env = Recorded(tmp_path, lambda c: ExecResult(0, CODEX_STREAM) if session_of(c) else ExecResult(0, ""))
    env.plan_admitted = True
    make("codex", params).run("x", environment=env)
    removal = next(c for c in env.calls if c["command"][:2] == ["rm", "-f"])
    assert removal["command"][2:] == ["/installed-agent/hermit-home/auth.json"] and removal["cwd"] == "/"
    stuck = Recorded(tmp_path, lambda c: ExecResult(1, "busy") if c[:2] == ["rm", "-f"] else
                     ExecResult(0, CODEX_STREAM) if session_of(c) else ExecResult(0, ""))
    stuck.plan_admitted = True
    with pytest.raises(RuntimeError, match="sign-in could not be removed"):
        make("codex", params).run("x", environment=stuck)


def test_codexs_lost_sign_in_and_refused_configuration_are_read_from_its_notices(tmp_path, monkeypatch):
    from hermit.agent.acting.workspace import CliAgent
    from hermit.agent.base.interface import LoginFailed
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    lost = json.dumps({"type": "turn.failed", "error": {"message": "unexpected status 401 Unauthorized: bad"}}) + "\n"
    model = json.dumps({"type": "turn.failed", "error": {"message": "unexpected status 404 Not Found: model "
                                                                    "gpt-x does not exist"}}) + "\n"
    params = {"backend": "codex", "web_search": False, "bin": "codex", "workspaces": str(tmp_path / "ws"),
              "retries": 1}
    agent = CliAgent(params, tmp_path)
    answer = lambda stream: (lambda c: ExecResult(1, stream) if session_of(c) else ExecResult(0, ""))
    with pytest.raises(LoginFailed, match="401"):                               # a lost sign-in ends the run
        agent.run({"id": "t", "instruction": "x"}, None, environment=Recorded(tmp_path, answer(lost)))
    out = agent.run({"id": "t", "instruction": "x"}, None, environment=Recorded(tmp_path, answer(model)))
    assert out.extra["infra"] == "model_rejected" and out.extra["infra_final"] is True   # never run again
    billed = json.dumps({"type": "turn.failed", "error": {"message": "unexpected status 402 Payment Required: "
                                                                     "insufficient credits"}}) + "\n"
    with pytest.raises(LoginFailed, match="usage_limit"):                       # nothing later can be served
        agent.run({"id": "t", "instruction": "x"}, None, environment=Recorded(tmp_path, answer(billed)))
    long = json.dumps({"type": "turn.failed", "error": {"message": "unexpected status 400 Bad Request: the prompt "
                                                                   "is too long"}}) + "\n"
    out = agent.run({"id": "t", "instruction": "x"}, None, environment=Recorded(tmp_path, answer(long)))
    assert "infra" not in out.extra                                             # the input's: the agent's result


def test_a_refusal_is_the_agents_unless_the_configuration_or_the_bill_refused_it():
    """The runner's session_verdict for every CLI: an oversized prompt and the model's own refusal are the agent's
    result; a configuration refused before any token, model_rejected; a bill or a spent quota, usage_limit (a halt)."""
    from hermit.agent.base.codex_stream import session_verdict
    base = {"served": False, "rejected": True}
    assert session_verdict({**base, "failure": "API Error: 400 prompt is too long"}, 0) is None
    assert session_verdict({**base, "failure": "API Error: 404 model not found", "model_refusal": True}, 0) is None
    assert session_verdict({**base, "failure": "API Error: 404 model not found"}, 0) == "model_rejected"
    assert session_verdict({**base, "failure": "API Error: 404 model not found"}, 12) is None    # it had served
    assert session_verdict({**base, "failure": "API Error: 402 credit balance is too low"}, 0) == "usage_limit"
    assert session_verdict({"served": False, "unavailable": True, "failure": "429 insufficient_quota"}, 0) == \
        "usage_limit"


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
    assert seen == [("Fix the repo.\n\n  keep this  \n", 28800.0)]
    # the provider could not serve it: never run again in this environment, the run runs it in a fresh one, its
    # spend kept
    assert out.extra["infra"] == "model_provider_failed" and out.extra["infra_final"] is False
    assert out.tokens == 105 and out.cost_usd == 0.5
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
    assert workspace._ensure_python3(env) is False and calls == []    # nothing installs during the agent phase
    workspace._collect_hooks(env, log)
    assert (log / "hook-calls.jsonl").is_file() and (log / "hook-state").is_dir()
    work = tmp_path / "work"
    work.mkdir()
    (work / "AGENTS.md").write_text("adapter")
    with pytest.raises(RuntimeError, match="could not be read"):          # there, but unread: never overwritten
        workspace._upload_staging(env, work, "codex", {"adapter_id": "a"}, {})
    from hermit.environment.harbor_errors import pipeline_cause
    assert pipeline_cause(f"RuntimeError: {workspace.IMAGE_CONTEXT_UNREAD} (AGENTS.md)") == ("harbor_failed", True)

    big = Recorded(tmp_path, lambda c: ExecResult(0, workspace.IMAGE_MARK + "x" * 40000) if c[:2] == ["sh", "-c"]
                   else ExecResult(0, ""))
    with pytest.raises(workspace.AdapterContextTooLong, match="over Codex's limit"):   # the merge within the cap
        workspace._upload_staging(big, work, "codex", {"adapter_id": "a"}, {})


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
    import contextlib
    environments = Environments(tmp_path)

    @contextlib.contextmanager
    def admission():
        environments.events.append(("plan",))
        yield
        environments.events.append(("plan done",))

    environments.admission = admission
    admitted = []
    for extra, status in (({"exit_code": 1}, "agent_error"), ({"timed_out": True}, "timeout"), ({}, "ok")):
        def pipeline(t, a, environment=None, extra=extra):
            admitted.append(environment.plan_admitted)
            return RunOutput(output="x", extra=dict(extra))
        out = rollout.environment_rollout(task, task.given(), None, pipeline, environments, 0.0)
        assert out.extra["status"] == status and out.extra["verifier_reward_raw"] == 1.0
    assert environments.events[:2] == [("plan",), ("start", "demo")] and environments.events[4] == ("plan done",)
    assert admitted == [True] * 3                   # the CLI in it takes no wait or slot of its own


def test_a_run_that_ends_stops_the_pause_before_a_run_again(tmp_path):
    import threading
    from hermit.task import Task
    from hermit.trainer import rollout
    environments = Environments(tmp_path, fails=["sandbox_failed"])
    environments.stopping = threading.Event()
    environments.infra_wait_s = 3600.0
    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo"})
    session = environments.session

    def failing(task_dir, name=None, installs=None):            # another task ends the run while this one fails
        environments.stopping.set()
        return session(task_dir, name)

    environments.session = failing
    out = rollout.environment_rollout(task, task.given(), None, lambda *a, **k: None, environments, 0.0)
    assert environments.starts == 1 and out.extra["unscored"] == "sandbox_failed"
    from hermit.agent.base.interface import LoginFailed
    with pytest.raises(LoginFailed):                            # and no environment starts for a task after it
        rollout.environment_rollout(task, task.given(), None, lambda *a, **k: None, environments, 0.0)
    assert environments.starts == 1


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
    assert "chatgpt.com" in environments.allowed_hosts and environments.admission is not None


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
        started = time.time()
        assert driver.close(1) is False                         # its writer stuck: killed, not waited on
        assert time.time() - started < 30
    finally:
        harbor_mod.DRIVER = original


def _driver_function(name: str, **space):
    """One function of the driver, loaded alone (the driver imports harbor, which this interpreter has not)."""
    import ast
    source = (Path(harbor_mod.__file__).with_name("harbor_driver.py")).read_text()
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module([node], []), name, "exec"), space)
    return space[name]


def test_the_agents_logs_are_copied_for_the_verifier_never_through_a_link_left_there(tmp_path):
    """safe_copy: a link the agent put in /logs/agent, a folder it shares with this machine, is replaced, never written
    through, and so is one it puts there while the copy runs (a background process of the agent's): a file where the
    copy is writing, a folder the copy is in swapped for a link to this machine's files."""
    import contextlib
    import shutil as shutil_mod
    import uuid
    victim, host_dir = tmp_path / "host-file", tmp_path / "host-dir"
    victim.write_text("precious")
    host_dir.mkdir()
    logs, shared = tmp_path / "logs", tmp_path / "agent-dir"
    (logs / "sub").mkdir(parents=True)
    (logs / "linked").mkdir()
    shared.mkdir()
    (logs / "agent-codex.jsonl").write_text("stream")
    (logs / "sub" / "a.txt").write_text("a")
    (logs / "linked" / "b.txt").write_text("b")
    (shared / "agent-codex.jsonl").symlink_to(victim)                     # there before the copy
    (shared / "linked").symlink_to(host_dir)
    (shared / "sub").mkdir()

    class Racing:                                     # shutil, but the agent moves while a file is being written
        def __getattr__(self, name):
            return getattr(shutil_mod, name)

        @staticmethod
        def copyfileobj(given, out):
            if os.readlink(f"/proc/self/fd/{given.fileno()}").endswith("a.txt"):
                (shared / "sub").rename(shared / "moved")                 # the folder the copy writes in, swapped
                (shared / "sub").symlink_to(host_dir)
                (shared / "moved" / "a.txt").symlink_to(victim)           # and a link where its file goes
            shutil_mod.copyfileobj(given, out)

    import stat as stat_mod
    copy = _driver_function("safe_copy", os=os, shutil=Racing(), Path=Path, contextlib=contextlib, uuid=uuid,
                            stat=stat_mod)
    copy(logs, shared)
    assert victim.read_text() == "precious" and list(host_dir.iterdir()) == []     # nothing of this machine's touched
    assert (shared / "agent-codex.jsonl").read_text() == "stream"
    assert not (shared / "agent-codex.jsonl").is_symlink()
    assert (shared / "linked" / "b.txt").read_text() == "b" and not (shared / "linked").is_symlink()
    assert (shared / "moved" / "a.txt").read_text() == "a"                # written where the copy held the folder
    assert not (shared / "moved" / "a.txt").is_symlink()


def _own_pid_namespace() -> bool:
    try:
        return subprocess.run(["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc", "true"],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


HARBOR_PYTHON = HARBOR_VENV.with_name("python")
DRIVER_FILE = Path(harbor_mod.__file__).with_name("harbor_driver.py")


def test_an_admitted_session_takes_no_slot_and_a_refused_one_holds_the_plan_and_comes_back(tmp_path, monkeypatch):
    from hermit.agent.base import cli, login
    from hermit.agent.base.interface import Result
    from hermit.agent.model.usage import Usage
    monkeypatch.setattr(login, "slot", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no slot inside")))
    monkeypatch.setattr(login, "wait", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no wait inside")))
    held = []
    monkeypatch.setattr(login, "hold", lambda plan, until=None, **k: held.append(until))
    monkeypatch.setattr(cli.CliAgent, "_session", lambda self, prompt, **k: Result(
        reply="", usage=[Usage(model="m", input_tokens=5, calls=1)],
        extra={"served": False, "refused": True, "held_until": 123.0}))
    env = Recorded(tmp_path)
    env.plan_admitted = True
    out = cli.Codex({"web_search": False, "login": "chatgpt", "model": "m",
                     "plan_dir": str(tmp_path / "plan")}).run("x", environment=env)
    assert out.extra["refused"] and held == [123.0]


def test_a_session_the_plan_refused_and_then_ran_counts_both_sessions_usage(tmp_path, monkeypatch):
    from hermit.agent.base import cli, login
    from hermit.agent.base.interface import Result
    from hermit.agent.model.usage import Usage
    import contextlib
    monkeypatch.setattr(login, "wait", lambda *a, **k: "")
    monkeypatch.setattr(login, "slot", lambda *a, **k: contextlib.nullcontext(True))
    monkeypatch.setattr(login, "held", lambda *a, **k: "")
    monkeypatch.setattr(login, "hold", lambda *a, **k: None)
    runs = iter([False, True])
    monkeypatch.setattr(cli.CliAgent, "_session", lambda self, prompt, **k: (lambda served: Result(
        reply="ok" if served else "", usage=[Usage(model="m", input_tokens=10, cost_usd=0.01, calls=1)],
        extra={"served": served, "refused": not served}))(next(runs)))
    out = cli.Codex({"web_search": False, "login": "chatgpt", "model": "m",
                     "plan_dir": str(tmp_path / "plan")}).run("x", environment=tmp_path)
    assert out.reply == "ok" and sum(u.input_tokens for u in out.usage) == 20


def test_a_provider_retry_reuses_the_images_own_context_and_a_failed_attempt_keeps_its_usage(tmp_path, monkeypatch):
    from hermit.agent.acting import workspace
    reads = []

    def answer(c):
        if c[:2] == ["sh", "-c"] and "cat" in c[2]:
            reads.append(c[2])
            return ExecResult(0, workspace.IMAGE_MARK + "IMAGE TEXT")
        return ExecResult(0, "")

    work = tmp_path / "work"
    work.mkdir()
    env, cache = Recorded(tmp_path, answer), {}
    for _ in range(3):                                          # three deployments into one environment
        (work / "CLAUDE.md").write_text("ADAPTER")
        workspace._upload_staging(env, work, "claude", {"adapter_id": "a"}, {}, cache)
        assert env.uploads["/app/CLAUDE.md"] == "ADAPTER\n\nIMAGE TEXT"
    assert len(reads) == 1                                      # read once, never the merge back
    removal = [c["command"] for c in env.calls if c["command"][:2] == ["sh", "-c"] and "-L" in c["command"][2]]
    assert removal and "rm -f -- CLAUDE.md" in removal[0][2]    # a link there replaced, never written through


def test_harbor_settings_override_on_the_command_line_and_eval_rows_keep_the_outcome(tmp_path):
    from hermit.config import Config
    examples = Path(__file__).resolve().parents[2] / "examples"
    config = Config.load(examples / "terminal_bench_2_0" / "baseline" / "codex.yaml",
                         ["environment.harbor.infra_retries=0", "environment.harbor.agent_timeout_sec=900",
                          "environment.harbor.disk_gate_gb=0"])
    harbor = config.data["environment"]["harbor"]
    assert (harbor["infra_retries"], harbor["agent_timeout_sec"], harbor["disk_gate_gb"]) == (0, 900, 0)
    from hermit.harbor.report import kind
    assert kind({"status": "judge_error", "outcome": {"status": "infra"}}) == "infra"


def test_a_host_pipelines_answer_agent_keeps_the_tasks_budget(tmp_path, monkeypatch):
    """With the pipeline on the host (no worker deadline), the task's agent budget still ends the model's call."""
    from hermit.agent.acting import answer as answer_mod
    from hermit.agent.model.llm import ChatAborted

    class Slow:
        model = "m"

        def chat(self, messages, abort=None, **kwargs):
            if abort is None or not abort.wait(10):
                raise AssertionError("never given up")
            raise ChatAborted("given up")

    monkeypatch.setattr(answer_mod.ChatClient, "from_spec", staticmethod(lambda spec: Slow()))
    env = Recorded(tmp_path)
    env.limits = Limits(timeout_s=0.3)
    env.instruction = "Q?"
    out = answer_mod.AnswerAgent({"model": {"base_url": "https://x", "model": "m"}, "logs": str(tmp_path / "l")},
                                 tmp_path).run({"id": "q"}, None, environment=env)
    assert out.extra["timed_out"] is True and env.uploads == {}             # no answer written past the budget


def test_a_rotated_sign_in_comes_back_though_the_sessions_exec_failed(tmp_path, monkeypatch):
    from hermit.agent.base import login
    from hermit.agent.base.interface import make
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    seen = []
    monkeypatch.setattr(login, "prepare", lambda root, home: (Path(home) / login.FILE).write_text('{"old": 1}'))
    monkeypatch.setattr(login, "keep_refreshed", lambda root, home: seen.append(("keep", (Path(home) / login.FILE)
                                                                                .read_text())))
    monkeypatch.setattr(login, "note_usage", lambda *a, **k: None)

    class Env(Recorded):
        def exec(self, command, **kw):
            if session_of(list(command)):
                raise RuntimeError("EnvironmentUnavailable: the transport broke")
            if "wc -c" in str(command[-1]):                     # the sign-in measured there first
                return ExecResult(0, "200\n")
            return super().exec(command, **kw)

        def download(self, source, target):
            if source.endswith(login.FILE):
                seen.append("download")
                Path(target).write_text(ROTATED)
                return Path(target)
            raise FileNotFoundError(source)

    env = Env(tmp_path)
    env.plan_admitted = True
    with pytest.raises(RuntimeError, match="transport"):
        make("codex", {"web_search": False, "login": "chatgpt", "model": "m", "bin": "codex",
                       "plan_dir": str(tmp_path / "plan"), "login_root": str(tmp_path / "root")}).run(
            "x", environment=env)
    assert seen == ["download", ("keep", ROTATED)]                 # brought back before the removal


def test_an_environment_failing_under_the_agent_or_this_machine_is_infra(harbor, monkeypatch):
    environments, root, _ = harbor
    with environments.session(environments.task_dir({"harbor_task": "demo"})) as env:
        with pytest.raises(EnvironmentUnavailable):           # a copy Docker failed: the environment's, not a 0
            env.download("/no/such/file", root.parent / "x")
    monkeypatch.setattr(harbor_mod, "_Driver", lambda *a, **k: (_ for _ in ()).throw(OSError(24, "Too many files")))
    with pytest.raises(EnvironmentUnavailable) as raised:
        environments.open(environments.task_dir({"harbor_task": "demo"})).start()
    assert raised.value.cause == "host_failed"
    from hermit.environment.harbor_errors import NO_RETRY
    assert "host_failed" in NO_RETRY


# --- review round 3 ---------------------------------------------------------------------------------------------------

def test_a_cli_sessions_provider_failure_runs_again_in_a_fresh_environment_on_its_own_allowance(tmp_path):
    """The runner's busy_retries: the provider's allowance over the task's runs (not infra_retries), the n-th after
    n x busy_wait_s, each run in a fresh environment; every run's spend kept."""
    from hermit.task import Task
    from hermit.trainer import rollout
    environments, pauses = Environments(tmp_path, infra_retries=0), []
    environments.busy_retries, environments.busy_wait_s = 2, 10.0
    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo"})
    down = lambda t, a, environment=None: RunOutput(output=None, cost_usd=0.01, tokens=5, api_speed={"model_calls": 1},
                                                    extra={"infra": "model_provider_failed", "infra_final": False})
    out = rollout.environment_rollout(task, task.given(), None, down, environments, 0.0, pause=pauses.append)
    assert environments.starts == 3 and pauses == [10.0, 20.0]
    assert out.extra["unscored"] == "model_provider_failed" and out.cost_usd == pytest.approx(0.03)


def test_an_adapter_that_failed_is_scored_zero_not_left_out(tmp_path, monkeypatch):
    """The runner's adapter_failed and context_over_limit: the agent's result, scored 0 with its message (no
    verifier), so a candidate's broken adapter stays in pass@1's denominator."""
    from hermit.agent.acting import workspace
    from hermit.harbor.report import kind
    from hermit.task import Task
    from hermit.task.verify import environment as judge
    from hermit.trainer import rollout
    monkeypatch.setattr(workspace, "_deploy", lambda *a, **k: (_ for _ in ()).throw(KeyError("the context tool broke")))
    agent = workspace.CliAgent({"backend": "codex", "web_search": False, "bin": "codex",
                                "workspaces": str(tmp_path / "ws")}, tmp_path)
    environments = Environments(tmp_path)
    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo", "instruction": "x"})
    record = rollout.run_one(task, tmp_path, agent.run, judge, environments)
    assert record["status"] == "ok" and record["judge"]["score"] == 0.0 and kind(record) == "scored"
    assert "the context tool broke" in record["judge"]["feedback"]
    assert not [e for e in environments.events if e[0] == "verify"]          # no verifier for it


def test_a_refused_session_without_a_plan_ends_the_run(tmp_path, monkeypatch):
    from hermit.agent.acting.workspace import CliAgent
    from hermit.agent.base import cli
    from hermit.agent.base.interface import LoginFailed, Result
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    monkeypatch.setattr(cli.Codex, "run", lambda self, prompt, **k: Result(
        reply="", exit_code=1, extra={"served": False, "refused": True, "unavailable": False, "started": True,
                                      "failed": True, "output_tail": "", "failure": "You've hit your usage limit"}))
    with pytest.raises(LoginFailed, match="no plan"):
        CliAgent({"backend": "codex", "web_search": False, "bin": "codex", "workspaces": str(tmp_path / "ws")},
                 tmp_path).run({"id": "t", "instruction": "x"}, None, environment=Recorded(tmp_path))


def test_an_admitted_claude_session_that_drew_credits_holds_the_plan(tmp_path, monkeypatch):
    from hermit.agent.base import cli, login
    from hermit.agent.base.interface import Result
    held, noted = [], []
    monkeypatch.setattr(login, "hold", lambda plan, until=None, why="session_refused", **k: held.append(why))
    monkeypatch.setattr(login, "note_usage", lambda *a, **k: noted.append(1))
    monkeypatch.setattr(cli.CliAgent, "_session", lambda self, prompt, **k: Result(
        reply="done", extra={"served": True, "plan_report": {"using_credits": True, "resets_at": 99.0}}))
    env = Recorded(tmp_path)
    env.plan_admitted = True
    monkeypatch.setenv(login.TOKEN_ENV, "t")
    cli.Claude({"web_search": False, "login": "claude", "model": "claude-haiku-5-5",
                "plan_dir": str(tmp_path / "plan")}).run("x", environment=env)
    assert noted and held == ["usage_credits"]


def test_a_local_sign_in_copy_goes_though_its_take_back_failed(tmp_path, monkeypatch):
    from hermit.agent.base import login
    from hermit.agent.base.interface import make
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    copies = []

    def prepare(root, home):
        (Path(home) / login.FILE).write_text("{}")
        copies.append(Path(home) / login.FILE)

    monkeypatch.setattr(login, "prepare", prepare)
    monkeypatch.setattr(login, "keep_refreshed", lambda *a: (_ for _ in ()).throw(OSError(28, "No space left")))
    env = Recorded(tmp_path, lambda c: ExecResult(0, CODEX_STREAM) if session_of(c) else ExecResult(0, ""))
    env.plan_admitted = True
    out = make("codex", {"web_search": False, "login": "chatgpt", "model": "m", "bin": "codex",
                         "home": str(tmp_path / "kept"), "plan_dir": str(tmp_path / "plan")}).run("x", environment=env)
    assert out.reply == "fixed it"                               # the finished session stands (the runner's)
    assert copies and not copies[0].exists()                     # removed however the take-back went


def test_a_react_timeout_keeps_what_the_loop_spent_and_asks_no_follow_up(tmp_path, monkeypatch):
    import time
    from hermit.agent.acting.react import ReactAgent, tool
    from hermit.agent.model import llm
    from hermit.agent.model.usage import Usage
    calls = []

    class Model:
        model = "m"

        def chat(self, messages, abort=None, **kwargs):
            calls.append(1)
            return ({"role": "assistant", "content": "", "tool_calls": [
                {"id": "c", "type": "function", "function": {"name": "slow", "arguments": "{}"}}]},
                Usage(model="m", input_tokens=100, output_tokens=10, calls=1, cost_usd=0.5))

    monkeypatch.setattr(llm.ChatClient, "from_spec", staticmethod(lambda spec: Model()))

    class Agent(ReactAgent):
        @tool
        def slow(self) -> str:
            """Takes longer than the budget."""
            time.sleep(0.5)
            return "done"

    env = Recorded(tmp_path)
    env.limits = Limits(timeout_s=0.2)
    out = Agent({"model": {"base_url": "https://x", "model": "m"}, "logs": str(tmp_path / "l"),
                 "answer_tag": "FINAL"}, tmp_path).run({"id": "t", "instruction": "go"}, None, environment=env)
    assert out.extra["timed_out"] is True and out.tokens == 110 and out.cost_usd == 0.5 and len(calls) == 1


def test_the_answer_agents_fd_retries_attachments_and_http_refusals(tmp_path, monkeypatch):
    import os as os_mod
    from hermit.agent.acting import answer as answer_mod
    from hermit.agent.base.interface import LoginFailed
    from hermit.agent.model.llm import ChatHTTPError
    from hermit.agent.model.usage import Usage
    made, sent = [], []

    class Model:
        model, max_retries = "m", 4

        def __init__(self):
            made.append(self)

        def chat(self, messages, **kwargs):
            sent.append(messages)
            return {"role": "assistant", "content": "A"}, Usage(model="m", calls=1)

    monkeypatch.setattr(answer_mod.ChatClient, "from_spec", staticmethod(lambda spec: Model()))
    fds = len(os_mod.listdir("/proc/self/fd"))

    class Env(Recorded):
        def download(self, source, target):
            Path(target).write_bytes(b"\x89PNG")
            return Path(target)

    def answer(c):                                  # the listing, then the image measured and read there
        if "wc -c" in c[-1]:
            return ExecResult(0, "4\n")
        if "base64" in c[-1]:                        # framed, a client's warning beside it
            return ExecResult(0, "WARN: compose\nhermit-attachment-begin\niVBORw==\nhermit-attachment-end\n")
        return ExecResult(0, "/app/a.png\n/app/a.png\n")

    env = Env(tmp_path, answer)
    agent = answer_mod.AnswerAgent({"model": {"base_url": "https://x", "model": "m", "max_retries": 0},
                                    "attachments": ["/app/*.png", "/app/a.png"], "logs": str(tmp_path / "l")},
                                   tmp_path)
    for _ in range(3):
        agent.run({"id": "q", "instruction": "Which?"}, None, environment=env)
    assert len(os_mod.listdir("/proc/self/fd")) == fds                     # no descriptor left per task
    assert made[0].max_retries == 0                                       # the model's own setting honoured
    assert sum(1 for part in sent[0][-1]["content"] if part["type"] == "image_url") == 1   # each image once

    def refused(code, body):
        class Refusing(Model):
            def chat(self, messages, **kwargs):
                raise ChatHTTPError(code, body.encode(), {})
        monkeypatch.setattr(answer_mod.ChatClient, "from_spec", staticmethod(lambda spec: Refusing()))
        return answer_mod.AnswerAgent({"model": {"base_url": "https://x", "model": "m"}, "logs": str(tmp_path / "l")},
                                      tmp_path).run({"id": "q", "instruction": "Which?"}, None,
                                                    environment=Recorded(tmp_path))
    with pytest.raises(LoginFailed, match="usage_limit"):
        refused(402, "payment required")
    with pytest.raises(LoginFailed, match="usage_limit"):
        refused(429, '{"error": {"code": "insufficient_quota"}}')
    with pytest.raises(RuntimeError, match="model_rejected"):
        refused(400, '{"error": {"message": "Unsupported parameter: max_tokens"}}')
    assert refused(400, '{"error": {"message": "prompt is too long"}}').output["answer"] == ""   # the agent's


def test_the_verifier_watchdog_follows_harbors_own_timeout(harbor, monkeypatch):
    environments, _, _ = harbor
    asked = []
    with environments.session(environments.task_dir({"harbor_task": "demo"})) as env:
        env.started["verifier_timeout_sec"] = 14400.0
        original = env.driver.request
        env.driver.request = lambda op, timeout, **f: asked.append((op, timeout)) or original(op, timeout, **f)
        env.verify()
    assert ("done", 14400.0 + harbor_mod.VERIFY_ALLOWANCE_S) in asked


def test_a_teardown_failure_keeps_the_tasks_verdict_and_a_host_failure_is_not_run_again(tmp_path):
    import contextlib
    from hermit.task import Task
    from hermit.trainer import rollout
    environments = Environments(tmp_path)
    session = environments.session

    @contextlib.contextmanager
    def breaking(task_dir, name=None, installs=None):
        with session(task_dir, name) as env:
            yield env
        raise OSError(28, "No space left on device: the cleanup lock")

    environments.session = breaking
    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo"})
    out = rollout.environment_rollout(task, task.given(), None, lambda t, a, environment=None: RunOutput(
        output="x", cost_usd=0.5, tokens=100), environments, 0.0)
    assert out.extra["verified"]["reward"] == 1.0 and out.cost_usd == 0.5      # the verdict and its spend kept

    def host(t, a, environment=None):
        raise OSError(24, "Too many open files")

    out = rollout.environment_rollout(task, task.given(), None, host, Environments(tmp_path), 0.0,
                                      pause=lambda s: None)
    assert out.extra["unscored"] == "host_failed" and len(out.extra.get("attempts", [1])) == 1


def test_a_lost_sign_in_stops_new_work_before_the_tasks_teardown(tmp_path):
    import contextlib
    import threading
    from hermit.agent.base.interface import LoginFailed
    from hermit.task import Task
    from hermit.trainer import rollout
    environments = Environments(tmp_path)
    environments.stopping = threading.Event()
    seen = []
    session = environments.session

    @contextlib.contextmanager
    def watched(task_dir, name=None, installs=None):
        try:
            with session(task_dir, name) as env:
                yield env
        finally:
            seen.append(environments.stopping.is_set())         # at the teardown: the run already ended

    environments.session = watched

    def lost(t, a, environment=None):
        raise LoginFailed("agent codex: 401")

    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo"})
    with pytest.raises(LoginFailed):
        rollout.environment_rollout(task, task.given(), None, lost, environments, 0.0)
    assert seen == [True]


def test_a_minimal_rows_context_is_computed_from_harbors_instruction(tmp_path, monkeypatch):
    from hermit.agent.acting import workspace
    asked = []
    monkeypatch.setattr(workspace, "_deploy", lambda task, *a, **k: asked.append(task) or
                        (_ for _ in ()).throw(KeyError("stop here")))
    env = Recorded(tmp_path)
    env.instruction = "What is 6 x 7?"
    workspace.CliAgent({"backend": "codex", "web_search": False, "bin": "codex", "workspaces": str(tmp_path / "w")},
                       tmp_path).run({"id": "t#1", "harbor_task": "t"}, tmp_path, environment=env)
    assert asked[0]["instruction"] == "What is 6 x 7?"


def test_rrsi_sees_a_harbor_rollouts_failure_and_a_cli_model_is_refused_for_harbor(tmp_path):
    from types import SimpleNamespace
    from hermit import isolate
    from hermit.agent.evolver.methods.rrsi_adapter.domain import AdapterDomain
    domain = AdapterDomain.__new__(AdapterDomain)
    assert domain.smoke_failure({"output": "done", "outcome": {"status": "agent_error"}}) is True
    assert domain.smoke_failure({"output": "done", "outcome": {"status": "ok"}}) is False
    (tmp_path / "tasks").mkdir()
    data = {"environment": {"harbor": {"tasks_dir": "tasks", "harbor": sys.executable}},
            "agent": {"params": {"model": {"cli": "codex", "model": "m", "login": "chatgpt"}}}}
    with pytest.raises(ValueError, match="a model a CLI reaches"):
        isolate.task_environments(SimpleNamespace(data=data, base=tmp_path))


def test_a_run_that_ended_while_the_disk_gate_held_a_task_starts_nothing(harbor, monkeypatch):
    """The run stopped (another task's lost sign-in) while this task waited at the disk gate, or between its gates:
    no driver, no container; the rollout ends as the stopped run's (LoginFailed), not as an infra failure."""
    environments, _, calls = harbor

    def gate(where, gate_gb, wait, **kw):
        calls["gate"].append(str(where))
        environments.stopping.set()                     # the stop arrives while it waits; room is found anyway
        return True

    monkeypatch.setattr(harbor_mod.docker, "wait_for_disk", gate)
    with pytest.raises(EnvironmentUnavailable, match="run ended") as raised:
        environments.open(environments.task_dir({"harbor_task": "demo"})).start()
    assert raised.value.cause == "stopped" and calls["gate"] == ["/tmp"]          # the second gate never entered
    assert not (environments.jobs_dir).exists() or not any(environments.jobs_dir.rglob("requests.jsonl"))
    assert not environments.live


def _prompt_adapter(tmp_path, text):
    a = tmp_path / "adapter"
    (a / ".hermit").mkdir(parents=True)
    (a / ".hermit" / "adapter.yaml").write_text("template: {id: big, version: '1'}\n"
                                                "structure: {learnable: [ADAPTER.md]}\n")
    (a / "exports.yaml").write_text("{context: ADAPTER.md}\n")
    (a / "ADAPTER.md").write_text(text)
    return a


def test_codex_in_a_tasks_environment_keeps_the_runners_256k_instruction_allowance(tmp_path, monkeypatch):
    """A 35,000-byte prompt-only adapter (the template allows 40,000) runs as it ran in the Harbor runner, whose Codex
    took project_doc_max_bytes = 262144: deployed, checked against that cap, and the session's config says it."""
    from hermit.agent.acting.workspace import CliAgent
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    env = Recorded(tmp_path, lambda c: ExecResult(0, CODEX_STREAM) if session_of(c) else
                   ExecResult(1, "") if c[:2] == ["test", "-e"] else ExecResult(0, ""))
    adapter = _prompt_adapter(tmp_path, "Be careful.\n" * 2917)               # 35,004 bytes
    out = CliAgent({"backend": "codex", "web_search": False, "bin": "codex", "workspaces": str(tmp_path / "ws")},
                   tmp_path).run({"id": "t", "instruction": "do it"}, adapter, environment=env)
    assert out.output == "fixed it" and "adapter_failed" not in out.extra
    assert len(env.uploads["/app/AGENTS.md"].encode()) > 32768
    assert "project_doc_max_bytes = 262144" in env.uploads["/installed-agent/hermit-home"]["config.toml"]


def test_a_hook_audit_that_cannot_be_read_keeps_the_finished_session_and_its_usage(tmp_path, monkeypatch):
    from hermit.agent.acting import workspace
    from hermit.agent.acting.workspace import CliAgent
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    real = workspace._deploy
    monkeypatch.setattr(workspace, "_deploy", lambda *a, **k: ({**real(*a, **k)[0], "hooks": True}, None))
    monkeypatch.setattr(workspace, "_ensure_python3", lambda env: True)
    monkeypatch.setattr(workspace, "_collect_hooks", lambda env, log: (_ for _ in ()).throw(
        RuntimeError("docker cp failed")))
    env = Recorded(tmp_path, lambda c: ExecResult(0, CODEX_STREAM) if session_of(c) else ExecResult(0, ""))
    out = CliAgent({"backend": "codex", "web_search": False, "bin": "codex", "workspaces": str(tmp_path / "ws")},
                   tmp_path).run({"id": "t", "instruction": "do it"}, None, environment=env)
    assert out.output == "fixed it" and out.tokens == 15 and "docker cp failed" in out.extra["hooks_audit_missing"]
    assert "infra" not in out.extra                                              # the verifier decides the rest


def test_the_benchmark_file_form_takes_nested_overrides_and_a_remote_cli_is_not_the_hosts(tmp_path, monkeypatch):
    from hermit.agent.base import login
    from hermit.config import Config
    import yaml
    folder = Path(__file__).resolve().parents[2] / "examples" / "terminal_bench_2_0" / "baseline"
    run = yaml.safe_load((folder / "codex.yaml").read_text())
    run["data"] = {k: str((folder / v).resolve()) for k, v in run["data"].items()}
    run["task_info"] = str((folder / run["task_info"]).resolve())
    benchmark = str((folder / "../utils/benchmark.yaml").resolve())
    run["environment"] = {"harbor": benchmark}                                   # the file alone
    (tmp_path / "run.yaml").write_text(yaml.safe_dump(run))
    config = Config.load(tmp_path / "run.yaml", ["environment.harbor.infra_retries=0"])
    assert config.data["environment"]["harbor"] == {"benchmark": benchmark, "infra_retries": 0}
    data = {"environment": {"harbor": {"benchmark": "b.yaml"}},
            "agent": {"params": {"login": "claude", "bin": "/opt/hermit-claude/claude"}}}
    ((who, _, program, contained, _),) = login.logins_of(data)
    assert who == "agent" and program == "/opt/hermit-claude/claude" and contained is True
    monkeypatch.setenv(login.TOKEN_ENV, "t")
    assert login.problems_of("claude", program, contained) == []                  # not looked for on this machine


def test_the_answer_agent_scores_its_adapters_failure_retries_its_environments_and_delivers_in_time(tmp_path,
                                                                                                    monkeypatch):
    import time
    from hermit.agent.acting import answer as answer_mod
    from hermit.agent.model.usage import Usage
    from hermit.environment.harbor_errors import pipeline_cause

    class Model:
        model = "m"

        def chat(self, messages, abort=None, **kwargs):
            return {"role": "assistant", "content": "<answer>B</answer>"}, Usage(model="m", calls=1, cost_usd=0.01)

    monkeypatch.setattr(answer_mod.ChatClient, "from_spec", staticmethod(lambda spec: Model()))
    params = {"model": {"base_url": "https://x", "model": "m"}, "logs": str(tmp_path / "l")}

    class Failing:
        last_context = None

        def context(self, **kw):
            raise ValueError("the context tool broke")

        def close(self):
            pass

    agent = answer_mod.AnswerAgent(params, tmp_path)
    monkeypatch.setattr(agent, "session", lambda adapter: Failing())
    env = Recorded(tmp_path)
    env.instruction = "Which?"
    out = agent.run({"id": "q"}, tmp_path, environment=env)
    assert out.extra["adapter_failed"] == "adapter_failed" and out.output is None      # the agent's result: scored 0
    gone = Recorded(tmp_path, lambda c: ExecResult(1, "no such file"))
    with pytest.raises(RuntimeError) as raised:
        answer_mod.AnswerAgent({**params, "instruction_file": "/app/q.txt", "wait_s": 0.01}, tmp_path).run(
            {"id": "q"}, None, environment=gone)
    assert pipeline_cause(f"RuntimeError: {raised.value}") == ("harbor_failed", True)  # run again, a fresh one

    class Late(Recorded):                           # the answer's upload outlasts the budget
        def upload(self, source, target):
            time.sleep(0.4)
            return super().upload(source, target)

    late = Late(tmp_path)
    late.limits, late.instruction = Limits(timeout_s=0.2), "Which?"
    out = answer_mod.AnswerAgent(params, tmp_path).run({"id": "q"}, None, environment=late)
    assert out.extra["timed_out"] is True and "/app/answer.txt" not in late.uploads
    on_time = Recorded(tmp_path)
    on_time.instruction = "Which?"
    answer_mod.AnswerAgent(params, tmp_path).run({"id": "q"}, None, environment=on_time)
    assert on_time.uploads == {"/app/answer.txt": "<answer>B</answer>"}                  # moved into place


def test_react_keeps_the_budget_through_the_answer_tag_follow_up_and_counts_an_abandoned_call(tmp_path, monkeypatch):
    from hermit.agent.acting.react import ReactAgent
    from hermit.agent.model import llm
    from hermit.agent.model.llm import ChatAborted
    from hermit.agent.model.usage import Usage
    calls = []

    class Model:
        model = "m"

        def chat(self, messages, abort=None, **kwargs):
            calls.append(abort)
            if len(calls) == 1:                         # an answer without the tag: the follow-up is asked
                return {"role": "assistant", "content": "B"}, Usage(model="m", input_tokens=10, calls=1, cost_usd=0.1)
            if abort is None or not abort.wait(10):
                raise AssertionError("the follow-up was not bounded by the task's budget")
            raise ChatAborted("given up")

    monkeypatch.setattr(llm.ChatClient, "from_spec", staticmethod(lambda spec: Model()))
    env = Recorded(tmp_path)
    env.limits = Limits(timeout_s=0.3)
    out = ReactAgent({"model": {"base_url": "https://x", "model": "m"}, "logs": str(tmp_path / "l"),
                      "answer_tag": "FINAL"}, tmp_path).run({"id": "t", "instruction": "go"}, None, environment=env)
    assert len(calls) == 2 and out.extra["timed_out"] is True
    assert out.cost_usd is None and out.api_speed["model_calls"] == 2 and out.tokens == 10   # its cost not known


def test_a_cli_agent_declares_what_harbors_setup_installs_and_the_phase_installs_nothing(harbor, tmp_path):
    """Agent.installs: the CLI (unless `bin` names one there; probed first unless a version is pinned) and python3
    for an adapter's hooks, which the run hands the environment's setup; during the phase install() only reports
    what setup installed, and refuses anything else."""
    from hermit.agent.acting.workspace import CliAgent
    from hermit.agent.acting.agent import Agent
    assert Agent().installs(None) == []                                      # an agent that needs nothing
    codex = CliAgent({"backend": "codex", "version": "0.162.0"}, tmp_path).installs(None)
    assert codex[0]["agent"] == "codex" and codex[0]["version"] == "0.162.0" and "command -v codex" in codex[0]["probe"]
    mounted = CliAgent({"backend": "claude", "bin": "/opt/claude"}, tmp_path).installs(None)
    assert mounted == [{"agent": "claude-code", "version": None, "bin": "/opt/claude", "program": "claude"}]
    environments, _, _ = harbor
    with environments.session(environments.task_dir({"harbor_task": "demo"}), installs=codex) as env:
        sent = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
        assert sent[0]["installs"] == codex
        env.started["installed"] = {"codex": "0.162.0"}
        assert env.install("codex", "0.162.0") == "0.162.0"
        with pytest.raises(Unsupported, match="not installed at the environment's setup"):
            env.install("claude-code")


def test_the_driver_scrubs_the_agents_credentials_from_the_logs_it_hands_the_verifier(tmp_path):
    import contextlib
    import shutil as shutil_mod
    import uuid
    import stat as stat_mod
    copy = _driver_function("safe_copy", os=os, shutil=shutil_mod, Path=Path, contextlib=contextlib, uuid=uuid,
                            stat=stat_mod,
                            scrubbed=_driver_function("scrubbed"))
    logs, shared = tmp_path / "logs", tmp_path / "agent"
    logs.mkdir()
    shared.mkdir()
    (logs / "agent-codex.jsonl").write_text(f"401: bad key {AGENT_KEY}\n")
    copy(logs, shared, {AGENT_KEY})
    assert (shared / "agent-codex.jsonl").read_text() == "401: bad key [REDACTED]\n"


def test_harbor_settings_redact_the_verifier_and_take_the_named_reward_only(harbor, tmp_path):
    environments, _, _ = harbor
    with pytest.raises(ValueError, match="verifier_redact"):
        HarborEnvironments({"tasks_dir": str(tmp_path / "tasks"), "harbor": sys.executable,
                            "verifier_redact": ["("]}, base=tmp_path)
    redacting = HarborEnvironments({"tasks_dir": str(tmp_path / "tasks"), "harbor": sys.executable,
                                    "reward_key": "accuracy", "verifier_redact": [r"expected \d+"]}, base=tmp_path)
    out = tmp_path / "verifier"
    out.mkdir()
    (out / "test-stdout.txt").write_text("FAILED: expected 44463, got 12\n")
    verdict = redacting.verdict({"rewards": {"accuracy": 0.0}, "verifier_dir": str(out)})
    assert "44463" not in verdict["feedback"] and "[redacted]" in verdict["feedback"]
    missing = redacting.verdict({"rewards": {"f1": 0.85, "answered": 1}, "verifier_dir": str(out)})
    assert (missing["status"], missing["failure_cause"], missing["reward"]) == ("infra", "verifier_failed", None)


def test_an_adapter_failure_on_a_scale_benchmark_has_no_pass(tmp_path):
    from hermit.trainer.rollout import _read
    from hermit.task import Task
    env = Recorded(tmp_path)
    env.owner = type("Owner", (), {"pass_reward": None})()
    out = _read(RunOutput(output=None, extra={"adapter_failed": "adapter_failed", "exception_message": "x"}), env,
                Task(id="t", public={}), 0.0)
    assert out.extra["verified"]["passed"] is None


def test_the_sandboxed_agent_uses_the_configured_chatgpt_login_root(tmp_path):
    from hermit import isolate
    from hermit.agent.base import login as codex_login
    agent = isolate.SandboxedAgent.__new__(isolate.SandboxedAgent)
    agent.params = {"login_root": str(tmp_path / "root")}
    assert agent._login_root() == tmp_path / "root"
    agent.params = {}
    assert agent._login_root() == codex_login.ROOT


def test_a_refreshed_sign_in_that_is_no_valid_one_never_replaces_the_shared_copy(tmp_path, monkeypatch):
    from hermit.agent.base import codex_stream, login
    from hermit.agent.base.interface import make
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    kept = []
    monkeypatch.setattr(login, "prepare", lambda root, home: (Path(home) / login.FILE).write_text(ROTATED))
    monkeypatch.setattr(login, "keep_refreshed", lambda root, home: kept.append((Path(home) / login.FILE).read_text()))
    monkeypatch.setattr(login, "note_usage", lambda *a, **k: None)
    no_tokens = json.dumps({"tokens": {"account_id": "acc"}, "last_refresh": "2026-11-19T00:00:00Z"})

    class Env(Recorded):
        def download(self, source, target):
            if source.endswith(login.FILE):
                Path(target).write_text(no_tokens)
                return Path(target)
            raise FileNotFoundError(source)

    env = Env(tmp_path, lambda c: ExecResult(0, CODEX_STREAM) if session_of(c) else
              ExecResult(0, "100\n") if "wc -c" in str(c[-1]) else ExecResult(0, ""))
    env.plan_admitted = True
    make("codex", {"web_search": False, "login": "chatgpt", "model": "m", "bin": "codex",
                   "plan_dir": str(tmp_path / "plan"), "login_root": str(tmp_path / "root")}).run("x", environment=env)
    assert kept == [ROTATED]                                     # the session's own copy, not the file it handed back
    assert not codex_stream.signed_in(b"x" * 10, 60) and codex_stream.regular_bytes(tmp_path / "none", 10) is None
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (codex_stream.SIGNIN_MAX_BYTES + 1))
    assert codex_stream.regular_bytes(big, codex_stream.SIGNIN_MAX_BYTES) is None


def test_a_long_codex_rejection_keeps_its_status_for_the_verdict(tmp_path):
    from hermit.agent.base.cli import Codex
    from hermit.agent.base.codex_stream import session_verdict
    raw = json.dumps({"type": "turn.failed", "error": {"message": "unexpected status 402 Payment Required: "
                                                                 + "proxy said " * 200}}) + "\n"
    agent = Codex({"web_search": False})
    agent.environment = Recorded(tmp_path)
    stream = tmp_path / "s.jsonl"
    stream.write_text(raw)
    said = agent.outcome(raw, stream, tmp_path / "none")
    assert said["failure"].startswith("unexpected status 402") or "402" in said["failure"][:100]
    assert session_verdict({**said, "served": False}, 0) == "usage_limit"


def test_a_session_cut_off_before_its_usage_reports_an_unknown_cost(tmp_path, monkeypatch):
    from hermit.agent.acting.workspace import CliAgent
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    started = json.dumps({"type": "thread.started", "thread_id": "t"}) + "\n" + json.dumps({"type": "turn.started"})
    env = Recorded(tmp_path, lambda c: ExecResult(1, started) if session_of(c) else ExecResult(0, ""))
    out = CliAgent({"backend": "codex", "web_search": False, "bin": "codex", "workspaces": str(tmp_path / "ws")},
                   tmp_path).run({"id": "t", "instruction": "x"}, None, environment=env)
    assert out.cost_usd is None                                  # never a known 0


def test_a_refused_session_whose_hold_cannot_be_written_pauses_before_the_next_environment(tmp_path, monkeypatch):
    from hermit.agent.base import login
    monkeypatch.setattr(login, "_hold", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left")))
    assert login.hold(tmp_path / "plan", max_s=0.0) is False
    from hermit.task import Task
    from hermit.task.verify import environment as judge
    from hermit.trainer import rollout
    pauses, runs = [], []

    def pipeline(task, adapter, environment=None):
        runs.append(1)
        if len(runs) == 1:
            return RunOutput(output=None, extra={"infra": "usage_limit", "infra_final": False,
                                                 "exception_message": "refused", "hold_unwritten": True})
        return RunOutput(output="done")

    monkeypatch.setattr(rollout, "_sleep", pauses.append)
    environments = Environments(tmp_path)
    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo"})
    record = rollout.run_one(task, None, pipeline, judge, environments)
    assert len(runs) == 2 and pauses == [login.HOLD_UNWRITTEN_S] and record["judge"]["score"] == 1.0


def test_the_answer_agent_reads_patterns_and_token_limits_as_the_runner(tmp_path, monkeypatch):
    from hermit.agent.acting import answer as answer_mod
    from hermit.agent.model.usage import Usage
    sent = []

    class Model:
        model = "m"

        def chat(self, messages, abort=None, **kwargs):
            sent.append(kwargs)
            return {"role": "assistant", "content": "yes"}, Usage(model="m", calls=1, cost_usd=0.01)

    monkeypatch.setattr(answer_mod.ChatClient, "from_spec", staticmethod(lambda spec: Model()))
    params = {"model": {"base_url": "https://x", "model": "m"}, "logs": str(tmp_path / "l")}

    env = Recorded(tmp_path)
    env.instruction = "Which?"
    answer_mod.AnswerAgent(params, tmp_path).run({"id": "q"}, None, environment=env)
    assert "max_tokens" not in sent[0]                           # none given: none sent (the runner's)
    agent = answer_mod.AnswerAgent({**params, "answer_pattern": r"yes(?:=(\w+))?"}, tmp_path)
    assert agent.answer("yes") == "yes" and agent.answer("yes=B") == "B"


def test_a_dead_driver_wakes_every_request_at_once(tmp_path):
    """The reader publishes the death under the lock a request registers under: no request waits its watchdog."""
    import contextlib
    import threading
    import time
    from hermit.environment.harbor import _Driver
    script = tmp_path / "dies.py"
    script.write_text("import sys, time\nsys.stdin.readline()\ntime.sleep(0.2)\n")
    original = harbor_mod.DRIVER
    harbor_mod.DRIVER = script
    try:
        driver = _Driver(sys.executable, tmp_path / "driver.log", dict(os.environ))
        ends = []

        def ask():
            started = time.time()
            with contextlib.suppress(EnvironmentUnavailable, RuntimeError):
                driver.request("exec", 30, command="x")
            ends.append(time.time() - started)

        threads = [threading.Thread(target=ask) for _ in range(20)]
        for t in threads:
            t.start()
            time.sleep(0.02)
        for t in threads:
            t.join(timeout=40)
        assert len(ends) == 20 and max(ends) < 10
        driver.close(5)
    finally:
        harbor_mod.DRIVER = original


def test_the_cli_agent_refuses_limits_given_with_the_environment(tmp_path):
    from hermit.agent.acting.workspace import CliAgent
    env = Recorded(tmp_path)
    env.limits = Limits(timeout_s=600, network=[])
    with pytest.raises(Unsupported, match="network limit"):
        CliAgent({"backend": "codex", "web_search": False, "bin": "codex", "workspaces": str(tmp_path / "ws")},
                 tmp_path).run({"id": "t", "instruction": "x"}, None, environment=env)


# ---- the single implementations ----------------------------------------------------------------------------------

def test_the_usage_ledger_sums_once_with_unknown_costs_and_empty_ledgers_as_the_caller_says():
    from hermit.agent.model.usage import Attempts, Ledger, Usage
    ledger = Ledger("m").add([Usage(model="m", input_tokens=10, output_tokens=5, calls=1, cost_usd=0.1)])
    assert (ledger.cost(), ledger.tokens(), ledger.calls()) == (0.1, 15, 1)
    ledger.unreported(1)                                 # a call given up in flight: counted, its cost unknown
    assert ledger.cost() is None and ledger.calls() == 2 and ledger.total().unreported_calls == 1
    assert Ledger().cost(0.0) == 0.0 and Ledger().tokens(None) is None             # nothing ran / nothing known
    cut = Ledger("m").unreported(0, sessions=1)        # a session cut off before any usage: unknown, never 0
    assert (cut.tokens(0), cut.cost(0.0)) == (None, None)
    attempts = Attempts()
    for tokens, cost, cause in ((10, 0.1, "sandbox_failed"), (None, None, None)):
        attempts.add(RunOutput(output=None, tokens=tokens, cost_usd=cost, api_speed={"model_calls": 1},
                               extra={"unscored": cause}))
    out = attempts.output()
    assert (out.tokens, out.cost_usd, out.api_speed["model_calls"]) == (10, None, 2)
    assert [a["unscored"] for a in out.extra["attempts"]] == ["sandbox_failed", None]


def test_a_credential_is_taken_back_and_removed_however_the_session_ends():
    from hermit.agent.base.credentials import held
    seen = []
    with pytest.raises(KeyError):
        with held(lambda: ["copy"], lambda c: seen.append(("back", c)),
                  lambda c: (seen.append(("removed", c)), (_ for _ in ()).throw(RuntimeError("rm failed")))):
            raise KeyError("the session failed")         # its error stands; the removal's is printed
    assert seen == [("back", ["copy"]), ("removed", ["copy"])]
    with pytest.raises(RuntimeError, match="rm failed"):     # a session that ended well: the removal's error raised
        with held(lambda: [], None, lambda c: (_ for _ in ()).throw(RuntimeError("rm failed"))):
            pass
    seen.clear()
    with pytest.raises(OSError):                             # a take-back that fails never keeps the copies
        with held(lambda: ["c"], lambda c: (_ for _ in ()).throw(OSError("full")), lambda c: seen.append(c)):
            pass
    assert seen == [["c"]]


def test_one_deadline_cuts_limits_and_ends_model_calls():
    import time
    from hermit.agent.base.deadline import Deadline
    from hermit.agent.model.llm import ChatAborted
    assert Deadline().cut(30) == 30 and Deadline().remaining() is None
    assert Deadline(time.time() - 1).cut(30) is False
    assert 9 < Deadline.within(10).cut(30) <= 10

    class Slow:
        model = "m"

        def chat(self, messages, abort=None, **kw):
            abort.wait(5)
            raise ChatAborted("given up")

    bound = Deadline.within(0.2).bound(Slow())
    with pytest.raises(ChatAborted):
        bound.chat([])
    assert bound.expired and bound.abandoned == 1
    bound.close()


def test_the_retry_state_machine_names_each_state():
    from types import SimpleNamespace
    from hermit.trainer import rollout
    envs = SimpleNamespace(infra_retries=1, busy_retries=1, busy_wait_s=120.0, infra_wait_s=300.0, stopping=None)
    out = lambda cause, **extra: RunOutput(output=None, extra={"unscored": cause, **extra})
    machine = rollout.Retries(envs)
    assert machine.next(out(None), False, False) == (rollout.DONE, 0.0)
    assert machine.next(out("model_provider_failed"), False, True) == (rollout.PROVIDER_BUSY, 120.0)
    assert machine.next(out("model_provider_failed"), False, True) == (rollout.DONE, 0.0)   # its allowance spent
    assert machine.next(out("usage_limit", hold_unwritten=True), False, True)[0] == rollout.PLAN_WAIT
    assert machine.next(out("sandbox_failed"), False, False) == (rollout.INFRA, 300.0)
    assert machine.next(out("sandbox_failed"), False, False) == (rollout.DONE, 0.0)          # infra_retries spent
    assert rollout.Retries(envs).next(out("disk"), False, False) == (rollout.DONE, 0.0)      # runs no better again
    import threading
    stopped = threading.Event()
    stopped.set()
    assert rollout.Retries(SimpleNamespace(**{**vars(envs), "stopping": stopped})).next(
        out("sandbox_failed"), False, False) == (rollout.STOP, 0.0)


@pytest.mark.skipif(not HARBOR_PYTHON.exists(), reason="needs Harbor's venv (~/.cache/hermit/harbor-venv)")
def test_the_driver_stops_unblocked_kills_cancelled_clients_and_keeps_a_verdict_it_could_not_write(tmp_path):
    """In Harbor's interpreter: a stop is never behind a start waiting on its trial; a client whose wait is cancelled
    is killed (a compose up never outlives the teardown); a hook package that fails to install leaves the setup
    going; a verdict whose result.json could not be written comes from the trial itself; Harbor's refusal of a
    Windows task at the trial's creation is UnsupportedTask."""
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent(f"""
        import asyncio, importlib.util, json, sys
        from pathlib import Path
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location("drv", {str(DRIVER_FILE)!r})
        drv = importlib.util.module_from_spec(spec); sys.modules["drv"] = drv; spec.loader.exec_module(drv)
        replies = []

        class Out:
            def write(self, s): replies.append(json.loads(s))
            def flush(self): pass

        async def main():
            out = []
            d = drv.DRIVER = drv.Driver(Out())
            async with d.lock:                           # a start holds the lock, waiting on its trial
                await asyncio.wait_for(d.handle({{"id": 7, "op": "stop"}}), 5)
            out.append(replies[-1]["id"] == 7 and replies[-1]["ok"])
            live = d.run_task = asyncio.ensure_future(asyncio.sleep(30)); await asyncio.sleep(0)
            await asyncio.wait_for(d.stop(), 5)          # a live trial (a start watchdog): cancelled, nothing raised
            out.append(live.cancelled())
            d.run_task = None
            async def waits():
                p = await asyncio.create_subprocess_exec("sleep", "30", stdout=asyncio.subprocess.PIPE)
                held.append(p)
                await p.communicate()
            held = []
            t = asyncio.create_task(waits()); await asyncio.sleep(0.3); t.cancel()
            await asyncio.gather(t, return_exceptions=True)
            await asyncio.sleep(0.2)
            out.append(held[0].returncode is not None)
            class Failing:
                async def ensure_system_dependencies(self, env, packages): raise RuntimeError("mirror down")
            d.installs, d.repaired = [{{"packages": ["python3"]}}], True
            d._installer = lambda name, version=None: Failing()
            await d.setup(SimpleNamespace())
            out.append("python3" not in d.installed)
            class Result:
                verifier_result = None
                def model_dump_json(self):
                    return json.dumps({{"verifier_result": {{"rewards": {{"reward": 1.0}}}}, "exception_info": None}})
            async def raising(): raise OSError(28, "No space left on device")
            d.run_task = asyncio.ensure_future(raising()); await asyncio.sleep(0)
            base = Path({str(tmp_path)!r})
            paths = SimpleNamespace(trial_dir=base / "t", result_path=base / "none", agent_dir=base / "t" / "agent",
                                    verifier_dir=Path({str(tmp_path)!r}) / "v")
            paths.trial_dir.mkdir()
            d.trial = SimpleNamespace(paths=paths, result=Result(), task=SimpleNamespace(config=None))
            drv.resolve_task_verifier_mode = lambda config: SimpleNamespace(value="shared")
            verdict = await d.done_op({{}})
            out.append(verdict["rewards"])
            async def refuse(config): raise ValueError("Windows containers are not supported by e2b")
            drv.Trial = SimpleNamespace(create=refuse)
            e = drv.Driver(Out())
            try:
                await e.start({{"task_dir": "/x", "trials_dir": "/y", "trial_name": "z"}})
            except drv.Failed as exc:
                out.append(exc.kind)
            print("RESULT", out)
        asyncio.run(main())
    """))
    done = subprocess.run([str(HARBOR_PYTHON), "-I", str(script)], capture_output=True, text=True, timeout=120)
    line = next((x for x in done.stdout.splitlines() if x.startswith("RESULT")), done.stdout + done.stderr)
    assert line == "RESULT " + str([True, True, True, True, {"reward": 1.0}, "UnsupportedTask"]), line


def test_round_6_hermit_side(tmp_path, monkeypatch):
    """A verifier that failed after the agent's timeout is read by its infra policy; a disabled verifier is refused;
    the claude-code alias installs Claude Code; an adapter whose exports do not parse still declares its CLI (the
    deployment scores it); a deployment Harbor's phase end cut short is not the adapter's failure."""
    from hermit.agent.acting.workspace import CliAgent, installs
    harbor_settings = {"tasks_dir": str(tmp_path), "harbor": sys.executable}
    envs = HarborEnvironments({**harbor_settings, "verifier_timeout": "infra"}, base=tmp_path)
    verdict = envs.verdict({"rewards": {"reward": 0.0}, "exception": {"type": "AgentTimeoutError", "message": "t"},
                            "later": [{"type": "VerifierTimeoutError", "message": "slow"}]})
    assert verdict["status"] == "infra"
    kept = envs.verdict({"rewards": {"reward": 0.0}, "exception": {"type": "AgentTimeoutError", "message": "t"},
                         "later": []})
    assert kept["status"] == "timeout"
    with pytest.raises(ValueError, match="verifier.disable"):
        HarborEnvironments({**harbor_settings, "verifier": {"disable": True}}, base=tmp_path)
    assert installs({"backend": "claude-code"}, None)[0]["agent"] == "claude-code"
    bad = tmp_path / "bad"
    (bad / ".hermit").mkdir(parents=True)
    (bad / ".hermit" / "adapter.yaml").write_text("template: {id: bad, version: '1'}\nstructure: {}\n")
    (bad / "exports.yaml").write_text("context: [unclosed\n")
    assert installs({"backend": "codex"}, bad)[0]["agent"] == "codex"
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))

    class Cut(Recorded):
        def upload(self, source, target):
            raise RuntimeError("AgentPhaseOver: upload: the agent's phase ended")

    with pytest.raises(RuntimeError, match="AgentPhaseOver"):
        CliAgent({"backend": "codex", "web_search": False, "bin": "codex", "workspaces": str(tmp_path / "ws")},
                 tmp_path).run({"id": "t", "instruction": "x"}, _prompt_adapter(tmp_path, "be careful\n"),
                               environment=Cut(tmp_path, lambda c: ExecResult(1, "") if c[:2] == ["test", "-e"]
                                               else ExecResult(0, "")))


def test_the_answer_agent_publishes_answer_json_in_its_log(tmp_path, monkeypatch):
    from hermit.agent.acting import answer as answer_mod
    from hermit.agent.model.usage import Usage

    class Model:
        model = "m"

        def chat(self, messages, **kwargs):
            return {"role": "assistant", "content": "B"}, Usage(model="m", input_tokens=3, output_tokens=1, calls=1)

    monkeypatch.setattr(answer_mod.ChatClient, "from_spec", staticmethod(lambda spec: Model()))
    env = Recorded(tmp_path)
    env.instruction = "Which?"
    out = answer_mod.AnswerAgent({"model": {"base_url": "https://x", "model": "m"}, "logs": str(tmp_path / "l"),
                                  "answer_file": None}, tmp_path).run({"id": "q"}, None, environment=env)
    published = json.loads((Path(out.log) / "answer.json").read_text())
    assert Path(out.log).is_dir() and published["answer"] == "B" and published["reply"] == "B"


def test_eval_checks_the_sign_in_before_it_replaces_results(tmp_path, monkeypatch):
    """The runner's check_agent: a Harbor eval on a Claude login with no token refuses before results.jsonl is
    opened (replaced) or any trial starts."""
    import yaml
    from hermit import evaluate as evaluate_mod
    from hermit.agent.base import login
    from hermit.config import Config
    folder = Path(__file__).resolve().parents[2] / "examples" / "terminal_bench_2_0" / "baseline"
    run = yaml.safe_load((folder / "claude.yaml").read_text())          # the migrated config, its data the test's
    (tmp_path / "tasks" / "t1").mkdir(parents=True)
    (tmp_path / "tasks.jsonl").write_text('{"id": "t1#1", "harbor_task": "t1"}\n')
    (tmp_path / "split.txt").write_text("t1#1\n")
    (tmp_path / "info").mkdir()
    (tmp_path / "info" / "README.md").write_text("A task set of the test's.\n")
    run["data"] = {"tasks": str(tmp_path / "tasks.jsonl"), "train": str(tmp_path / "split.txt"),
                   "test": str(tmp_path / "split.txt")}
    run["task_info"] = str(tmp_path / "info")
    run["environment"]["harbor"] = {"tasks_dir": str(tmp_path / "tasks")}
    run["trainer"] = {**run.get("trainer", {}), "output_dir": str(tmp_path / "runs")}
    (tmp_path / "run.yaml").write_text(yaml.safe_dump(run))
    config = Config.load(tmp_path / "run.yaml", [])
    monkeypatch.delenv(login.TOKEN_ENV, raising=False)
    monkeypatch.setattr(evaluate_mod, "agent_pipeline", lambda *a, **k: (None, lambda: None))
    monkeypatch.setattr(evaluate_mod, "judge_for", lambda *a, **k: None)
    monkeypatch.setattr(evaluate_mod, "task_environments", lambda *a, **k: object())
    with pytest.raises(SystemExit, match="eval refuses to start"):
        evaluate_mod.evaluate(config, "none", log=lambda m: None)
    assert not list((tmp_path / "runs").rglob("results.jsonl"))


@pytest.mark.skipif(not HARBOR_PYTHON.exists(), reason="needs Harbor's venv (~/.cache/hermit/harbor-venv)")
def test_a_mounted_codex_serves_only_as_the_asked_version_and_gets_rg_and_ca_roots(tmp_path):
    """The runner's _use_mounted and _after_mount, in the bridge's setup: a mounted binary that answers another
    version is not used (the CLI is installed instead); one that serves is linked, with the release's rg and this
    machine's CA bundle for an image that has none."""
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent(f"""
        import asyncio, importlib.util, sys
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location("drv", {str(DRIVER_FILE)!r})
        drv = importlib.util.module_from_spec(spec); sys.modules["drv"] = drv; spec.loader.exec_module(drv)

        class Env:
            def __init__(self, version): self.version, self.calls, self.uploads = version, [], []
            async def exec(self, command, user=None, **kw):
                self.calls.append(command)
                if "--version" in command:
                    return SimpleNamespace(return_code=0, stdout=f"hermit-version-begin\\ncodex-cli {{self.version}}"
                                                                 f"\\nhermit-version-end\\n", stderr="")
                if command.startswith("[ -s"):            # the image has no CA bundle
                    return SimpleNamespace(return_code=1, stdout="", stderr="")
                return SimpleNamespace(return_code=0, stdout="", stderr="")
            async def upload_file(self, source, target): self.uploads.append(target)

        class Installer:
            def parse_version(self, text): return text.split()[-1]

        async def main():
            d = drv.Driver(None)
            d._installer = lambda name, version=None: Installer()
            item = {{"agent": "codex", "version": "0.162.0", "bin": "/opt/hermit-codex/codex", "program": "codex"}}
            old = Env("0.170.0")
            served_old = await d._use_mounted(old, item)
            new = Env("0.162.0")
            served_new = await d._use_mounted(new, item)
            print("RESULT", [served_old, served_new, any("codex-path/rg" in c for c in new.calls),
                             new.uploads == ["/etc/ssl/certs/ca-certificates.crt"], d.installed.get("codex")])
        asyncio.run(main())
    """))
    done = subprocess.run([str(HARBOR_PYTHON), "-I", str(script)], capture_output=True, text=True, timeout=120)
    line = next((x for x in done.stdout.splitlines() if x.startswith("RESULT")), done.stdout + done.stderr)
    assert line == "RESULT " + str([False, True, True, True, "0.162.0"]), line


def test_the_cli_runs_the_installed_program_when_the_mounted_one_did_not_serve_and_claude_gets_no_raw_key(
        tmp_path, monkeypatch):
    from hermit.agent.base.interface import make
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    env = Recorded(tmp_path, lambda c: ExecResult(0, CODEX_STREAM) if session_of(c) else ExecResult(0, "/usr/bin/x"))
    env.bin_served = {"codex": False}
    make("codex", {"web_search": False, "bin": "/opt/hermit-codex/codex"}).run("x", environment=env)
    session = next(c for c in env.calls if session_of(c["command"]))
    assert session["command"][4] == "codex"                       # the installed program, not the mount
    monkeypatch.setenv("FAKE_KEY", "the-key")
    binding = {"ref": "fake/m", "backend": "fake", "model": "m", "base_url": "https://models.example/v1",
               "api_key_env": "FAKE_KEY", "key_required": True, "apis": ["messages"]}
    stream = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done",
                         "usage": {"input_tokens": 1, "output_tokens": 1}}) + "\n"
    claude_env = Recorded(tmp_path, lambda c: ExecResult(0, stream) if session_of(c) else ExecResult(0, "/x"))
    make("claude", {"web_search": False, "binding": binding, "model": "m", "bin": "claude"}).run(
        "hi", environment=claude_env)
    session = next(c for c in claude_env.calls if session_of(c["command"]))
    assert "FAKE_KEY" not in session["env"]                       # its key in its own variable (session_env)


def test_round_7_hermit_side(harbor, tmp_path, monkeypatch):
    """The driver never sees an ambient credential the run dropped; a stop Harbor acknowledged with its projects
    still there takes them down by name; every command's time comes off the phase's absolute deadline; the start's
    and a separate verifier's watchdogs follow Harbor's own budgets; eval checks the acting agent's sign-in only."""
    import time
    environments, root, calls = harbor
    seen = {}
    original = harbor_mod._Driver

    def capture(python, log_path, env):
        seen.update(env)
        return original(python, log_path, env)

    monkeypatch.setattr(harbor_mod, "_Driver", capture)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-token")
    environments.drop_env = {"CLAUDE_CODE_OAUTH_TOKEN"}
    monkeypatch.setattr(harbor_mod.docker, "compose_projects", lambda prefix: [prefix + "__env"])
    task = environments.task_dir({"harbor_task": "demo"})
    with environments.session(task) as env:
        env.deadline_at = time.time() + 5
        env.exec(["true"], timeout=100)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen
    sent = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert next(r for r in sent if r["op"] == "exec")["timeout_sec"] <= 5
    assert calls["down"]                                       # Harbor's down failed: by name
    (task / "task.toml").write_text('[environment]\ndocker_image = "demo:1"\nbuild_timeout_sec = 1800\n')
    scaled = HarborEnvironments({"tasks_dir": str(tmp_path / "tasks"), "harbor": sys.executable,
                                 "environment_build_timeout_multiplier": 3}, base=tmp_path)
    assert scaled.start_timeout(task) == 1800 * 3 + harbor_mod.SETUP_TIMEOUT_S + harbor_mod.START_ALLOWANCE_S
    from hermit import evaluate as evaluate_mod
    from hermit.agent.base import login
    monkeypatch.setattr(login, "logins_of", lambda d: [("llm", "claude", None, False, None),
                                                       ("agent", "chatgpt", None, True, None)])
    monkeypatch.setattr(login, "problems_of", lambda *a: [f"no {a[0]}"])
    assert evaluate_mod.sign_in_problems({}) == ["agent: no chatgpt"]          # never the learner's


def test_a_separate_verifiers_watchdog_includes_its_start_and_a_timed_out_rollout_keeps_its_stream(harbor, tmp_path):
    from hermit.task import Task
    from hermit.trainer import rollout
    environments, _, _ = harbor
    asked = []
    with environments.session(environments.task_dir({"harbor_task": "demo"})) as env:
        env.started.update(verifier_timeout_sec=60.0, verifier_mode="separate", build_timeout_sec=2700.0)
        original = env.driver.request
        env.driver.request = lambda op, timeout, **f: asked.append((op, timeout)) or original(op, timeout, **f)
        env.verify()
    assert ("done", 60.0 + 2700.0 + harbor_mod.VERIFY_ALLOWANCE_S) in asked
    kept = tmp_path / "trial-agent"
    kept.mkdir()
    (kept / "agent-codex.jsonl").write_text(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 200}}) + "\n")
    timed = Environments(tmp_path, verdicts=[{"reward": 0.0, "rewards": {"reward": 0.0}, "status": "timeout",
                                              "failure_cause": "agent_exhausted_budget", "passed": False,
                                              "feedback": "", "agent_dir": str(kept)}])

    def phase_over(task, adapter, environment=None):
        raise RuntimeError("AgentPhaseOver: exec: the agent's phase ended")

    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo"})
    from hermit.agent.acting.workspace import CliAgent
    timed.usage_of = CliAgent({"backend": "codex", "model": "m", "web_search": False,
                               "price": {"input": 1.0, "output": 2.0}},
                              tmp_path).usage_of
    out = rollout.environment_rollout(task, task.given(), None, phase_over, timed, 0.0, pause=lambda s: None)
    assert Path(out.log) == kept                                   # the stream the bridge kept, for the learner
    # what the CLI had reported before Harbor's timeout cut it off is counted (the runner parsed the kept stream)
    assert out.tokens == 1200 and out.api_speed["model_calls"] == 1 and out.cost_usd == pytest.approx(0.0014)


def test_a_pipeline_error_on_a_run_again_keeps_what_the_runs_before_it_spent(tmp_path):
    from hermit.task import Task
    from hermit.trainer import rollout
    runs = []

    def pipeline(task, adapter, environment=None):
        runs.append(1)
        if len(runs) == 1:
            return RunOutput(output=None, tokens=2200, cost_usd=0.1, api_speed={"model_calls": 1},
                             extra={"infra": "model_provider_failed", "infra_final": False, "exception_message": "x"})
        raise KeyError("a bug of the agent's own")

    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo"})
    record = rollout.environment_rollout(task, task.given(), None, pipeline, Environments(tmp_path), 0.0,
                                         pause=lambda s: None)
    assert record["status"] == "error" and record["tokens"] == 2200 and record["cost_usd"] is None
    assert record["api_speed"]["model_calls"] == 1 and record["pipeline_extra"]["attempts"]


def test_an_attachment_over_the_limit_is_measured_there_and_never_read(tmp_path, monkeypatch):
    from hermit.agent.acting import answer as answer_mod
    from hermit.agent.model.usage import Usage
    asked = []

    class Model:
        model = "m"

        def chat(self, messages, **kwargs):
            asked.append(messages)
            return {"role": "assistant", "content": "B"}, Usage(model="m", calls=1)

    monkeypatch.setattr(answer_mod.ChatClient, "from_spec", staticmethod(lambda spec: Model()))

    def answer(c):
        if "wc -c" in c[-1]:
            return ExecResult(0, f"{answer_mod.MAX_ATTACHMENT_BYTES + 1}\n")
        if "base64" in c[-1]:
            raise AssertionError("an image over the limit is never read")
        return ExecResult(0, "/app/big.png\n")

    env = Recorded(tmp_path, answer)
    env.instruction = "Which?"
    answer_mod.AnswerAgent({"model": {"base_url": "https://x", "model": "m"}, "logs": str(tmp_path / "l"),
                            "attachments": ["/app/*.png"]}, tmp_path).run({"id": "q"}, None, environment=env)
    assert isinstance(asked[0][-1]["content"], str)                 # sent without it


def test_round_8_hermit_side(harbor, tmp_path, monkeypatch):
    environments, root, calls = harbor
    # 6: the binding's key variable, whatever its name, reaches the driver as a credential to scrub
    with environments.session(environments.task_dir({"harbor_task": "demo"})) as env:
        env.exec(["true"], env={"LLM_BEARER": "a-credential-of-any-name"}, secret_env=["LLM_BEARER"])
    sent = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert next(r for r in sent if r["op"] == "exec")["secret_env"] == ["LLM_BEARER"]
    # 11: the deadline is the phase's as the bridge saw it begin
    monkeypatch.setenv("FAKE_ELAPSED", "100")
    import time
    with environments.session(environments.task_dir({"harbor_task": "demo"})) as env:
        assert abs(env.deadline_at - (time.time() + 900 - 100 - 60)) < 5
    # 15: Harbor told to keep its containers: never taken down by name
    kept = HarborEnvironments({"tasks_dir": str(tmp_path / "tasks"), "harbor": sys.executable,
                               "environment": {"kwargs": {"keep_containers": True}}}, base=tmp_path)
    assert kept.local_docker is False
    # 7: the exception's own message redacted too
    redacting = HarborEnvironments({"tasks_dir": str(tmp_path / "tasks"), "harbor": sys.executable,
                                    "verifier_redact": [r"expected: \d+"]}, base=tmp_path)
    out = tmp_path / "v"
    out.mkdir()
    verdict = redacting.verdict({"rewards": None, "verifier_dir": str(out),
                                 "exception": {"type": "ValueError", "message": "bad reward, expected: 42"}})
    assert "42" not in verdict["exception"]["message"] and "42" not in verdict["exception_message"]


def test_round_8_agents(tmp_path, monkeypatch):
    from hermit.agent.acting import answer as answer_mod
    from hermit.agent.model.usage import Usage

    class Model:
        model = "m"

        def chat(self, messages, **kwargs):
            return {"role": "assistant", "content": "B"}, Usage(model="m", input_tokens=12, output_tokens=5, calls=1,
                                                                 cost_usd=0.1)

    monkeypatch.setattr(answer_mod.ChatClient, "from_spec", staticmethod(lambda spec: Model()))

    class Failing(Recorded):
        def upload(self, source, target):
            raise RuntimeError("EnvironmentUnavailable: upload failed in the task's environment")

    env = Failing(tmp_path)
    env.instruction = "Which?"
    out = answer_mod.AnswerAgent({"model": {"base_url": "https://x", "model": "m"}, "logs": str(tmp_path / "l")},
                                 tmp_path).run({"id": "q"}, None, environment=env)
    assert (out.tokens, out.cost_usd, out.extra["infra"]) == (17, 0.1, "harbor_failed")   # its spend kept
    # 13: an image whose bytes do not come back whole is the environment's failure, never an empty image sent
    broken = Recorded(tmp_path, lambda c: ExecResult(0, "4\n") if "wc -c" in c[-1] else
                      ExecResult(0, "") if "base64" in c[-1] else ExecResult(0, "/app/a.png\n"))
    broken.instruction = "Which?"
    with pytest.raises(RuntimeError, match="did not provide the attachment"):
        answer_mod.AnswerAgent({"model": {"base_url": "https://x", "model": "m"}, "logs": str(tmp_path / "l"),
                                "attachments": ["/app/*.png"]}, tmp_path).run({"id": "q"}, None, environment=broken)
    # 14: a sign-in larger than the limit there is never brought here
    from hermit.agent.base.cli import CliAgent as Base
    downloads = []

    class Big(Recorded):
        def download(self, source, target):
            downloads.append(source)
            raise AssertionError("never downloaded")

    Base._signin_back(Big(tmp_path, lambda c: ExecResult(0, "10000000\n")), PurePosixPath("/h"), tmp_path)
    assert downloads == []
    # 16: a CLI's model on this machine's loopback, which the task's container cannot reach, is refused
    from types import SimpleNamespace
    from hermit import isolate
    binding = {"ref": "local/m", "backend": "local", "model": "m", "base_url": "http://127.0.0.1:8000/v1",
               "apis": ["responses"]}
    config = SimpleNamespace(base=tmp_path, data={"environment": {"harbor": {"tasks_dir": str(tmp_path)}},
                                                   "agent": {"name": "codex", "params": {"binding": binding}}})
    with pytest.raises(ValueError, match="loopback"):
        isolate.task_environments(config)


def test_round_9_hermit_side(harbor, tmp_path, monkeypatch):
    from hermit.agent.acting import answer as answer_mod
    from hermit.agent.model.usage import Usage

    class Model:
        model = "m"

        def chat(self, messages, **kwargs):
            return {"role": "assistant", "content": "B"}, Usage(model="m", input_tokens=12, output_tokens=5, calls=1,
                                                                 cost_usd=0.1)

    monkeypatch.setattr(answer_mod.ChatClient, "from_spec", staticmethod(lambda spec: Model()))

    class Closed(Recorded):
        def upload(self, source, target):
            raise RuntimeError("AgentPhaseOver: upload: the agent's phase ended")

    env = Closed(tmp_path)
    env.instruction = "Which?"
    out = answer_mod.AnswerAgent({"model": {"base_url": "https://x", "model": "m"}, "logs": str(tmp_path / "l")},
                                 tmp_path).run({"id": "q"}, None, environment=env)
    assert out.extra["timed_out"] is True and (out.tokens, out.cost_usd) == (17, 0.1)   # Harbor's verdict, its spend
    environments, root, calls = harbor
    kept = HarborEnvironments({"tasks_dir": str(tmp_path / "tasks"), "harbor": sys.executable,
                               "jobs_dir": str(tmp_path / "jobs"),
                               "environment": {"kwargs": {"keep_containers": True}}}, base=tmp_path)
    assert kept.keep_containers and not kept.local_docker
    from types import SimpleNamespace
    from hermit import isolate
    binding = {"ref": "local/m", "backend": "local", "model": "m", "base_url": "http://127.0.0.1:8000/v1",
               "apis": ["responses"]}
    config = SimpleNamespace(base=tmp_path, data={"environment": {"harbor": {"tasks_dir": str(tmp_path)}},
                                                   "agent": {"name": "cli", "params": {"binding": binding}}})
    with pytest.raises(ValueError, match="loopback"):            # `cli` is Codex by default
        isolate.task_environments(config)



# ---- the simple end of the agent phase -------------------------------------------------------------------------

def _kill_program(shell: str, mark: str, *, stuck: bool = False) -> str:
    """A PID 1 of its own (Python, which never reaps an orphan, as a container's `sleep infinity` does not): an agent
    command's group started under setsid as the bridge starts it, then kill_groups run by `shell` (a stuck kill first
    when `stuck`), then the count of that group's processes left alive (a zombie is not)."""
    pidfile = f"/tmp/hermit-pid-{mark}"
    kill = _driver_function("kill_groups", TOKEN="hermit-mark-", PIDS="/tmp/hermit-pid-")(pidfile)
    scenario = (f'setsid bash -c "sleep 60 & sleep 60; exit" hermit-mark-{mark} >/dev/null 2>&1 & echo $! > {pidfile}; '
                "echo leader=$!; sleep 0.5; ")
    if stuck:                                                    # a group the kill does not end
        scenario += (f"{shell} -c {shlex.quote(kill.replace('kill -KILL', ': kill -KILL'))}; echo stuck=$?; "
                     f"[ -f {pidfile} ] && echo kept; ")
    scenario += f"{shell} -c {shlex.quote(kill)}; echo killed=$?; [ -f {pidfile} ] || echo removed"
    return textwrap.dedent(f"""
        import os, subprocess
        lines = subprocess.run(["sh", "-c", {scenario!r}], capture_output=True, text=True).stdout.split()
        leader = next(x for x in lines if x.startswith("leader="))[len("leader="):]
        left = 0
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                state, _, group = open(f"/proc/{{pid}}/stat").read().rsplit(") ", 1)[1].split()[:3]
            except (OSError, ValueError):
                continue
            left += group == leader and state not in "ZX"
        print(*[x for x in lines if not x.startswith("leader=")], f"left={{left}}")
    """)


@pytest.mark.skipif(not _own_pid_namespace(), reason="needs unshare (a PID namespace of its own, as a container's)")
@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_a_commands_process_group_is_killed_from_its_pid_file_and_checked_gone(tmp_path, shell):
    """kill_groups, as the runner's _stop_session: the group of the command whose leader's pid file names it (its
    word on its command line confirming it), what it started included, then one check that none is left; under a
    PID 1 that never reaps (its zombies are gone) and in POSIX sh (dash on Debian and Ubuntu) as in bash."""
    mark = "m" + os.urandom(4).hex()
    kill = _driver_function("kill_groups", TOKEN="hermit-mark-", PIDS="/tmp/hermit-pid-")
    done = subprocess.run(["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc", sys.executable,
                           "-c", _kill_program(shell, mark)], capture_output=True, text=True, timeout=60)
    assert done.stdout.split() == ["killed=0", "removed", "left=0"], done.stdout + done.stderr
    assert not os.path.exists(f"/tmp/hermit-pid-{mark}")
    fake = tmp_path / "fake"                            # a pid file naming a process without the word: left alone
    keep = subprocess.Popen(["sleep", "30"])
    try:
        fake_file = f"/tmp/hermit-pid-{mark}x"
        Path(fake_file).write_text(str(keep.pid))
        subprocess.run(["sh", "-c", kill(fake_file)], capture_output=True, timeout=30)
        assert keep.poll() is None
    finally:
        keep.kill()
        fake.touch()


@pytest.mark.skipif(not HARBOR_PYTHON.exists(), reason="needs Harbor's venv (~/.cache/hermit/harbor-venv)")
def test_the_bridge_ends_its_phase_at_the_runners_level(tmp_path):
    """The bridge's run: the phase starts with the instruction; a relayed command is refused outside it; at `done`
    and at Harbor's timeout the groups are killed (one root exec), the registered credentials removed, Hermit's logs
    copied scrubbed; a kill or a removal that fails is HarborDriverError (no verifier); a cancelled client's whole
    group is killed (docker's compose plugin with it)."""
    script = tmp_path / "probe.py"
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent-codex.jsonl").write_text(f"key {AGENT_KEY}\n")
    script.write_text(textwrap.dedent(f"""
        import asyncio, importlib.util, sys
        from pathlib import Path
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location("drv", {str(DRIVER_FILE)!r})
        drv = importlib.util.module_from_spec(spec); sys.modules["drv"] = drv; spec.loader.exec_module(drv)
        drv.resolve_task_verifier_mode = lambda config: SimpleNamespace(value="shared")

        class Env:
            def __init__(self, kill_ok=True, rm_ok=True): self.calls, self.kill_ok, self.rm_ok = [], kill_ok, rm_ok
            async def exec(self, command, cwd=None, user=None, **kw):
                self.calls.append((command, user))
                if command.startswith("printf"):
                    return SimpleNamespace(return_code=0, stdout="hermit-workdir:/app\\n", stderr="")
                if "hermit-owner:" in command:
                    return SimpleNamespace(return_code=0, stdout="hermit-owner:0:0\\n", stderr="")
                ok = self.kill_ok if "hermit-pid-" in command else self.rm_ok if command.startswith("rm -f") else True
                if ok == "raise":
                    raise ConnectionError("docker's transport")
                return SimpleNamespace(return_code=0 if ok else 1, stdout="", stderr="")

        def trial(tmp):
            plan = SimpleNamespace(agent_phase=SimpleNamespace(network_mode="no-network"))
            paths = SimpleNamespace(trial_dir=Path(tmp), agent_dir=Path(tmp) / "agent")
            config = SimpleNamespace(agent=SimpleNamespace(user=None), environment=SimpleNamespace(
                os=SimpleNamespace(value="linux")))
            return SimpleNamespace(task=SimpleNamespace(config=config), paths=paths, agent_timeout_sec=60,
                                   agent_setup_timeout_sec=60, verifier_timeout_sec=60,
                                   environment_build_timeout_sec=600, _network_plan=lambda step: plan)

        class Out:
            def write(self, s): pass
            def flush(self): pass

        async def main():
            out = []
            for how, kill_ok, rm_ok in (("done", True, True), ("timeout", True, True), ("kill fails", False, True),
                                        ("rm fails", True, False), ("kill raises", "raise", True)):
                d = drv.DRIVER = drv.Driver(Out())
                d.trial = trial({str(tmp_path)!r} + "/" + how.replace(" ", "-"))
                env = Env(kill_ok, rm_ok)
                await d.handle({{"id": 1, "op": "exec", "command": "true"}})    # before the phase: refused
                run = asyncio.create_task(d.phase("Do it.", env))
                started = await d.phase_started
                d.secret_files.add("/installed-agent/hermit-home/auth.json")
                try:
                    if how == "timeout":
                        run.cancel()
                        r = await asyncio.gather(run, return_exceptions=True)
                        if isinstance(r[0], drv.HarborDriverError):
                            raise r[0]
                    else:
                        d.agent_logs, d.secrets = {str(logs)!r}, {{{AGENT_KEY!r}}}
                        d.done.set()
                        await run
                    ended = "returned"
                except drv.HarborDriverError:
                    ended = "raised"
                killed = [c for c, u in env.calls if "hermit-pid-" in c and u == "root"]
                removed = [c for c, u in env.calls if c.startswith("rm -f") and "auth.json" in c]
                kept = Path({str(tmp_path)!r}) / how.replace(" ", "-") / "agent" / "agent-codex.jsonl"
                out.append([how, started["instruction"], ended, len(killed), len(removed),
                            kept.read_text() if kept.exists() else None])
            held = []
            async def waits():
                p = await asyncio.create_subprocess_exec("sh", "-c", "sleep 30 & echo $!; wait",
                                                         stdout=asyncio.subprocess.PIPE)
                held.append(p)
                await p.communicate()
            t = asyncio.create_task(waits()); await asyncio.sleep(0.5); t.cancel()
            await asyncio.gather(t, return_exceptions=True)
            await asyncio.sleep(0.3)
            out.append(not drv.group_alive(held[0]))
            print("RESULT", out)
        asyncio.run(main())
    """))
    done = subprocess.run([str(HARBOR_PYTHON), "-I", str(script)], capture_output=True, text=True, timeout=120)
    line = next((x for x in done.stdout.splitlines() if x.startswith("RESULT")), done.stdout + done.stderr)
    assert line == "RESULT " + str([["done", "Do it.", "returned", 1, 1, "key [REDACTED]\n"],
                                    ["timeout", "Do it.", "returned", 1, 1, None],
                                    ["kill fails", "Do it.", "raised", 1, 1, None],
                                    ["rm fails", "Do it.", "raised", 1, 1, None],
                                    ["kill raises", "Do it.", "raised", 1, 1, None], True]), line


def test_a_harbor_task_sets_agent_runs_in_this_process(tmp_path):
    """No worker and no proxy for a task in Harbor's environment: its commands and CLIs run in the task's own
    container, the agent's loop here (host_pipeline)."""
    from types import SimpleNamespace
    from hermit.isolate import host_pipeline
    config = SimpleNamespace(data={"sandbox": {"enabled": True}, "environment": {"harbor": {"tasks_dir": "x"}}})
    assert "environment.harbor" in host_pipeline(config)


def test_the_agents_setup_defaults_to_twenty_minutes_and_a_setting_wins(harbor, tmp_path):
    environments, _, _ = harbor
    task = environments.task_dir({"harbor_task": "demo"})
    assert environments.start_request(task, tmp_path, "t")["agent"]["override_setup_timeout_sec"] == 1200.0
    assert environments.start_timeout(task) >= 600 + 1200 + harbor_mod.START_ALLOWANCE_S      # the watchdog follows
    own = HarborEnvironments({"tasks_dir": str(tmp_path / "tasks"), "harbor": sys.executable,
                              "setup_timeout_sec": 300}, base=tmp_path)
    assert own.start_request(task, tmp_path, "t")["agent"]["override_setup_timeout_sec"] == 300.0


@pytest.mark.skipif(not HARBOR_PYTHON.exists(), reason="needs Harbor's venv (~/.cache/hermit/harbor-venv)")
def test_any_failure_of_the_phases_end_is_infra(tmp_path):
    """A failure of the phase's end of any kind (its bound, a log folder it cannot write) is HarborDriverError: no
    verifier, infra, never the agent's zero."""
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent(f"""
        import asyncio, importlib.util, sys
        spec = importlib.util.spec_from_file_location("drv", {str(DRIVER_FILE)!r})
        drv = importlib.util.module_from_spec(spec); sys.modules["drv"] = drv; spec.loader.exec_module(drv)

        async def main():
            out = []
            for failing in ("hangs", "denied"):
                d = drv.Driver(None)
                async def end(how=failing):
                    if how == "hangs":
                        await asyncio.sleep(60)
                    raise PermissionError("the agent logs' folder")
                d._end_phase = end
                drv.END_PHASE_S = 0.2
                try:
                    await d._close_phase(False); out.append("returned")
                except drv.HarborDriverError:
                    out.append("infra")
            print("RESULT", out)
        asyncio.run(main())
    """))
    done = subprocess.run([str(HARBOR_PYTHON), "-I", str(script)], capture_output=True, text=True, timeout=60)
    line = next((x for x in done.stdout.splitlines() if x.startswith("RESULT")), done.stdout + done.stderr)
    assert line == "RESULT " + str(["infra", "infra"]), line


@pytest.mark.skipif(not HARBOR_PYTHON.exists(), reason="needs Harbor's venv (~/.cache/hermit/harbor-venv)")
def test_a_clis_live_stream_is_scrubbed_before_the_verifier_even_when_harbors_timeout_ends_the_phase(tmp_path):
    """A CLI's output is kept in /logs/agent as it runs, which a shared verifier reads. Harbor's timeout ends the phase
    with no logs of Hermit's to copy over it, so the phase's end scrubs the stream there itself (a local Docker
    environment shares that folder with this machine); a link planted in its place is never followed. Elsewhere it is
    left for Harbor's download, which the trial's scrub covers."""
    agent_dir, outside = tmp_path / "agent", tmp_path / "outside.txt"
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent(f"""
        import asyncio, importlib.util, sys
        from pathlib import Path
        from types import SimpleNamespace as NS
        spec = importlib.util.spec_from_file_location("drv", {str(DRIVER_FILE)!r})
        drv = importlib.util.module_from_spec(spec); sys.modules["drv"] = drv; spec.loader.exec_module(drv)
        agent_dir, outside = Path({str(agent_dir)!r}), Path({str(outside)!r})

        async def ran(command, **kwargs):
            return NS(return_code=0, stdout="yes", stderr="")

        async def main():
            out = []
            for mounted in (True, False):
                agent_dir.mkdir(exist_ok=True)
                outside.write_text("{AGENT_KEY}")
                (agent_dir / "planted.jsonl").unlink(missing_ok=True)
                (agent_dir / "planted.jsonl").symlink_to(outside)
                d = drv.Driver(None)
                d.env = NS(exec=ran, capabilities=NS(mounted=mounted), _merge_env=lambda env: env)
                d.trial = NS(paths=NS(agent_dir=agent_dir))
                key = {{"OPENAI_API_KEY": {AGENT_KEY!r}}}
                for name in ("agent-codex.jsonl", "planted.jsonl"):     # the CLI's session, its key passed by name
                    await d.exec({{"command": "codex exec -", "stream": name, "env": key}})
                (agent_dir / "agent-codex.jsonl").write_text("401: bad key {AGENT_KEY}\\n")   # what tee kept
                await d._end_phase()                     # no agent_logs: Harbor's timeout came first
                out.append([(agent_dir / "agent-codex.jsonl").read_text(), outside.read_text(),
                            (agent_dir / "planted.jsonl").is_symlink()])
            # a stream this machine cannot read (another uid, a strict umask) whose own log follows: never infra
            logs = agent_dir.parent / "logs"
            logs.mkdir(exist_ok=True)
            (logs / "agent-codex.jsonl").write_text("401: bad key {AGENT_KEY}\\n")
            (agent_dir / "agent-codex.jsonl").chmod(0)
            d.env, d.agent_logs = NS(exec=ran, capabilities=NS(mounted=True)), str(logs)
            await d._end_phase()
            out.append((agent_dir / "agent-codex.jsonl").read_text())
            print("RESULT", out)
        asyncio.run(main())
    """))
    done = subprocess.run([str(HARBOR_PYTHON), "-I", str(script)], capture_output=True, text=True, timeout=60)
    line = next((x for x in done.stdout.splitlines() if x.startswith("RESULT")), done.stdout + done.stderr)
    assert line == "RESULT " + str([["401: bad key [REDACTED]\n", AGENT_KEY, True],
                                    [f"401: bad key {AGENT_KEY}\n", AGENT_KEY, True],
                                    "401: bad key [REDACTED]\n"]), line


@pytest.mark.skipif(not HARBOR_PYTHON.exists(), reason="needs Harbor's venv (~/.cache/hermit/harbor-venv)")
def test_a_multi_step_harbor_task_is_refused_as_unsupported_and_never_run_again(tmp_path):
    """A task with [[steps]] is refused at the driver's start as a task Harbor runs for none of Hermit's agents
    (UnsupportedTask), which Hermit reads as unsupported_task and does not run again, rather than an environment that
    failed to start (sandbox_failed), which spent the infra retries on a task that can never run."""
    (tmp_path / "task").mkdir()
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent(f"""
        import asyncio, importlib.util, sys
        from types import SimpleNamespace as NS
        spec = importlib.util.spec_from_file_location("drv", {str(DRIVER_FILE)!r})
        drv = importlib.util.module_from_spec(spec); sys.modules["drv"] = drv; spec.loader.exec_module(drv)

        async def create(config):
            return NS(task=NS(has_steps=True, name="multi"), _record_exception=lambda exc: None)

        async def main():
            drv.Trial.create = create
            try:
                await drv.Driver(None).start({{"task_dir": {str(tmp_path / "task")!r},
                                               "trials_dir": {str(tmp_path / "trials")!r}, "trial_name": "t"}})
                print("RESULT started")
            except drv.Failed as exc:
                print("RESULT", exc.kind)
        asyncio.run(main())
    """))
    done = subprocess.run([str(HARBOR_PYTHON), "-I", str(script)], capture_output=True, text=True, timeout=60)
    line = next((x for x in done.stdout.splitlines() if x.startswith("RESULT")), done.stdout + done.stderr)
    assert line == "RESULT UnsupportedTask", line
    from hermit.environment import harbor_errors
    assert "unsupported_task" in harbor_errors.NO_RETRY


def test_a_harbor_agents_adapter_code_runs_in_the_strict_adapter_sandbox(tmp_path, monkeypatch):
    """No worker for a Harbor task set's agent: its call in this process still confines the adapter's own code as the
    worker did (the runner's adapter sandbox): a context tool's Session is sandboxed, its exports' render goes through
    the strict sandbox; outside the call nothing is."""
    from types import SimpleNamespace
    from hermit import Session
    from hermit import isolate
    from hermit.hosts import inputs
    monkeypatch.delenv("HA_ADAPTER_SANDBOX", raising=False)
    adapter = tmp_path / "adapter"
    shutil.copytree(Path(__file__).resolve().parents[2] / "hermit/adapter/templates/prompt_only", adapter)
    rendered = []
    monkeypatch.setattr(inputs, "run_in_strict_sandbox", lambda command, **kw: rendered.append(("strict", kw)) or
                        SimpleNamespace(returncode=0, stdout="{}", stderr=""))
    monkeypatch.setattr(inputs.subprocess, "run", lambda command, **kw: rendered.append(("host", kw)) or
                        SimpleNamespace(returncode=0, stdout="{}", stderr=""))

    def run(task, adapter_dir, environment=None):
        inputs.render_exports(adapter_dir, sys.executable)
        return Session(adapter_dir).sandboxed

    config = SimpleNamespace(data={"sandbox": {"enabled": True, "enforce": "best_effort", "hide": []},
                                   "environment": {"harbor": {"tasks_dir": "x"}},
                                   "agent": {"name": "codex", "params": {}}}, base=tmp_path)
    monkeypatch.setattr("hermit.agent.acting.agent.load_agent",
                        lambda name, params, base: SimpleNamespace(run=run))
    pipeline, close = isolate.agent_pipeline(config, "rollout", records=None)
    assert pipeline({"id": "q"}, adapter) is True
    assert rendered == [("strict", {"role": "render", "readable": [adapter], "enforce": "best_effort"})]
    assert Session(adapter).sandboxed is False                      # outside the call: as before
    close()


def test_a_harbor_cli_signs_in_by_its_binding_or_a_named_login_only(tmp_path, monkeypatch):
    """The runner's refusal: a CLI in a task's container without a model binding or a login is refused before any
    trial; and a session in a remote environment never carries this machine's own CLI sign-in there."""
    from types import SimpleNamespace
    from hermit import isolate
    from hermit.agent.base import login
    from hermit.agent.base.interface import make
    (tmp_path / "tasks").mkdir()
    data = {"data": {"tasks": None}, "judge": {}, "environment": {"harbor": {"tasks_dir": "tasks",
                                                                            "harbor": sys.executable}},
            "agent": {"name": "codex", "params": {"model": "gpt-5.6-luna", "web_search": False}}}
    config = SimpleNamespace(data=data, base=tmp_path)
    with pytest.raises(ValueError, match="needs a model binding"):
        isolate.task_environments(config)
    own = tmp_path / "user-codex"
    own.mkdir()
    (own / "auth.json").write_text('{"tokens": "mine"}')
    monkeypatch.setattr(login, "source", lambda: own / "auth.json")
    env = Recorded(tmp_path, lambda c: ExecResult(0, CODEX_STREAM) if session_of(c) else ExecResult(0, ""))
    out = make("codex", {"web_search": False, "model": "m", "bin": "codex",
                         "home": str(tmp_path / "kept")}).run("x", environment=env)
    assert out.reply == "fixed it" and env.uploads
    assert not any("auth.json" in (files or {}) for files in env.uploads.values())


def test_the_log_a_record_names_is_scrubbed_of_the_runs_keys(tmp_path, monkeypatch):
    """An agent in a task's own environment runs in this process: the log its record names (results.jsonl points at
    it) is scrubbed of the run's key values, as the worker scrubbed what it returned."""
    from hermit.task import Task
    from hermit.trainer import rollout
    monkeypatch.setenv("FIREWORKS_API_KEY", AGENT_KEY)
    log = tmp_path / "agent-codex.jsonl"
    log.write_text(f"provider said: bad key {AGENT_KEY}\n")

    def pipeline(task, adapter, environment=None):
        return RunOutput(output="done", log=log)

    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo"})
    rollout.environment_rollout(task, task.given(), None, pipeline, Environments(tmp_path), 0.0, pause=lambda s: None)
    assert AGENT_KEY not in log.read_text() and "bad key" in log.read_text()


@pytest.mark.skipif(not _own_pid_namespace(), reason="needs unshare (a PID namespace of its own, as a container's)")
@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_a_group_not_confirmed_gone_keeps_its_pid_file_for_the_phases_end(shell):
    """kill_groups removes a command's pid file only once its group is confirmed gone: one that survives keeps its
    record, so the phase's end tries again and fails the trial (infra) rather than letting the verifier run."""
    mark = "m" + os.urandom(4).hex()
    try:
        done = subprocess.run(["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc",
                               sys.executable, "-c", _kill_program(shell, mark, stuck=True)],
                              capture_output=True, text=True, timeout=60)
    finally:
        Path(f"/tmp/hermit-pid-{mark}").unlink(missing_ok=True)
    assert done.stdout.split() == ["stuck=1", "kept", "killed=0", "removed", "left=0"], done.stdout + done.stderr


@pytest.mark.skipif(not _docker_up() or subprocess.run(["docker", "image", "inspect", "python:3.12-slim"],
                                                       capture_output=True).returncode != 0,
                    reason="needs Docker and python:3.12-slim (Debian: dash is its sh)")
def test_kill_groups_in_a_debian_container_whose_pid_1_never_reaps():
    """The same in a container: Debian's sh (dash, whose kill reads `--` as a pid) and a PID 1 that never reaps."""
    mark = "m" + os.urandom(4).hex()
    done = subprocess.run(["docker", "run", "--rm", "--name", f"hermit-test-kill-{mark}", "python:3.12-slim",
                           "python3", "-c", _kill_program("sh", mark)], capture_output=True, text=True, timeout=180)
    assert done.stdout.split() == ["killed=0", "removed", "left=0"], done.stdout + done.stderr


@pytest.mark.skipif(not HARBOR_PYTHON.exists(), reason="needs Harbor's venv (~/.cache/hermit/harbor-venv)")
def test_a_download_of_a_link_the_agent_left_is_refused(tmp_path):
    """The runner refused a link at the notes' folder: a download whose source is a link (a hook audit or a notes
    folder the agent replaced) is never copied out with Harbor's privileged copy."""
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent(f"""
        import asyncio, importlib.util, sys
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location("drv", {str(DRIVER_FILE)!r})
        drv = importlib.util.module_from_spec(spec); sys.modules["drv"] = drv; spec.loader.exec_module(drv)

        class Env:
            copied = []
            async def exec(self, command, user=None, **kw):
                return SimpleNamespace(return_code=0 if "/app/.hermit-hooks" in command else 1, stdout="", stderr="")
            async def is_dir(self, path, user=None): return True
            async def download_dir(self, source, target): self.copied.append(source)
            async def download_file(self, source, target): self.copied.append(source)

        async def main():
            d = drv.Driver(None)
            d.env = Env()
            out = []
            for source in ("/app/.hermit-hooks", "/app/notes"):
                try:
                    await d.download({{"source": source, "target": {str(tmp_path / "got")!r} + source}})
                    out.append("copied")
                except drv.Failed as exc:
                    out.append(exc.kind)
            print("RESULT", out, Env.copied)
        asyncio.run(main())
    """))
    done = subprocess.run([str(HARBOR_PYTHON), "-I", str(script)], capture_output=True, text=True, timeout=60)
    line = next((x for x in done.stdout.splitlines() if x.startswith("RESULT")), done.stdout + done.stderr)
    assert line == "RESULT " + str(["PermissionError", "copied"]) + " " + str(["/app/notes"]), line


def test_a_forced_teardown_signals_only_the_drivers_own_group(tmp_path):
    """Killing the driver (its watchdog) signals the driver's own process group, never a group recorded earlier whose
    pid this busy machine may since have given to another program."""
    from hermit.environment.harbor import _Driver
    script = tmp_path / "driver.py"
    script.write_text("import time\ntime.sleep(60)\n")
    other = subprocess.Popen(["sleep", "60"], start_new_session=True)       # not the driver's: left alone
    original = harbor_mod.DRIVER
    harbor_mod.DRIVER = script
    try:
        (tmp_path / "client-groups").write_text(f"{other.pid}\n")           # what the driver once recorded there
        driver = _Driver(sys.executable, tmp_path / "driver.log", dict(os.environ))
        driver._kill()
        assert driver.proc.poll() is not None
        with pytest.raises(subprocess.TimeoutExpired):                     # still running
            other.wait(timeout=1)
    finally:
        harbor_mod.DRIVER = original
        other.kill()
        other.wait()


def test_a_sandbox_this_machine_cannot_start_is_infra_never_the_adapters_zero(tmp_path, monkeypatch):
    """The runner's preflight and staging_failed: a run whose strict adapter sandbox does not start here is refused at
    startup, and a deployment or context that fails because it stopped starting is infra (run again), not the
    adapter's scored zero."""
    from types import SimpleNamespace
    from hermit import isolate, sandbox
    from hermit.adapter.session import CONFINED
    from hermit.agent.acting import answer as answer_mod
    from hermit.agent.model.usage import Usage
    fault = "the strict adapter sandbox did not start (exit 1): no user namespaces"
    monkeypatch.setattr(sandbox, "sandbox_fault", lambda enforce, fresh=True: fault)
    monkeypatch.setattr(isolate, "sandbox_fault", sandbox.sandbox_fault)
    monkeypatch.setattr("hermit.agent.acting.agent.load_agent",
                        lambda name, params, base: SimpleNamespace(run=lambda *a, **k: None))
    config = SimpleNamespace(data={"sandbox": {"enabled": True, "enforce": "required", "hide": []},
                                   "environment": {"harbor": {"tasks_dir": "x"}},
                                   "agent": {"name": "codex", "params": {}}}, base=tmp_path)
    with pytest.raises(ValueError, match="did not start"):
        isolate.agent_pipeline(config, "rollout", records=None)

    class Model:
        model = "m"

        def chat(self, messages, abort=None, **kwargs):
            return {"role": "assistant", "content": "B"}, Usage(model="m", calls=1)

    class Failing:
        last_context = None

        def context(self, **kw):
            raise RuntimeError("the sandbox's server did not start")

        def close(self):
            pass

    def server_fails(adapter):                     # the context tool's server, started in its sandbox, did not start
        raise RuntimeError("bwrap: setting up uid map: Permission denied")

    monkeypatch.setattr(answer_mod.ChatClient, "from_spec", staticmethod(lambda spec: Model()))
    agent = answer_mod.AnswerAgent({"model": {"base_url": "https://x", "model": "m"}, "logs": str(tmp_path / "l")},
                                   tmp_path)
    env = Recorded(tmp_path)
    env.instruction = "Which?"
    token = CONFINED.set("required")
    try:
        for session in (lambda adapter: Failing(), server_fails):
            monkeypatch.setattr(agent, "session", session)
            out = agent.run({"id": "q"}, tmp_path, environment=env)
            assert out.extra["infra"] == "staging_failed" and not out.extra["infra_final"]     # run again
            assert (out.cost_usd, out.tokens) == (0.0, 0)                                      # nothing spent
            assert "adapter_failed" not in out.extra
    finally:
        CONFINED.reset(token)
    def disk_full(adapter):                        # the server's files not made: this machine's, whatever the sandbox
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(agent, "session", disk_full)
    out = agent.run({"id": "q"}, tmp_path, environment=env)
    assert out.extra["infra"] == "staging_failed" and out.cost_usd == 0.0 and "adapter_failed" not in out.extra
    monkeypatch.undo()
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)                  # bubblewrap not installed
    assert "not installed" in sandbox.sandbox_fault("required") and sandbox.sandbox_fault("best_effort") is None


def test_the_usage_an_error_response_reports_is_counted(tmp_path, monkeypatch):
    """A terminal HTTP error whose body reports usage (a relay's or provider's): counted on the agent's scored result
    (an input refused) and kept on the infra end (a provider that could not serve), as the runner's relay priced it."""
    from hermit.agent.acting import answer as answer_mod
    from hermit.agent.model.llm import ChatHTTPError
    from hermit.task import Task
    from hermit.trainer import rollout
    body = json.dumps({"error": {"message": "nope"}, "usage": {"prompt_tokens": 120, "completion_tokens": 20}})

    def model_failing(code):
        class Model:
            model = "m"

            def cost(self, prompt, cached, completion):
                return (prompt + completion) / 1e6

            def chat(self, messages, abort=None, **kwargs):
                raise ChatHTTPError(code, body.encode(), {})
        return Model()

    params = {"model": {"base_url": "https://x", "model": "m"}, "logs": str(tmp_path / "l")}
    monkeypatch.setattr(answer_mod.ChatClient, "from_spec", staticmethod(lambda spec: model_failing(400)))
    env = Recorded(tmp_path)
    env.instruction = "Which?"
    out = answer_mod.AnswerAgent(params, tmp_path).run({"id": "q"}, None, environment=env)
    assert out.tokens == 140 and out.api_speed["model_calls"] == 1 and out.cost_usd == pytest.approx(1.4e-4)
    monkeypatch.setattr(answer_mod.ChatClient, "from_spec", staticmethod(lambda spec: model_failing(503)))
    agent = answer_mod.AnswerAgent(params, tmp_path)
    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo", "question": "Which?"})
    record = rollout.environment_rollout(task, task.given(), None, lambda t, a, environment=None: agent.run(
        t, a, environment=environment), Environments(tmp_path), 0.0, pause=lambda s: None)
    calls = record.api_speed["model_calls"]                      # each run again's spend kept, its call counted
    assert calls >= 2 and record.tokens == 140 * calls and record.extra["unscored"] == "model_provider_failed"
    assert record.cost_usd == pytest.approx(1.4e-4 * calls)
    from hermit.trainer.rollout import run_one                   # and outside a task's own environment: the record
    failed = run_one(task, None, lambda t, a: agent.run(t, a), lambda row, out: {"score": 0.0})
    assert failed["status"] == "error" and failed["tokens"] == 140 and failed["cost_usd"] == pytest.approx(1.4e-4)


def test_an_unreadable_verifier_log_leaves_the_reward(tmp_path, monkeypatch):
    """feedback.txt is optional (the runner's log reader tolerated one it could not read): its output's tail is read
    instead, and a reward the verifier wrote stands."""
    logs = tmp_path / "verifier"
    logs.mkdir()
    (logs / "feedback.txt").write_text("hidden")
    (logs / "test-stdout.txt").write_text("1 passed")
    original = Path.read_text

    def read_text(self, *a, **k):
        if self.name == "feedback.txt":
            raise PermissionError(13, "Permission denied", str(self))
        return original(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    envs = HarborEnvironments({"tasks_dir": str(tmp_path), "harbor": sys.executable}, base=tmp_path)
    verdict = envs.verdict({"rewards": {"reward": 1.0}, "verifier_dir": str(logs)})
    assert verdict["reward"] == 1.0 and verdict["status"] == "ok" and verdict["feedback"] == "1 passed"


def test_a_staging_failure_is_run_again_and_spends_nothing(tmp_path):
    """The runner's staging_failed (this machine's, before any session): infra, run again, its attempt's spend 0, so
    the run that then passes keeps a known cost."""
    from hermit.adapter.session import staging_failed
    from hermit.task import Task
    from hermit.trainer import rollout
    runs = []

    def pipeline(task, adapter, environment=None):
        runs.append(1)
        if len(runs) == 1:
            return staging_failed("staging_failed: OSError: [Errno 28] No space left on device", 0.0)
        return RunOutput(output="done", cost_usd=0.25, tokens=100, api_speed={"model_calls": 1})

    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo"})
    out = rollout.environment_rollout(task, task.given(), None, pipeline, Environments(tmp_path), 0.0,
                                      pause=lambda s: None)
    assert len(runs) == 2 and out.cost_usd == pytest.approx(0.25) and out.tokens == 100
    assert [a["unscored"] for a in out.extra["attempts"]] == ["staging_failed", None]


def test_a_cli_agents_staging_folders_on_a_full_disk_are_staging_failed(tmp_path, monkeypatch):
    """The runner guarded its job's folders with the staging: a full disk making them is staging_failed (run again,
    nothing spent), never this machine's final host_failed."""
    from hermit.agent.acting.workspace import CliAgent
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    original = Path.mkdir

    def mkdir(self, *a, **k):
        if self.name == "work":
            raise OSError(28, "No space left on device", str(self))
        return original(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    env = Recorded(tmp_path, lambda c: ExecResult(0, CODEX_STREAM) if session_of(c) else ExecResult(0, ""))
    out = CliAgent({"backend": "codex", "web_search": False, "bin": "codex", "workspaces": str(tmp_path / "ws")},
                   tmp_path).run({"id": "t", "instruction": "do it"}, None, environment=env)
    assert out.extra["infra"] == "staging_failed" and not out.extra["infra_final"]
    assert (out.cost_usd, out.tokens, out.api_speed["model_calls"]) == (0.0, 0, 0)


def test_a_terminal_failure_after_a_run_again_keeps_every_attempts_spend(tmp_path):
    """A pipeline's own failure after an infra run again: the record sums the earlier attempts' spend and the failed
    one's (what it said it billed), its cost known when each is."""
    from hermit.task import Task
    from hermit.trainer import rollout
    runs = []

    def pipeline(task, adapter, environment=None):
        runs.append(1)
        if len(runs) == 1:
            return RunOutput(output=None, tokens=140, cost_usd=0.014, api_speed={"model_calls": 1},
                             extra={"infra": "model_provider_failed", "infra_final": False, "exception_message": "x"})
        failed = KeyError("a bug of the agent's own")
        failed.spent = RunOutput(output=None, tokens=200, cost_usd=0.020, api_speed={"model_calls": 1})
        raise failed

    task = Task(id="demo#1", public={"id": "demo#1", "harbor_task": "demo"})
    record = rollout.environment_rollout(task, task.given(), None, pipeline, Environments(tmp_path), 0.0,
                                         pause=lambda s: None)
    assert record["status"] == "error" and record["tokens"] == 340 and record["cost_usd"] == pytest.approx(0.034)
    assert record["api_speed"]["model_calls"] == 2 and len(record["pipeline_extra"]["attempts"]) == 2


def test_a_sessions_placeholder_usage_in_an_error_is_no_report(tmp_path, monkeypatch):
    """A CLI endpoint's error carries all-zero usage when its session reported none: unknown, never a measured 0
    (hermit.tools.failed_usage's rule)."""
    from hermit.agent.acting.answer import _error_usage
    from hermit.agent.model.llm import ChatHTTPError
    zero = json.dumps({"error": {"message": "timed out"}, "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                                                                    "prompt_tokens_details": {"cached_tokens": 0}}})

    class Client:
        model = "m"

        def cost(self, prompt, cached, completion):
            return 0.0

    assert _error_usage(Client(), ChatHTTPError(504, zero.encode(), {})) == []
