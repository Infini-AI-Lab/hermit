"""user.py's session with a user, and judge.py, without a container: a stand-in remote environment records the
commands the session sends and answers them as a task's Harbor environment would (exit codes, merged output); the
Codex session itself is not run (its RunOutput is given). What a learner may read of a verdict, how the adapter's hooks
merge with the user's, which failures are infra, and what the judges score are checked; the channel's container side is
test_channel*.py's, and a real session is ../../smoke/run.yaml's."""

import json
from pathlib import Path, PurePosixPath
import subprocess
import sys

import pytest

from hermit.common import RunOutput
from hermit.environment.base import Environment, ExecResult, Limits
from hermit.task.verify import Unscored

UTILS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(UTILS))
import judge  # noqa: E402
import user  # noqa: E402
from channel import Conversation  # noqa: E402


class Remote(Environment):
    """A task's own environment as the agent sees it (isolation remote): every command recorded; `run_local` runs it
    with this machine's bash instead (for the user's command wrapper, whose parsing is under test)."""
    isolation = "remote"

    def __init__(self, run_local=False):
        super().__init__(PurePosixPath("/app"), Limits(timeout_s=600))
        self.commands, self.uploads, self.run_local = [], [], run_local
        self.instruction, self.name = "do the task", "t__abc"
        self.task_dir = Path("/tasks/t")
        self.network_public = True

    def exec(self, command, *, cwd=None, env=None, timeout=None, stdin=None, user=None, by_fd=None, stream=None,
             secret_env=()):
        self.commands.append({"command": command, "user": user, "stream": stream, "secret_env": list(secret_env)})
        if self.run_local:
            done = subprocess.run(list(command), input=stdin, capture_output=True, text=True, timeout=30)
            return ExecResult(done.returncode, done.stdout + done.stderr)
        return ExecResult(0, "")

    def upload(self, source, target):
        self.uploads.append((Path(source), str(target), Path(source).read_text() if Path(source).is_file() else None))
        return PurePosixPath(str(target))

    def with_limits(self, limits):
        other = Remote(self.run_local)
        other.commands, other.uploads, other.limits = self.commands, self.uploads, limits
        return other


def session(tmp_path, environment=None, spec=None):
    env = environment or Remote()
    return user._Session(object(), {"id": "r01-s00-t"}, None, env, spec or {"wait_s": 30}, tmp_path / "private", {})


def finished(s, tmp_path, transcript=(), extra=None):
    log = tmp_path / "log"
    log.mkdir(parents=True, exist_ok=True)
    s.output = RunOutput(output="reply", log=log, tokens=10, extra=dict(extra or {}))
    s.conversation = Conversation(None, lambda text: None, lambda: False, lambda: None, transport=object())
    s.conversation.transcript = list(transcript)
    s.private.mkdir(parents=True, exist_ok=True)
    return log


def test_the_adapters_stop_hooks_run_inside_the_users_and_its_other_hooks_stay(tmp_path):
    s = session(tmp_path)
    adapter = {"Stop": [{"hooks": [{"type": "command", "command": "python3 stop.py", "timeout": 10}]}],
               "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 audit.py"}]}]}
    merged = s.merged(adapter)
    assert s.settings["adapter_stop"] == [{"type": "command", "command": "python3 stop.py", "timeout": 10}]
    assert [g["hooks"][0]["command"] for g in merged["Stop"]] == [f"python3 -B {user.HOOK} Stop"]
    assert merged["PostToolUse"][0]["matcher"] == "Bash"                       # the adapter's own, first
    assert merged["PostToolUse"][1]["hooks"][0]["command"].endswith("PostToolUse")
    assert merged["Stop"][0]["hooks"][0]["timeout"] == 30 + 10 + 60            # the user's wait and the adapter's


def test_a_conditional_stop_hook_of_the_adapter_is_refused(tmp_path):
    with pytest.raises(ValueError, match="without a matcher"):
        session(tmp_path).merged({"Stop": [{"matcher": "x", "hooks": [{"type": "command", "command": "true"}]}]})


def test_the_deployments_codex_folder_is_merged_before_its_upload_and_locked_after(tmp_path):
    env = Remote()
    s = session(tmp_path, env)
    staged = tmp_path / "staging" / ".codex"
    staged.mkdir(parents=True)
    (staged / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "cd hermit/a || exit 1\npython3 hook_runner.py stop"}]}]}}))
    s.first = "the user's first message"
    hooked = user._Hooked(env, s)
    assert hooked.instruction == "the user's first message"                    # Codex's prompt
    hooked.upload(staged, ".codex")
    hooks = json.loads((staged / "hooks.json").read_text())["hooks"]
    assert [h["command"] for g in hooks["Stop"] for h in g["hooks"]] == [f"python3 -B {user.HOOK} Stop"]
    settings = json.loads(next(text for _, target, text in env.uploads if text and "adapter_stop" in text))
    assert settings["adapter_stop"][0]["command"].endswith("hook_runner.py stop")
    assert any(".codex" in str(c["command"]) and "chown -R root:root" in str(c["command"]) and c["user"] == "root"
               for c in env.commands)
    hooked.with_limits(Limits(timeout_s=5)).exec(["codex"], stream="agent-codex.jsonl", secret_env=["K"])
    assert env.commands[-1]["stream"] == "agent-codex.jsonl" and env.commands[-1]["secret_env"] == ["K"]


