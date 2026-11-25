"""user.py's session with a user, and judge.py, without a container: a stand-in remote environment records the
commands the session sends and answers them as a task's Harbor environment would (exit codes, merged output); the
Codex session itself is not run (its RunOutput is given). What a learner may read of a verdict, how the adapter's hooks
merge with the user's, which failures are infra, and what the judges score are checked; the channel's container side is
test_channel*.py's, and a real session is ../../smoke/run.yaml's."""

import functools
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import threading
import time
import types

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


def test_the_deployments_codex_folder_is_merged_before_its_upload_and_locked_after(tmp_path, monkeypatch):
    monkeypatch.setattr(user._Session, "preserve", lambda self, paths, **kw: None)   # kept aside: elsewhere
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
    assert any(".codex" in str(c["command"]) and "chown root:root" in str(c["command"]) and c["user"] == "root"
               for c in env.commands)
    assert not any("chown -R" in str(c["command"]) for c in env.commands)   # a task's own files keep their owner
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
    assert (s.records / "files/session.json").read_text() == "{}" and (s.records / "record.json").is_file()
    assert not (s.private / "files").exists()                                 # the simulator's folder: its own alone
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


class Probe(Remote):
    """The python3 probe answered with a given exit code (and timeout); every other command succeeds."""

    def __init__(self, code, timed_out=False):
        super().__init__()
        self.code, self.timed_out = code, timed_out

    def exec(self, command, **kwargs):
        if list(command[:2]) == ["python3", "-c"]:
            return ExecResult(self.code, "", self.timed_out)
        return super().exec(command, **kwargs)


def test_only_an_old_or_missing_python_is_final_and_a_failed_probe_is_run_again(tmp_path):
    """A probe that timed out said nothing about the image: run again, not recorded as unrunnable for good."""
    for code in (1, 126, 127):
        with pytest.raises(user.ChannelUnavailable):
            session(tmp_path, Probe(code)).start_channel()
    with pytest.raises(RuntimeError) as failed:
        session(tmp_path, Probe(124, timed_out=True)).start_channel()
    assert not isinstance(failed.value, user.ChannelUnavailable)


def test_a_session_whose_staging_failed_keeps_its_infra(tmp_path):
    """A rollout whose staging failed has no log (hermit.adapter.session.staging_failed): its infra is returned."""
    s = session(tmp_path)
    s.output = RunOutput(output=None, log=None, extra={"infra": "staging_failed", "infra_final": False})
    s.conversation = Conversation(None, lambda text: None, lambda: False, lambda: None, transport=object())
    out = s.result(None, RuntimeError("Codex exited without a reply"), 0.0)
    assert out.extra["infra"] == "staging_failed" and Path(out.log).is_dir()


def test_what_the_run_places_in_the_workspace_is_out_of_the_agents_change(tmp_path, monkeypatch):
    """AHE-Beta reviews the agent's change as `git add -A` of the tree into an index of its own: the channel's hooks and
    the adapter's deployment (untracked) are listed in the repository's exclude file; a tracked file stays the
    agent's."""
    repo = tmp_path / "repo"

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    repo.mkdir()
    git("init", "-q")
    (repo / "src.py").write_text("a = 1\n")
    git("add", "src.py")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "task")
    for name in (".codex/hooks.json", "AGENTS.md", ".hermit-deploy.json", "hermit/ahe-beta-adapter/ADAPTER.md"):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("deployed\n")
    (repo / "src.py").write_text("a = 2\n")                                  # the agent's change
    (repo / "fix.py").write_text("b = 1\n")
    monkeypatch.setattr(user, "DEPLOYED", str(tmp_path / "deployed"))        # never the machine's own /run
    env = Remote(run_local=True)
    env.workspace = PurePosixPath(str(repo))
    s = session(tmp_path, env)
    s.exclude([".codex/hooks.json"])                                           # as start_channel does
    staged = tmp_path / "staged"
    (staged / "hermit/ahe-beta-adapter").mkdir(parents=True)
    for name in ("AGENTS.md", ".hermit-deploy.json", "hermit/ahe-beta-adapter/ADAPTER.md"):
        (staged / name).write_text("deployed\n")
    hooked = user._Hooked(env, s)
    for entry in sorted(staged.iterdir()):                                     # as the deployment uploads
        hooked.upload(entry, entry.name)
    index = tmp_path / "index"
    subprocess.run(["sh", "-c", f'cd "{repo}" && export GIT_INDEX_FILE="{index}" && git read-tree HEAD && git add -A'],
                   check=True)
    diff = f'cd "{repo}" && GIT_INDEX_FILE="{index}" git diff --cached --name-only HEAD'
    changed = subprocess.run(["sh", "-c", diff], capture_output=True, text=True, check=True).stdout.split()
    assert changed == ["fix.py", "src.py"]


class Deploying(Remote):
    """Remote, whose uploads land in its workspace on this machine, as a deployment's land in a task's."""

    def upload(self, source, target):
        super().upload(source, target)
        place = Path(str(self.workspace)) / str(target)
        if Path(source).is_dir():
            shutil.copytree(source, place, dirs_exist_ok=True)
        else:
            shutil.copy2(source, place)
        return PurePosixPath(str(target))


def task_repository(tmp_path, files, links=()):
    """A task's repository: `files` {path: text} and `links` {path: target}, committed."""
    repo = tmp_path / "repo"
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    for path, target in dict(links).items():
        (repo / path).symlink_to(target)
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


