"""AHE-Beta's own delivery Evolver (../../delivery/evolver.py) and what prepare.py, bootstrap.py and the launcher give
it, without OpenAgents or AHE-Beta: a stand-in sandbox records what each process is shown (`view`, then `wrap`),
stand-in processes play the worker, the controller and the sandboxed Git, and a local bare repository plays Gitea. What
the worker and the controller may read, write and keep of the environment, that nothing of the team runs without an
enforced sandbox, that the delivered adapter comes from Gitea and the binding moves last, the binding round 1's sessions
carry, and the launcher's config path are checked. A run against a real Gitea and OpenAgents is not (the README's
Validation reports the one made)."""

import json
import os
from pathlib import Path
import re
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
    paths = {name: tmp_path / name for name in ("repo", "worker", "openagents", "ahe", "state", "output")}
    for path in paths.values():
        path.mkdir()
    for name in ("repo", "worker"):                      # the clones bootstrap.py prepared
        git(paths[name], "init", "-q")
    for folder in ("backend/ahe_beta", "backend/agent_defs"):   # AHE-Beta's checkout, at the commit the run names
        (paths["ahe"] / folder).mkdir(parents=True)
        (paths["ahe"] / folder / "__init__.py").write_text("")
    git(paths["ahe"], "init", "-q")
    git(paths["ahe"], "add", "-A")
    git(paths["ahe"], "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ahe")
    controller_sha = git(paths["ahe"], "rev-parse", "HEAD")
    snapshot = cohort / "ahe-beta" / controller_sha                           # prepare.py's, of that commit
    shutil.copytree(paths["ahe"] / "backend", snapshot / "backend", ignore=shutil.ignore_patterns(".git"))
    git(paths["openagents"], "init", "-q")                                     # the pinned OpenAgents checkout
    git(paths["openagents"], "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "oa")
    adapter = tmp_path / "adapter"
    shutil.copytree(EXAMPLE / "template", adapter)
    (adapter / ".hermit/CARD.md").write_text("the Trainer's\n")
    from binding import record                           # the seed's: the adapter round 1's sessions ran
    record(binding, adapter=adapter, commit="c0ffee", run_id="unit", round_no=1, anew=True)
    cfg = {"root": str(paths["state"]), "repo": str(paths["repo"]), "roster": {},
           "identity": {"run_id": "unit", "controller_sha": controller_sha}, "snapshot": str(snapshot),
           "tracker": {"base_url": "http://10.0.0.5:3000", "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"},
           "definitions_root": str(snapshot / "backend/agent_defs"), "delivery_timeout": 1,
           "sessions_per_round": 1, "rounds": str(cohort / "rounds.json"), "binding": str(binding),
           "ahe_root": str(paths["ahe"]), "ahe_python": str(paths["ahe"] / ".venv/bin/python"),
           "worker": {"clone": str(paths["worker"]), "parallel": 2,
                      "env": ["GITEA_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"],
                      "config": str(cohort / "openagents-worker.yml")},   # this machine's copy (bootstrap.py)
           "openagents": {"repository": "https://example.invalid/openagents",
                          "commit": git(paths["openagents"], "rev-parse", "HEAD"),
                          "checkout": str(paths["openagents"])},
           "controller": {"env": ["DEMAND_LLM_BASE_URL", "DEMAND_LLM_API_KEY", "DEMAND_LLM_MODEL"]}}
    (cohort / "openagents-worker.yml").write_text("version: 1\nforge: gitea\n")
    for name, value in (("GITEA_TOKEN", "t0k3n"), ("ANTHROPIC_BASE_URL", "https://anthropic.example"),
                        ("ANTHROPIC_MODEL", "glm-5.3"), ("ANTHROPIC_AUTH_TOKEN", "sk-ant-gateway-0123456789"),
                        ("DEMAND_LLM_MODEL", "glm"), ("DEMAND_LLM_BASE_URL", "https://demand.example/v1"),
                        ("DEMAND_LLM_API_KEY", "dk"), ("UNRELATED_SECRET", "x")):   # what a run's shell has
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("OPENAGENTS_PERMISSION_MODE", raising=False)            # not the operator's
    claude = tmp_path / "share/claude/versions/2.1.312"                         # the native installer's layout:
    claude.parent.mkdir(parents=True)                                           # one binary
    claude.write_bytes(b"\x7fELF\x02\x01\x01\x00")
    (tmp_path / "local-bin").mkdir()
    (tmp_path / "local-bin/claude").symlink_to(claude)
    monkeypatch.setattr(module.shutil, "which",
                        lambda name, **kw: str(tmp_path / "local-bin/claude") if name == "claude" else None)
    proc = types.SimpleNamespace(receipt=None, worker=None, controller=1, git=0, on_git=None,   # None: runs on
                                 probe=0,                # setup's start of the controller's interpreter
                                 spend=None,             # the demand roles' usage events, into the controller's store
                                 version="2.1.284 (Claude Code)")   # what `claude --version` prints
    started, processes = [], {}

    def popen(argv, **kw):
        started.append(argv)
        if "vigil" in argv:
            code = proc.worker
        elif "ahe_beta.integrations.hermit.feedback" in argv:
            if proc.spend is not None:                  # where AHE-Beta's controller puts them (feedback.py main)
                with open(paths["state"] / "demand-usage.jsonl", "a") as store:
                    store.write("".join(json.dumps(event) + "\n" for event in proc.spend))
            if proc.receipt is not None:                # a delivered round: its receipt, then exit 0
                Path(argv[argv.index("--result") + 1]).write_text(json.dumps(proc.receipt))
            code = 0 if proc.receipt is not None else proc.controller
        elif getattr(module, "CONTROLLER_IMPORTS", None) in argv:   # setup's check that the controller starts
            code = proc.probe
        elif "--version" in argv:                        # the developers' Claude Code, run in the worker's view
            kw["stdout"].write(proc.version + "\n")
            code = proc.git
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
    (tmp_path / "trainer-state").mkdir()                 # the Evolver's state folder, in the Trainer's run folder
    ctx = types.SimpleNamespace(params={"delivery_config": "delivery.json"}, base=cohort, sandbox=box, cancel=None,
                                tools={"check_spec": lambda path: {"passed": True}},
                                workspace=types.SimpleNamespace(learning_material=tmp_path / "material",
                                                                adapter=adapter, output=paths["output"],
                                                                state=tmp_path / "trainer-state"))

    def configure(**changes):
        (cohort / "delivery.json").write_text(json.dumps({**cfg, **changes}))
    configure()

    def rebind():                                        # the adapter a test changed is the one round 1 ran
        record(binding, adapter=adapter, commit="c0ffee", run_id="unit", round_no=1, anew=True)
    return types.SimpleNamespace(module=module, evolver=cls(), ctx=ctx, box=box, started=started, ended=ended,
                                 processes=processes, private=private, cohort=cohort, binding=binding, claude=claude,
                                 proc=proc, configure=configure, rebind=rebind, adapter=adapter, snapshot=snapshot,
                                 **paths)


def refused(delivery, match):
    """The step's result, failed for `match`: a refusal after AHE-Beta's team ran returns what it spent."""
    result = delivery.evolver.step(delivery.ctx)
    assert result.status == "failed" and re.search(match, result.summary), result.summary
    assert [u.model for u in result.usage][-1:] == ["ahe-developers"]
    return result


def shown(call):
    return {Path(p).resolve() for p in [*call.get("readable", ()), *call.get("writable", ())]}


def view(delivery, role, n=-1):
    """The `n`th view of the run's sandbox the Evolver opened for `role` (ahe-claude, ahe-worker, ahe-controller,
    ahe-git)."""
    return [v for v in delivery.box.views if v.view_args.get("role") == role][n]


def test_the_worker_and_the_controller_run_in_views_of_the_runs_sandbox_with_only_what_they_need(delivery):
    """The worker (OpenAgents' developers, which run model-written commands) sees its clone, its home, its checkout and
    its tools; the controller, AHE-Beta's backend, the round's session records and the delivery state; neither the
    cohort's files (the personas), nor the run's own layout (each runs in a view: the run's own wraps bind the adapter,
    the material and the output at /w), nor a variable it was not handed."""
    refused(delivery, "feedback unavailable")
    assert delivery.box.calls == []
    assert [v.view_args for v in delivery.box.views] == [{"framework": False, "role": "ahe-claude"},
                                                         {"framework": False, "role": "ahe-worker"},
                                                         {"framework": False, "role": "ahe-controller"}]
    assert all(v.closed for v in delivery.box.views)
    worker, controller = (view(delivery, role).calls[0] for role in ("ahe-worker", "ahe-controller"))
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
    assert "ANTHROPIC_SMALL_FAST_MODEL" not in worker["env"]
    assert (worker["env"]["OPENAGENTS_PERMISSION_MODE"], worker["env"]["GIT_AUTHOR_NAME"]) == ("bypassPermissions",
                                                                                              "openagents")
    assert (worker["env"]["AUTO_DESIGNER_RUN_ID"], worker["env"]["AUTO_DESIGNER_ROUND"]) == ("unit", "1")
    assert controller["command"][:4] == ["/bin/sh", "-c", 'exec "$@"', "sh"]   # its venv's python by that name
    assert controller["command"][5:8] == ["-P", "-m", "ahe_beta.integrations.hermit.feedback"]
    readable = {Path(p).resolve() for p in controller["readable"]}
    backend = (delivery.snapshot / "backend").resolve()                       # the run's commit of AHE-Beta
    assert {delivery.private.resolve(), backend / "ahe_beta", backend / "agent_defs"} <= readable
    assert not readable & {delivery.ahe.resolve(), backend}                     # no experiments, secrets file, data
    assert {delivery.state.resolve(), delivery.repo.resolve()} <= {Path(p).resolve() for p in controller["writable"]}
    assert Path(controller["chdir"]).resolve() == (delivery.state / "controller-home").resolve()   # not the
                                                                                # developers' tree: no module of theirs
    assert "DEMAND_LLM_MODEL" in controller["env"] and "ANTHROPIC_BASE_URL" not in controller["env"]
    snapshot = delivery.snapshot.resolve()                                    # the controller's code, in the cohort
    for grants in (worker, controller):                                        # beside its files, not them
        assert not any(p == delivery.cohort.resolve() or delivery.cohort.resolve() in p.parents
                       for p in shown(grants) if snapshot not in p.parents)
        assert "UNRELATED_SECRET" not in grants["env"] and grants["forward"] == []
    assert "t0k3n" not in (call / "tools" / "askpass.sh").read_text()           # the token stays in the environment
    assert [a[-2:] for a in delivery.started[:1]] == [["claude", "--version"]]   # the developers' CLI, checked first
    assert len([a for a in delivery.started if "vigil" in a]) == 1
    assert len(delivery.ended) == 1 and delivery.processes[delivery.ended[0]].code == -15   # the worker, with the step


def test_the_worker_runs_on_the_model_and_the_configuration_it_is_handed(delivery, tmp_path, monkeypatch):
    """Its subagents locked on the model delivery.json hands it, and its configuration this machine's copy, shown
    read-only where OpenAgents reads it: a round's developers cannot change the next round's stages or models."""
    kept = tmp_path / "openagents-worker.yml"
    kept.write_text("version: 1\n")
    names = ["GITEA_TOKEN", "ANTHROPIC_MODEL", "ANTHROPIC_AUTH_TOKEN"]
    delivery.configure(worker={"clone": str(delivery.worker), "env": names, "config": str(kept)})
    monkeypatch.setenv("OPENAGENTS_PERMISSION_MODE", "default")             # this shell's, not the run's
    refused(delivery, ".")
    worker = view(delivery, "ahe-worker").calls[0]
    assert worker["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "glm-5.3"
    assert worker["env"]["ANTHROPIC_SMALL_FAST_MODEL"] == "glm-5.3"             # the light calls too: no Haiku there
    assert worker["binds"] == [(kept, str(delivery.worker.resolve() / ".openagents/openagents.yml"), False)]
    assert worker["env"]["OPENAGENTS_PERMISSION_MODE"] == "bypassPermissions"   # as AHE-Beta runs its developers
    delivery.configure(worker={"clone": str(delivery.worker), "env": [*names, "OPENAGENTS_PERMISSION_MODE"],
                               "config": str(kept)})
    refused(delivery, ".")
    assert view(delivery, "ahe-worker").calls[0]["env"]["OPENAGENTS_PERMISSION_MODE"] == "default"   # named: kept


def test_setup_refuses_what_no_step_could_mend_before_any_session_is_paid_for(delivery, monkeypatch):
    for name in ("GITEA_TOKEN", "ANTHROPIC_AUTH_TOKEN", "DEMAND_LLM_API_KEY"):
        monkeypatch.delenv(name)
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


def ready(delivery, monkeypatch, **worker):
    """All setup asks for (bar the sandbox's own Claude Code check), with the worker's folders changed by `worker`."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-gateway-0123456789")
    monkeypatch.setattr(delivery.module.os, "geteuid", lambda: 1000)           # not root, whoever runs the tests
    for name, value in (("DEMAND_LLM_BASE_URL", "https://demand.example/v1"), ("DEMAND_LLM_API_KEY", "dk"),
                        ("DEMAND_LLM_MODEL", "glm")):
        monkeypatch.setenv(name, value)
    python = delivery.ahe / ".venv/bin/python"
    python.parent.mkdir(parents=True, exist_ok=True)
    if not python.exists():
        python.symlink_to(sys.executable)
    (delivery.ahe / ".venv/pyvenv.cfg").write_text("home = /usr/bin\n")
    monkeypatch.setattr(delivery.module, "ensure_checkout", lambda spec, path: path)
    monkeypatch.setattr(delivery.module, "ensure_environment", lambda checkout: None)
    cfg = json.loads((delivery.cohort / "delivery.json").read_text())
    delivery.configure(tracker={"base_url": "http://localhost:3000", "owner": "o", "repo": "r",
                                "token_env": "GITEA_TOKEN"},
                       worker={**cfg["worker"], "env": [*cfg["worker"]["env"], "ANTHROPIC_AUTH_TOKEN"],
                               **worker.pop("worker", {})}, **worker)


def test_setup_runs_the_developers_claude_in_the_workers_view_and_every_round_runs_that_one(delivery, monkeypatch):
    """Setup found `claude` on this machine's PATH and every step resolved it again: a wrapper the worker's view cannot
    run failed only after a paid round of sessions, and an installer's update changed the developers between rounds."""
    ready(delivery, monkeypatch)
    delivery.evolver.setup(delivery.ctx)
    probe = view(delivery, "ahe-claude")
    assert probe.calls[0]["command"][-2:] == ["claude", "--version"]
    assert delivery.claude.resolve() in shown(probe.calls[0]) and probe.closed
    assert probe.calls[0]["env"]["AHE_WORKER_PATH"].startswith(str(Path(probe.calls[0]["readable"][0]) / "bin"))
    record = delivery.cohort / "delivery-developers.json"
    kept = json.loads(record.read_text())
    assert (kept["path"], kept["run_id"], kept["version"]) == (str(delivery.claude.resolve()), "unit",
                                                               "2.1.284 (Claude Code)")
    delivery.proc.version = "2.1.290 (Claude Code)"                            # a wrapper's program updated behind it
    with pytest.raises(delivery.module.RunUnavailable, match="changed since the run was set up"):
        delivery.evolver.step(delivery.ctx)
    delivery.proc.version = "2.1.284 (Claude Code)"
    delivery.claude.write_bytes(b"\x7fELF an update")                         # the binary itself, between two rounds
    with pytest.raises(delivery.module.RunUnavailable, match="changed since the run was set up"):
        delivery.evolver.step(delivery.ctx)
    record.unlink()                                                            # a resumed run with no record of it
    with pytest.raises(delivery.module.RunUnavailable, match="record of the developers' Claude Code"):
        delivery.evolver.step(delivery.ctx)
    delivery.claude.write_bytes(b"\x7fELF\x02\x01\x01\x00")
    record.write_text(json.dumps(kept))
    delivery.proc.git = 1                                                      # it no longer starts in that view
    with pytest.raises(delivery.module.RunUnavailable, match="does not run in the worker's view"):
        delivery.evolver.step(delivery.ctx)


def test_setup_refuses_folders_that_give_a_process_more_than_its_own(delivery, tmp_path, monkeypatch):
    """The worker's home is beside its clone (`<clone>-home`): an OpenAgents checkout there was the worker's to write,
    and this machine runs uv in it; a clone that holds the cohort showed the worker its personas."""
    venv = delivery.worker.with_name("worker-home") / "venv"                  # AHE-Beta's environment in the worker's
    (venv / "bin").mkdir(parents=True)                                        # home: the worker writes what the
    (venv / "bin/python").symlink_to(sys.executable)                          # controller runs
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    ready(delivery, monkeypatch)
    delivery.configure(**{**json.loads((delivery.cohort / "delivery.json").read_text()),
                          "ahe_python": str(venv / "bin/python")})
    with pytest.raises(delivery.module.RunUnavailable, match="the controller's environment overlap"):
        delivery.evolver.setup(delivery.ctx)
    checkout = tmp_path / "dev-home"
    checkout.mkdir()
    cfg = json.loads((delivery.cohort / "delivery.json").read_text())
    cfg["ahe_python"] = str(delivery.ahe / ".venv/bin/python")
    delivery.configure(**cfg)
    ready(delivery, monkeypatch, worker={"clone": str(tmp_path / "dev")},
          openagents={**cfg["openagents"], "checkout": str(checkout)})
    with pytest.raises(delivery.module.RunUnavailable, match="the worker's home and the OpenAgents checkout overlap"):
        delivery.evolver.setup(delivery.ctx)
    ready(delivery, monkeypatch, worker={"readable": [str(tmp_path)]})
    with pytest.raises(delivery.module.RunUnavailable, match="is in or holds the cohort"):
        delivery.evolver.setup(delivery.ctx)
    ready(delivery, monkeypatch, worker={"clone": str(delivery.cohort / ".logs")})   # the sessions' records
    with pytest.raises(delivery.module.RunUnavailable, match="the sessions' records overlap"):
        delivery.evolver.setup(delivery.ctx)
    ready(delivery, monkeypatch, worker={"readable": [str(delivery.cohort / "assignments.json")]})   # the personas
    with pytest.raises(delivery.module.RunUnavailable, match="is in or holds the cohort"):
        delivery.evolver.setup(delivery.ctx)
    ready(delivery, monkeypatch, worker={"readable": [str(delivery.state / "sessions")]})   # the users' records
    with pytest.raises(delivery.module.RunUnavailable, match="overlaps the controller's state"):
        delivery.evolver.setup(delivery.ctx)


def test_a_gitea_on_this_machines_loopback_is_forwarded_into_the_sandbox(delivery):
    delivery.configure(tracker={"base_url": "http://localhost:3000", "owner": "o", "repo": "r",
                                "token_env": "GITEA_TOKEN"})
    refused(delivery, ".")
    assert [view(delivery, role).calls[0]["forward"] for role in ("ahe-worker", "ahe-controller")] == [[3000], [3000]]


def test_the_developers_find_claude_on_the_workers_path(delivery):
    """OpenAgents runs `claude` by name (AHE-Beta puts it in a bin folder on the worker's PATH); a link on this
    machine's PATH (~/.local/bin/claude, the native installer's) is not shown inside, only the file it leads to."""
    refused(delivery, ".")
    worker = view(delivery, "ahe-worker").calls[0]
    bin_ = delivery.state / "calls" / "round-1" / "tools" / "bin"
    assert (bin_ / "claude").resolve() == delivery.claude and delivery.claude in shown(worker)
    assert delivery.claude.parent not in shown(worker)                         # the binary, nothing beside it
    assert worker["env"]["AHE_WORKER_PATH"].split(":")[:2] == [str(bin_),           # the pin first: an OpenAgents
                                                               str(delivery.openagents.resolve() / ".venv/bin")]


def test_a_claude_installed_with_npm_brings_its_node(delivery, tmp_path, monkeypatch):
    """A node CLI (npm's claude, `#!/usr/bin/env node`) needs the node beside it, which nvm keeps in the home."""
    node = tmp_path / "nvm/versions/node/v22/bin/node"
    node.parent.mkdir(parents=True)
    node.write_text("")
    node.chmod(0o755)
    cli = tmp_path / "nvm/versions/node/v22/lib/node_modules/@anthropic-ai/claude-code/cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/usr/bin/env node\n")
    cli.chmod(0o755)                                                           # as npm leaves a package's bin
    (node.parent / "claude").symlink_to(cli)
    found = {"claude": str(node.parent / "claude"), "node": str(node)}
    monkeypatch.setattr(delivery.module.shutil, "which", lambda name, **kw: found.get(name) or (
        name if Path(name).is_absolute() and os.access(name, os.X_OK) else None))   # a path: itself, as which
    refused(delivery, ".")
    worker = view(delivery, "ahe-worker").calls[0]
    assert {cli.parent, node.parent.parent} <= shown(worker)
    other = tmp_path / "nvm/versions/node/v24"                                 # another npm installation made the
    (other / "bin").mkdir(parents=True)                                        # PATH's: what the pinned one needs
    (other / "bin/node").write_text("")                                        # is still found from its own link
    (other / "bin/node").chmod(0o755)
    found["claude"], found["node"] = str(other / "bin/claude"), str(other / "bin/node")
    refused(delivery, ".")
    again = view(delivery, "ahe-worker").calls[0]
    assert {cli.parent, node.parent.parent} <= shown(again) and other.resolve() not in shown(again)
    pinned = delivery.state / "calls/round-1/tools/bin/node"                  # `#!/usr/bin/env node` finds this one
    assert pinned.is_symlink() and pinned.resolve() == node.resolve()


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
    refused(delivery, ".")
    worker = view(delivery, "ahe-worker").calls[0]
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
    refused(delivery, "worker exited")
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
    delivery.proc.receipt = {"round": 1, "tested_adapter": "c0ffee", "delivered_adapter": commit}
    delivery.proc.spend = [{"round_no": 1, "input_tokens": 10, "output_tokens": 2, "total_tokens": 12}]
    gits, run = [], delivery.module.subprocess.run
    monkeypatch.setattr(delivery.module.subprocess, "run", lambda argv, **kw: gits.append(argv) or run(argv, **kw))
    result = delivery.evolver.step(delivery.ctx)
    assert all(argv[1:3] == ["-c", "credential.helper="] for argv in gits if "fetch" in argv)   # no helper keeps it
    assert result.status == "done" and (delivery.adapter / "ADAPTER.md").read_text() == "delivered\n"
    assert (delivery.adapter / ".hermit/CARD.md").read_text() == "the Trainer's\n"
    assert not (delivery.adapter / ".openagents").exists() and not (delivery.adapter / "specs").exists()
    git_view = view(delivery, "ahe-git")
    assert git_view.view_args["role"] == "ahe-git" and git_view.calls[0]["command"][-1] == commit
    assert git_view.calls[0]["writable"] == [delivery.repo.resolve()] and git_view.closed
    bound = json.loads(delivery.binding.read_text())
    assert (bound["round"], bound["harness_sha"]) == (2, commit)
    assert bound["adapter_sha256"] == fingerprint(delivery.adapter)
    kept = json.loads((delivery.output / "delivery.json").read_text())
    assert kept["delivered_adapter"] == kept["bound_adapter"] == commit       # Hermit's tree kept it as it is
    assert [(u.model, u.input_tokens + u.output_tokens, u.unreported_calls) for u in result.usage] == [
        ("ahe-demand", 12, 0), ("ahe-developers", 0, 1)]


def test_a_round_after_a_failed_one_runs_and_delivers_on_the_last_delivered_commit(delivery, tmp_path):
    """Round 1's step failed: the Trainer kept the seed adapter and went on. The one binding said round 1, so round 2's
    sessions refused it and every later step found its binding \"outside this logical round\"."""
    url, commit = gitea(tmp_path, {"ADAPTER.md": "delivered in round 2\n"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    (delivery.cohort / "rounds.json").write_text(json.dumps({"r01-s00-t0": 2}))
    delivery.proc.receipt = {"round": 2, "tested_adapter": "c0ffee", "delivered_adapter": commit}
    assert delivery.evolver.step(delivery.ctx).status == "done"
    request = json.loads((delivery.state / "calls/round-2/request.json").read_text())
    assert (request["round"], request["user_sha"]) == (2, "c0ffee")             # the commit its sessions ran
    bound = json.loads(delivery.binding.read_text())
    assert (bound["round"], bound["harness_sha"]) == (3, commit) and [e["round"] for e in bound["history"]] == [1, 3]


def test_a_round_sealed_before_a_crash_is_taken_again_without_rerunning_the_team(delivery, tmp_path):
    """AHE-Beta sealed round 1 and the binding moved, then this machine stopped before the Trainer's checkpoint: the
    Trainer redoes the step, with new sessions, which upstream refuses for a frozen round. Its delivery is taken again
    from the receipt; neither the worker nor the controller starts."""
    from binding import fingerprint
    url, commit = gitea(tmp_path, {"ADAPTER.md": "delivered\n"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    receipt = {"schema_version": 1, "round": 1, "tested_adapter": "c0ffee", "delivered_adapter": commit,
               "session_ids": ["s0"]}
    (delivery.state / "round-1-adapter.json").write_text(json.dumps(receipt))
    (delivery.state / "calls/round-1").mkdir(parents=True)
    (delivery.state / "demand-usage.jsonl").write_text(                       # round 0's, then the stopped attempt's
        json.dumps({"round_no": 0, "input_tokens": 50, "output_tokens": 5}) + "\n")
    offset = (delivery.state / "demand-usage.jsonl").stat().st_size
    (delivery.state / "calls/round-1/usage-offset").write_text(str(offset))
    with open(delivery.state / "demand-usage.jsonl", "a") as ledger:
        ledger.write(json.dumps({"round_no": 1, "input_tokens": 7, "output_tokens": 3, "total_tokens": 10}) + "\n")
    value = json.loads(delivery.binding.read_text())   # what the stopped attempt bound for round 2
    value["history"].append({"round": 2, "harness_sha": commit, "adapter_sha256": "its delivered files"})
    delivery.binding.write_text(json.dumps({**value, **value["history"][-1]}))
    result = delivery.evolver.step(delivery.ctx)
    assert result.status == "done" and result.extra["delivery"]["resealed"] is True
    assert not any("vigil" in argv or "ahe_beta.integrations.hermit.feedback" in argv for argv in delivery.started)
    assert (delivery.adapter / "ADAPTER.md").read_text() == "delivered\n"
    bound = json.loads(delivery.binding.read_text())
    assert bound["history"][-1] == {"round": 2, "harness_sha": commit, "adapter_sha256": fingerprint(delivery.adapter)}
    assert (result.usage[0].input_tokens, result.usage[0].output_tokens) == (7, 3)   # that attempt's, unrecorded


def test_what_the_demand_roles_spent_counts_when_the_round_fails_after_them(delivery):
    """A round that failed after its demand roles ran reported none of it (and an attempt's own file, which AHE-Beta's
    controller overrides with its state's demand-usage.jsonl, was empty). A call that reported its total alone and one
    its prompt alone are each kept as they said."""
    (delivery.state / "demand-usage.jsonl").write_text(                       # an earlier round's: not this one's
        json.dumps({"round_no": 1, "input_tokens": 999, "output_tokens": 1, "run_id": "an earlier run"}) + "\n"
        + json.dumps({"round_no": 0, "input_tokens": 999, "output_tokens": 1}) + "\n")
    delivery.proc.spend = [{"round_no": 1, "input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100,
                            "cached_input_tokens": 400}, {"round_no": 1, "usage_source": "unavailable"},
                           {"round_no": 1, "total_tokens": 100}, {"round_no": 1, "input_tokens": 60},
                           {"round_no": 1, "input_tokens": 60, "output_tokens": 20, "total_tokens": 100},
                           {"round_no": 1, "cached_input_tokens": 8000}]   # its cached ones alone: at least those
    result = delivery.evolver.step(delivery.ctx)                                # the controller then exits 1
    assert result.status == "failed" and "feedback unavailable" in result.summary
    demand = result.usage[0]                                                   # the last: a total its parts leave short
    assert (demand.model, demand.input_tokens + demand.output_tokens, demand.cached_input_tokens, demand.calls,
            demand.unreported_calls) == ("ahe-demand", 1100 + 100 + 60 + 100 + 8000, 400 + 8000, 6, 5)


def test_a_demand_receipt_with_a_total_alone_still_counts_its_tokens(delivery, tmp_path):
    url, commit = gitea(tmp_path, {"ADAPTER.md": "delivered\n"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    delivery.proc.spend = [{"round_no": 1, "total_tokens": 4000}]
    demand = delivery.evolver.step(delivery.ctx).usage[0]
    assert (demand.input_tokens + demand.output_tokens, demand.calls, demand.unreported_calls) == (4000, 1, 1)


def test_a_round_sealed_in_ahe_betas_record_before_its_receipt_is_taken_again(delivery, tmp_path):
    """Upstream writes the sealed commit to its recovery record, then the receipt (not atomically): a stop between the
    two left no receipt, or part of one, and the redone round was sent to a controller that refuses it."""
    url, commit = gitea(tmp_path, {"ADAPTER.md": "delivered\n"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    (delivery.state / "recovery.json").write_text(json.dumps({"revision": 9, "rounds": {"1": {
        "status": "complete", "user_sha": "c0ffee", "sealed_sha": commit, "plan": {"sessions": ["s0"]}}}}))
    (delivery.state / "round-1-adapter.json").write_text('{"schema_version": 1, "round": 1, "tested_')   # cut short
    result = delivery.evolver.step(delivery.ctx)
    assert result.status == "done" and result.extra["delivery"]["session_ids"] == ["s0"]
    assert not any("ahe_beta.integrations.hermit.feedback" in argv for argv in delivery.started)
    assert json.loads(delivery.binding.read_text())["harness_sha"] == commit


def test_a_fresh_trainer_run_on_a_used_delivery_state_is_refused_before_any_session(delivery, monkeypatch):
    """`resume_mode=disable` starts the Trainer over and leaves the cohort's delivery state. A cohort of two rounds
    trains on its first (prepare.py keeps the last for the test): a finished run has round 1 sealed and round 2 bound,
    which a fresh run took for its own. A run's own claim (its state folder, in its run folder, which a restart keeps)
    lets a run stopped before its first checkpoint set up again."""
    ready(delivery, monkeypatch)
    (delivery.state / "calls/round-1").mkdir(parents=True)                    # the finished run's round 1
    (delivery.state / "round-1-adapter.json").write_text("{}")
    value = json.loads(delivery.binding.read_text())
    value["history"].append({"round": 2, "harness_sha": "d" * 40, "adapter_sha256": "its delivered files"})
    delivery.binding.write_text(json.dumps({**value, **value["history"][-1]}))
    with pytest.raises(delivery.module.RunUnavailable, match=r"an earlier Trainer run's \(rounds \[1\]\)"):
        delivery.evolver.setup(delivery.ctx)
    (delivery.ctx.workspace.state / "ahe-delivery.json").write_text('{"run_id": "unit"}')   # this run's, then a stop
    delivery.evolver.setup(delivery.ctx)

def test_the_controller_and_the_sessions_run_ahe_betas_commit_not_its_checkout(delivery):
    """The controller ran the live checkout's backend/ahe_beta: a commit other than the one the run's identity names,
    and the checkout's own files, such as the simulator's backend/ahe_beta/user/.env (ignored by AHE-Beta's Git,
    loaded by its persona client), which the roles' read-only tools could read into a prompt."""
    (delivery.ahe / "backend/ahe_beta/user").mkdir()
    (delivery.ahe / "backend/ahe_beta/user/.env").write_text("USER_LLM_API_KEY=sk-not-for-the-controller\n")
    (delivery.ahe / "backend/ahe_beta/roles.py").write_text("CHANGED = True\n")   # the checkout moved on since
    refused(delivery, "feedback unavailable")
    controller = view(delivery, "ahe-controller").calls[0]
    shown_ = {Path(p).resolve() for p in controller["readable"]}
    assert (delivery.snapshot / "backend/ahe_beta").resolve() in shown_
    assert not any(delivery.ahe.resolve() in p.parents or p == delivery.ahe.resolve() for p in shown_)
    assert controller["env"]["PYTHONPATH"] == str(delivery.snapshot.resolve() / "backend")
    shutil.rmtree(delivery.snapshot)
    with pytest.raises(delivery.module.RunUnavailable, match="snapshot of the run's commit"):
        delivery.evolver.step(delivery.ctx)

def test_a_session_whose_native_index_holds_two_entries_gives_no_evidence(delivery):
    """AHE-Beta's import requires exactly one entry: one valid beside an invalid one passed here and failed there."""
    (delivery.private / "sessions/index.json").write_text(json.dumps([{"session_id": "s1"},
                                                                     {"session_id": "s2", "evaluation_status": "x"}]))
    with pytest.raises(delivery.module.RunUnavailable, match="were rated but left no native record"):
        delivery.evolver.step(delivery.ctx)


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
    refused(delivery, "feedback unavailable")
    request = json.loads((delivery.state / "calls/round-1/request.json").read_text())
    assert request["session_roots"] == [str(delivery.private.resolve())] and request["sessions_per_round"] == 1


def test_a_session_whose_adapter_failed_is_not_given_to_the_feedback_import(delivery, tmp_path):
    """A session whose adapter failed before Codex ran (its instruction and the adapter past Codex's context, say) is
    the agent's 0, with no rating of its user's: taken for a lost record, or given with the error record it kept, it
    refused the round's other users."""
    absent = tmp_path / "users" / "r01-s01-t1-def"                             # no record of its own
    absent.mkdir(parents=True)
    kept = tmp_path / "users" / "r01-s02-t2-ghi"                               # one kept, its error in it
    shutil.copytree(delivery.private, kept)
    for task, private in (("r01-s01-t1", absent), ("r01-s02-t2", kept)):
        (tmp_path / "material/current/tasks" / f"{task}.json").write_text(json.dumps(
            {"id": task, "status": "ok", "pipeline_extra": {"adapter_failed": "context_over_limit",
                                                            "user": {"private": str(private)}}}))
    (delivery.cohort / "rounds.json").write_text(json.dumps({"r01-s00-t0": 1, "r01-s01-t1": 1, "r01-s02-t2": 1}))
    delivery.configure(sessions_per_round=3)
    refused(delivery, "feedback unavailable")
    request = json.loads((delivery.state / "calls/round-1/request.json").read_text())
    assert request["session_roots"] == [str(delivery.private.resolve())] and request["sessions_per_round"] == 1


def test_a_rated_session_whose_native_record_is_gone_stops_the_round(delivery, tmp_path):
    """Its index lost (not a session rated out of time): left out, the round's feedback ran on the rest, with the
    count AHE-Beta's completeness check compares made to match."""
    other = tmp_path / "users" / "r01-s01-t1-def"
    other.mkdir(parents=True)                                                  # no sessions/index.json
    (tmp_path / "material/current/tasks/r01-s01-t1.json").write_text(json.dumps(
        {"id": "r01-s01-t1", "status": "ok", "pipeline_extra": {"user": {"private": str(other)}}}))
    (delivery.cohort / "rounds.json").write_text(json.dumps({"r01-s00-t0": 1, "r01-s01-t1": 1}))
    delivery.configure(sessions_per_round=2)
    with pytest.raises(delivery.module.RunUnavailable, match=r"r01-s01-t1.*were rated but left no native record"):
        delivery.evolver.step(delivery.ctx)
    assert not [a for a in delivery.started if "vigil" in a]


def test_a_folder_the_worker_is_shown_that_holds_the_sessions_records_is_refused_before_it_runs(delivery, tmp_path):
    """The sessions' stores where `agent.params.workspaces` puts them, inside a folder delivery.json shows the worker
    besides (a wrapper's programs, say): its developers would read the users' native records."""
    tools = tmp_path / "srv-tools"
    private = tools / "workspaces/user/r01-s00-t0-abc"
    shutil.copytree(delivery.private, private)
    (tmp_path / "material/current/tasks/r01-s00-t0.json").write_text(json.dumps(
        {"id": "r01-s00-t0", "status": "ok", "pipeline_extra": {"user": {"private": str(private)}}}))
    worker = json.loads((delivery.cohort / "delivery.json").read_text())["worker"]
    delivery.configure(worker={**worker, "readable": [str(tools)]})
    with pytest.raises(delivery.module.RunUnavailable, match="overlaps the sessions' records"):
        delivery.evolver.step(delivery.ctx)
    assert not [a for a in delivery.started if "vigil" in a or "--version" in a]   # neither the probe nor the worker


def test_setup_refuses_a_controller_environment_that_cannot_run_it(delivery, monkeypatch):
    """A virtual environment without AHE-Beta's dependencies (NexAU, say) passed setup: the run paid for a round of
    sessions, then every step's controller failed to import."""
    ready(delivery, monkeypatch)
    delivery.proc.probe = 1
    with pytest.raises(delivery.module.RunUnavailable, match="controller does not start"):
        delivery.evolver.setup(delivery.ctx)
    check = view(delivery, "ahe-controller-check").calls[0]
    assert check["command"][-1] == delivery.module.CONTROLLER_IMPORTS
    assert check["env"]["PYTHONPATH"] == str(delivery.snapshot.resolve() / "backend")   # as each step runs it
    assert not (delivery.ctx.workspace.state / "ahe-delivery.json").exists()  # not set up


def test_a_worker_whose_kept_configuration_is_gone_is_refused(delivery, tmp_path, monkeypatch):
    """The sandbox skips a bind whose source is gone, and none was asked without the setting: OpenAgents then read the
    clone's own copy, which its developers may write (the stages, review and merge settings). Refused at setup, before
    a round of sessions, and at each step."""
    worker = json.loads((delivery.cohort / "delivery.json").read_text())["worker"]
    delivery.configure(worker={**worker, "config": str(tmp_path / "gone.yml")})
    with pytest.raises(delivery.module.RunUnavailable, match="configuration .* is gone"):
        delivery.evolver.step(delivery.ctx)
    assert not [a for a in delivery.started if "vigil" in a]
    for config in ({"config": str(tmp_path / "gone.yml")}, {"config": None}):
        ready(delivery, monkeypatch, worker=config)
        with pytest.raises(delivery.module.RunUnavailable, match="no worker configuration this machine keeps"):
            delivery.evolver.setup(delivery.ctx)


def test_setup_refuses_a_worker_configuration_the_worker_could_write(delivery, monkeypatch):
    """Kept in the worker's home, it stayed writable at its own name though shown read-only at the clone's: a round's
    developers changed the next round's stages."""
    home = delivery.worker.resolve().with_name(delivery.worker.name + "-home")
    home.mkdir(exist_ok=True)
    (home / "openagents.yml").write_text("version: 1\n")
    ready(delivery, monkeypatch, worker={"config": str(home / "openagents.yml")})
    with pytest.raises(delivery.module.RunUnavailable, match="named through a folder the worker writes"):
        delivery.evolver.setup(delivery.ctx)
    (home / "openagents.yml").unlink()                                         # a link there to this machine's copy:
    (home / "openagents.yml").symlink_to(delivery.cohort / "openagents-worker.yml")   # the worker could replace it
    with pytest.raises(delivery.module.RunUnavailable, match="named through a folder the worker writes"):
        delivery.evolver.setup(delivery.ctx)


def test_a_worker_grant_named_through_its_own_clone_is_refused(delivery, monkeypatch, tmp_path):
    """A readable folder named by a link in the worker's clone, to an installation it may see: the worker could point
    the link at another host folder (its keys, say) before a later round mounted it."""
    installed = tmp_path / "tools-installed"
    installed.mkdir()
    (delivery.worker / "tools").symlink_to(installed)
    ready(delivery, monkeypatch, worker={"readable": [str(delivery.worker / "tools")]})
    with pytest.raises(delivery.module.RunUnavailable, match="named through a folder the worker writes"):
        delivery.evolver.setup(delivery.ctx)
    (tmp_path / "link-out").symlink_to(delivery.worker / "tools")              # a link to such a link: the same
    ready(delivery, monkeypatch, worker={"readable": [str(tmp_path / "link-out")]})
    with pytest.raises(delivery.module.RunUnavailable, match="named through a folder the worker writes"):
        delivery.evolver.setup(delivery.ctx)
    ready(delivery, monkeypatch, worker={"readable": [str(installed)]})         # named directly: given
    delivery.evolver.setup(delivery.ctx)


def test_a_gitea_on_this_machine_is_reached_without_this_machines_proxy(monkeypatch, tmp_path):
    """urllib and Git gave the proxy a loopback Gitea's requests, the token in clear (bootstrap allows plain http on
    this machine)."""
    import http.server
    import threading
    import gitea_runtime
    asked = []

    class Gitea(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            asked.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"login": "o"}')

        def log_message(self, *args):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Gitea)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(gitea_runtime, "_OPEN", lambda *a, **k: pytest.fail("the opener with this machine's proxy"))
    try:
        monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")         # unreachable: through it, a failure
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        assert gitea_runtime._request(f"http://127.0.0.1:{server.server_port}", "t", "GET", "user") == \
            (200, {"login": "o"})
        assert asked == ["/api/v1/user"]
    finally:
        server.shutdown()
    env = gitea_runtime.direct({"NO_PROXY": "corp.example"}, "http://localhost:3000/o/r.git")
    assert env["NO_PROXY"] == env["no_proxy"] and env["no_proxy"].split(",")[:2] == ["corp.example", "localhost"]
    assert gitea_runtime.direct({"no_proxy": "*"}, "http://localhost:3000/o/r.git")["no_proxy"] == "*"
    assert gitea_runtime.direct({"A": "b"}, "https://gitea.example/o/r.git") == {"A": "b"}   # elsewhere: as it was


def test_setup_refuses_two_claude_credentials_for_the_developers(delivery, monkeypatch):
    """An Anthropic key already in the shell beside the gateway's token: both reached every developer, and Claude Code
    could send the Anthropic key to the gateway."""
    ready(delivery, monkeypatch, worker={"env": ["GITEA_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                                                 "ANTHROPIC_API_KEY"]})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-the-operators-own")
    with pytest.raises(delivery.module.RunUnavailable, match="more than one Claude Code credential"):
        delivery.evolver.setup(delivery.ctx)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    delivery.evolver.setup(delivery.ctx)


def test_setup_refuses_a_subscription_token_for_a_gateway_and_a_host_the_sandbox_blocks(delivery, monkeypatch):
    """A Claude subscription token beside a gateway's ANTHROPIC_BASE_URL went to that gateway; a demand model moved to
    another host since bootstrap passed setup, and the sandbox's proxy refused it after the round's sessions."""
    ready(delivery, monkeypatch, worker={"env": ["GITEA_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"]})
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-subscription")
    with pytest.raises(delivery.module.RunUnavailable, match="subscription token with ANTHROPIC_BASE_URL"):
        delivery.evolver.setup(delivery.ctx)
    ready(delivery, monkeypatch)                                               # the gateway's own token, then
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")                              # a host the sandbox does not list
    from hermit.sandbox import AllowlistProxy

    def through(allow):                                                        # the proxy's own set (then closed)
        proxy = AllowlistProxy(allow)
        try:
            return proxy.hosts
        finally:
            proxy.close()
    for allow in (["anthropic.example", "demand-before.example"], ["anthropic.example", "*.example"]):
        delivery.ctx.sandbox.allow = allow                                     # the proxy takes names exactly
        assert "demand.example" not in through(allow)
        with pytest.raises(delivery.module.RunUnavailable, match="does not let demand.example through"):
            delivery.evolver.setup(delivery.ctx)
    delivery.ctx.sandbox.allow = []                                            # none: the run has no network
    with pytest.raises(delivery.module.RunUnavailable, match="anthropic.example, demand.example through"):
        delivery.evolver.setup(delivery.ctx)
    delivery.ctx.sandbox.allow = ["anthropic.example", "demand.example."]     # the proxy compares names exactly
    assert "demand.example" not in through(delivery.ctx.sandbox.allow)
    with pytest.raises(delivery.module.RunUnavailable, match="does not let demand.example through"):
        delivery.evolver.setup(delivery.ctx)
    delivery.ctx.sandbox.allow = ["https://anthropic.example", "demand.example"]   # a URL: its host, as the proxy
    assert {"anthropic.example", "demand.example"} <= through(delivery.ctx.sandbox.allow)
    delivery.evolver.setup(delivery.ctx)
    monkeypatch.delenv("ANTHROPIC_BASE_URL")                                   # Claude Code's own default
    with pytest.raises(delivery.module.RunUnavailable, match="does not let api.anthropic.com through"):
        delivery.evolver.setup(delivery.ctx)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://anthropic.example")
    monkeypatch.setenv("DEMAND_LLM_BASE_URL", "http://demand.example/v1")      # its key in the clear
    with pytest.raises(delivery.module.RunUnavailable, match="over plain http"):
        delivery.evolver.setup(delivery.ctx)
    for local in ("http://127.0.0.2:8000/v1", "http://[::1]:8000/v1"):        # this machine's, under a name whose
        monkeypatch.setenv("DEMAND_LLM_BASE_URL", local)                       # port the sandbox does not forward
        with pytest.raises(delivery.module.RunUnavailable, match="whose port the sandbox forwards"):
            delivery.evolver.setup(delivery.ctx)
    monkeypatch.setenv("DEMAND_LLM_BASE_URL", "http://localhost:8000/v1")      # one it forwards
    delivery.evolver.setup(delivery.ctx)


def test_setup_takes_a_subscription_token_for_anthropics_api_however_its_port_is_written(delivery, monkeypatch):
    """ANTHROPIC_BASE_URL=https://api.anthropic.com:443 beside a subscription token was refused as a gateway's."""
    ready(delivery, monkeypatch, worker={"env": ["GITEA_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"]})
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-subscription")
    for spelling in ("https://api.anthropic.com:443", "https://API.anthropic.com/"):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", spelling)
        delivery.evolver.setup(delivery.ctx)
    for other in ("https://api.anthropic.com:8443", "https://someone@api.anthropic.com", "http://api.anthropic.com"):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", other)
        with pytest.raises(delivery.module.RunUnavailable, match="subscription token"):
            delivery.evolver.setup(delivery.ctx)


def test_setup_refuses_a_sandbox_that_does_not_let_the_gitea_through(delivery, monkeypatch):
    """A remote Gitea left off the run's allowed hosts (narrowed by hand): setup passed, the round's sessions were paid
    for, and the controller could not deliver their feedback."""
    ready(delivery, monkeypatch)
    cfg = json.loads((delivery.cohort / "delivery.json").read_text())
    delivery.configure(**{**cfg, "tracker": {**cfg["tracker"], "base_url": "https://gitea.example"}})
    delivery.ctx.sandbox.allow = ["anthropic.example", "demand.example"]
    with pytest.raises(delivery.module.RunUnavailable, match="does not let gitea.example through"):
        delivery.evolver.setup(delivery.ctx)
    delivery.ctx.sandbox.allow = ["anthropic.example", "demand.example", "gitea.example", "localhost"]
    with pytest.raises(delivery.module.RunUnavailable, match="lets this machine through"):   # every port of it
        delivery.evolver.setup(delivery.ctx)
    delivery.ctx.sandbox.allow = ["anthropic.example", "demand.example", "gitea.example"]
    delivery.evolver.setup(delivery.ctx)


def test_bootstrap_refuses_a_worker_home_another_cohort_left(tmp_path, monkeypatch):
    """An external --worker-clone removed after a run left its home beside it: the next cohort at that path gave its
    developers the earlier cohort's files and Claude Code state."""
    import bootstrap
    import gitea_runtime
    answers = {"repos/o/new": (404, {}), "user": (200, {"login": "o"})}
    monkeypatch.setattr(gitea_runtime, "_request", lambda base, token, method, path, body=None:
                        answers.get(path, (201, {"private": True, "empty": True})) if method == "GET" else
                        (201, {"private": True, "empty": True}))
    monkeypatch.setattr(bootstrap, "seed_repository", lambda **kw: (_ for _ in ()).throw(RuntimeError("stop here")))
    monkeypatch.setenv("GITEA_TOKEN", "t")
    given = dict(ahe_root=tmp_path, ahe_python=None, run_id="unit", gitea_url="https://gitea.example",
                 gitea_owner="o", gitea_repo="new", worker_clone=tmp_path / "elsewhere/worker")
    for name in ("first", "second"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "cohort.json").write_text(json.dumps({"roles": {"developer": 1}}))
    with pytest.raises(RuntimeError, match="stop here"):
        bootstrap.bootstrap(cohort=tmp_path / "first", **given)
    assert (tmp_path / "elsewhere/worker-home").is_dir()                       # made: no other cohort clones there
    with pytest.raises(RuntimeError, match="stop here"):                       # another clone path for a while,
        bootstrap.bootstrap(cohort=tmp_path / "first", **{**given, "worker_clone": tmp_path / "moved/worker"})
    (tmp_path / "elsewhere/worker-home/.claude.json").write_text("{}")
    with pytest.raises(RuntimeError, match="stop here"):                       # its own cohort's: taken again
        bootstrap.bootstrap(cohort=tmp_path / "first", **given)
    answers["repos/o/new"] = (200, {"private": True, "empty": True})          # (a Gitea call would be refused)
    with pytest.raises(ValueError, match="not this cohort's"):                 # another cohort's: refused before
        bootstrap.bootstrap(cohort=tmp_path / "second", **given)               # anything is made for it
    assert not (tmp_path / "second/bootstrap.json").exists() or "marker" not in json.loads(
        (tmp_path / "second/bootstrap.json").read_text())


def test_setup_refuses_a_prepared_clone_that_is_gone(delivery, monkeypatch):
    """The delivery clone on scratch storage, cleared since bootstrap: setup passed, the sandbox made an empty folder,
    and the controller failed after the round's sessions were paid for, every round."""
    for clone in (delivery.repo, delivery.worker):
        shutil.rmtree(clone / ".git")
        ready(delivery, monkeypatch)
        with pytest.raises(delivery.module.RunUnavailable, match="is gone .bootstrap.py prepared it"):
            delivery.evolver.setup(delivery.ctx)
        git(clone, "init", "-q")


def test_a_step_resumed_without_the_demand_models_key_starts_no_process(delivery, monkeypatch):
    """A run resumed in a shell without the filled experiment.env skips setup: the worker started, then the controller
    failed without its key, a round each step."""
    ready(delivery, monkeypatch)
    delivery.evolver.setup(delivery.ctx)
    monkeypatch.delenv("DEMAND_LLM_API_KEY")
    with pytest.raises(delivery.module.RunUnavailable, match="team cannot run.*DEMAND_LLM_API_KEY"):
        delivery.evolver.step(delivery.ctx)
    assert not [a for a in delivery.started if "vigil" in a or "ahe_beta.integrations.hermit.feedback" in a]


def test_setup_refuses_a_demand_model_the_controller_is_not_handed(delivery, monkeypatch):
    """Set on this machine but not named in controller.env: the controller ran without its key, after a paid round."""
    ready(delivery, monkeypatch, controller={"env": ["DEMAND_LLM_MODEL", "DEMAND_LLM_BASE_URL"]})
    with pytest.raises(delivery.module.RunUnavailable, match="not handed to the controller.*DEMAND_LLM_API_KEY"):
        delivery.evolver.setup(delivery.ctx)


def test_a_setup_stopped_by_a_full_disk_sets_up_again(delivery, monkeypatch):
    """Its frozen configuration written in place, a full disk left half of it, which every later setup failed to read
    before anything ran."""
    ready(delivery, monkeypatch)
    write = Path.write_text

    def full(self, *args, **kwargs):
        if self.name.endswith(".partial") and "effective-delivery" in self.name:
            write(self, "{\"root\": ")                                         # half, then the disk is full
            raise OSError(28, "No space left on device")
        return write(self, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", full)
    with pytest.raises(OSError, match="No space"):
        delivery.evolver.setup(delivery.ctx)
    assert not (delivery.state / "effective-delivery.json").exists()
    monkeypatch.setattr(Path, "write_text", write)
    delivery.evolver.setup(delivery.ctx)
    assert json.loads((delivery.state / "effective-delivery.json").read_text())["root"] == str(delivery.state)


def test_a_delivery_state_moved_after_setup_is_refused(delivery, monkeypatch, tmp_path):
    """The frozen copy is in the state folder the configuration names: a `root` changed since setup found none to
    compare, and the next round ran on a fresh recovery and usage store with the old binding's history."""
    ready(delivery, monkeypatch)
    delivery.evolver.setup(delivery.ctx)
    elsewhere = tmp_path / "another-state"
    elsewhere.mkdir()
    cfg = json.loads((delivery.cohort / "delivery.json").read_text())
    (delivery.cohort / "delivery.json").write_text(json.dumps({**cfg, "root": str(elsewhere)}))
    with pytest.raises(delivery.module.RunUnavailable, match="configuration changed"):
        delivery.evolver.step(delivery.ctx)


def test_a_cache_file_a_delivery_committed_is_not_in_the_commit_bound(delivery, tmp_path):
    """Hermit's tree never keeps __pycache__: left out of the files compared, it stayed in the commit bound, which
    AHE-Beta's roles read as what the sessions ran."""
    cache = ".agents/skills/a/scripts/__pycache__/helper.cpython-312.pyc"
    url, commit = gitea(tmp_path, {".agents/skills/a/SKILL.md": "---\ndescription: a\n---\n",
                                   ".agents/skills/a/scripts/helper.py": "x = 1\n", cache: "compiled"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    result = delivery.evolver.step(delivery.ctx)
    assert result.status == "done" and cache in result.extra["changed_by_tree"], result.summary
    bound = git(tmp_path / "gitea/o/r.git", "ls-tree", "-r", "--name-only", result.extra["bound_adapter"])
    assert cache not in bound and ".agents/skills/a/scripts/helper.py" in bound


def test_what_hermits_tree_would_not_keep_of_a_delivery_is_left_out_and_said(delivery, tmp_path, monkeypatch):
    """Hermit's tree commits with `git add -A` (this machine's excludes, the delivery's own .gitignore files) and
    exports with `git archive` (a .gitattributes `export-ignore`): bound as delivered, the next round's sessions would
    run other files than the binding names, and refuse it. What the tree keeps is what is taken and bound."""
    from binding import bound, fingerprint
    skill = ".agents/skills/a/scripts"
    url, commit = gitea(tmp_path, {"scripts/helper.py": "x = 1\n", f"{skill}/data.txt": "data\n",
                                   f"{skill}/.gitattributes": "data.txt export-ignore\n",
                                   f"{skill}/run.py": "print('kept')\n"})
    (tmp_path / "ignore").write_text("scripts/helper.py\n")
    (tmp_path / "gitconfig").write_text(f"[core]\n\texcludesFile = {tmp_path / 'ignore'}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    result = delivery.evolver.step(delivery.ctx)
    assert result.status == "done" and result.extra["changed_by_tree"] == [f"{skill}/.gitattributes",
                                                                          "scripts/helper.py"]
    assert (delivery.adapter / skill / "run.py").exists() and (delivery.adapter / skill / "data.txt").exists()
    assert not (delivery.adapter / "scripts/helper.py").exists() and "helper.py" in result.summary
    # AHE-Beta's roles read the bound commit back (`git archive`): it is the child holding what the sessions run
    canonical, gitea_ = result.extra["bound_adapter"], tmp_path / "gitea/o/r.git"
    assert canonical != commit and git(gitea_, "rev-parse", "main") == canonical
    assert git(gitea_, "rev-parse", f"{canonical}^") == commit
    listed = git(gitea_, "ls-tree", "-r", "--name-only", canonical).split("\n")
    assert "scripts/helper.py" not in listed and f"{skill}/.gitattributes" not in listed and f"{skill}/run.py" in listed
    assert bound(json.loads(delivery.binding.read_text()), adapter_sha256=fingerprint(delivery.adapter),
                 round_no=2)["harness_sha"] == canonical


def test_a_commit_whose_root_attributes_change_an_archive_is_bound_as_the_commit_without_them(delivery, tmp_path):
    """A root .gitattributes (no adapter file, so never taken) marking an adapter file `export-ignore`: the sessions ran
    the file, AHE-Beta's roles (`git archive` of the bound commit) never saw it. And a replay of the round (a stop after
    the push, before the binding) makes the same commit again, not a sibling main refuses."""
    from binding import fingerprint
    url, commit = gitea(tmp_path, {"scripts/helper.py": "x = 1\n",
                                   ".gitattributes": "scripts/helper.py export-ignore\n"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"round": 1, "tested_adapter": "c0ffee", "delivered_adapter": commit}
    result = delivery.evolver.step(delivery.ctx)
    canonical, gitea_ = result.extra["bound_adapter"], tmp_path / "gitea/o/r.git"
    assert result.status == "done" and ".gitattributes" in result.extra["changed_by_tree"] and canonical != commit
    archived = tmp_path / "archived"
    archived.mkdir()
    subprocess.run(f"git -C {gitea_} archive {canonical} | tar -x -C {archived}", shell=True, check=True)
    assert (archived / "scripts/helper.py").read_text() == "x = 1\n" and not (archived / ".gitattributes").exists()
    assert fingerprint(delivery.adapter) == json.loads(delivery.binding.read_text())["adapter_sha256"]
    shutil.rmtree(delivery.adapter)                                            # the stop: before the binding moved,
    shutil.copytree(EXAMPLE / "template", delivery.adapter)                    # the Trainer kept the seed adapter,
    (delivery.adapter / ".hermit/CARD.md").write_text("the Trainer's\n")      # and the round is sealed
    delivery.rebind()
    (delivery.state / "round-1-adapter.json").write_text(json.dumps(delivery.proc.receipt))
    again = delivery.evolver.step(delivery.ctx)
    assert again.status == "done" and again.extra["bound_adapter"] == canonical   # the same commit: main has it


def test_a_file_the_trainers_tree_tracks_stays_when_a_later_delivery_ignores_it(delivery, tmp_path):
    """A delivered .gitignore covering a helper an earlier round delivered: the Trainer's tree keeps tracking it
    (`git add -A` leaves a tracked file tracked), while a fresh index dropped it, and the commit of that pushed its
    deletion."""
    skill = ".agents/skills/a/scripts"
    (delivery.adapter / skill).mkdir(parents=True)
    (delivery.adapter / skill / "helper.py").write_text("x = 1\n")
    git(delivery.adapter, "init", "-q")                                        # the Trainer's export: its tree's files
    git(delivery.adapter, "add", "-A")
    git(delivery.adapter, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "round 1's")
    delivery.rebind()
    url, _ = gitea(tmp_path, {f"{skill}/helper.py": "x = 2\n"})             # the team's helper, tracked, then a
    team = tmp_path / "developer"                                              # .gitignore naming it (git keeps it
    (team / skill / ".gitignore").write_text("helper.py\n")                   # tracked)
    git(team, "add", f"{skill}/.gitignore")
    git(team, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ignore its outputs")
    git(team, "push", "-q", str(tmp_path / "gitea/o/r.git"), "main")
    commit = git(team, "rev-parse", "HEAD")
    assert f"{skill}/helper.py" in git(team, "ls-tree", "-r", "--name-only", commit)
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    result = delivery.evolver.step(delivery.ctx)
    assert result.status == "done" and "changed_by_tree" not in result.extra   # bound as delivered
    assert (delivery.adapter / skill / "helper.py").read_text() == "x = 2\n"


def test_a_session_whose_records_are_in_the_workers_folder_is_refused(delivery):
    """The sessions' private records (the native sessions and the user model's) under the worker's clone: the
    developers would read the users."""
    git(delivery.private.parent, "init", "-q")                                # a clone there, as bootstrap makes one
    delivery.configure(worker={**json.loads((delivery.cohort / "delivery.json").read_text())["worker"],
                               "clone": str(delivery.private.parent)})
    with pytest.raises(delivery.module.RunUnavailable, match="the worker's clone and the sessions' records"):
        delivery.evolver.step(delivery.ctx)
    assert not [a for a in delivery.started if "vigil" in a]


def test_an_edited_openagents_checkout_is_refused(delivery):
    """Its HEAD at the pin, a module added since: the worker ran other code than the pinned one (whatever Git's
    settings show of untracked files)."""
    git(delivery.openagents, "config", "status.showUntrackedFiles", "no")
    (delivery.openagents / "openagents.py").write_text("print('changed')\n")
    with pytest.raises(delivery.module.RunUnavailable, match="has changes"):
        delivery.evolver.step(delivery.ctx)


def test_an_openagents_environment_linked_out_since_setup_is_refused_before_the_worker_starts(delivery, tmp_path):
    """The checkout's .venv relinked to a shared environment between rounds (Git ignores it: the checkout stays clean):
    the worker was shown what the link leads to."""
    with open(delivery.openagents / ".git/info/exclude", "a") as exclude:
        exclude.write(".venv\n")
    shutil.rmtree(delivery.openagents / ".venv", ignore_errors=True)
    (tmp_path / "shared").mkdir()
    (delivery.openagents / ".venv").symlink_to(tmp_path / "shared")
    with pytest.raises(delivery.module.RunUnavailable, match="leads out of it"):
        delivery.evolver.step(delivery.ctx)
    assert not [a for a in delivery.started if "vigil" in a]


def test_a_moved_openagents_checkout_is_refused_before_the_worker_starts(delivery):
    """Another cohort's bootstrap can move a shared checkout to its own pin, and the worker then ran that version."""
    git(delivery.openagents, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    with pytest.raises(delivery.module.RunUnavailable, match="not the pinned"):
        delivery.evolver.step(delivery.ctx)
    assert not [a for a in delivery.started if "vigil" in a]


def test_a_delivered_file_past_any_limit_is_refused_before_it_is_read(delivery, tmp_path):
    url, commit = gitea(tmp_path, {"scripts/big.py": "x = 1\n" * 200_000})   # 1.2 MB
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    refused(delivery, "past any of the template's")
    assert not (delivery.adapter / "scripts").exists() and json.loads(delivery.binding.read_text())["round"] == 1


def test_a_delivered_file_where_a_folder_was_lands_at_its_path(delivery, tmp_path):
    """A skill's scripts/run folder replaced by a script scripts/run: the emptied folder stayed, the copy put the
    script inside it (scripts/run/run), and the binding bound those bytes to the commit."""
    skill = delivery.adapter / ".agents/skills/a"
    (skill / "scripts/run").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\ndescription: a\n---\n")
    (skill / "scripts/run/x.py").write_text("x = 1\n")
    delivery.rebind()
    url, commit = gitea(tmp_path, {".agents/skills/a/SKILL.md": "---\ndescription: a\n---\n",
                                   ".agents/skills/a/scripts/run": "#!/bin/sh\necho run\n"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    assert delivery.evolver.step(delivery.ctx).status == "done"
    assert (skill / "scripts/run").is_file() and (skill / "scripts/run").read_text() == "#!/bin/sh\necho run\n"


def test_a_delivered_gitea_token_is_refused_under_any_name(delivery, tmp_path, monkeypatch):
    """Hermit's redaction knows ..._KEY, ..._TOKEN, ..._SECRET names: a token under another name (GITEA_PAT) would go
    out with the adapter to every task's environment."""
    monkeypatch.setenv("GITEA_PAT", "gitea-pat-0123456789abcdef")
    url, commit = gitea(tmp_path, {"scripts/push.py": "TOKEN = 'gitea-pat-0123456789abcdef'\n"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_PAT"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    refused(delivery, "holds the Gitea token")
    assert json.loads(delivery.binding.read_text())["round"] == 1


def test_a_rejected_delivery_moves_neither_the_clone_nor_the_binding(delivery, tmp_path):
    url, commit = gitea(tmp_path, {"ADAPTER.md": "delivered\n"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    delivery.ctx.tools["check_spec"] = lambda path: {"passed": False}
    before = delivery.binding.read_text()
    refused(delivery, "fixed template")
    assert delivery.binding.read_text() == before   # no Git moved the clone
    assert not [v for v in delivery.box.views if v.view_args.get("role") == "ahe-git"]
    assert not (delivery.output / "delivery.json").exists()


def test_a_call_the_trainer_stopped_moves_neither_the_clone_nor_the_binding(delivery, tmp_path):
    """The Trainer stops a call past its timeout and keeps the adapter: a binding written after that would bind the
    next round's sessions to an adapter they do not run."""
    url, commit = gitea(tmp_path, {"ADAPTER.md": "delivered\n"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    delivery.ctx.cancel = threading.Event()
    delivery.proc.on_git = delivery.ctx.cancel.set      # stopped while the clone moved
    refused(delivery, "stopped by the Trainer")
    assert json.loads(delivery.binding.read_text())["round"] == 1


def test_a_delivered_key_value_is_refused_before_anything_moves(delivery, tmp_path, monkeypatch):
    """The Trainer scrubs a key's value from the adapter before its commit: the bytes bound would not be the adapter
    the next round runs, and its sessions would all refuse it."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-test-0123456789abcdef")
    url, commit = gitea(tmp_path, {"scripts/call.py": "KEY = 'sk-ant-test-0123456789abcdef'\n"})
    delivery.configure(tracker={"base_url": url, "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"})
    delivery.proc.receipt = {"delivered_adapter": commit}
    refused(delivery, "key's value")
    assert json.loads(delivery.binding.read_text())["round"] == 1
    assert not [v for v in delivery.box.views if v.view_args.get("role") == "ahe-git"]
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
    refused(delivery, match)
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


def delivery_clone(tmp_path, pinned=True) -> Path:
    """The AHE-Beta checkout prepare.py reads (a Git repository, with a venv's interpreter) and a clean delivery clone
    of the template."""
    git(tmp_path, "init", "-q")
    for folder in ("backend/ahe_beta", "backend/agent_defs"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
        (tmp_path / folder / "__init__.py").write_text("")
    if pinned:                                           # the OpenAgents commit its compatibility.json pins
        source = tmp_path / "openagents-source"
        source.mkdir()
        git(source, "init", "-q")
        git(source, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "oa")
        (tmp_path / "backend/compatibility.json").write_text(json.dumps(
            {"openagents": {"repository": str(source), "commit": git(source, "rev-parse", "HEAD")}}))
    git(tmp_path, "add", "backend")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ahe")
    repo = tmp_path / "delivery"
    shutil.copytree(EXAMPLE / "template", repo)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")                # cloned from Gitea's main
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


def test_a_delivery_whose_ahe_beta_pins_no_openagents_commit_is_not_published(tmp_path):
    """Without the pin there is no worker to start: published, run-delivery.yaml marked bootstrap done, and setup
    refused the run."""
    from prepare import prepare
    from test_prepare import cohort
    root = cohort(tmp_path)
    repo = delivery_clone(tmp_path, pinned=False)
    with pytest.raises(ValueError, match="pins no OpenAgents commit"):
        prepare(cohort=root, ahe_root=tmp_path, run_id="unit", delivery_repo=repo,
                tracker={"base_url": "http://10.0.0.5:3000", "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"},
                worker={"clone": str(tmp_path / "worker"), "env": []})
    assert not (root / "run-delivery.yaml").exists()


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
    (tmp_path / "binding.json").write_text(json.dumps({
        "harness_sha": "beef", "adapter_sha256": "d2", "round": 2, "run_id": "unit",
        "history": [{"round": 1, "harness_sha": "c0ffee", "adapter_sha256": "d"},
                    {"round": 2, "harness_sha": "beef", "adapter_sha256": "d2"}]}))
    params = {"artifact_root": str(tmp_path / "s1"), "session_id": "s1", "adapter_sha256": "d", "round": 2,
              "run_id": "unit",
              "binding": str(tmp_path / "binding.json"), "persona": {"identity": "p"},
              "scenario": {"workspace": {"params": {}}}}
    for changed, commit in (({}, "c0ffee"),              # round 2 redone after a crash: the adapter it had
                            ({"round": 3}, "c0ffee"),    # round 3 after a failed one: the last delivered adapter
                            ({"adapter_sha256": "d2"}, "beef")):
        captured.clear()
        ahe_user.simulate({"id": "r02-s00-t", "name": "t"}, None, None, {**params, **changed})
        assert captured[0]["harness_sha"] == commit
    for changed in ({"adapter_sha256": "other"}, {"adapter_sha256": "d2", "round": 1}, {"run_id": "another"},
                    {"binding": str(tmp_path / "missing.json")}):
        with pytest.raises(ahe_user.NotBound) as refused:          # no commit, or a later round's only
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
    from binding import fingerprint
    (tmp_path / "ignore").write_text(".codex/\n")                             # and core.autocrlf: the clone checked
    (tmp_path / "gitconfig").write_text(f"[core]\n\texcludesFile = {tmp_path / 'ignore'}\n\tautocrlf = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    bare = tmp_path / "r.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    calls, run = [], subprocess.run
    monkeypatch.setattr(gitea_runtime.subprocess, "run", lambda argv, **kw: calls.append(argv) or run(argv, **kw))
    clone = gitea_runtime.seed_repository(template=EXAMPLE / "template", repository={"clone_url": str(bare),
                                                                                     "empty": True},
                                          destination=tmp_path / "clone", token="t0k3n", owner="o")
    assert ".codex/hooks.json" in git(clone, "ls-files").split("\n")
    assert fingerprint(clone) == fingerprint(EXAMPLE / "template")             # out as CRLF: the binding refused it
    worker = gitea_runtime.clone_repository(clone_url=str(bare), destination=tmp_path / "worker", token="t0k3n",
                                            owner="o")
    assert fingerprint(worker) == fingerprint(EXAMPLE / "template")
    remote = [argv for argv in calls if "push" in argv or "clone" in argv]
    assert len(remote) == 3 and all(argv[1:3] == ["-c", "credential.helper="] for argv in remote)   # none keeps it


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
                          "Password for 'http://o@gitea.example:3000': ",        # or the clear: nothing
                          "Password for 'https://o@gitea.example:30001/o/r.git': "):
            assert asked(elsewhere) == ""
        assert asked("Password for 'https://o@gitea.example:3000/o/r.git': ") == "t0k3n-value"   # useHttpPath
        for named in ("Password for 'https://alice@gitea.example:3000': ",    # a user Git's own configuration
                      "Password for 'https://alice@gitea.example:3000/research/demo.git': "):   # names (an
            assert asked(named) == "t0k3n-value"                               # organization's repository)
        for elsewhere in ("Password for 'https://alice@evil.example/x@gitea.example:3000': ",
                          "Password for 'https://gitea.example:3000@evil.example': ",
                          "Password for 'https://alice@gitea.example:3000.evil.example': ", "Password: "):
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
    (tmp_path / "cohort.json").write_text(json.dumps({"roles": {"developer": 1}}))   # the stock u4-e2b roster
    bootstrap.bootstrap(cohort=tmp_path, ahe_root=tmp_path, ahe_python=None, run_id="unit",
                        gitea_url="https://10.0.0.5:3000", gitea_owner="o", gitea_repo="r", review_seconds=None,
                        delivery_hosts=["proxy.example"])
    assert seen["worker"]["config"] == str(tmp_path / "openagents-worker.yml")   # this machine's, shown read-only
    assert seen["worker"]["parallel"] == 1                                      # the exported roster's developers,
    assert bootstrap.roster(tmp_path, 3) == 3                                   # unless given (--developers)
    (tmp_path / "cohort.json").write_text("{}")
    with pytest.raises(ValueError, match="--developers"):
        bootstrap.roster(tmp_path)
    monkeypatch.setattr(bootstrap, "ensure_repository", lambda **kw: pytest.fail("provisioned"))
    (tmp_path / "run-delivery.yaml").unlink(missing_ok=True)
    with pytest.raises(ValueError, match="--developers"):                     # before anything is made for it
        bootstrap.bootstrap(cohort=tmp_path / "another", ahe_root=tmp_path, ahe_python=None, run_id="unit",
                            gitea_url="https://10.0.0.5:3000", gitea_owner="o", gitea_repo="r2")
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


def test_a_delivery_added_to_a_prepared_cohort_keeps_its_review_time(tmp_path, monkeypatch):
    """bootstrap.py has prepare.py add the delivery files with the default review time: the cohort's own (240 s, which
    its 600 s budgets allow) is kept, not refused by the default's 600."""
    from prepare import prepare
    from test_prepare import cohort
    root = cohort(tmp_path)
    (tmp_path / ".gitignore").write_text(".env\n")                           # AHE-Beta's checkout ignores its own
    (tmp_path / "backend/ahe_beta").mkdir(parents=True, exist_ok=True)        # simulator's key file
    (tmp_path / "backend/ahe_beta/.env").write_text("USER_LLM_API_KEY=not-for-the-snapshot\n")
    rows = [json.loads(line) for line in (root / "tasks.jsonl").read_text().splitlines()]
    for row in rows:                                     # as the exporter writes `--agent-seconds 600`
        row["ahe_session"]["scenario"]["workspace"] = {"params": {"execution": {"task_budget_seconds": 600}}}
    (root / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    repo = delivery_clone(tmp_path)
    monkeypatch.setenv("DEMAND_LLM_BASE_URL", "https://demand.example/v1")
    monkeypatch.setenv("HARNESS_LLM_BASE_URL", "https://harness.example/v1")
    prepare(cohort=root, ahe_root=tmp_path, run_id="unit", review_seconds=240)
    run = yaml.safe_load((root / "run.yaml").read_text())
    run["agent"]["params"]["user"]["pythonpath"] = ["/a/checkout/moved/since/backend"]   # another AHE-Beta checkout
    (root / "run.yaml").write_text(yaml.safe_dump(run))
    prepare(cohort=root, ahe_root=tmp_path, run_id="unit", delivery_repo=repo,
            tracker={"base_url": "http://10.0.0.5:3000", "owner": "o", "repo": "r", "token_env": "GITEA_TOKEN"},
            worker={"clone": str(tmp_path / "worker"), "env": []})
    run = yaml.safe_load((root / "run-delivery.yaml").read_text())
    user = run["agent"]["params"]["user"]
    hosts = run["sandbox"]["evolver"]["allowed_hosts"]                         # upstream's harness model is no
    assert "demand.example" in hosts and "harness.example" not in hosts        # process's here: neither its host nor
    assert not [name for name in json.loads((root / "delivery.json").read_text())["controller"]["env"]   # its key
                if name.startswith("HARNESS_LLM_")]
    snapshot = root / "ahe-beta" / git(tmp_path, "rev-parse", "HEAD")           # one AHE-Beta: the run's commit
    assert user["review_s"] == 240 and user["pythonpath"] == [str(snapshot / "backend")]
    assert (snapshot / "backend/ahe_beta/__init__.py").exists() and not (snapshot / "backend/ahe_beta/.env").exists()


def test_bootstrap_refuses_a_run_that_starts_from_another_adapter(tmp_path, monkeypatch):
    """The delivery repository and the binding round 1's sessions carry hold the bundled template: a prepared run.yaml
    that starts elsewhere ran a round whose every session found no commit of its own (NotBound)."""
    import bootstrap
    monkeypatch.setenv("GITEA_TOKEN", "t0k3n")
    monkeypatch.setattr(bootstrap, "ensure_repository", lambda **kw: pytest.fail("provisioned"))
    (tmp_path / "run.yaml").write_text(yaml.safe_dump({"start_from": "runs/earlier/final",
                                                       "template": str(EXAMPLE / "template")}))
    with pytest.raises(ValueError, match="starts from 'runs/earlier/final'"):
        bootstrap.bootstrap(cohort=tmp_path, ahe_root=tmp_path, ahe_python=None, run_id="unit",
                            gitea_url="https://gitea.example", gitea_owner="o", gitea_repo="r")


def test_a_run_yamls_relative_template_is_the_cohorts_whatever_folder_bootstrap_runs_in(tmp_path, monkeypatch):
    """Hermit resolves a relative template from the configuration's folder: from the caller's, `template: template`
    run in this example's folder passed for the bundled one while Hermit loaded the cohort's own."""
    from prepare import start_problem
    (tmp_path / "template").mkdir()
    (tmp_path / "run.yaml").write_text(yaml.safe_dump({"template": "template"}))
    for folder in (EXAMPLE, tmp_path):
        monkeypatch.chdir(folder)
        assert "seeds the bundled one" in start_problem(tmp_path)
    (tmp_path / "run.yaml").write_text(yaml.safe_dump({"template": os.path.relpath(EXAMPLE / "template", tmp_path)}))
    for folder in (EXAMPLE, tmp_path):
        monkeypatch.chdir(folder)
        assert start_problem(tmp_path) is None


def test_the_snapshot_holds_the_commits_pins_not_the_checkouts(tmp_path):
    """An uncommitted change to the checkout's backend/compatibility.json chose the worker's OpenAgents pin, while the
    controller ran the commit's code."""
    from openagents_runtime import from_compatibility
    from prepare import _snapshot
    ahe = tmp_path / "ahe"
    for folder in ("backend/ahe_beta", "backend/agent_defs"):
        (ahe / folder).mkdir(parents=True)
        (ahe / folder / "__init__.py").write_text("")
    pin = {"openagents": {"repository": "https://example.invalid/oa", "commit": "a" * 40}}
    (ahe / "backend/compatibility.json").write_text(json.dumps(pin))
    git(ahe, "init", "-q")
    git(ahe, "add", "-A")
    git(ahe, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ahe")
    pin["openagents"]["commit"] = "b" * 40                                    # edited since, not committed
    (ahe / "backend/compatibility.json").write_text(json.dumps(pin))
    snapshot = _snapshot(ahe, git(ahe, "rev-parse", "HEAD"), tmp_path / "cohort/ahe-beta")
    assert from_compatibility(snapshot).commit == "a" * 40
    (snapshot / "backend/compatibility.json").unlink()                         # an earlier one, without its pins
    assert from_compatibility(_snapshot(ahe, git(ahe, "rev-parse", "HEAD"), tmp_path / "cohort/ahe-beta")).commit \
        == "a" * 40


def test_bootstrap_refuses_folders_that_overlap(tmp_path, monkeypatch):
    """The OpenAgents checkout inside the worker's clone: its developers would write what this machine then runs."""
    import bootstrap
    monkeypatch.setenv("GITEA_TOKEN", "t0k3n")
    monkeypatch.setattr(bootstrap, "ensure_repository", lambda **kw: pytest.fail("provisioned"))
    for folders, said in (({"worker_clone": tmp_path / "w", "openagents_checkout": tmp_path / "w/.openagents/oa"},
                           "overlap"),
                          ({"worker_clone": tmp_path / "dev", "openagents_checkout": tmp_path / "dev-home"},
                           "the worker's home"),                 # the worker's home is beside its clone
                          ({"delivery_repo": tmp_path.parent}, "personas"),    # a clone holding the cohort
                          ({"worker_clone": tmp_path / "ahe-beta"}, "AHE-Beta's snapshots")):   # prepare's, later
        with pytest.raises(ValueError, match=said):
            bootstrap.bootstrap(cohort=tmp_path, ahe_root=tmp_path, ahe_python=None, run_id="unit",
                                gitea_url="https://gitea.example", gitea_owner="o", gitea_repo="r", **folders)


def test_a_repository_whose_creation_lost_its_answer_is_this_cohorts_on_a_retry(tmp_path, monkeypatch):
    """Gitea made the repository and the connection closed before its answer: a retry knows it by the marker this
    cohort's bootstrap noted and gave it. A cohort exported again at the same path (its record gone) does not: the
    repository holds the earlier cohort's open issues, which AHE-Beta's triage takes."""
    import bootstrap
    import gitea_runtime
    made = {}

    def gitea(base, token, method, path, body=None):
        if method == "GET":
            return {"user": (200, {"login": "o"})}.get(path) or (
                (200, {"private": True, "empty": True, **made}) if made and path == "repos/o/r" else (404, {}))
        made["description"] = body["description"]                             # made, then the answer lost
        raise ConnectionResetError("closed before the answer")

    monkeypatch.setattr(gitea_runtime, "_request", gitea)
    monkeypatch.setattr(bootstrap, "seed_repository", lambda **kw: (_ for _ in ()).throw(RuntimeError("seeding")))
    monkeypatch.setenv("GITEA_TOKEN", "t")
    cohort = tmp_path / "cohort"
    cohort.mkdir()
    (cohort / "cohort.json").write_text(json.dumps({"roles": {"developer": 1}}))
    run = dict(cohort=cohort, ahe_root=tmp_path, ahe_python=None, run_id="unit", gitea_url="https://gitea.example",
               gitea_owner="o", gitea_repo="r")
    with pytest.raises(ConnectionResetError):
        bootstrap.bootstrap(**run)
    with pytest.raises(RuntimeError, match="seeding"):                         # past the repository: its own
        bootstrap.bootstrap(**run)
    (cohort / "bootstrap.json").unlink()                                       # exported again at the same path:
    with pytest.raises(ValueError, match="not this cohort's"):                 # the home the earlier one made,
        bootstrap.bootstrap(**run)                                             # then (that removed) its repository
    (cohort / "openagents-worker-home").rmdir()
    with pytest.raises(gitea_runtime.GiteaError, match="exists"):
        bootstrap.bootstrap(**run)


def test_bootstrap_takes_again_only_what_this_cohorts_bootstrap_made(tmp_path, monkeypatch):
    """An existing repository kept an earlier cohort's open issues, which AHE-Beta's triage takes, and could be public;
    an existing clone could be one a team wrote (Git on this machine runs its configuration), push elsewhere, or sit on
    a commit Gitea never had. Only what this cohort's bootstrap made, noted before it made it, is taken again."""
    import bootstrap
    import gitea_runtime
    answers = {"repos/o/r": (200, {"private": True, "empty": False})}
    monkeypatch.setattr(gitea_runtime, "_request", lambda base, token, method, path, body=None: answers.get(
        path, (201, {"private": True, "empty": True})) if method == "GET" or path != "user/repos" else
        (201, {"private": True, "empty": True}))
    with pytest.raises(gitea_runtime.GiteaError, match="exists"):              # not this cohort's
        gitea_runtime.ensure_repository(base_url="https://gitea.example", owner="o", name="r", token="t")
    answers["repos/o/r"] = (200, {"private": False})
    with pytest.raises(gitea_runtime.GiteaError, match="public"):
        gitea_runtime.ensure_repository(base_url="https://gitea.example", owner="o", name="r", token="t",
                                        existing_ok=True)
    answers["repos/o/made"] = (404, {})                                       # made public whatever was asked
    with monkeypatch.context() as patched:
        patched.setattr(gitea_runtime, "_request", lambda base, token, method, path, body=None: answers.get(
            path, (200, {"login": "o"})) if method == "GET" else (201, {"private": False}))
        with pytest.raises(gitea_runtime.GiteaError, match="created public"):
            gitea_runtime.ensure_repository(base_url="https://gitea.example", owner="o", name="made", token="t")
    clone = tmp_path / "clone"
    clone.mkdir()
    git(clone, "init", "-q")
    git(clone, "remote", "add", "origin", "https://gitea.example/o/another.git")
    for ours in (False, True):
        with pytest.raises(gitea_runtime.GiteaError, match="exists" if not ours else "not a clone of"):
            gitea_runtime.clone_repository(clone_url="https://gitea.example/o/r.git", destination=clone, token="t",
                                           owner="o", existing_ok=ours)
    git(clone, "remote", "set-url", "origin", "https://o@gitea.example:443/o/r/")   # this one, written otherwise
    assert gitea_runtime.clone_repository(clone_url="https://gitea.example/o/r.git", destination=clone, token="t",
                                          owner="o", existing_ok=True) == clone.resolve()
    answers["repos/o/r"] = (200, {"private": True, "description": "AHE-Beta delivery for the cohort mine"})
    assert gitea_runtime.ensure_repository(base_url="https://gitea.example", owner="o", name="r", token="t",
                                           marker="AHE-Beta delivery for the cohort mine")   # made, its answer lost
    bare = tmp_path / "bare.git"                                               # a clone stopped halfway: never taken
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)     # for a whole one, nor in the way
    (tmp_path / ".fresh.cloning").mkdir()
    (tmp_path / ".fresh.cloning/half").write_text("x")
    fresh = gitea_runtime.clone_repository(clone_url=f"file://{bare}", destination=tmp_path / "fresh", token="t",
                                           owner="o")
    assert (fresh / ".git").is_dir() and not (fresh / "half").exists()
    assert (tmp_path / ".fresh.cloning/half").exists()                         # someone else's: left alone
    assert not [p for p in tmp_path.glob(".fresh.*.cloning")]                  # this call's own: removed
    noted = []                                                                 # bootstrap notes before it makes
    answers.pop("repos/o/r")
    answers["repos/o/new"] = (404, {})
    monkeypatch.setattr(bootstrap, "seed_repository", lambda **kw: pytest.fail("seeded before the record") if not
                        json.loads((tmp_path / "cohort/bootstrap.json").read_text()).get("marker") else
                        noted.append(kw) or (_ for _ in ()).throw(RuntimeError("stop here")))
    monkeypatch.setenv("GITEA_TOKEN", "t")
    (tmp_path / "cohort").mkdir()
    (tmp_path / "cohort/cohort.json").write_text(json.dumps({"roles": {"developer": 1}}))
    answers[("user")] = (200, {"login": "o"})
    monkeypatch.setattr(gitea_runtime, "_request", lambda base, token, method, path, body=None:
                        answers.get(path, (201, {"private": True, "empty": True})) if method == "GET" else
                        (201, {"private": True, "empty": True}))
    with pytest.raises(RuntimeError, match="stop here"):
        bootstrap.bootstrap(cohort=tmp_path / "cohort", ahe_root=tmp_path, ahe_python=None, run_id="unit",
                            gitea_url="https://gitea.example", gitea_owner="o", gitea_repo="new")
    made = json.loads((tmp_path / "cohort/bootstrap.json").read_text())
    assert made["marker"].startswith("AHE-Beta delivery ") and len(made["clones"]) == 2
    answers["repos/o/taken"] = (404, {})                                      # another cohort makes it meanwhile:
    monkeypatch.setattr(gitea_runtime, "_request", lambda base, token, method, path, body=None:   # its creation
                        answers.get(path, (409, {})) if method == "GET" or path != "user/repos" else (409, {}))
    with pytest.raises(gitea_runtime.GiteaError):                              # refused, and on a retry too: its
        bootstrap.bootstrap(cohort=tmp_path / "cohort", ahe_root=tmp_path, ahe_python=None, run_id="unit",
                            gitea_url="https://gitea.example", gitea_owner="o", gitea_repo="taken")
    answers["repos/o/taken"] = (200, {"private": True, "description": "AHE-Beta delivery of another cohort"})
    with pytest.raises(gitea_runtime.GiteaError, match="exists"):              # description is not this cohort's
        bootstrap.bootstrap(cohort=tmp_path / "cohort", ahe_root=tmp_path, ahe_python=None, run_id="unit",
                            gitea_url="https://gitea.example", gitea_owner="o", gitea_repo="taken")
    monkeypatch.setattr(gitea_runtime, "_request", lambda base, token, method, path, body=None:
                        answers.get(path, (201, {"private": True, "empty": True})) if method == "GET" else
                        (201, {"private": True, "empty": True}))
    assert noted and noted[0]["existing_ok"] is False                          # a first attempt: nothing of its own
    foreign = tmp_path / "foreign"                                             # a clone it did not make, refused:
    foreign.mkdir()                                                            # never noted, so a retry refuses it too
    with pytest.raises(RuntimeError, match="stop here"):
        bootstrap.bootstrap(cohort=tmp_path / "cohort", ahe_root=tmp_path, ahe_python=None, run_id="unit",
                            gitea_url="https://gitea.example", gitea_owner="o", gitea_repo="new",
                            delivery_repo=foreign)
    assert str(foreign.resolve()) not in json.loads((tmp_path / "cohort/bootstrap.json").read_text())["clones"]