def test_a_learner_reads_the_visible_verdict_and_the_rest_stays_private(tmp_path):
    s = session(tmp_path)
    transcript = [{"from": "user", "kind": "message", "text": "go"}, {"from": "agent", "kind": "reply", "text": "ok"},
                  {"from": "user", "kind": "interrupt", "text": "stop"}]
    log = finished(s, tmp_path, transcript)
    verdict = {"score": 2 / 3, "rating": 3, "outcome": "accept", "reason": "fine", "issues": [{"title": "slow"}],
               "persona": {"id": "secret"}, "record": {"private": "checks"}, "files": {"session.json": "{}"}}
    out = s.result(verdict, None, 0.0)
    visible = json.loads((log / "user-verdict.json").read_text())
    assert set(visible) == {"score", "rating", "outcome", "reason", "issues"}
    assert "secret" not in (log / "user-verdict.json").read_text()
    assert (s.private / "files/session.json").read_text() == "{}" and (s.private / "record.json").is_file()
    assert [json.loads(line)["kind"] for line in (log / "user-transcript.jsonl").read_text().splitlines()] == \
        ["message", "reply", "interrupt"]
    assert (log / "user-reward.txt").read_text().strip() == f"{2 / 3:g}"
    assert out.extra["user"]["rating"] == 3 and out.extra["user"]["interrupts"] == 1
    assert "secret" not in json.dumps(out.extra) and "record" not in out.extra["user"]   # the learning material's too
    assert "infra" not in out.extra and out.tokens == 10                       # the agent's own usage


def test_an_environment_without_python_is_final_infra(tmp_path):
    s = session(tmp_path)
    finished(s, tmp_path)
    out = s.result(None, user.ChannelUnavailable("the user channel needs python3"), 0.0)
    assert out.extra["infra"] == "user_failed" and out.extra["infra_final"] is True


def test_a_user_that_failed_is_infra_and_leaves_no_reward(tmp_path):
    s = session(tmp_path)
    log = finished(s, tmp_path)
    out = s.result(None, RuntimeError("the user model was unreachable"), 0.0)
    assert out.extra["infra"] == "user_failed" and out.extra["infra_final"] is False
    assert "unreachable" in out.extra["exception_message"]
    assert not (log / "user-reward.txt").exists()
    assert "error" in json.loads((log / "user-verdict.json").read_text())


def test_the_sessions_own_infra_and_an_adapter_failure_are_kept_as_they_are(tmp_path):
    for word in ({"infra": "model_provider_failed"}, {"adapter_failed": "adapter_failed"}):
        s = session(tmp_path / next(iter(word)))
        finished(s, tmp_path / next(iter(word)), extra=word)
        out = s.result(None, RuntimeError("no reply"), 0.0)
        assert {k: out.extra[k] for k in word} == word and out.extra.get("infra") != "user_failed"


def test_the_users_commands_get_stdout_and_stderr_apart():
    s = session(Path("/nonexistent"), Remote(run_local=True))
    got = user.Workspace(s).run("echo out; echo err >&2; printf 'no newline'; exit 3")
    assert got["code"] == 3 and got["stdout"] == "out\nno newline" and got["stderr"] == "err\n"


def test_the_user_judge_scores_the_verdict_and_says_what_the_user_said():
    out = RunOutput(output="x", extra={"user": {"score": 1 / 3, "rating": 2, "reason": "it never showed progress",
                                                "issues": [{"title": "no progress", "body": "silent for 10 min"}]},
                                       "verified": {"status": "ok", "reward": 1.0}})
    result = judge.user({}, out)
    assert result.score == pytest.approx(1 / 3) and result.extra["rating"] == 2
    assert "never showed progress" in result.feedback and "no progress: silent for 10 min" in result.feedback
    assert "reward" not in result.feedback                                     # no verifier reaches the learner


def test_the_judges_refuse_a_rollout_that_is_not_the_agents_result():
    for extra in ({"unscored": "user_failed"}, {"user": {"error": "x", "score": None}}):
        with pytest.raises(Unscored):
            judge.user({}, RunOutput(output=None, extra=extra))
    with pytest.raises(Unscored):
        judge.verified({}, RunOutput(output=None, extra={"unscored": "sandbox_failed",
                                                         "verified": {"status": "infra"}}))
    failed = judge.user({}, RunOutput(output=None, extra={"adapter_failed": "context_over_limit"}))
    assert failed.score == 0.0 and failed.extra["status"] == "agent_error"