def deployed_session(tmp_path, monkeypatch, repo, adapter=True):
    """A session over `repo` with the channel's hooks placed as start_channel places them (with an adapter to deploy,
    the context file too: the deployment writes it, and may unlink a link there before failing)."""
    monkeypatch.setattr(user, "DEPLOYED", str(tmp_path / "deployed"))
    env = Deploying(run_local=True)
    env.workspace = PurePosixPath(str(repo))
    s = session(tmp_path, env)
    s.preserve([".codex/hooks.json"])
    s.preserve([user.CONTEXT], placing=adapter)
    (repo / ".codex").mkdir(exist_ok=True)
    (repo / ".codex/hooks.json").write_text("{}")
    return env, s


def deploy(tmp_path, env, s, context="adapter\n"):
    """The adapter's deployment, entry by entry, as Hermit stages and uploads it."""
    staged = tmp_path / "staged"
    (staged / "hermit/ahe-beta-adapter/agents").mkdir(parents=True)              # empty, as Codex's .codex/agents
    (staged / "hermit/ahe-beta-adapter/ADAPTER.md").write_text("adapter\n")
    (staged / "AGENTS.md").write_text(context)
    (staged / ".hermit-deploy.json").write_text("{}")
    hooked = user._Hooked(env, s)
    for entry in sorted(staged.iterdir()):
        hooked.upload(entry, entry.name)


def status(repo):
    out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
                         capture_output=True, text=True, check=True).stdout
    return sorted(line for line in out.split("\n") if line)


def test_what_the_deployment_overlaid_is_put_back_before_the_review(tmp_path, monkeypatch):
    """A task's own AGENTS.md, which the deployment prepends the adapter to, and its own hermit/ folder, beside which
    the deployment places its own: once the agent stops, only what the run placed is taken back."""
    repo = task_repository(tmp_path, {"AGENTS.md": "the task's notes\n", "hermit/production.py": "a = 1\n"})
    env, s = deployed_session(tmp_path, monkeypatch, repo)
    deploy(tmp_path, env, s, context="adapter\n\nthe task's notes\n")
    assert (repo / "AGENTS.md").read_text().startswith("adapter")
    (repo / "hermit/ahe-beta-adapter/__pycache__").mkdir()                    # a deployed helper imported by another
    (repo / "hermit/ahe-beta-adapter/__pycache__/helper.cpython-312.pyc").write_bytes(b"")
    (repo / "hermit/production.py").write_text("a = 2\n")                    # the agent's change
    (repo / "hermit/new.py").write_text("b = 1\n")
    (repo / "fix.py").write_text("c = 1\n")
    s.undeploy()
    assert (repo / "AGENTS.md").read_text() == "the task's notes\n"
    assert not any((repo / name).exists() for name in (".codex", "hermit/ahe-beta-adapter", ".hermit-deploy.json"))
    assert status(repo) == [" M hermit/production.py", "?? fix.py", "?? hermit/new.py"]
    assert "hermit-user-channel" not in (repo / ".git/info/exclude").read_text()   # its exclusions taken back too


def test_a_link_the_deployment_unlinked_before_failing_is_a_link_again(tmp_path, monkeypatch):
    """An adapter whose merged context is over Codex's limit fails after Hermit unlinked a linked AGENTS.md, with no
    upload: the context file was kept aside and marked placed at the channel's start."""
    repo = task_repository(tmp_path, {"CLAUDE.md": "notes\n"}, links={"AGENTS.md": "CLAUDE.md"})
    env, s = deployed_session(tmp_path, monkeypatch, repo)
    (repo / "AGENTS.md").unlink()
    s.undeploy()
    assert os.readlink(repo / "AGENTS.md") == "CLAUDE.md" and status(repo) == []


def test_a_file_that_could_not_be_kept_aside_is_never_taken_back(tmp_path, monkeypatch):
    """Marked placed before its copy failed, the original would be removed with nothing to put back."""
    repo = task_repository(tmp_path, {"AGENTS.md": "notes\n"})
    monkeypatch.setattr(user, "DEPLOYED", str(tmp_path / "a-file"))
    (tmp_path / "a-file").write_text("not a folder")                         # every copy aside fails
    env = Deploying(run_local=True)
    env.workspace = PurePosixPath(str(repo))
    s = session(tmp_path, env)
    with pytest.raises(RuntimeError):
        s.preserve([user.CONTEXT])
    assert user.CONTEXT not in s.touched
    s.undeploy()
    assert (repo / "AGENTS.md").read_text() == "notes\n"


def test_a_folder_the_run_made_that_became_a_link_is_not_followed(tmp_path, monkeypatch):
    """An empty folder the deployment made (Codex's .codex/agents, here the adapter's agents/), replaced by a link:
    the cleanup of its bytecode would follow it as root."""
    outside = tmp_path / "outside"
    (outside / "__pycache__").mkdir(parents=True)
    (outside / "__pycache__/keep.pyc").write_bytes(b"")
    repo = task_repository(tmp_path, {"src.py": "a = 1\n"})
    env, s = deployed_session(tmp_path, monkeypatch, repo)
    deploy(tmp_path, env, s, context="adapter\n")
    (repo / "hermit/ahe-beta-adapter/agents").rmdir()
    (repo / "hermit/ahe-beta-adapter/agents").symlink_to(outside)
    with pytest.raises(RuntimeError, match="could not be put back"):
        s.undeploy()
    assert (outside / "__pycache__/keep.pyc").exists()


