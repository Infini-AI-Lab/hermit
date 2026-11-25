"""AHE-Beta's own delivery Evolver (../../delivery/evolver.py) and what prepare.py, bootstrap.py and the launcher give
it, without OpenAgents or AHE-Beta: a stand-in sandbox records what each process is shown (`view`, then `wrap`),
stand-in processes play the worker, the controller and the sandboxed Git, and a local bare repository plays Gitea. What
the worker and the controller may read, write and keep of the environment, that nothing of the team runs without an
enforced sandbox, that the delivered adapter comes from Gitea and the binding moves last, the binding round 1's sessions
carry, and the launcher's config path are checked. A run against a real Gitea and OpenAgents is not: the route is not
validated end to end."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import types

import pytest
import yaml

from hermit.agent.evolver.loader import find, load_class

EXAMPLE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(EXAMPLE), str(EXAMPLE / "utils")]


class Box:
    """The run's sandbox as the Evolver reaches it (hermit.sandbox.Sandbox): each wrapped call recorded, the command
    and the environment handed back as given; each view (`view`) a Box of its own, its arguments kept."""
    enabled = True

    def __init__(self, confined=True, **view):
        self.calls, self.views, self.view_args, self.closed, self.confined = [], [], view, False, confined

    def confines(self):
        return self.confined

    def wrap(self, command, **kwargs):
        self.calls.append({"command": list(command), **kwargs})
        return list(command), dict(kwargs.get("env") or {})

    def view(self, **kwargs):
        self.views.append(Box(**kwargs))
        return self.views[-1]

    def close(self):
        self.closed = True


class Process:
    """A process that has ended with `code`, or (None) runs until the step ends it (os.killpg, below)."""
    count = 4194300

    def __init__(self, code):
        Process.count += 1
        self.code, self.pid = code, Process.count

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        if self.code is None and timeout is not None:
            raise subprocess.TimeoutExpired("stand-in", timeout)
        self.code = -9 if self.code is None else self.code
        return self.code


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def gitea(tmp_path, files: dict, links=()) -> tuple[str, str]:
    """A Gitea stand-in (owner o, repository r) whose main holds the template with `files` changed: its URL, the
    commit."""
    work = tmp_path / "developer"
    shutil.copytree(EXAMPLE / "template", work)
    for name, text in files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(text)
    for name, target in links:
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).symlink_to(target)
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "-A")
    git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "delivered")
    bare = tmp_path / "gitea" / "o" / "r.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    git(work, "push", "-q", str(bare), "main")
    return f"file://{tmp_path / 'gitea'}", git(work, "rev-parse", "HEAD")


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    """A round of one session ready for the delivery Evolver: its material, the cohort's files, the binding and
    delivery.json; the controller fails (exit 1) unless a test gives it a receipt to write (`receipt`)."""
    cls = load_class(find(str(EXAMPLE / "delivery")))
    module = sys.modules[cls.__module__]
    private = tmp_path / "users" / "r01-s00-t0-abc"
    (private / "sessions").mkdir(parents=True)                                  # AHE-Beta's native session record
    (private / "sessions/index.json").write_text(json.dumps([{"session_id": "s1"}]))
    current = tmp_path / "material" / "current"
    (current / "tasks").mkdir(parents=True)
    (current / "summary.json").write_text(json.dumps({"adapter_commit": "abc"}))
    (current / "tasks" / "r01-s00-t0.json").write_text(json.dumps(
        {"id": "r01-s00-t0", "status": "ok", "pipeline_extra": {"user": {"private": str(private)}}}))
    cohort = tmp_path / "cohort"
    cohort.mkdir()
    (cohort / "assignments.json").write_text(json.dumps({"r01-s00-t0": {"round": 1, "persona": {"id": "sr-secret"}}}))
    (cohort / "rounds.json").write_text(json.dumps({"r01-s00-t0": 1}))
    binding = cohort / "delivery-binding.json"
    binding.write_text(json.dumps({"run_id": "unit", "round": 1, "harness_sha": "c0ffee", "adapter_sha256": "d"}))
    paths = {name: tmp_path / name for name in ("repo", "worker", "openagents", "ahe", "state", "output")}
    for path in paths.values():
        path.mkdir()
    (paths["ahe"] / "backend").mkdir()
    adapter = tmp_path / "adapter"
    shutil.copytree(EXAMPLE / "template", adapter)
    (adapter / ".hermit/CARD.md").write_text("the Trainer's\n")
    cfg = {"root": str(paths["state"]), "repo": str(paths["repo"]), "identity": {"run_id": "unit"}, "roster": {},
           "tracker": {"base_url": "http://10.0.0.5:3000", "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"},
           "definitions_root": str(paths["ahe"] / "backend/agent_defs"), "delivery_timeout": 1,
           "sessions_per_round": 1, "rounds": str(cohort / "rounds.json"), "binding": str(binding),
           "ahe_root": str(paths["ahe"]), "ahe_python": str(paths["ahe"] / ".venv/bin/python"),
           "worker": {"clone": str(paths["worker"]), "parallel": 2, "env": ["GITEA_TOKEN", "ANTHROPIC_BASE_URL"]},
           "openagents": {"repository": "https://example.invalid/openagents", "commit": "0" * 40,
                          "checkout": str(paths["openagents"])},
           "controller": {"env": ["DEMAND_LLM_MODEL"]}}
    for name, value in (("GITEA_TOKEN", "t0k3n"), ("ANTHROPIC_BASE_URL", "https://anthropic.example"),
                        ("ANTHROPIC_MODEL", "glm-5.3"), ("DEMAND_LLM_MODEL", "glm"), ("UNRELATED_SECRET", "x")):
        monkeypatch.setenv(name, value)
    for name in ("OPENAGENTS_PERMISSION_MODE", "DEMAND_LLM_BASE_URL", "DEMAND_LLM_API_KEY"):   # not the operator's
        monkeypatch.delenv(name, raising=False)
    claude = tmp_path / "share/claude/versions/2.1.312"                         # the native installer's layout:
    claude.parent.mkdir(parents=True)                                           # one binary
    claude.write_bytes(b"\x7fELF\x02\x01\x01\x00")
    (tmp_path / "local-bin").mkdir()
    (tmp_path / "local-bin/claude").symlink_to(claude)
    monkeypatch.setattr(module.shutil, "which",
                        lambda name, **kw: str(tmp_path / "local-bin/claude") if name == "claude" else None)
    proc = types.SimpleNamespace(receipt=None, worker=None, controller=1, git=0, on_git=None)   # None: runs on
    started, processes = [], {}

    def popen(argv, **kw):
        started.append(argv)
        if "vigil" in argv:
            code = proc.worker
        elif "ahe_beta.integrations.hermit.feedback" in argv:
            if proc.receipt is not None:                # a delivered round: its receipt, then exit 0
                Path(argv[argv.index("--result") + 1]).write_text(json.dumps(proc.receipt))
            code = 0 if proc.receipt is not None else proc.controller
        else:                                            # the sandboxed Git that moves the team's clone
            code = proc.git
            if proc.on_git is not None:
                proc.on_git()
        process = Process(code)
        processes[process.pid] = process
        return process
    started_by = types.SimpleNamespace(**{name: getattr(subprocess, name) for name in dir(subprocess)
                                          if not name.startswith("__")})
    started_by.Popen = popen                             # the processes the Evolver starts; its own Git runs for real
    monkeypatch.setattr(module, "subprocess", started_by)
    ended = []

    def killpg(pid, sig):
        ended.append(pid)
        processes[pid].code = -int(sig)
    monkeypatch.setattr(module.os, "killpg", killpg)
    box = Box()
    ctx = types.SimpleNamespace(params={"delivery_config": "delivery.json"}, base=cohort, sandbox=box, cancel=None,
                                tools={"check_spec": lambda path: {"passed": True}},
                                workspace=types.SimpleNamespace(learning_material=tmp_path / "material",
                                                                adapter=adapter, output=paths["output"]))

    def configure(**changes):
        (cohort / "delivery.json").write_text(json.dumps({**cfg, **changes}))
    configure()
    return types.SimpleNamespace(module=module, evolver=cls(), ctx=ctx, box=box, started=started, ended=ended,
                                 processes=processes, private=private, cohort=cohort, binding=binding, claude=claude,
                                 proc=proc, configure=configure, adapter=adapter, **paths)


def shown(call):
    return {Path(p).resolve() for p in [*call.get("readable", ()), *call.get("writable", ())]}


def test_the_worker_and_the_controller_run_in_views_of_the_runs_sandbox_with_only_what_they_need(delivery):
    """The worker (OpenAgents' developers, which run model-written commands) sees its clone, its home, its checkout and
    its tools; the controller, AHE-Beta's backend, the round's session records and the delivery state; neither the
    cohort's files (the personas), nor the run's own layout (each runs in a view: the run's own wraps bind the adapter,
    the material and the output at /w), nor a variable it was not handed."""
    with pytest.raises(delivery.module.RunUnavailable, match="feedback unavailable"):
        delivery.evolver.step(delivery.ctx)
    assert delivery.box.calls == []
    assert [v.view_args for v in delivery.box.views] == [{"framework": False, "role": "ahe-worker"},
                                                         {"framework": False, "role": "ahe-controller"}]
    assert all(v.closed for v in delivery.box.views)
    worker, controller = (v.calls[0] for v in delivery.box.views)
    call = delivery.state / "calls" / "round-1"
    assert worker["command"][:4] == ["/bin/sh", "-c", delivery.module.WORKER, "sh"]   # its PATH set inside
    assert worker["command"][4:] == [str(delivery.openagents.resolve() / ".venv/bin/openagents"), "vigil",
                                     "--parallel", "2"] and worker["chdir"] == delivery.worker.resolve()
    assert {delivery.worker.resolve(), delivery.openagents.resolve(), (call / "tools").resolve()} <= shown(worker)
    assert not any(p == delivery.state.resolve() or delivery.state.resolve() in p.parents
                   for p in shown(worker) - {(call / "tools").resolve()})   # neither the controller's state nor logs
    assert delivery.private.resolve() not in shown(worker)                      # the users' records: the controller's
    assert {"GITEA_TOKEN", "ANTHROPIC_BASE_URL", "GIT_ASKPASS"} <= set(worker["env"])
    assert worker["env"]["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "1"             # AHE-Beta's lock, on the model the
    assert "CLAUDE_CODE_SUBAGENT_MODEL" not in worker["env"]                    # worker is handed (none here)
    assert (worker["env"]["OPENAGENTS_PERMISSION_MODE"], worker["env"]["GIT_AUTHOR_NAME"]) == ("bypassPermissions",
                                                                                              "openagents")
    assert (worker["env"]["AUTO_DESIGNER_RUN_ID"], worker["env"]["AUTO_DESIGNER_ROUND"]) == ("unit", "1")
    assert controller["command"][:4] == ["/bin/sh", "-c", 'exec "$@"', "sh"]   # its venv's python by that name
    assert controller["command"][5:8] == ["-P", "-m", "ahe_beta.integrations.hermit.feedback"]
    readable = {Path(p).resolve() for p in controller["readable"]}
    backend = (delivery.ahe / "backend").resolve()
    assert {delivery.private.resolve(), backend / "ahe_beta", backend / "agent_defs"} <= readable
    assert not readable & {delivery.ahe.resolve(), backend}                     # no experiments, secrets file, data
    assert {delivery.state.resolve(), delivery.repo.resolve()} <= {Path(p).resolve() for p in controller["writable"]}
    assert Path(controller["chdir"]).resolve() == (delivery.state / "controller-home").resolve()   # not the
                                                                                # developers' tree: no module of theirs
    assert "DEMAND_LLM_MODEL" in controller["env"] and "ANTHROPIC_BASE_URL" not in controller["env"]
    for grants in (worker, controller):
        assert not any(p == delivery.cohort.resolve() or delivery.cohort.resolve() in p.parents for p in shown(grants))
        assert "UNRELATED_SECRET" not in grants["env"] and grants["forward"] == []
    assert "t0k3n" not in (call / "tools" / "askpass.sh").read_text()           # the token stays in the environment
    assert [delivery.started[0]] == [a for a in delivery.started if "vigil" in a]
    assert len(delivery.ended) == 1 and delivery.processes[delivery.ended[0]].code == -15   # the worker, with the step


def test_the_worker_runs_on_the_model_and_the_configuration_it_is_handed(delivery, tmp_path):
    """Its subagents locked on the model delivery.json hands it, and its configuration this machine's copy, shown
    read-only where OpenAgents reads it: a round's developers cannot change the next round's stages or models."""
    kept = tmp_path / "openagents-worker.yml"
    kept.write_text("version: 1\n")
    delivery.configure(worker={"clone": str(delivery.worker), "env": ["GITEA_TOKEN", "ANTHROPIC_MODEL"],
                               "config": str(kept)})
    with pytest.raises(delivery.module.RunUnavailable):
        delivery.evolver.step(delivery.ctx)
    worker = delivery.box.views[0].calls[0]
    assert worker["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "glm-5.3"
    assert worker["binds"] == [(kept, str(delivery.worker.resolve() / ".openagents/openagents.yml"), False)]


def test_setup_refuses_what_no_step_could_mend_before_any_session_is_paid_for(delivery, monkeypatch):
    monkeypatch.delenv("GITEA_TOKEN")
    monkeypatch.setattr(delivery.module.shutil, "which", lambda name, **kw: None)
    delivery.ctx.sandbox = Box(confined=False)
    with pytest.raises(delivery.module.RunUnavailable) as refused:
        delivery.evolver.setup(delivery.ctx)
    for what in ("not enforced", "Claude Code", "Gitea token", "interpreter", "Claude Code credential",
                 "demand roles' model", "virtual environment"):
        assert what in str(refused.value)
    delivery.configure(openagents=None)
    with pytest.raises(delivery.module.RunUnavailable, match="names no OpenAgents worker"):
        delivery.evolver.setup(delivery.ctx)
    for host in ("127.0.0.2", "[::1]"):                  # the forwarder listens on 127.0.0.1
        delivery.configure(tracker={"base_url": f"http://{host}:3000", "owner": "o", "repo": "r",
                                    "token_env": "GITEA_TOKEN"})
        with pytest.raises(delivery.module.RunUnavailable, match="give it as localhost"):
            delivery.evolver.setup(delivery.ctx)
    delivery.configure(tracker={"base_url": "http://gitea.example:3000", "owner": "o", "repo": "r",
                                "token_env": "GITEA_TOKEN"})
    with pytest.raises(delivery.module.RunUnavailable, match="over https"):  # the token in the clear
        delivery.evolver.setup(delivery.ctx)
    delivery.configure(tracker={"base_url": "https://gitea.com", "owner": "o", "repo": "r",
                                "token_env": "GITEA_TOKEN"})
    with pytest.raises(delivery.module.RunUnavailable, match="Gitea.com"):
        delivery.evolver.setup(delivery.ctx)


def test_a_gitea_on_this_machines_loopback_is_forwarded_into_the_sandbox(delivery):
    delivery.configure(tracker={"base_url": "http://localhost:3000", "owner": "o", "repo": "r",
                                "token_env": "GITEA_TOKEN"})
    with pytest.raises(delivery.module.RunUnavailable):
        delivery.evolver.step(delivery.ctx)
    assert [v.calls[0]["forward"] for v in delivery.box.views] == [[3000], [3000]]


def test_the_developers_find_claude_on_the_workers_path(delivery):
    """OpenAgents runs `claude` by name (AHE-Beta puts it in a bin folder on the worker's PATH); a link on this
    machine's PATH (~/.local/bin/claude, the native installer's) is not shown inside, only the file it leads to."""
    with pytest.raises(delivery.module.RunUnavailable):
        delivery.evolver.step(delivery.ctx)
    worker = delivery.box.views[0].calls[0]
    bin_ = delivery.state / "calls" / "round-1" / "tools" / "bin"
    assert (bin_ / "claude").resolve() == delivery.claude and delivery.claude in shown(worker)
    assert delivery.claude.parent not in shown(worker)                         # the binary, nothing beside it
    assert worker["env"]["AHE_WORKER_PATH"].split(":")[:2] == [str(delivery.openagents.resolve() / ".venv/bin"),
                                                               str(bin_)]


def test_a_claude_installed_with_npm_brings_its_node(delivery, tmp_path, monkeypatch):
    """A node CLI (npm's claude, `#!/usr/bin/env node`) needs the node beside it, which nvm keeps in the home."""
    node = tmp_path / "nvm/versions/node/v22/bin/node"
    node.parent.mkdir(parents=True)
    node.write_text("")
    node.chmod(0o755)
    cli = tmp_path / "nvm/versions/node/v22/lib/node_modules/@anthropic-ai/claude-code/cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/usr/bin/env node\n")
    (node.parent / "claude").symlink_to(cli)
    found = {"claude": str(node.parent / "claude"), "node": str(node)}
    monkeypatch.setattr(delivery.module.shutil, "which", lambda name, **kw: found.get(name))
    with pytest.raises(delivery.module.RunUnavailable):
        delivery.evolver.step(delivery.ctx)
    worker = delivery.box.views[0].calls[0]
    assert {cli.parent, node.parent.parent} <= shown(worker)


def test_a_wrapper_claude_in_the_homes_bin_brings_only_itself(delivery, tmp_path, monkeypatch):
    """A wrapper script in ~/.local/bin: Hermit's program_paths grants the bin folder's parent, the whole ~/.local
    (its share/ and state/ data too)."""
    home = tmp_path / "home"
    wrapper = home / ".local/bin/claude"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text('#!/bin/sh\nexec node /opt/claude/cli.js "$@"\n')
    wrapper.chmod(0o755)
    (home / ".local/share/an-application").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(delivery.module.shutil, "which",
                        lambda name, **kw: str(wrapper) if name == "claude" else None)
    with pytest.raises(delivery.module.RunUnavailable):
        delivery.evolver.step(delivery.ctx)
    worker = delivery.box.views[0].calls[0]
    assert wrapper.resolve() in shown(worker) and (home / ".local").resolve() not in shown(worker)


def test_without_claude_on_this_machines_path_nothing_starts(delivery, monkeypatch):
    monkeypatch.setattr(delivery.module.shutil, "which", lambda name, **kw: None)
    with pytest.raises(delivery.module.RunUnavailable, match="Claude Code"):
        delivery.evolver.step(delivery.ctx)
    assert delivery.started == []


def test_a_worker_that_exits_fails_the_step_without_waiting_for_the_delivery(delivery):
    """The controller would wait for its tickets until the delivery timeout (a day): the step fails at once, and the
    controller is ended with it."""
    delivery.proc.worker, delivery.proc.controller = 1, None                 # the worker exited; the controller runs
    with pytest.raises(delivery.module.RunUnavailable, match="worker exited"):
        delivery.evolver.step(delivery.ctx)
    assert [delivery.processes[pid].code for pid in delivery.ended] == [-15]   # the controller, ended


def test_nothing_of_the_team_runs_without_an_enforced_sandbox(delivery):
    for sandbox in (None, Box(confined=False)):          # none, or best_effort without bubblewrap: unconfined
        delivery.ctx.sandbox = sandbox
        with pytest.raises(delivery.module.RunUnavailable, match="only in the run's sandbox"):
            delivery.evolver.step(delivery.ctx)
    assert delivery.started == []


def test_a_delivered_round_comes_from_gitea_itself_and_moves_the_binding_last(delivery, tmp_path, monkeypatch):
    """The adapter files of the commit AHE-Beta's controller names, taken from Gitea by this process (none of the
    team's Git state), the commit checked on main; the team's clone moved to it by a Git in the sandbox; the binding the
    next round's sessions carry written last."""
    from binding import fingerprint
    url, commit = gitea(tmp_path, {"ADAPTER.md": "delivered\n", ".openagents/notes.md": "forge\n",
                                   "specs/x.md": "a specification\n", ".hermit/CARD.md": "the team's\n"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"round": 1, "tested_adapter": "c0ffee", "delivered_adapter": commit,
                              "usage": {"input_tokens": 10, "output_tokens": 2, "event_count": 1}}
    gits, run = [], delivery.module.subprocess.run
    monkeypatch.setattr(delivery.module.subprocess, "run", lambda argv, **kw: gits.append(argv) or run(argv, **kw))
    result = delivery.evolver.step(delivery.ctx)
    assert all(argv[1:3] == ["-c", "credential.helper="] for argv in gits if "fetch" in argv)   # no helper keeps it
    assert result.status == "done" and (delivery.adapter / "ADAPTER.md").read_text() == "delivered\n"
    assert (delivery.adapter / ".hermit/CARD.md").read_text() == "the Trainer's\n"
    assert not (delivery.adapter / ".openagents").exists() and not (delivery.adapter / "specs").exists()
    git_view = delivery.box.views[2]
    assert git_view.view_args["role"] == "ahe-git" and git_view.calls[0]["command"][-1] == commit
    assert git_view.calls[0]["writable"] == [delivery.repo.resolve()] and git_view.closed
    bound = json.loads(delivery.binding.read_text())
    assert (bound["round"], bound["harness_sha"]) == (2, commit)
    assert bound["adapter_sha256"] == fingerprint(delivery.adapter)
    assert json.loads((delivery.output / "delivery.json").read_text())["delivered_adapter"] == commit
    assert [(u.model, u.unreported_calls) for u in result.usage] == [("ahe-demand", 0), ("ahe-developers", 1)]


def test_a_session_rated_out_of_time_is_not_given_to_the_feedback_import(delivery, tmp_path):
    """A session that ran out of its time before the user's verdict is rated 1 here and has no native record (nor one
    AHE-Beta's import accepts): given to it, it refused the whole round."""
    late = tmp_path / "users" / "r01-s01-t1-def"
    late.mkdir(parents=True)
    current = tmp_path / "material" / "current"
    (current / "tasks" / "r01-s01-t1.json").write_text(json.dumps(
        {"id": "r01-s01-t1", "status": "ok", "pipeline_extra": {"user": {"private": str(late),
                                                                         "outcome": "timed_out"}}}))
    (delivery.cohort / "rounds.json").write_text(json.dumps({"r01-s00-t0": 1, "r01-s01-t1": 1}))
    delivery.configure(sessions_per_round=2)
    with pytest.raises(delivery.module.RunUnavailable, match="feedback unavailable"):
        delivery.evolver.step(delivery.ctx)
    request = json.loads((delivery.state / "calls/round-1/request.json").read_text())
    assert request["session_roots"] == [str(delivery.private.resolve())] and request["sessions_per_round"] == 1


def test_a_delivered_file_this_machines_git_ignores_is_refused(delivery, tmp_path, monkeypatch):
    """Hermit's tree (git add -A) would leave the helper out: the next round would run another adapter than the bound
    one, and refuse it."""
    url, commit = gitea(tmp_path, {"scripts/helper.py": "x = 1\n"})
    (tmp_path / "ignore").write_text("scripts/helper.py\n")
    (tmp_path / "gitconfig").write_text(f"[core]\n\texcludesFile = {tmp_path / 'ignore'}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    with pytest.raises(delivery.module.RunUnavailable, match="ignores the delivered scripts/helper.py"):
        delivery.evolver.step(delivery.ctx)
    assert json.loads(delivery.binding.read_text())["round"] == 1


def test_a_delivered_file_past_any_limit_is_refused_before_it_is_read(delivery, tmp_path):
    url, commit = gitea(tmp_path, {"scripts/big.py": "x = 1\n" * 200_000})   # 1.2 MB
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    with pytest.raises(delivery.module.RunUnavailable, match="past any of the template's"):
        delivery.evolver.step(delivery.ctx)
    assert not (delivery.adapter / "scripts").exists() and json.loads(delivery.binding.read_text())["round"] == 1


def test_a_delivered_file_where_a_folder_was_lands_at_its_path(delivery, tmp_path):
    """A skill's scripts/run folder replaced by a script scripts/run: the emptied folder stayed, the copy put the
    script inside it (scripts/run/run), and the binding bound those bytes to the commit."""
    skill = delivery.adapter / ".agents/skills/a"
    (skill / "scripts/run").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\ndescription: a\n---\n")
    (skill / "scripts/run/x.py").write_text("x = 1\n")
    url, commit = gitea(tmp_path, {".agents/skills/a/SKILL.md": "---\ndescription: a\n---\n",
                                   ".agents/skills/a/scripts/run": "#!/bin/sh\necho run\n"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    delivery.evolver.step(delivery.ctx)
    assert (skill / "scripts/run").is_file() and (skill / "scripts/run").read_text() == "#!/bin/sh\necho run\n"


def test_a_delivered_gitea_token_is_refused_under_any_name(delivery, tmp_path, monkeypatch):
    """Hermit's redaction knows ..._KEY, ..._TOKEN, ..._SECRET names: a token under another name (GITEA_PAT) would go
    out with the adapter to every task's environment."""
    monkeypatch.setenv("GITEA_PAT", "gitea-pat-0123456789abcdef")
    url, commit = gitea(tmp_path, {"scripts/push.py": "TOKEN = 'gitea-pat-0123456789abcdef'\n"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_PAT"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    with pytest.raises(delivery.module.RunUnavailable, match="holds the Gitea token"):
        delivery.evolver.step(delivery.ctx)
    assert json.loads(delivery.binding.read_text())["round"] == 1


def test_a_rejected_delivery_moves_neither_the_clone_nor_the_binding(delivery, tmp_path):
    url, commit = gitea(tmp_path, {"ADAPTER.md": "delivered\n"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    delivery.ctx.tools["check_spec"] = lambda path: {"passed": False}
    before = delivery.binding.read_text()
    with pytest.raises(delivery.module.RunUnavailable, match="fixed template"):
        delivery.evolver.step(delivery.ctx)
    assert delivery.binding.read_text() == before and len(delivery.box.views) == 2   # no Git moved the clone
    assert not (delivery.output / "delivery.json").exists()


def test_a_call_the_trainer_stopped_moves_neither_the_clone_nor_the_binding(delivery, tmp_path):
    """The Trainer stops a call past its timeout and keeps the adapter: a binding written after that would bind the
    next round's sessions to an adapter they do not run."""
    url, commit = gitea(tmp_path, {"ADAPTER.md": "delivered\n"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    delivery.ctx.cancel = threading.Event()
    delivery.proc.on_git = delivery.ctx.cancel.set      # stopped while the clone moved
    with pytest.raises(delivery.module.RunUnavailable, match="stopped by the Trainer"):
        delivery.evolver.step(delivery.ctx)
    assert json.loads(delivery.binding.read_text())["round"] == 1


def test_a_delivered_key_value_is_refused_before_anything_moves(delivery, tmp_path, monkeypatch):
    """The Trainer scrubs a key's value from the adapter before its commit: the bytes bound would not be the adapter
    the next round runs, and its sessions would all refuse it."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-test-0123456789abcdef")
    url, commit = gitea(tmp_path, {"scripts/call.py": "KEY = 'sk-ant-test-0123456789abcdef'\n"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    with pytest.raises(delivery.module.RunUnavailable, match="key's value"):
        delivery.evolver.step(delivery.ctx)
    assert json.loads(delivery.binding.read_text())["round"] == 1 and len(delivery.box.views) == 2
    assert not (delivery.adapter / "scripts").exists()


@pytest.mark.parametrize("case", ["link", "off-main", "not-a-commit"])
def test_a_delivered_link_a_commit_off_main_or_a_receipt_without_a_commit_is_refused(delivery, tmp_path, case):
    url, commit = gitea(tmp_path, {"ADAPTER.md": "delivered\n"}, links=[("scripts/run.py", "/etc/passwd")]
                        if case == "link" else ())
    if case == "off-main":
        clone = tmp_path / "developer"
        git(clone, "switch", "-q", "-c", "side")
        (clone / "ADAPTER.md").write_text("never merged\n")
        git(clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "side")
        git(clone, "push", "-q", str(tmp_path / "gitea/o/r.git"), "side")
        commit = git(clone, "rev-parse", "HEAD")
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"delivered_adapter": "--upload-pack=touch /tmp/x" if case == "not-a-commit" else commit}
    match = {"link": "link or a submodule", "off-main": "not on the repository's main",
             "not-a-commit": "names no delivered commit"}[case]
    with pytest.raises(delivery.module.RunUnavailable, match=match):
        delivery.evolver.step(delivery.ctx)
    assert (delivery.adapter / "ADAPTER.md").read_text() == (EXAMPLE / "template/ADAPTER.md").read_text()
    assert json.loads(delivery.binding.read_text())["round"] == 1


def test_prepare_binds_the_seed_adapter_before_round_one_and_hands_the_binding_to_the_sessions(tmp_path, monkeypatch):
    """AHE-Beta's feedback import requires each session to carry the tested adapter's delivery commit: the binding is
    written with the delivery configuration, before any session, where none of the team's processes reaches, and each
    session is given it; the Evolver reads each session's round from rounds.json, never the assignments."""
    from prepare import prepare
    from test_prepare import cohort
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)                     # the developers on Anthropic's own
    root = cohort(tmp_path)
    repo = delivery_clone(tmp_path)
    prepare(cohort=root, ahe_root=tmp_path, run_id="first")                    # prepared before, then the delivery
    prepare(cohort=root, ahe_root=tmp_path, run_id="unit", delivery_repo=repo,
            tracker={"base_url": "http://10.0.0.5:3000", "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"},
            worker={"clone": str(tmp_path / "worker"), "parallel": 2, "env": ["GITEA_TOKEN"]})
    request = json.loads((root / "delivery.json").read_text())
    binding = json.loads(Path(request["binding"]).read_text())
    assert Path(request["binding"]) == root / "delivery-binding.json"          # not in the controller's state
    assert Path(request["root"]) not in Path(request["binding"]).parents
    assert binding["harness_sha"] == git(repo, "rev-parse", "HEAD") and binding["round"] == 1
    assert json.loads(Path(request["rounds"]).read_text()) == {"r01-s00-t0": 1, "r01-s01-t1": 1}
    assert "assignments" not in request
    assert request["ahe_python"] == str(tmp_path / ".venv/bin/python")         # not the base interpreter it links to
    assert request["worker"]["parallel"] == 2 and "DEMAND_LLM_REASONING_EFFORT" in request["controller"]["env"]
    run = yaml.safe_load((root / "run-delivery.yaml").read_text())
    user_params = run["agent"]["params"]["user"]["params"]
    assert user_params["binding"] == request["binding"] and user_params["run_id"] == binding["run_id"] == "unit"
    assert {"10.0.0.5", "api.anthropic.com"} <= set(run["sandbox"]["evolver"]["allowed_hosts"])
    assert not {"localhost", "127.0.0.1"} & set(run["sandbox"]["evolver"]["allowed_hosts"])   # MODEL_URLS: none
    assert "binding" not in yaml.safe_load((root / "run.yaml").read_text())["agent"]["params"]["user"]["params"]
    assert run["evolver"]["name"] == str(EXAMPLE / "delivery") and run["evolver_access"]["send_material_out"]
    assert load_class(find(run["evolver"]["name"])).__name__ == "AHEBetaEvolver"
    assert request["tracker"]["token_env"] == "GITEA_TOKEN" and "token" not in request["tracker"]


def delivery_clone(tmp_path) -> Path:
    """The AHE-Beta checkout prepare.py reads (a Git repository, with a venv's interpreter) and a clean delivery clone
    of the template."""
    git(tmp_path, "init", "-q")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "ahe")
    repo = tmp_path / "delivery"
    shutil.copytree(EXAMPLE / "template", repo)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/python").symlink_to(sys.executable)                  # a venv's interpreter: a link
    return repo


def test_a_delivery_is_prepared_only_with_its_worker_and_an_interrupted_one_is_redone(tmp_path):
    """prepare.py's own command line gave no worker: the run failed at its first learner step, after a round of
    paid sessions. And a preparation interrupted after delivery.json, before run-delivery.yaml, is redone."""
    from prepare import prepare
    from test_prepare import cohort
    root = cohort(tmp_path)
    repo = delivery_clone(tmp_path)
    kwargs = dict(cohort=root, ahe_root=tmp_path, run_id="unit", delivery_repo=repo,
                  tracker={"base_url": "http://10.0.0.5:3000", "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    with pytest.raises(ValueError, match="bootstrap.py"):
        prepare(**kwargs)
    assert not (root / "run.yaml").exists()
    (root / "delivery.json").write_text("{}")                                   # an interrupted earlier preparation,
    (root / "delivery-binding.json").write_text(json.dumps({"run_id": "old", "round": 1}))   # under another name
    prepare(**{**kwargs, "tracker": {**kwargs["tracker"], "base_url": "http://localhost:3000"}},
            worker={"clone": str(tmp_path / "worker"), "env": []},
            delivery_hosts=["http://localhost:3000", "LOCALHOST:3000", "proxy.example:8080", "127.0.0.2",
                            "[::ffff:127.0.0.1]:3000", "localhost.", "127.1", "0x7f.1"])
    assert json.loads((root / "delivery.json").read_text())["repo"] == str(repo.resolve())
    assert json.loads((root / "delivery-binding.json").read_text())["run_id"] == "unit"   # bound anew
    hosts = yaml.safe_load((root / "run-delivery.yaml").read_text())["sandbox"]["evolver"]["allowed_hosts"]
    assert "localhost" not in hosts                      # the proxy would open every local port: forwarded instead
    assert "proxy.example" in hosts and not any("/" in host or ":" in host for host in hosts)   # names, as compared
    assert not any(host.startswith(("127.", "localhost", "0x7f")) for host in hosts)   # no loopback, however spelt
    with pytest.raises(FileExistsError):
        prepare(**kwargs, worker={"clone": str(tmp_path / "worker"), "env": []})


def test_a_session_carries_the_bound_commit_and_refuses_another_adapter_round_or_no_binding(tmp_path, monkeypatch):
    import ahe_user
    from test_user import native_session
    captured = []
    module = types.ModuleType("ahe_beta.integrations.hermit.session")
    module.simulate = native_session(captured)
    for name in ("ahe_beta", "ahe_beta.integrations", "ahe_beta.integrations.hermit"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "ahe_beta.integrations.hermit.session", module)
    monkeypatch.setattr(ahe_user, "_MODEL", {})
    (tmp_path / "binding.json").write_text(json.dumps({"harness_sha": "c0ffee", "adapter_sha256": "d", "round": 2,
                                                       "run_id": "unit"}))
    params = {"artifact_root": str(tmp_path / "s1"), "session_id": "s1", "adapter_sha256": "d", "round": 2,
              "run_id": "unit",
              "binding": str(tmp_path / "binding.json"), "persona": {"identity": "p"},
              "scenario": {"workspace": {"params": {}}}}
    ahe_user.simulate({"id": "r02-s00-t", "name": "t"}, None, None, params)
    assert captured[0]["harness_sha"] == "c0ffee"
    for changed in ({"adapter_sha256": "other"}, {"round": 3}, {"run_id": "another"},
                    {"binding": str(tmp_path / "missing.json")}):
        with pytest.raises(ahe_user.NotBound) as refused:          # a stale binding after a failed round, too
            ahe_user.simulate({"id": "r02-s00-t", "name": "t"}, None, None, {**params, **changed})
        assert refused.value.final


def test_the_launchers_config_path_is_absolute_before_it_changes_folder(tmp_path, monkeypatch):
    import openagents_launcher as launcher
    seen = {}
    monkeypatch.setattr(launcher, "command", lambda manifest, args: seen.setdefault("args", list(args)))
    monkeypatch.setattr(launcher.os, "execv", lambda program, argv: seen.update(program=program))
    (tmp_path / "worker/.openagents").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["openagents_launcher.py", "--manifest", "m.json", "status", "--config",
                                      "worker/.openagents/openagents.yml"])
    launcher.main()
    assert seen["args"][-1] == str((tmp_path / "worker/.openagents/openagents.yml").resolve())


def test_the_launcher_never_starts_the_worker_itself(tmp_path, monkeypatch):
    """`vigil` takes tickets and runs the developers' commands, with this machine's home and network here."""
    import openagents_launcher as launcher
    monkeypatch.setattr(launcher.os, "execv", lambda program, argv: pytest.fail("started"))
    monkeypatch.setattr(sys, "argv", ["openagents_launcher.py", "--manifest", "m.json", "vigil"])
    with pytest.raises(SystemExit):
        launcher.main()


def test_the_gitea_helper_sends_its_token_to_no_other_origin():
    """urllib carries the Authorization header along a redirect, to wherever it leads."""
    import gitea_runtime
    from urllib.request import Request
    request = Request("http://gitea.example:3000/api/v1/user", headers={"Authorization": "token t0k3n"})
    handler = gitea_runtime._SameOrigin()
    with pytest.raises(gitea_runtime.GiteaError, match="another origin"):
        handler.redirect_request(request, None, 302, "Found", {}, "https://sso.example/login")
    same = handler.redirect_request(request, None, 302, "Found", {}, "http://gitea.example:3000/api/v1/user/")
    assert same.full_url.endswith("/user/")


def test_the_seed_holds_every_template_file_whatever_this_machines_ignore_rules(tmp_path, monkeypatch):
    """A global ignore of .codex/ dropped the template's hooks from the seed, and the binding then refused the
    clone."""
    import gitea_runtime
    (tmp_path / "ignore").write_text(".codex/\n")
    (tmp_path / "gitconfig").write_text(f"[core]\n\texcludesFile = {tmp_path / 'ignore'}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    bare = tmp_path / "r.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    calls, run = [], subprocess.run
    monkeypatch.setattr(gitea_runtime.subprocess, "run", lambda argv, **kw: calls.append(argv) or run(argv, **kw))
    clone = gitea_runtime.seed_repository(template=EXAMPLE / "template", repository={"clone_url": str(bare),
                                                                                     "empty": True},
                                          destination=tmp_path / "clone", token="t0k3n", owner="o")
    assert ".codex/hooks.json" in git(clone, "ls-files").split("\n")
    remote = [argv for argv in calls if "push" in argv or "clone" in argv]
    assert len(remote) == 2 and all(argv[1:3] == ["-c", "credential.helper="] for argv in remote)   # none keeps it


def test_the_gitea_helper_writes_no_credential_down():
    import gitea_runtime
    directory, env = gitea_runtime._with_askpass("t0k3n-value", "o", "https://gitea.example:3000/o/r.git")
    try:
        assert "t0k3n-value" not in (directory / "askpass.sh").read_text()

        def asked(prompt):
            return subprocess.run([str(directory / "askpass.sh"), prompt], env=env, capture_output=True,
                                  text=True).stdout.strip()
        assert asked("Password for 'https://o@gitea.example:3000': ") == "t0k3n-value"
        assert asked("Username for 'https://gitea.example:3000': ") == "o"
        for elsewhere in ("Password for 'https://o@github.com': ", "Password for 'https://o@gitea.example.evil': ",
                          "Password for 'https://o@gitea.example:8080': ",       # another service on that host,
                          "Password for 'http://o@gitea.example:3000': "):       # or the clear: nothing
            assert asked(elsewhere) == ""
    finally:
        shutil.rmtree(directory)


def test_the_worker_clone_stays_clean_with_openagents_own_state(tmp_path):
    """bootstrap.py writes .openagents/openagents.yml into the worker clone; a retry refused a clone with changes."""
    from bootstrap import keep_out
    git(tmp_path, "init", "-q")
    keep_out(tmp_path, ".openagents/")
    keep_out(tmp_path, ".openagents/")
    (tmp_path / ".openagents").mkdir()
    (tmp_path / ".openagents/openagents.yml").write_text("version: 1\n")
    assert git(tmp_path, "status", "--porcelain") == ""
    assert (tmp_path / ".git/info/exclude").read_text().count(".openagents/") == 1


def test_the_worker_is_configured_as_ahe_beta_configures_its_own(tmp_path, monkeypatch):
    """AHE-Beta's roles triage and its CI and end-to-end stages are skipped (a fresh repository has no checks to wait
    for); each delivery shows its evidence."""
    import bootstrap
    monkeypatch.setenv("GITEA_TOKEN", "t0k3n")
    monkeypatch.setattr(bootstrap, "ensure_repository",   # the API advertises its ROOT_URL, another origin
                        lambda **kw: {"clone_url": "https://public.example/o/r.git"})
    origins = []
    monkeypatch.setattr(bootstrap, "seed_repository",
                        lambda **kw: origins.append(kw["repository"]["clone_url"]) or kw["destination"])

    def clone(**kw):
        origins.append(kw["clone_url"])
        kw["destination"].mkdir(parents=True)
        git(kw["destination"], "init", "-q")
        return kw["destination"]
    monkeypatch.setattr(bootstrap, "clone_repository", clone)
    seen = {}
    monkeypatch.setattr(bootstrap, "prepare", lambda **kw: seen.update(kw) or {})
    bootstrap.bootstrap(cohort=tmp_path, ahe_root=tmp_path, ahe_python=None, run_id="unit",
                        gitea_url="https://10.0.0.5:3000", gitea_owner="o", gitea_repo="r", review_seconds=None,
                        delivery_hosts=["proxy.example"])
    assert seen["worker"]["config"] == str(tmp_path / "openagents-worker.yml")   # this machine's, shown read-only
    assert origins == ["https://10.0.0.5:3000/o/r.git"] * 2                     # the Gitea this run reaches
    assert (tmp_path / "openagents-worker.yml").read_text() == \
        (tmp_path / "openagents-worker/.openagents/openagents.yml").read_text()
    config = yaml.safe_load((tmp_path / "openagents-worker/.openagents/openagents.yml").read_text())
    assert config["stages"] == {"triage": "skip", "rfc": "agent", "conflict": "agent", "ci": "skip", "e2e": "skip",
                                "review": "agent", "merge": "agent"}
    assert config["execution_evidence"] == {"required": True} and config["defaults"]["interval"] == 30
    assert "ANTHROPIC_SMALL_FAST_MODEL" in seen["worker"]["env"] and seen["delivery_hosts"] == ["proxy.example"]
    assert "review_seconds" not in seen                                         # not given: prepare.py's own


def test_bootstrap_touches_nothing_of_a_delivered_cohort_and_ignores_no_setting(tmp_path, monkeypatch):
    """Once a run's team has had the clones, nothing on this machine runs Git in them or writes there; and a prepared
    cohort's settings are the delivery's, so a model given here would be silently ignored."""
    import bootstrap
    monkeypatch.setenv("GITEA_TOKEN", "t0k3n")
    monkeypatch.setattr(bootstrap, "ensure_repository", lambda **kw: pytest.fail("provisioned"))
    common = dict(cohort=tmp_path, ahe_root=tmp_path, ahe_python=None, run_id="unit", gitea_url="https://h:3000",
                  gitea_owner="o", gitea_repo="r")
    with pytest.raises(ValueError, match="over https"):                       # the token would cross in the clear
        bootstrap.bootstrap(**{**common, "gitea_url": "http://gitea.example:3000"})
    (tmp_path / "run.yaml").write_text("{}")
    with pytest.raises(ValueError, match="user_model"):
        bootstrap.bootstrap(**common, user_model="fireworks/glm")
    (tmp_path / "run-delivery.yaml").write_text("{}")
    with pytest.raises(FileExistsError):
        bootstrap.bootstrap(**common)


def test_a_template_file_this_machines_git_ignores_stops_the_preparation(tmp_path, monkeypatch):
    """Hermit makes the adapter's tree with `git add -A`: a globally ignored .codex/ would be left out of every
    adapter the run deploys (and the binding of the seed would never match it)."""
    from prepare import prepare
    from test_prepare import cohort
    root = cohort(tmp_path)
    (tmp_path / "ignore").write_text(".codex/\n")
    (tmp_path / "gitconfig").write_text(f"[core]\n\texcludesFile = {tmp_path / 'ignore'}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    with pytest.raises(ValueError, match=r"\.codex/hooks\.json"):
        prepare(cohort=root, ahe_root=tmp_path, run_id="unit")
    assert not (root / "run.yaml").exists()


def test_a_delivery_added_to_a_prepared_cohort_keeps_its_review_time(tmp_path):
    """bootstrap.py has prepare.py add the delivery files with the default review time: the cohort's own (240 s, which
    its 600 s budgets allow) is kept, not refused by the default's 600."""
    from prepare import prepare
    from test_prepare import cohort
    root = cohort(tmp_path)
    rows = [json.loads(line) for line in (root / "tasks.jsonl").read_text().splitlines()]
    for row in rows:                                     # as the exporter writes `--agent-seconds 600`
        row["ahe_session"]["scenario"]["workspace"] = {"params": {"execution": {"task_budget_seconds": 600}}}
    (root / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    repo = delivery_clone(tmp_path)
    prepare(cohort=root, ahe_root=tmp_path, run_id="unit", review_seconds=240)
    run = yaml.safe_load((root / "run.yaml").read_text())
    run["agent"]["params"]["user"]["pythonpath"] = ["/a/checkout/moved/since/backend"]   # another AHE-Beta checkout
    (root / "run.yaml").write_text(yaml.safe_dump(run))
    prepare(cohort=root, ahe_root=tmp_path, run_id="unit", delivery_repo=repo,
            tracker={"base_url": "http://10.0.0.5:3000", "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"},
            worker={"clone": str(tmp_path / "worker"), "env": []})
    user = yaml.safe_load((root / "run-delivery.yaml").read_text())["agent"]["params"]["user"]
    assert user["review_s"] == 240 and user["pythonpath"] == [str(tmp_path.resolve() / "backend")]   # one AHE-Beta


def test_bootstrap_refuses_folders_that_overlap(tmp_path, monkeypatch):
    """The OpenAgents checkout inside the worker's clone: its developers would write what this machine then runs."""
    import bootstrap
    monkeypatch.setenv("GITEA_TOKEN", "t0k3n")
    monkeypatch.setattr(bootstrap, "ensure_repository", lambda **kw: pytest.fail("provisioned"))
    with pytest.raises(ValueError, match="overlap"):
        bootstrap.bootstrap(cohort=tmp_path, ahe_root=tmp_path, ahe_python=None, run_id="unit",
                            gitea_url="https://gitea.example", gitea_owner="o", gitea_repo="r",
                            worker_clone=tmp_path / "w", openagents_checkout=tmp_path / "w/.openagents/oa")
