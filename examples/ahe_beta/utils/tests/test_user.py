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
    assert (s.private / "session.json").read_text() == "{}" and (s.private / "record.json").is_file()
    assert [json.loads(line)["kind"] for line in (log / "user-transcript.jsonl").read_text().splitlines()] == \
        ["message", "reply", "interrupt"]
    assert (log / "user-reward.txt").read_text().strip() == f"{2 / 3:g}"
    assert out.extra["user"]["rating"] == 3 and out.extra["user"]["interrupts"] == 1
    assert "infra" not in out.extra and out.tokens == 10                       # the agent's own usage


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
        judge.verified({}, RunOutput(output=None, extra={"unscored": "sandbox_failed", "verified": {"status": "infra"}}))
    failed = judge.user({}, RunOutput(output=None, extra={"adapter_failed": "context_over_limit"}))
    assert failed.score == 0.0 and failed.extra["status"] == "agent_error"


def test_the_verified_judge_scores_the_tests_and_keeps_the_users_rating():
    out = RunOutput(output="x", extra={"verified": {"status": "ok", "reward": 1.0, "passed": True},
                                       "user": {"score": 0.0, "rating": 1}})
    result = judge.verified({}, out)
    assert result.score == 1.0 and result.extra["user_rating"] == 1 and result.extra["passed"] is True