def test_a_link_on_a_placed_files_way_stops_root_from_following_it(tmp_path, monkeypatch):
    """Root's commands follow a linked folder: the run neither keeps aside nor takes back a path through one."""
    outside = tmp_path / "outside"
    (outside / "ahe-beta-adapter").mkdir(parents=True)
    (outside / "ahe-beta-adapter/ADAPTER.md").write_text("not the workspace's\n")
    repo = task_repository(tmp_path, {"src.py": "a = 1\n"})
    env, s = deployed_session(tmp_path, monkeypatch, repo)
    (repo / "hermit").symlink_to(outside)
    with pytest.raises(RuntimeError, match="a link on the way"):
        s.preserve(["hermit/ahe-beta-adapter/ADAPTER.md"])
    s.touched.add("hermit/ahe-beta-adapter/ADAPTER.md")                       # as if the link came afterwards
    with pytest.raises(RuntimeError, match="could not be put back"):
        s.undeploy()
    assert (outside / "ahe-beta-adapter/ADAPTER.md").read_text() == "not the workspace's\n"


def test_a_tracked_link_the_deployment_unlinked_is_a_link_again(tmp_path, monkeypatch):
    """Hermit's deployment unlinks a linked AGENTS.md before it writes its own (hermit.agent.acting.workspace): it was
    kept aside at the channel's start, before the deployment staged anything."""
    repo = task_repository(tmp_path, {"CLAUDE.md": "the task's notes\n"}, links={"AGENTS.md": "CLAUDE.md"})
    env, s = deployed_session(tmp_path, monkeypatch, repo)
    (repo / "AGENTS.md").unlink()                                              # as the deployment stages
    deploy(tmp_path, env, s)
    s.undeploy()
    assert (repo / "AGENTS.md").is_symlink() and os.readlink(repo / "AGENTS.md") == "CLAUDE.md"
    assert status(repo) == []


def test_a_workspace_that_could_not_be_put_back_is_never_reviewed(tmp_path, monkeypatch):
    repo = task_repository(tmp_path, {"AGENTS.md": "the task's notes\n"})
    env, s = deployed_session(tmp_path, monkeypatch, repo)
    deploy(tmp_path, env, s)
    (tmp_path / "deployed" / "AGENTS.md").unlink()                             # what it overlaid, lost
    with pytest.raises(RuntimeError, match="could not be put back"):
        s.undeploy()


def test_a_result_without_a_mapping_or_with_files_that_are_not_files_is_refused():
    for got in ("unavailable", {"score": 1.0, "files": ["notes.txt"]}, {"score": 1.0, "files": {"../x": "y"}}):
        with pytest.raises(user.UserFailed):
            user._valid(got)


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
    """An empty stdout is an empty line of the framing: stderr alone used to come back as stdout."""
    s = session(Path("/nonexistent"), Remote(run_local=True))
    got = user.Workspace(s).run("echo out; echo err >&2; printf 'no newline'; exit 3")
    assert got["code"] == 3 and got["stdout"] == "out\nno newline" and got["stderr"] == "err\n"
    got = user.Workspace(s).run("echo missing >&2; exit 1")
    assert got["stdout"] == "" and got["stderr"] == "missing\n"


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
        Codex.seen.append({"prompt": environment.instruction, "timeout_s": self.params.get("timeout_s"),
                           "deadline_s": self.params.get("deadline_s")})
        memory = _session().memory
        memory.events.append({"seq": 1, "kind": "reply", "text": "done: " + environment.instruction})
        stops = Codex.stops
        if stops is None:
            memory.ended.wait(30)
            if Codex.phase_at_end:                       # Harbor's phase ends while the session is being stopped
                raise RuntimeError("AgentPhaseOver: exec: the agent's phase ended")
        if Codex.fail is not None:                       # the environment's failure under Codex: the run's to classify
            raise Codex.fail
        log = Path(Codex.log)
        log.mkdir(parents=True, exist_ok=True)
        return RunOutput(output="done", log=log, tokens=7, cost_usd=Codex.cost, extra=dict(stops or {}))


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
    if params.get("then") == "string":
        return "unavailable"
    if params.get("then") == "late":
        workspace.quiesce()                    # the agent's session stopped in time; the review then fails late
        __import__("time").sleep(params.get("sleep", 1.2))
        raise RuntimeError("the review's model call failed")
    if params.get("then") == "misconfigured":
        raise type("Misconfigured", (ValueError,), {"final": True})("USER_KEY is not set")
    if params.get("then") == "usage":
        return {"score": 0.5, "rating": 2, "usage": {"tokens": 5, "cost_usd": None}}
    if params.get("then") == "priced":
        return {"score": 0.5, "rating": 2, "usage": {"tokens": 5, "cost_usd": 0.25}}
    if params.get("then") == "signin":
        raise RuntimeError("the simulated user's model lost its sign-in [hermit_login_failed]")
    if params.get("then") == "misconfigured_late":     # the user's own setup ran past the session's deadline
        __import__("time").sleep(params.get("sleep", 1.2))
        raise type("NotBound", (RuntimeError,), {"final": True})("this session's adapter is not the bound one")
    if params.get("then") == "signin_late":            # its client's retries ran past the session's deadline
        __import__("time").sleep(params.get("sleep", 1.2))
        raise RuntimeError("the simulated user's model lost its sign-in [hermit_login_failed]")
    if params.get("then") == "partly":
        return {"score": 0.5, "rating": 2, "usage": {"tokens": 5, "cost_usd": 0.25, "uncounted_calls": 1}}
    if params.get("then") == "spent":
        failure = RuntimeError("the capture failed")
        failure.usage = {"tokens": 5, "cost_usd": 0.25}
        raise failure
    if params.get("then") == "direct":
        agent.write("and more")
        try:
            agent.poll(30)                     # Codex is gone: AHE-Beta's direct path keeps only the text
        except Exception:
            pass
        raise RuntimeError("session failed; inspect the retained native session artifacts")
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
    Codex.seen, Codex.log, Codex.phase_at_end, Codex.cost = [], str(tmp_path / "codex-log"), False, None
    Codex.fail = None

    def go(then=None, stops=None, exit_wait_s=5, deadline_in=None, review_s=300):
        Codex.stops = stops
        env = Remote()
        env.limits = Limits(timeout_s=1000)
        if deadline_in is not None:                      # Harbor's deadline for the agent's phase
            env.deadline_at = time.time() + deadline_in
        spec = {"simulate": "sim.py:simulate", "params": {"then": then}, "exit_wait_s": exit_wait_s,
                "review_s": review_s}
        agent = user.UserCodexAgent({"user": spec, "workspaces": str(tmp_path / "ws"), "prompt": "ignored"}, tmp_path)
        return agent.run({"id": "r01-s00-t"}, None, environment=env)
    return go