def test_the_verified_judge_scores_the_tests_and_keeps_the_users_rating():
    out = RunOutput(output="x", extra={"verified": {"status": "ok", "reward": 1.0, "passed": True},
                                       "user": {"score": 0.0, "rating": 1}})
    result = judge.verified({}, out)
    assert result.score == 1.0 and result.extra["user_rating"] == 1 and result.extra["passed"] is True


# --- the session's lifecycle, through UserCodexAgent.run: the channel's container side stood in by an in-memory
# transport, Codex by a stand-in that replies to the first message and ends on the user's end (or not at all)

class Memory:
    """The bridge as _Channel reaches it: records the user's records, hands the stand-in Codex's events back."""

    def __init__(self, session):
        self.session, self.sent, self.events, self.ended = session, [], [], __import__("threading").Event()
        session.memory = self

    def send(self, record):
        self.sent.append(record)
        if record.get("kind") == "end":
            self.ended.set()

    def read(self, since, wait=0.0):
        new = [e for e in self.events if e["seq"] > since]
        return (max([since, *(e["seq"] for e in new)]), new)

    def close(self):
        pass


class Codex:
    """Codex's session: it reads its prompt (the user's first message), replies, and runs until the user ends it,
    unless `extra` says it stopped by itself (its clock ran out)."""
    seen: list = []

    def __init__(self, params, base):
        self.params = params

    def run(self, task, adapter, environment):
        Codex.seen.append({"prompt": environment.instruction, "timeout_s": self.params.get("timeout_s")})
        memory = _session().memory
        memory.events.append({"seq": 1, "kind": "reply", "text": "done: " + environment.instruction})
        stops = Codex.stops
        if stops is None:
            memory.ended.wait(30)
        log = Path(Codex.log)
        log.mkdir(parents=True, exist_ok=True)
        return RunOutput(output="done", log=log, tokens=7, extra=dict(stops or {}))


def _session():
    return Codex.session


SIMULATE = '''
def simulate(task, agent, workspace, params):
    agent.write("please " + task["instruction"])
    reply = agent.poll(30)
    if params.get("then") == "raise":
        raise RuntimeError("the user model was unreachable")
    if params.get("then") == "phase":
        raise RuntimeError("AgentPhaseOver: exec: the agent's phase ended")
    if params.get("then") == "poll":
        agent.write("and more")
        agent.poll(30)                     # Codex is gone: ConversationClosed
    if params.get("then") == "service":
        workspace.service_run("measure", service="capture", timeout=10, cwd="/")
    return {"score": 1.0, "rating": 4, "reason": reply["text"], "persona": "secret"}
'''


@pytest.fixture
def run(tmp_path, monkeypatch):
    """UserCodexAgent.run on a stand-in environment with a 1,000 s agent budget, then: its RunOutput."""
    (tmp_path / "sim.py").write_text(SIMULATE)
    real = user._Session.__init__

    def init(self, *args, **kwargs):
        real(self, *args, **kwargs)
        Codex.session = self
    monkeypatch.setattr(user._Session, "__init__", init)
    monkeypatch.setattr(user._Session, "start_channel", lambda self: None)
    monkeypatch.setattr(user._Session, "stop_channel", lambda self: None)
    monkeypatch.setattr(user, "_Channel", Memory)
    monkeypatch.setattr(user, "CliAgent", Codex)
    Codex.seen, Codex.log = [], str(tmp_path / "codex-log")

    def go(then=None, stops=None, exit_wait_s=5):
        Codex.stops = stops
        env = Remote()
        env.limits = Limits(timeout_s=1000)
        spec = {"simulate": "sim.py:simulate", "params": {"then": then}, "exit_wait_s": exit_wait_s, "review_s": 300}
        agent = user.UserCodexAgent({"user": spec, "workspaces": str(tmp_path / "ws"), "prompt": "ignored"}, tmp_path)
        return agent.run({"id": "r01-s00-t"}, None, environment=env)
    return go


def test_the_first_message_is_codexs_prompt_and_its_clock_leaves_the_user_time(run):
    out = run()
    assert Codex.seen == [{"prompt": "please do the task", "timeout_s": 700.0}]     # 1,000 s less review_s
    assert out.extra["user"]["score"] == 1.0 and out.extra["user"]["reason"] == "done: please do the task"
    assert "infra" not in out.extra and "persona" not in out.extra["user"] and out.tokens == 7
    assert [r["kind"] for r in Codex.session.memory.sent] == ["end"]                # the session ended by the user