def test_the_first_message_is_codexs_prompt_and_its_clock_leaves_the_user_time(run):
    out = run()
    assert Codex.seen == [{"prompt": "please do the task", "timeout_s": 700.0, "deadline_s": None}]   # less review_s
    assert out.extra["user"]["score"] == 1.0 and out.extra["user"]["reason"] == "done: please do the task"
    assert "infra" not in out.extra and "persona" not in out.extra["user"] and out.tokens == 7
    assert [r["kind"] for r in Codex.session.memory.sent] == ["end"]                # the session ended by the user


def test_codexs_clock_ends_review_s_before_the_tasks_deadline(run):
    """What the setup and the user's first turn took comes off Codex's time, not the user's review's."""
    run(deadline_in=400)
    seen = Codex.seen[0]                                 # the whole call's window: the deployment comes off it too
    assert 90 <= seen["deadline_s"] <= 100 and seen["timeout_s"] == seen["deadline_s"]   # 400 s left less review_s


def test_a_direct_session_whose_codex_clock_ended_is_the_agents_zero_though_its_error_lost_the_cause(run):
    """AHE-Beta's direct path keeps a closed conversation as text and raises afresh after its review."""
    out = run("direct", stops={"timed_out": True})
    assert "infra" not in out.extra and out.extra["user"]["outcome"] == "timed_out"


def test_a_user_failure_before_the_deadline_stays_infra_though_the_phase_ended_during_the_cleanup(run):
    Codex.phase_at_end = True
    out = run("raise")
    assert out.extra["infra"] == "user_failed" and out.extra["user"].get("outcome") != "timed_out"


def test_a_user_failure_after_the_session_was_stopped_in_time_is_infra_though_it_came_late(run):
    """AHE-Beta freezes the workspace (and stops Codex) before its review: a review that then fails past the deadline
    is the user's failure, not the agent's time running out."""
    out = run("late", deadline_in=1.0, review_s=0)
    assert out.extra["infra"] == "user_failed" and out.extra["user"].get("outcome") != "timed_out"


def test_a_user_failure_its_own_configuration_makes_is_final(run):
    out = run("misconfigured")
    assert out.extra["infra"] == "user_failed" and out.extra["infra_final"] is True


def test_the_user_models_spend_is_the_rollouts_and_the_actors_own_is_kept(run):
    out = run("usage")
    assert out.tokens == 12 and out.extra["actor"] == {"tokens": 7, "cost_usd": None}   # 7 the actor's, 5 the user's
    assert out.cost_usd is None and out.extra["user_cost_unknown"] is True


def test_what_is_priced_counts_whichever_side_has_no_price(run):
    """An API actor with a subscription user, or the other way round: the known part is not lost (a None cost
    counts as 0 in the run's budget)."""
    assert run("priced").cost_usd == 0.25                                       # a subscription actor, a paid user
    Codex.cost = 0.75
    out = run("usage")                                                          # a paid actor, a subscription user
    assert out.cost_usd == 0.75 and out.extra["user_cost_unknown"] is True
    assert run("priced").cost_usd == 1.0


def test_the_counted_calls_cost_counts_though_one_call_went_uncounted(run):
    """A failed attempt the persona client retried reports no usage: the session's known cost still counts."""
    out = run("partly")
    assert out.cost_usd == 0.25 and out.extra["user_cost_unknown"] is True


def test_a_user_whose_model_lost_its_sign_in_ends_the_run(run):
    """Scored as the user's failure, it would be run again: another acting session bought for a sign-in that serves
    nobody until it is restored. Raised, the rollout reads Hermit's mark and ends the run (LoginFailed)."""
    from hermit.agent.base.interface import login_failure
    with pytest.raises(RuntimeError) as failed:
        run("signin")
    assert login_failure(failed.value)
    for then, kwargs in (("signin", {"stops": {"infra": "model_provider_failed"}}),   # with the actor's own failure,
                         ("signin_late", {"deadline_in": 1.0, "review_s": 0})):     # or past the deadline: still
        with pytest.raises(RuntimeError) as failed:                                  # the run's end
            run(then, **kwargs)
        assert login_failure(failed.value)
    Codex.fail = RuntimeError("the environment's exec failed")    # and with an exception of the actor's, raised
    with pytest.raises(RuntimeError) as failed:
        run("signin")
    assert login_failure(failed.value)


def test_a_session_no_run_mends_stays_so_past_the_deadline(run):
    """Unbound or misconfigured, it never had a valid session: not the agent's 0 for running out of time."""
    out = run("misconfigured_late", deadline_in=1.0, review_s=0)
    assert out.extra["infra"] == "user_failed" and out.extra["infra_final"] is True
    assert out.extra["user"].get("outcome") != "timed_out"


def test_a_failed_users_spend_still_counts(run):
    out = run("spent")
    assert out.extra["infra"] == "user_failed" and out.tokens == 12 and out.cost_usd == 0.25


def test_a_codex_failure_still_carries_what_the_user_model_spent(run):
    """An environment's failure under Codex is the run's to classify (run again): the user's spend of that attempt
    goes with it (the Trainer's `spent`), else its model calls are never counted."""
    Codex.fail = RuntimeError("the environment's exec failed")
    with pytest.raises(RuntimeError, match="exec failed") as failed:
        run("priced")
    assert (failed.value.spent.tokens, failed.value.spent.cost_usd) == (5, 0.25)


def test_a_quote_in_a_placed_paths_folder_stays_a_name_in_roots_script(tmp_path):
    """A skill folder the learner named with a quote broke the parsing of the script run as root (and its text could
    have been read as shell): the diagnostic is one quoted word."""
    (tmp_path / "author's").mkdir()

    def checked():
        return subprocess.run(["bash", "-c", "set -e\n" + "\n".join(user._unlinked(["author's/SKILL.md"]))],
                              cwd=tmp_path, capture_output=True, text=True)
    assert checked().returncode == 0
    (tmp_path / "author's").rmdir()
    (tmp_path / "author's").symlink_to(tmp_path)
    linked = checked()
    assert linked.returncode == 3 and "a link on the way: author's" in linked.stderr


def test_the_smoke_scores_the_file_its_steps_leave_not_any_file_its_reply_repeats():
    import scripted_user as scripted
    sent = {scripted.INSERT: "u2", scripted.FOLLOWUP: "u3"}
    seen = [{"kind": "delivered", "at": "PostToolUse", "ids": ["u2"]}, {"kind": "delivered", "at": "Stop",
                                                                         "ids": ["u3"]},
            {"kind": "denied", "input": {"command": "echo four >> /app/a.txt"}}]
    replies = [{"interrupted": False}, {"interrupted": True}]
    right = scripted.scored(seen, [*replies, {"text": "The file reads: one two inserted three."}],
                            "one\ntwo\ninserted\nthree\n", sent)
    assert all(right.values())
    short = scripted.scored(seen, [*replies, {"text": "inserted three"}], "inserted\nthree\n", sent)
    assert not any(short[name] for name in ("inserted_line", "followup_done", "final_reply_is_the_file_in_order"))
    doubled = scripted.scored(seen, [*replies, {"text": "one two inserted three"}],
                              "one\ntwo\ninserted\nthree\nthree\n", sent)
    assert not doubled["followup_done"]
    one_line = scripted.scored(seen, [*replies, {"text": "one two inserted three five"}], "one two inserted three\n",
                               sent)
    assert not one_line["followup_done"] and not one_line["final_reply_is_the_file_in_order"]


def test_a_persona_reply_that_failed_to_parse_stays_in_the_runs_records(tmp_path, monkeypatch):
    """AHE-Beta writes such replies into its checkout (backend/data), shared by every run: here, the run's records."""
    import ahe_user
    persona = types.ModuleType("ahe_beta.user.sim.persona_client")
    persona._dump_unparseable = lambda **kw: pytest.fail("written into the checkout")
    session = types.ModuleType("ahe_beta.integrations.hermit.session")
    session.simulate = lambda task, agent, workspace, native: {"score": 1.0, "rating": 4}
    for name in ("ahe_beta", "ahe_beta.user", "ahe_beta.user.sim", "ahe_beta.integrations",
                 "ahe_beta.integrations.hermit"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "ahe_beta.user.sim.persona_client", persona)
    monkeypatch.setitem(sys.modules, "ahe_beta.integrations.hermit.session", session)
    monkeypatch.setattr(ahe_user, "_MODEL", None)
    monkeypatch.setattr(ahe_user, "_CLOSE", [])
    for name, value in (("USER_LLM_BASE_URL", "http://127.0.0.1:9/v1"), ("USER_LLM_MODEL", "m"),
                        ("AHE_BETA_USAGE_PATH", "")):
        monkeypatch.setenv(name, value)
    ahe_user.simulate({"id": "r01-s00-t", "name": "t"}, None, None,
                      {"artifact_root": str(tmp_path / "users/s1"), "session_id": "s1", "persona": {},
                       "scenario": {"workspace": {"params": {}}}})
    persona._dump_unparseable(first="not json", second="still not json")
    kept = (tmp_path / "users/user-model/parse_failures.jsonl").read_text()
    assert json.loads(kept) == {"first_raw": "not json", "retry_raw": "still not json"}