def test_a_user_that_raises_ends_codex_and_is_infra(run):
    out = run("raise")
    assert Codex.session.memory.ended.is_set() and not Codex.session.thread.is_alive()
    assert out.extra["infra"] == "user_failed" and out.extra["infra_final"] is False


def test_harbors_end_of_the_phase_is_the_agents_timeout_not_infra(run):
    out = run("phase", exit_wait_s=0.5)
    Codex.session.memory.ended.set()                     # the stand-in's session ends as the phase's end ends a CLI's
    Codex.session.thread.join(5)
    assert "infra" not in out.extra and out.extra["timed_out"] is True
    assert out.extra["user"]["score"] == 0.0 and out.extra["user"]["rating"] == 1
    assert "AgentPhaseOver" in out.extra["user_error"]


def test_a_codex_that_ran_out_of_its_time_is_scored_zero_not_retried(run):
    out = run("poll", stops={"timed_out": True})
    assert "infra" not in out.extra and out.extra["user"]["outcome"] == "timed_out"


def test_a_task_that_needs_a_companion_container_fails_for_good(run):
    out = run("service")
    assert out.extra["infra"] == "user_failed" and out.extra["infra_final"] is True
    assert "companion container 'capture'" in out.extra["exception_message"]


# --- AHE-Beta's verdict: what a learner may read of it (ahe_user.visible)

def test_ahe_betas_outcome_and_session_stay_private():
    import ahe_user
    native = {"score": 1 / 3, "rating": 2, "score_kind": "user_experience", "session_id": "s", "artifact_root": "/p",
              "outcome": {"status": "completed", "review_verdict": "unfulfilled", "harness_reply": "x",
                          "review_trace": [{"system": "# PERSONA (id: sr-secret) PERSONA-SECRET"}]},
              "feedback": {"experience": {"reason": "slow, no progress"}, "task_assessment": {"status": "unfulfilled"},
                           "opportunities": [{"kind": "diagnostic", "title": "t", "body": "show progress"}]},
              "usage": {"total_tokens": 9}, "files": {"session.json": "{}"}}
    out = ahe_user.visible(native)
    shown = {k: v for k, v in out.items() if k not in ("files", "record")}
    assert "PERSONA-SECRET" not in json.dumps(shown) and "PERSONA-SECRET" in out["files"]["outcome.json"]
    assert out["outcome"] == {"status": "completed", "review_verdict": "unfulfilled"}
    assert out["reason"] == "slow, no progress" and out["issues"][0]["body"] == "show progress"
    assert out["usage"] == {"total_tokens": 9} and out["files"]["session.json"] == "{}"


# --- the container side on the oldest Python the channel supports

def _old_python():
    import shutil
    for name in ("python3.8", "python3.9"):
        found = shutil.which(name)
        if found:
            return found
    uv = shutil.which("uv")
    if uv:
        done = subprocess.run([uv, "python", "find", "3.8"], capture_output=True, text=True)
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout.strip()
    return None


def test_the_bridge_and_the_hook_run_on_python_3_8(tmp_path):
    old = _old_python()
    if old is None:
        pytest.skip("no Python 3.8 or 3.9 on this machine (an image's python3 may be that old)")
    server = subprocess.Popen([old, "-B", str(UTILS / "channel_server.py"), "serve", "--root", "root", "--pid-file",
                               "pid"], cwd=tmp_path, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(100):
            if (tmp_path / "root/channel.sock").exists():
                break
            __import__("time").sleep(0.05)
        __import__("time").sleep(1.0)                    # past its accept timeout: still serving
        assert server.poll() is None, server.stderr.read()
        (tmp_path / "user_hook.py").write_bytes((UTILS / "user_hook.py").read_bytes())
        (tmp_path / "user_hook.json").write_text(json.dumps({"channel": "root/channel.sock", "wait_s": 1,
                                                             "adapter_stop": [], "reply_frame": "{text}",
                                                             "insert_frame": "{text}", "interrupt_frame": "{text}"}))
        (tmp_path / "codex").write_text(f"#!/bin/sh\n{old} \"$1\" Stop\n")
        (tmp_path / "codex").chmod(0o755)
        subprocess.run([old, "-B", str(UTILS / "channel_server.py"), "enqueue", "--root", "root"], cwd=tmp_path,
                       input=json.dumps({"id": "u1", "kind": "message", "text": "go on"}), text=True, check=True)
        hook = subprocess.run(["./codex", "./user_hook.py"], cwd=tmp_path, capture_output=True, text=True, timeout=20,
                              input=json.dumps({"last_assistant_message": "done"}))
        assert hook.returncode == 0, hook.stderr
        assert json.loads(hook.stdout) == {"decision": "block", "reason": "go on"}
    finally:
        (tmp_path / "root").mkdir(exist_ok=True)
        (tmp_path / "root/STOP").write_text("stop\n")
        server.wait(timeout=10)