def test_the_bridges_drain_output_is_read_by_its_line_ends_only():
    """A reply holding U+2028 (text copied from a document): splitlines cut its JSON line there, and the read failed."""
    printed = "noise\n" + json.dumps({"next": 1, "events": [{"kind": "reply", "text": "a\u2028b"}]}, ensure_ascii=False)
    channel = user._Channel(types.SimpleNamespace(root=lambda command, **kw: printed))
    assert channel.read(0) == (1, [{"kind": "reply", "text": "a\u2028b"}])


def test_a_simulator_result_that_is_no_mapping_is_infra(run):
    out = run("string")
    assert out.extra["infra"] == "user_failed" and "not a dict" in out.extra["exception_message"]


def test_a_failure_to_stop_after_the_users_verdict_keeps_the_verdict(run, monkeypatch):
    real = user._Session.stop

    def stop(self):
        real(self)
        raise RuntimeError("the rollout's processes would not stop")
    monkeypatch.setattr(user._Session, "stop", stop)
    out = run()
    assert out.extra["user"]["score"] == 1.0 and "infra" not in out.extra
    assert "would not stop" in out.extra["user_cleanup_error"]


def test_a_user_model_failure_after_codexs_clock_ran_out_is_infra_not_the_agents_zero(run):
    """Codex's own timeout makes a session the user could not continue the agent's 0, not any failure of the user's."""
    out = run("raise", stops={"timed_out": True})
    assert out.extra["infra"] == "user_failed" and out.extra["user"].get("outcome") != "timed_out"


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


def native_session(captured):
    """AHE-Beta's session module, stood in: it titles an untitled issue as upstream does (sim_user.py: the scenario's
    task_key, else the persona's identity) and returns its native verdict."""
    def simulate(task, agent, workspace, native):
        captured.append(native)
        title = native["scenario"]["workspace"]["params"].get("task_key") or native["persona"]["identity"]
        return {"score": 0.0, "rating": 1, "feedback": {"opportunities": [{"title": title, "body": "show progress"}]},
                "outcome": {}, "session_id": native["session_id"], "artifact_root": native["artifact_root"]}
    return simulate


def test_ahe_betas_untitled_issue_is_titled_with_the_task_not_the_persona(tmp_path, monkeypatch):
    import ahe_user
    captured = []
    module = types.ModuleType("ahe_beta.integrations.hermit.session")
    module.simulate = native_session(captured)
    for name in ("ahe_beta", "ahe_beta.integrations", "ahe_beta.integrations.hermit"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "ahe_beta.integrations.hermit.session", module)
    monkeypatch.setattr(ahe_user, "_MODEL", {})                               # the user's model already set up
    params = {"artifact_root": str(tmp_path / "s1"), "session_id": "s1", "adapter_sha256": "d",
              "persona": {"identity": "sr-secret"}, "scenario": {"workspace": {"params": {"task_id": "o14"}}}}
    out = ahe_user.simulate({"id": "r01-s00-o14", "name": "o14"}, None, None, params)
    shown = {k: v for k, v in out.items() if k not in ("files", "record")}      # what a learner may read
    assert out["issues"][0]["title"] == "o14" and "sr-secret" not in json.dumps(shown)
    assert "task_key" not in params["scenario"]["workspace"]["params"]       # the caller's assignment unchanged
    assert captured[0]["harness_sha"] == "d"


def test_a_keyless_backend_is_sent_a_placeholder_and_a_reference_keeps_its_effort(tmp_path, monkeypatch):
    """AHE-Beta's client falls back to the ambient OPENAI_API_KEY when its own key is empty, and sends an ambient
    USER_LLM_REASONING_EFFORT (persona_client.py): the model a run names is sent with its own settings only."""
    import ahe_user
    from hermit.agent.base import endpoint
    from hermit.agent.model import backends
    monkeypatch.setattr(endpoint, "relay", lambda url: url)
    monkeypatch.setattr(backends, "registry", functools.partial(backends.registry, user_file=None, environ={}))
    monkeypatch.delenv("VLLM_API_KEY", raising=False)                         # vllm: key_required false
    monkeypatch.setenv("USER_LLM_REASONING_EFFORT", "high")
    env = ahe_user._model_env({"ref": "vllm/m", "effort": "low"}, tmp_path)
    assert env["USER_LLM_API_KEY"] == ahe_user.NO_KEY and env["USER_LLM_REASONING_EFFORT"] == "low"
    assert ahe_user._model_env({"ref": "vllm/m"}, tmp_path)["USER_LLM_REASONING_EFFORT"] == ""


def test_a_sessions_user_tokens_are_priced_as_hermit_prices_a_call():
    import ahe_user
    usage = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 100, "total_tokens": 1100}
    price = {"input": 1.0, "cached_input": 0.5, "output": 4.0}                 # USD per million tokens
    assert ahe_user.spend(usage, price) == {"tokens": 1100, "cost_usd": (600 * 1.0 + 400 * 0.5 + 100 * 4.0) / 1e6}
    assert ahe_user.spend(usage, None) == {"tokens": 1100, "cost_usd": None}


def test_a_reference_whose_backend_needs_a_missing_key_or_sets_extra_body_is_refused(tmp_path, monkeypatch):
    """Refused before a session: a missing key would fail every session's model call, and the relay cannot add a
    binding's extra_body to AHE-Beta's requests."""
    import dataclasses
    import ahe_user
    from hermit.agent.base import endpoint
    from hermit.agent.model import backends
    monkeypatch.setattr(endpoint, "relay", lambda url: url)
    monkeypatch.setattr(backends, "registry", functools.partial(backends.registry, user_file=None, environ={}))
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    with pytest.raises(ahe_user.Misconfigured, match="FIREWORKS_API_KEY is not set") as refused:
        ahe_user._model_env({"ref": "fireworks/accounts/fireworks/models/glm-5p3-flash"}, tmp_path)
    assert refused.value.final                                                 # not run again (user.py)
    real = backends.as_binding
    monkeypatch.setattr(backends, "as_binding",
                        lambda ref, registry: dataclasses.replace(real(ref, registry), extra_body={"top_k": 5}))
    with pytest.raises(ValueError, match="extra_body"):
        ahe_user._model_env({"ref": "vllm/m"}, tmp_path)


def test_the_persona_line_ahe_beta_appends_to_an_issue_stays_private():
    """AHE-Beta's issue artifact (user/sim/loop.py, emit_issue_artifact) ends with a metadata line for its developers:
    the persona's id, capability tier and correction style. A learner reads the issue without it."""
    import ahe_user
    meta = "_auto-collected · sr-15-secret · novice/gentle · 6 turns, 2 blamed the assistant · last reply: done_"
    body = f"It never showed progress.\n\n---\n{meta}\n"                  # as emit_issue_artifact writes it
    native = {"score": 0.0, "rating": 1, "outcome": {},
              "feedback": {"opportunities": [{"kind": "diagnostic", "body": body}]}}
    out = ahe_user.visible(native)
    shown = json.dumps({k: v for k, v in out.items() if k not in ("files", "record")}, ensure_ascii=False)
    assert "sr-15-secret" not in shown and "novice" not in shown
    assert out["issues"][0]["body"] == out["feedback"]["opportunities"][0]["body"] == "It never showed progress."


def test_the_users_usage_ledger_is_the_runs_though_another_was_inherited(tmp_path, monkeypatch):
    """An inherited empty AHE_BETA_USAGE_PATH would send AHE-Beta's records (persona ids among them) to its checkout."""
    import ahe_user
    module = types.ModuleType("ahe_beta.integrations.hermit.session")
    module.simulate = native_session([])
    for name in ("ahe_beta", "ahe_beta.integrations", "ahe_beta.integrations.hermit"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "ahe_beta.integrations.hermit.session", module)
    monkeypatch.setenv("AHE_BETA_USAGE_PATH", "")
    monkeypatch.setattr(ahe_user, "_MODEL", None)
    monkeypatch.setattr(ahe_user, "_model_env", lambda spec, records, admitted=False: {})
    monkeypatch.setattr(ahe_user.atexit, "register", lambda function: None)
    params = {"artifact_root": str(tmp_path / "s1"), "session_id": "s1", "persona": {"identity": "p"},
              "scenario": {"workspace": {"params": {}}}}
    ahe_user.simulate({"id": "r01-s00-t", "name": "t"}, None, None, params)
    assert os.environ["AHE_BETA_USAGE_PATH"] == str(tmp_path / "user-model" / "usage.jsonl")


def test_the_users_sessions_share_the_rollouts_plan_only_when_the_actor_holds_it(tmp_path, monkeypatch):
    """The run admits a task's environment to the actor's login only (hermit.isolate): a user model on that login
    shares the admission when the actor is on it too (shared_plan), else it is admitted as any session is."""
    import ahe_user
    from hermit.agent.base import endpoint
    import hermit.isolate
    seen = []

    class Endpoint:
        url = "http://127.0.0.1:9/v1"

        def __init__(self, settings, records, sandbox=None):
            seen.append(settings)

        def place(self, work):
            return types.SimpleNamespace()

        def answer(self, request, authorization=None, abort=None):
            return 200, b"{}", {}

        def close(self):
            pass
    monkeypatch.setattr(hermit.isolate, "cli_sandbox", lambda settings, **kw: (object(), None))
    monkeypatch.setattr(endpoint, "cli_endpoint", Endpoint)
    monkeypatch.setattr(ahe_user, "_CLOSE", [])
    spec = {"cli": "codex", "model": "m", "login": "chatgpt"}
    for shared, admitted in ((True, True), (False, True), (True, False)):   # the flag, and the rollout's admission
        ahe_user._model_env({**spec, **({"shared_plan": True} if shared else {})}, tmp_path, admitted)
        placed = ahe_user._CLOSE[-1].__self__.place("work")
        assert getattr(placed, "plan_admitted", False) is (shared and admitted) and "shared_plan" not in seen[-1]


def test_a_cli_user_model_that_cannot_serve_is_refused_before_any_sandbox_opens(tmp_path, monkeypatch):
    import ahe_user
    import hermit.isolate
    opened = []
    monkeypatch.setattr(hermit.isolate, "cli_sandbox", lambda settings, **kw: opened.append(settings) or (None, None))
    with pytest.raises(ahe_user.Misconfigured, match="login") as refused:
        ahe_user._model_env({"cli": "codex", "model": "m", "login": "claude"}, tmp_path)
    assert refused.value.final and opened == []


def test_a_failed_session_carries_what_its_user_model_spent(tmp_path, monkeypatch):
    """AHE-Beta raises on a failed capture or review before it returns the session's usage; its ledger has every
    call already, under the session's id."""
    import ahe_user
    module = types.ModuleType("ahe_beta.integrations.hermit.session")

    def simulate(task, agent, workspace, native):
        raise RuntimeError("task output collection failed; no score may be emitted")
    module.simulate = simulate
    usage = types.ModuleType("ahe_beta.usage")
    usage.default_usage_store = lambda: types.SimpleNamespace(summary=lambda session_id: {
        "input_tokens": 1000, "output_tokens": 100, "cached_input_tokens": 0, "total_tokens": 1100,
        "unavailable_count": 0} if session_id == "s1" else {})
    for name in ("ahe_beta", "ahe_beta.integrations", "ahe_beta.integrations.hermit"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "ahe_beta.integrations.hermit.session", module)
    monkeypatch.setitem(sys.modules, "ahe_beta.usage", usage)
    monkeypatch.setattr(ahe_user, "_MODEL", {})
    monkeypatch.setattr(ahe_user, "_PRICE", {"input": 1.0, "output": 4.0})
    params = {"artifact_root": str(tmp_path / "s1"), "session_id": "s1", "persona": {"identity": "p"},
              "scenario": {"workspace": {"params": {}}}}
    with pytest.raises(RuntimeError) as failed:
        ahe_user.simulate({"id": "r01-s00-t", "name": "t"}, None, None, params)
    assert failed.value.usage == {"tokens": 1100, "cost_usd": (1000 * 1.0 + 100 * 4.0) / 1e6}
    assert ahe_user.spend({"total_tokens": 1100, "input_tokens": 1000, "output_tokens": 100, "unavailable_count": 1},
                          {"input": 1.0, "output": 4.0}) == {"tokens": 1100, "cost_usd": 0.0014, "uncounted_calls": 1}
    assert ahe_user.spend({"unavailable_count": 1}, {"input": 1.0}) == {"tokens": 0, "cost_usd": None,
                                                                         "uncounted_calls": 1}   # unknown, not 0
    assert ahe_user.spend({"total_tokens": 1000}, {"input": 1.0})["cost_usd"] is None   # a total alone: no split
    assert ahe_user.spend({}, {"input": 1.0})["cost_usd"] is None                # an unreadable ledger
    missing = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "total_tokens": 0,
               "unavailable_count": 0, "event_count": 0}                         # AHE-Beta's summary of no ledger
    assert ahe_user.spend(missing, {"input": 1.0})["cost_usd"] is None
    mixed = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 1110}     # a call counted, one a total alone
    assert ahe_user.spend(mixed, {"input": 1.0, "output": 4.0}) == {"tokens": 1110, "cost_usd": 0.00014,
                                                                    "uncounted_calls": 1}


def test_the_user_models_lost_sign_in_is_named_in_its_sessions_failure(tmp_path, monkeypatch):
    """Hermit's endpoint answers 401; AHE-Beta's client retries it into a plain failure, which then names it."""
    import ahe_user
    from hermit.agent.base import endpoint
    from hermit.agent.base.interface import login_failure
    import hermit.isolate

    class Endpoint:
        url = "http://127.0.0.1:9/v1"

        def __init__(self, settings, records, sandbox=None):
            pass

        def answer(self, request, authorization=None, abort=None):
            return 401, b'{"error": {"code": "hermit_login_failed"}}', {}

        def close(self):
            pass
    monkeypatch.setattr(hermit.isolate, "cli_sandbox", lambda settings, **kw: (object(), None))
    monkeypatch.setattr(endpoint, "cli_endpoint", Endpoint)
    monkeypatch.setattr(ahe_user, "_CLOSE", [])
    monkeypatch.setattr(ahe_user, "_SIGN_IN_LOST", threading.Event())
    ahe_user._model_env({"cli": "codex", "model": "m", "login": "chatgpt"}, tmp_path)
    assert ahe_user._CLOSE[-1].__self__.answer({}, None)[0] == 401 and ahe_user._SIGN_IN_LOST.is_set()
    module = types.ModuleType("ahe_beta.integrations.hermit.session")

    def simulate(task, agent, workspace, native):
        raise RuntimeError("persona request failed after retries")
    module.simulate = simulate
    for name in ("ahe_beta", "ahe_beta.integrations", "ahe_beta.integrations.hermit"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "ahe_beta.integrations.hermit.session", module)
    monkeypatch.setattr(ahe_user, "_MODEL", {})
    with pytest.raises(ahe_user.SignInLost) as lost:
        ahe_user.simulate({"id": "r01-s00-t", "name": "t"}, None, None,
                          {"artifact_root": str(tmp_path / "s1"), "session_id": "s1", "persona": {"identity": "p"},
                           "scenario": {"workspace": {"params": {}}}})
    assert login_failure(lost.value)


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


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the bridge checks its peer with Linux's SO_PEERCRED "
                    "and /proc")
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
