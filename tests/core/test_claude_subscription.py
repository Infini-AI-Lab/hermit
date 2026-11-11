"""Claude Code on the user's subscription (`login: claude`, `llm: {cli: claude, ...}`), through the shared entry points:
the run config, the `claude` Base Agent, the chat endpoint with the real client, the acting agent in the task sandbox
and in a container of its own, the Trainer and its sandboxes, and the roles that used to be Codex's. The CLI is a
stand-in that replays streams the real CLI printed (tests/fixtures/claude_streams); the last tests run the real CLI on
a scripted local model when it is installed. No test uses a real login, and none reaches a real host."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

import pytest
import yaml

from hermit.agent.acting import workspace as acting
from hermit.agent.base import cli as base_cli, login as subscription
from hermit.agent.base.endpoint import cli_endpoint, cli_model, cli_problems, endpoint_spec
from hermit.agent.base.interface import Limits, LoginFailed, Result, Unsupported, make
from hermit.agent.base.records import parse_log
from hermit.agent.model.backends import normalize_config
from hermit.agent.model.llm import ChatAborted, ChatClient, ChatHTTPError
from hermit.config import Config, layered
from hermit.sandbox import Sandbox

from claude_stand import INDEX, ROOT, STREAMS, TOKEN, bwrap_works, stand  # noqa: F401 (stand: the fixture)

EXAMPLE = ROOT / "examples" / "claude_subscription"


def session_agent(**settings):
    return make("claude", {"web_search": False, "login": "claude", "model": "claude-sonnet-5-5", **settings})


def sandbox_off():
    """The sandbox the Trainer makes for the sessions when the run's sandbox is off: it holds nothing back (the
    sandbox itself has its tests below)."""
    return Sandbox(enabled=False, role="llm")


def text_endpoint(**spec):
    """The endpoint the Trainer builds (`isolation: sandbox`, the default), in a sandbox that confines nothing."""
    return cli_endpoint({"cli": "claude", "model": "claude-sonnet-5-5", "login": "claude", **spec},
                        sandbox=sandbox_off())


def client(endpoint, **kwargs):
    kwargs.setdefault("max_retries", 0)
    return ChatClient(endpoint.url, endpoint.agent.settings.get("model") or "claude", **kwargs)


# ---- the run config -------------------------------------------------------------------------------------------------

def layers(**data) -> dict:
    full = {"agent": {}, "evolver": {}, **data}
    layered(full)
    normalize_config(full)
    return full


def test_the_login_reaches_the_base_agents_from_the_config():
    """`model: {name, login, effort}` and the base's section are read into the flat settings, as for Codex; the Base Agent
    gets the login and what steers its plan."""
    data = layers(agent={"base": "claude", "model": {"name": "claude-haiku-5-5", "login": "claude", "effort": "low"},
                         "claude": {"web_search": False, "timeout_s": 60, "usage_credits": True, "plan_max_wait_s": 5,
                                    "plan_dir": "/plans/mine"}})
    params = data["agent"]["params"]
    assert data["agent"]["name"] == "claude-code" and params["login"] == "claude" and params["model"] == "claude-haiku-5-5"
    backend = acting._check("claude", params)
    agent = acting.base_agent(backend, params)
    assert agent.name == "claude" and agent.settings["login"] == "claude" and agent.settings["usage_credits"] is True
    assert agent.settings["plan_max_wait_s"] == 5 and agent.settings["plan_dir"] == "/plans/mine"
    assert "binding" not in params                                   # a Claude model name, not a backend reference


def test_the_evolver_gets_the_login_and_the_plan_settings(tmp_path):
    from hermit.agent.evolver.agent import AgentEvolver
    from types import SimpleNamespace as NS
    params = {"backend": "claude", "login": "claude", "model": "m", "web_search": False, "plan_dir": "/p",
              "plan_max_wait_s": 9, "plan_rerun": False, "usage_credits": True}
    ctx = NS(params=params, sandbox=None, cancel=None,
             workspace=NS(state=tmp_path / "state", output=tmp_path / "out"))
    settings = AgentEvolver().settings(ctx, "claude")
    assert {k: settings[k] for k in ("login", "model", "plan_dir", "plan_max_wait_s", "plan_rerun", "usage_credits")} \
        == {"login": "claude", "model": "m", "plan_dir": "/p", "plan_max_wait_s": 9, "plan_rerun": False,
            "usage_credits": True}
    ctx.params = {**params, "plan_max_wait_s": None}                    # said: no limit, not the default
    assert AgentEvolver().settings(ctx, "claude")["plan_max_wait_s"] is None
    del ctx.params["plan_max_wait_s"]
    assert "plan_max_wait_s" not in AgentEvolver().settings(ctx, "claude")


def test_a_model_through_the_cli_stays_a_cli_spec_for_learning_and_acting():
    """`llm.cli` and `agent.model.cli` keep their form through the config, a reference among them is bound (its key pays,
    not the login), and another CLI is refused. It never collapses into a chat endpoint's spec, which would send the
    requests elsewhere."""
    spec = {"cli": "claude", "model": "claude-haiku-5-5", "login": "claude", "isolation": "sandbox"}
    data = layers(llm=dict(spec), agent={"base": "react", "model": dict(spec)})
    assert data["llm"] == spec and data["agent"]["params"]["model"] == spec
    data = layers(llm={"cli": "claude-code", "model": "fireworks/accounts/fireworks/models/glm-5p3-flash"})
    assert data["llm"]["cli"] == "claude" and data["llm"]["binding"]["backend"] == "fireworks"
    assert data["llm"]["env"]["ANTHROPIC_BASE_URL"].startswith("https://")
    data = layers(evolver={"base": "direct", "model": dict(spec)})   # the per-layer spelling of the same `llm`
    assert data["llm"] == spec
    with pytest.raises(ValueError, match="llm.cli must be codex, claude or pi"):
        normalize_config({"llm": {"cli": "aider"}, "agent": {}})
    with pytest.raises(ValueError, match="is that CLI itself"):
        normalize_config({"agent": {"name": "claude-code", "params": {"model": dict(spec)}}})


@pytest.mark.parametrize("spec,match", [
    ({"model": "m", "isolation": "container"}, "needs login: claude"),
    ({"login": "claude", "isolation": "container"}, "model is required"),
    ({"login": "claude", "model": "m"}, "which the Trainer makes"),         # isolation: sandbox, the default
    ({"login": "claude", "model": "m", "isolation": "sandbox!"}, "isolation: sandbox"),
    ({"login": "claude", "model": "m", "isolation": "none"}, "isolation: sandbox"),     # sandbox.enabled says that
    ({"login": "claude", "model": "m", "isolation": "container", "container": {"img": "x"}}, "unknown keys"),
    ({"login": "claude", "model": "m", "isolation": "container", "web_search": True}, "web_search cannot be on"),
    ({"login": "claude", "model": "m", "isolation": "container", "binding": {"ref": "x/m"}}, "not both")])
def test_a_text_session_that_does_not_combine_is_refused_before_any_session(spec, match):
    with pytest.raises(ValueError, match=match):
        cli_model({"cli": "claude", **spec})


def test_a_text_session_on_either_cli_runs_in_a_sandbox_or_a_container_never_unconfined():
    """Codex's and Claude Code's text sessions run in a sandbox of their own (the default) or a container, never
    unconfined by a setting of their own (only the run's sandbox.enabled: false, warned): Codex's model can run
    commands, and the CLI may do what the sandbox holds besides."""
    assert cli_problems({"cli": "codex"}) == [] == cli_problems({"cli": "codex", "isolation": "container"})
    assert any("isolation: sandbox" in p for p in cli_problems({"cli": "codex", "isolation": "none"}))
    assert cli_problems({"cli": "claude", "login": "claude", "model": "m"}) == []
    assert cli_problems({"cli": "claude", "login": "claude", "model": "m", "isolation": "sandbox"}) == []
    assert cli_problems({"cli": "claude", "login": "claude", "model": "m", "isolation": "container",      # as Codex's
                         "container": {"image": "img:1", "network": "net", "proxy": "http://px:3128"}}) == []
    assert any("isolation: sandbox" in p for p in cli_problems({"cli": "claude", "login": "claude", "model": "m",
                                                                "isolation": "none"}))


@pytest.mark.parametrize("params,match", [
    ({"backend": "claude", "login": "chatgpt", "model": "m"}, "Codex's sign-in"),
    ({"backend": "codex", "login": "claude", "model": "m"}, "Claude Code's sign-in"),
    ({"backend": "claude", "login": "claude"}, "Claude Code model name"),
    ({"backend": "claude", "login": "claude", "model": "m", "binding": {"ref": "x/m"}}, "Claude Code model name"),
    ({"backend": "claude", "login": "api", "model": "m"}, "chatgpt or claude or absent|claude or chatgpt or absent")])
def test_what_does_not_combine_on_an_acting_agent_is_refused(params, match):
    with pytest.raises(ValueError, match=match):
        acting._check(params["backend"], {"web_search": False, **params})


def test_the_run_config_takes_each_clis_own_login_and_refuses_another_when_it_loads(stand, tmp_path):
    """The task sandbox and the sandbox of a model only a CLI reaches sign in through a model reference or the CLI's
    own login (Claude Code's: login: claude): another CLI's login, or none, is refused when the config loads, not at
    the first session. environment.image takes Claude Code as it takes Codex."""
    path = _project(tmp_path, stand)
    Config.load(path)                                                 # the acting agent and the llm on login: claude
    Config.load(path, ["environment.image=officeqa-claude:2.1.318"])  # Claude Code in a container of its own
    for override, match in (("agent.model.login=chatgpt", "Codex's sign-in"),
                            ("agent.model.login=null", "model reference"),    # the user's own sign-in is not copied in
                            ("llm.login=chatgpt", "llm.cli runs in a sandbox of its own"),
                            ("llm.login=null", "llm.cli runs in a sandbox of its own")):
        with pytest.raises(ValueError, match=match):
            Config.load(path, [override])


def test_what_every_session_would_refuse_is_refused_when_the_config_loads_wherever_the_agent_runs(stand, tmp_path):
    """A login that is not the CLI's own, an effort Claude Code does not take and a plan setting beside the model's
    name are refused at load, in a container and on the host too (not by each task's session, one error record at a
    time), as is an environment pattern that would take the token into the whole sandbox."""
    path = _project(tmp_path, stand)
    for where in (["environment.image=officeqa-claude:2.1.318"], ["sandbox.enabled=false"], []):
        for override, match in (("agent.model.login=chatgpt", "Codex's sign-in"),
                                ("agent.model.effort=ultra", "agent.model.effort is one of"),
                                ("agent.model.plan_max_wait_s=60", "belong to how the CLI runs")):
            with pytest.raises(ValueError, match=match):
                Config.load(path, [*where, override])
    for name in ("CLAUDE_CODE_*", "*_TOKEN", "*"):
        for key in ("environment.env", "sandbox.evolver.env"):
            with pytest.raises(ValueError, match="matches CLAUDE_CODE_OAUTH_TOKEN"):
                Config.load(path, [f"{key}=[{json.dumps(name)}]"])
    Config.load(path, ["environment.env=[CLAUDE_CODE_OAUTH_TOKEN]", "environment.network=no-network"])  # on purpose
    Config.load(path, ["agent.claude.plan_max_wait_s=60"])                                              # its place


def test_an_evolvers_own_login_is_checked_against_its_cli_and_its_model_before_the_first_step():
    assert subscription.evolver_problems({"backend": "claude", "login": "claude", "model": "m"}) == []
    assert subscription.evolver_problems({"backend": "codex", "login": "chatgpt"}) == []      # Codex has a default
    assert subscription.evolver_problems({"model": "m"}) == []                                # no login: nothing
    assert "Codex's sign-in" in " ".join(subscription.evolver_problems({"backend": "claude", "login": "chatgpt",
                                                                       "model": "m"}))
    assert "Claude Code's sign-in" in " ".join(subscription.evolver_problems({"backend": "codex", "login": "claude",
                                                                             "model": "m"}))
    assert "give evolver.model.name" in " ".join(subscription.evolver_problems({"backend": "claude-code",
                                                                               "login": "claude"}))


def test_a_plan_directory_given_by_the_user_is_the_same_wherever_a_worker_runs(tmp_path, monkeypatch):
    """`~` is expanded and a relative path made absolute in the process that resolves it (the parent, which hands it
    to its workers), so every session on it shares one plan."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert subscription.plan_dir("~/second-plan", "claude") == tmp_path / "second-plan"
    assert subscription.plan_dir("rel/plan") == tmp_path / "rel" / "plan"
    assert subscription.plan_dir(None, "claude") == subscription.CLAUDE_PLAN


def test_the_base_agent_refuses_another_clis_login_and_what_would_pay_instead(stand, tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    with pytest.raises(Unsupported, match="does not sign in with login: chatgpt"):
        make("claude", {"web_search": False, "login": "chatgpt", "model": "m"}).run("go", workspace=work)
    with pytest.raises(Unsupported, match="does not sign in with login: claude"):
        make("codex", {"web_search": False, "login": "claude", "model": "m"}).run("go", workspace=work)
    with pytest.raises(Unsupported, match="would pay instead"):
        session_agent(env={"ANTHROPIC_API_KEY": "sk-x"}).run("go", workspace=work)
    with pytest.raises(Unsupported, match="keep their own --settings last"):
        session_agent(args=["--settings", "{}"]).run("go", workspace=work)
    with pytest.raises(Unsupported, match="no tools or roles"):
        session_agent(text_only=True).run("go", workspace=work, roles=[{"name": "a", "description": "b", "instructions": "c"}])
    with pytest.raises(Unsupported, match="takes effort low, medium, high, xhigh, max, not 'minimal'"):
        session_agent(effort="minimal").run("go", workspace=work)       # a Codex effort: the CLI would ignore it
    for settings in ({"output_format": "json"}, {"args": ["--output-format", "json"]}):
        with pytest.raises(Unsupported, match="stream-json alone"):     # the payer and the plan read from the stream
            session_agent(**settings).run("go", workspace=work)
    with pytest.raises(Unsupported, match="stream-json alone"):
        make("claude", {"web_search": False, "text_only": True, "output_format": "json"}).run("go", workspace=work)
    assert stand.seen() == []


def test_check_and_train_refuse_a_missing_token_and_name_the_command(stand, tmp_path, monkeypatch):
    from hermit.settings_check import run_check
    from hermit.trainer.trainer import Trainer
    path = _project(tmp_path, stand)
    best_effort = ["sandbox.enforce=best_effort"]           # what is checked here, on a machine with no bubblewrap too
    lines = []
    assert run_check(Config.load(path, best_effort), probe=False, log=lines.append) is True
    assert any(line.startswith("[ok] login: agent") for line in lines) and any("[ok] login: llm" in l for l in lines)
    assert any("[ok] model through a CLI: llm" in line for line in lines)
    monkeypatch.delenv(subscription.TOKEN_ENV)
    lines.clear()
    assert run_check(Config.load(path, best_effort), probe=False, log=lines.append) is False
    assert any("claude setup-token" in line for line in lines)
    with pytest.raises(SystemExit, match="claude setup-token"):
        Trainer(Config.load(path, best_effort), log=lambda *_: None).train()


def test_check_says_what_is_wrong_with_the_cli_and_its_version(stand):
    assert subscription.problems_of("claude") == []
    (stand.dir / "version").write_text("2.1.100 (Claude Code)")
    assert "needs Claude Code 2.1.248 or later" in subscription.problems_of("claude")[0]
    (stand.dir / "version").unlink()
    assert "not on PATH" in subscription.problems_of("claude", "no-such-claude")[0]
    assert "must be one of" in subscription.problems_of("kerberos")[0]


def test_each_login_names_the_hosts_its_sessions_reach_and_no_key():
    """One table for every sandbox's allowlist (the task sandbox's, the evolver sandbox's, a CLI model's): a login's
    hosts, in order and once each; its token is no key a sandbox passes, it goes by name with each session."""
    assert subscription.hosts("claude") == list(subscription.CLAUDE_HOSTS) == ["api.anthropic.com"]
    assert subscription.hosts("chatgpt", None, "claude", "chatgpt") == ["chatgpt.com", "auth.openai.com",
                                                                        "api.anthropic.com"]
    assert subscription.hosts() == [] == subscription.hosts(None, "api")


def test_a_session_in_a_container_needs_the_token_and_not_the_machines_cli(stand, monkeypatch):
    """In a container the image's Claude Code runs (the agent checks the image before its first session): the machine's
    CLI may be missing or old, the token is still needed. The acting agent is in one with environment.image while the
    run's sandbox is on (with it off its pipeline runs on the host, on the machine's CLI). RRSI's roles sign in too."""
    data = {"agent": {"params": {"login": "claude"}}, "environment": {"image": "officeqa-claude:2.1.318"},
            "sandbox": {"enabled": True}, "llm": {"cli": "claude", "login": "claude", "isolation": "sandbox"}}
    assert [(who, contained) for who, _, _, contained, _ in subscription.logins_of(data)] == [("agent", True),
                                                                                         ("llm", False)]
    off = {**data, "sandbox": {"enabled": False}}
    assert [(who, contained) for who, _, _, contained, _ in subscription.logins_of(off)] == [("agent", False),
                                                                                        ("llm", False)]
    roles = subscription.logins_of({"agent": {"params": {}}}, {"roles": {"login": "claude"}})     # RRSI's sessions
    assert [(login, contained) for _, login, _, contained, _ in roles] == [("claude", False)]
    assert subscription.problems_of("claude", "no-such-claude", contained=True) == []
    (stand.dir / "version").write_text("2.1.100 (Claude Code)")
    assert subscription.problems_of("claude", contained=True) == []
    monkeypatch.delenv(subscription.TOKEN_ENV)
    assert subscription.problems_of("claude", "no-such-claude", contained=True) == [subscription.NO_TOKEN]


def test_the_check_reads_the_login_and_the_cli_the_sessions_use(stand, tmp_path, monkeypatch):
    """The check before the first session reads what the sessions will: a Codex login kept only in the shared copy
    under the session's `login_root` is a login (the session's home is given that copy), and Harness²'s `claude_bin` is
    the CLI it checks, not a `claude` on PATH (before, both were refused at the start, though the sessions ran)."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex-home"))         # no user file
    monkeypatch.setattr(subscription, "ROOT", tmp_path / "default-root")      # no copy where the default is
    root = tmp_path / "own-root"
    [(who, login, program, contained, named)] = subscription.logins_of(
        {"agent": {"params": {"login": "chatgpt", "login_root": str(root)}}})
    assert (who, named) == ("agent", str(root)) and subscription.problems_of(login, program, contained, named)
    (root / "login").mkdir(parents=True)
    (root / "login" / subscription.FILE).write_text("{}")
    assert subscription.problems_of(login, program, contained, named) == []
    assert subscription.problems_of(login, program, contained)                # the default's copy is still none
    harness2 = {"login": "claude", "model": "claude-haiku-5-5", "claude_bin": str(stand.dir / "claude")}
    [(who, login, program, contained, _)] = subscription.logins_of({"agent": {"params": {}}}, harness2)
    assert (who, program) == ("evolver", str(stand.dir / "claude"))
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))                      # no `claude` on PATH
    assert subscription.problems_of(login, program, contained) == []
    (stand.dir / "version").write_text("2.1.100 (Claude Code)")               # the version is claude_bin's
    assert "needs Claude Code 2.1.248 or later" in subscription.problems_of(login, program, contained)[0]


# ---- a session -----------------------------------------------------------------------------------------------------

def test_a_session_pays_with_the_token_and_nothing_else(stand, tmp_path):
    """The token is the session's one credential: what a shell left that the CLI would take over it (an API key pays
    without asking in print mode) is dropped, a settings file cannot bring it back (the lock is the last argument), the
    user's own sign-in is not copied, and no parent session's variables leak."""
    work = tmp_path / "w"
    work.mkdir()
    result = session_agent().run("do the task", workspace=work)
    assert result.extra["served"] and not result.extra["failed"] and result.reply == "mock-reply"
    saw = stand.seen()[0]
    assert saw["env"][subscription.TOKEN_ENV] == TOKEN and saw["prompt"] == "do the task"
    for name in base_cli.CLAUDE_BILLING:
        assert name not in saw["env"]
    assert "CLAUDECODE" not in saw["env"] and "CLAUDE_CODE_SESSION_ID" not in saw["env"]
    assert saw["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert saw["env"]["CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"] == "1"     # no silent move to another model
    assert saw["args"][-2:] == ["--settings", base_cli.CLAUDE_LOCK]
    lock = json.loads(base_cli.CLAUDE_LOCK)
    assert lock["apiKeyHelper"] == "" and subscription.TOKEN_ENV not in lock["env"]
    assert lock["env"]["ANTHROPIC_API_KEY"] == "" and lock["env"]["ANTHROPIC_BASE_URL"] == ""
    assert ".credentials.json" not in saw["home"] and saw["args"][saw["args"].index("--model") + 1] == "claude-sonnet-5-5"


def test_in_a_container_claude_codes_own_sandbox_holds_the_commands(tmp_path):
    """A working session in a container: the last --settings carries the lock and Claude Code's own sandbox (the
    commands in bubblewrap with a process namespace of their own, no network, writing only the workspace and /tmp,
    nothing run outside it), which no project settings file can widen, and keeps the session's own file tools off its
    Claude Code settings (a hook written there would run outside the sandbox). A session that only replies has no
    commands to hold; outside a container neither is asked for. The token goes by a descriptor (container.run)."""
    work = tmp_path / "w"
    work.mkdir()

    def last(agent):
        command = agent.command("go", work, tmp_path / "last")
        assert command[-2] == "--settings"
        return json.loads(command[-1])
    boxed = last(session_agent(isolation="container"))
    assert boxed == {**json.loads(base_cli.CLAUDE_LOCK), "sandbox": base_cli.CLAUDE_SANDBOX,
                     "permissions": base_cli.CLAUDE_OWN_SETTINGS}
    assert boxed["permissions"] == {"deny": ["Edit(.claude/**)"]}      # no hook the session writes itself
    assert boxed["sandbox"]["enabled"] is True and boxed["sandbox"]["allowUnsandboxedCommands"] is False
    assert boxed["sandbox"]["failIfUnavailable"] is True
    assert boxed["sandbox"]["network"] == {"allowedDomains": [], "strictAllowlist": True}
    assert "sandbox" not in last(session_agent(isolation="container", text_only=True))
    assert "sandbox" not in last(session_agent(isolation="none"))
    assert last(session_agent(isolation="sandbox")) == json.loads(base_cli.CLAUDE_LOCK)     # the run's: the lock alone
    assert last(make("claude", {"web_search": False, "isolation": "container"})) == {
        "sandbox": base_cli.CLAUDE_SANDBOX, "permissions": base_cli.CLAUDE_OWN_SETTINGS}
    assert "--settings" not in make("claude", {"web_search": False}).command("go", work, tmp_path / "last")
    with pytest.raises(Unsupported, match="keep their own --settings last"):
        make("claude", {"web_search": False, "isolation": "container", "args": ["--settings={}"]}).command(
            "go", work, tmp_path / "last")
    assert session_agent().by_fd() == {subscription.TOKEN_ENV: "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"}
    assert make("claude", {"web_search": False}).by_fd() == {} == make("codex", {"web_search": False}).by_fd()


FAKE_DOCKER = f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] in ("image", "network"):
    sys.exit(0)
if any(a.startswith("hermit-check-") for a in args):          # the agent's check of the image, before a session
    print(os.environ.get("FAKE_DOCKER_CHECK", "2.1.318 (Claude Code)\\nhermit: sandbox ok"))
    sys.exit(0)
Path(os.environ["FAKE_DOCKER_ARGS"]).write_text(json.dumps({{"args": args, "stdin": sys.stdin.read(),
                                                             "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")}}))
for event in [{{"type": "system", "subtype": "init", "apiKeySource": "none", "model": "claude-haiku-5-5", "tools": ["Bash"]}},
              {{"type": "assistant", "message": {{"model": "claude-haiku-5-5", "content": [{{"type": "text", "text": "<answer>7</answer>"}}],
                                                "usage": {{"input_tokens": 3, "output_tokens": 2}}}}}},
              {{"type": "result", "subtype": "success", "is_error": False, "result": "<answer>7</answer>", "num_turns": 1,
                "usage": {{"input_tokens": 3, "output_tokens": 2}}, "modelUsage": {{"claude-haiku-5-5": {{}}}}}}]:
    print(json.dumps(event))
'''


def test_an_acting_session_in_a_container_has_the_token_by_descriptor_and_no_plan(tmp_path, monkeypatch):
    """Claude Code in a container of its own as Codex is in its: the config's image, network and proxy, the token by
    name to docker and by a descriptor to the CLI (the container's shell moves it into a pipe and out of the
    environment), Claude Code's sandbox in the last --settings with what it needs of the container, and no plan
    directory: the plan is waited on and kept by this process, outside the container."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_ARGS", str(tmp_path / "docker.json"))
    monkeypatch.setenv(subscription.TOKEN_ENV, TOKEN)
    plan = tmp_path / "plan"
    params = {"backend": "claude", "web_search": False, "login": "claude", "model": "claude-haiku-5-5",
              "plan_dir": str(plan), "prompt": "go", "workspaces": "ws", "answer": "<answer>(.*?)</answer>",
              "isolation": "container", "container": {"image": "img:1", "network": "net", "proxy": "http://px:3128"}}
    base_cli._ready.clear()
    out = acting.run({"id": "q1"}, None, params=params, base=tmp_path)
    seen = json.loads((tmp_path / "docker.json").read_text())
    args = seen["args"]
    assert out.output["answer"] == "7" and out.extra["isolation"] == "container" and "go" in seen["stdin"]
    assert seen["token"] == TOKEN and TOKEN not in json.dumps(args)              # to docker by name only
    assert args[args.index(subscription.TOKEN_ENV) - 1] == "-e" and "CLAUDE_CONFIG_DIR" in args
    assert args[args.index("--network") + 1] == "net" and "img:1" in args and "HTTPS_PROXY=http://px:3128" in args
    assert args[args.index("systempaths=unconfined") - 1] == "--security-opt"   # the commands' own /proc
    script = next(a for a in args if a.endswith('exec "$@"'))
    assert ("exec 3<<HERMIT_FD\n${CLAUDE_CODE_OAUTH_TOKEN}\nHERMIT_FD\nunset CLAUDE_CODE_OAUTH_TOKEN; "
            "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=3; export CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR\n") in script
    command = args[args.index(script) + 2:]
    assert command[0] == "claude" and json.loads(command[-1])["sandbox"] == base_cli.CLAUDE_SANDBOX
    assert "bwrap" not in json.dumps(command)                                  # no nested bubblewrap: nothing to hide
    assert not any(str(plan) in a or str(plan.resolve()) in a for a in args)    # the plan is not in the container


def test_the_run_configs_image_runs_the_acting_agent_in_a_container_on_this_machines_plan(stand, tmp_path, monkeypatch):
    """`environment.image` as a run config gives it: each call a worker on the host (ContainerAgent) whose session runs
    in the image, on the config's network and proxy, the token by name to docker and by a descriptor to the CLI. The
    plan is the one this process names (its SLOTS hold the session), and no container is given it."""
    from hermit.isolate import agent_pipeline
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_ARGS", str(tmp_path / "docker.json"))
    plan = subscription.plan_dir(None, "claude")                    # the stand-in's: tmp_path/plan, this process's
    plan.mkdir(parents=True)
    (plan / "SLOTS").write_text("1")
    path = _project(tmp_path, stand, environment={"image": "img:1",
                                                  "container": {"network": "net", "proxy": "http://px:3128"}})
    pipeline, close = agent_pipeline(Config.load(path), "rollout", records=tmp_path / "records",
                                     work_root=tmp_path / "calls")
    try:
        out = pipeline({"id": "train-001", "prompt": "Classify it."}, None)
    finally:
        close()
    seen = json.loads((tmp_path / "docker.json").read_text())
    args = seen["args"]
    assert out.output == "<answer>7</answer>" and out.extra["isolation"] == "container"
    assert seen["token"] == TOKEN and TOKEN not in json.dumps(args)              # to docker by name only
    assert "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=3" in next(a for a in args if a.endswith('exec "$@"'))
    assert args[args.index("--network") + 1] == "net" and "img:1" in args and "HTTPS_PROXY=http://px:3128" in args
    assert (plan / subscription.LOCKS).is_dir()                     # the worker took its slot in this machine's plan
    assert not any(str(plan) in a or str(plan.resolve()) in a for a in args)
    kept = [p for d in (tmp_path / "calls", path.parent / ".logs") for p in d.rglob("*") if p.is_file()]
    assert kept and not any(TOKEN in p.read_text(errors="replace") for p in kept)   # the call's and its session's


def test_a_container_session_reaches_its_tool_server_directly(tmp_path, monkeypatch):
    """The call's tools in a container: one MCP server, the tool server on the network's gateway, which the session
    reaches directly (NO_PROXY): Claude Code would send even its plain HTTP through HTTPS_PROXY, the egress proxy."""
    from hermit.agent.base.tool import Tool
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER.replace(
        'if args[0] in ("image", "network"):',
        'if args[0] == "network" and "-f" in args:\n    print("127.0.0.1")\n    sys.exit(0)\n'
        'if args[0] in ("image", "network"):').replace(
        '"token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")', '"no_proxy": os.environ.get("NO_PROXY")'))
    (bin_dir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_ARGS", str(tmp_path / "docker.json"))
    monkeypatch.delenv("NO_PROXY", raising=False)
    work = tmp_path / "w"
    work.mkdir()
    base_cli._ready.clear()
    tool = Tool("count", "Count.", lambda: {"n": 1})
    make("claude", {"web_search": False, "isolation": "container",
                    "container": {"image": "img:1", "network": "net", "proxy": "http://px:3128"}}).run(
        "go", workspace=work, tools=[tool])
    seen = json.loads((tmp_path / "docker.json").read_text())
    args = seen["args"]
    assert seen["no_proxy"] == "127.0.0.1" and args[args.index("NO_PROXY") - 1] == "-e"
    configs = [json.loads(args[i + 1]) for i, a in enumerate(args) if a == "--mcp-config"]
    assert len(configs) == 1 and configs[0]["mcpServers"]["hermit_tools"]["url"].startswith("http://127.0.0.1:")


def test_the_agent_checks_the_images_claude_code_and_its_sandbox_before_a_session(tmp_path, monkeypatch):
    """Before the first session: Claude Code in the image, recent enough for the settings checked here, and its sandbox
    able to start there (else every command would fail and the session still count as served)."""
    from hermit.agent.base import container
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    conf = {"image": "img:1", "network": "net", "proxy": "http://px:3128"}
    assert container.check(conf, "claude") == [] and container.check(conf, "codex") == []
    for said, problem in [("sh: 1: claude: not found", "no Claude Code in the image"),
                          ("2.1.200 (Claude Code)\nhermit: sandbox ok", "needs 2.1.318 or later"),
                          ("2.1.318 (Claude Code)\nbwrap: Can't mount proc", "sandbox cannot start")]:
        monkeypatch.setenv("FAKE_DOCKER_CHECK", said)
        assert problem in container.check(conf, "claude")[0]
        assert container.check(conf, "codex") == []                     # Codex's image is not asked
    base_cli._ready.clear()
    work = tmp_path / "w"
    work.mkdir()
    with pytest.raises(RuntimeError, match="container is not ready: .*sandbox cannot start"):
        make("claude", {"web_search": False, "isolation": "container", "container": conf}).run("go", workspace=work)


def test_a_secret_by_descriptor_is_one_env_passes_and_named_plainly(tmp_path):
    from hermit.agent.base import container
    kw = dict(conf={"image": "i", "network": "n", "proxy": "p"}, work=tmp_path, home=tmp_path, mounts=[],
              timeout=1, stdout=tmp_path / "out")
    with pytest.raises(ValueError, match="does not pass"):
        container.run(["true"], env={}, by_fd={"SECRET": "SECRET_FD"}, **kw)
    with pytest.raises(ValueError, match="not variable names"):
        container.run(["true"], env={"A; rm -rf ~": "x"}, by_fd={"A; rm -rf ~": "B"}, **kw)


def test_no_token_is_no_session_and_says_how(stand, tmp_path, monkeypatch):
    monkeypatch.delenv(subscription.TOKEN_ENV)
    work = tmp_path / "w"
    work.mkdir()
    result = session_agent().run("go", workspace=work)
    assert not result.extra["served"] and result.extra["login_failed"] and "claude setup-token" in result.extra["failure"]
    assert stand.seen() == []


def test_a_session_on_the_login_can_be_held_to_its_model_where_the_sandbox_allows_only_its_host(tmp_path):
    """The login's host (api.anthropic.com) is the session's own, as a provider's is: a network limited to the model
    holds where the run's sandbox allows just that host; another allowed host still refuses it."""
    for allow, refused in ((["api.anthropic.com"], False), (["api.anthropic.com", "other.example.com"], True)):
        agent = session_agent(isolation="sandbox", sandbox=Sandbox(network="allowlist", allow=allow))
        if refused:
            with pytest.raises(Unsupported, match="more than the model"):
                agent._isolation(tmp_path, Limits(network=[]))
        else:
            assert agent._isolation(tmp_path, Limits(network=[]))[0] == "sandbox"


def test_a_session_that_paid_with_a_key_is_no_answer(stand, tmp_path):
    """The dropped variables and the lock should make this impossible; if a session reports it paid otherwise (a managed
    setting, a later CLI), its reply is not taken, and the sign-in is what is wrong."""
    stand.play("api_key_won")
    work = tmp_path / "w"
    work.mkdir()
    result = session_agent().run("go", workspace=work)
    assert not result.extra["served"] and result.extra["login_failed"] and result.reply == ""
    assert "ANTHROPIC_API_KEY" in result.extra["failure"]


def test_without_a_login_a_session_is_left_as_it_was(stand, tmp_path):
    """No `login`: the provider settings or the CLI's own sign-in, as before: nothing dropped, no lock, its cost kept."""
    work = tmp_path / "w"
    work.mkdir()
    result = make("claude", {"web_search": False, "model": "m", "home_files": []}).run("go", workspace=work)
    saw = stand.seen()[0]
    assert saw["env"]["ANTHROPIC_API_KEY"] == "sk-would-bill-the-key" and "--settings" not in saw["args"]
    assert result.usage[0].cost_usd is not None and result.extra["served"]


def test_usage_counts_everything_sent_and_a_subscription_has_no_cost(stand, tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    result = session_agent().run("go", workspace=work)
    u = result.usage[0]
    assert (u.input_tokens, u.cached_input_tokens, u.output_tokens) == (123 + 89 + 67, 89, 45)
    assert u.cost_usd is None and result.extra["served_model"] == "claude-sonnet-5-5"


def test_a_stream_with_several_results_is_summed_and_a_resume_is_not_counted_twice(tmp_path):
    """Sub-agents in the background end with a second result, and a resumed session's `modelUsage` carries its earlier
    invocations: `model_usage_since_last_result` is what each result adds."""
    part = lambda n: {"m": {"inputTokens": n, "outputTokens": n, "cacheReadInputTokens": 2 * n,
                            "cacheCreationInputTokens": 3 * n, "costUSD": 0.5}}
    log = tmp_path / "s.jsonl"
    log.write_text("\n".join(json.dumps({"type": "result", "session_id": "s", "num_turns": 1, "total_cost_usd": 9.0,
                                         "modelUsage": part(100), "usage": {"input_tokens": 100},
                                         "model_usage_since_last_result": part(n)}) for n in (10, 20)) + "\n")
    session, [usage] = parse_log(log, "claude", "m")
    assert session == "s" and usage.input_tokens == 6 * 30 and usage.cached_input_tokens == 60 and usage.output_tokens == 30
    assert usage.cost_usd == 1.0 and usage.calls == 2


def test_a_failed_sessions_tokens_are_kept(tmp_path):
    log = tmp_path / "s.jsonl"
    log.write_text(json.dumps({"type": "result", "num_turns": 0, "is_error": True, "subtype": "error_during_execution",
                               "usage": {"input_tokens": 7, "output_tokens": 2}}) + "\n")
    _, [usage] = parse_log(log, "claude", "m")
    assert usage.input_tokens == 7 and usage.output_tokens == 2


def test_a_prompt_is_text_for_the_model_whatever_it_starts_with(stand, tmp_path):
    """On the command line a prompt starting with `-` is an option (`--version` prints the version as the session's whole
    output), and one starting with `/` runs one of the CLI's commands (`/cost` never reaches the model). So: stdin, and a
    line break before a `/`."""
    work = tmp_path / "w"
    work.mkdir()
    agent = session_agent()
    for prompt in ("--version", "- a list item"):
        agent.run(prompt, workspace=work)
    agent.run("/cost is a word", workspace=work)
    seen = stand.seen()
    assert [s["prompt"] for s in seen] == ["--version", "- a list item", "\n/cost is a word"]
    assert not any(p in s["args"] for s in seen for p in ("--version", "- a list item", "/cost is a word"))


def test_a_long_system_text_goes_by_file_and_a_short_one_stays_an_argument(stand, tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    long = "S" * (base_cli.ARG_MAX + 10)
    home = tmp_path / "home"
    session_agent(home=home, append_system_prompt=long).run("go", workspace=work)
    saw = stand.seen()[0]
    assert "--append-system-prompt-file" in saw["args"] and long not in saw["args"]
    assert Path(saw["args"][saw["args"].index("--append-system-prompt-file") + 1]).read_text() == long
    stand.play("reply")
    session_agent(append_system_prompt="short").run("go", workspace=work)
    assert stand.seen()[0]["args"][stand.seen()[0]["args"].index("--append-system-prompt") + 1] == "short"


def test_a_system_file_is_not_written_through_a_link_a_session_left(stand, tmp_path):
    work, home = tmp_path / "w", tmp_path / "home"
    work.mkdir()
    home.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("untouched")
    for name in ("system-prompt.md", "append-system-prompt.md"):       # one file per flag
        (home / name).symlink_to(target)
    session_agent(home=home, append_system_prompt="S" * (base_cli.ARG_MAX + 10)).run("go", workspace=work)
    session_agent(home=home, text_only=True, system_prompt="T" * (base_cli.ARG_MAX + 10)).run("go", workspace=work)
    assert target.read_text() == "untouched"
    assert not any((home / name).is_symlink() for name in ("system-prompt.md", "append-system-prompt.md"))
    assert (home / "system-prompt.md").read_text().startswith("T") and \
        (home / "append-system-prompt.md").read_text().startswith("S")


# What the CLI's own streams say: never a reply, each read from its fields. (kind, exit code from the real CLI)
FAILURES = {"limit_five_hour": "refused", "limit_seven_day": "refused", "limit_opus": "refused",
            "throttled": "unavailable", "overloaded": "unavailable", "server_error": "unavailable",
            "connection_refused": "unavailable", "bad_token": "login_failed", "no_access": "login_failed",
            "not_logged_in": "login_failed", "credit_balance": "login_failed", "too_long": "rejected",
            "no_model": "rejected", "model_refusal": "rejected", "always_cut": "rejected", "terminated": "failed",
            "interrupted": "failed", "unknown_session": "failed"}


@pytest.mark.parametrize("case,kind", FAILURES.items())
def test_a_failed_session_says_so_and_has_no_reply(stand, tmp_path, case, kind):
    stand.play({"case": case, "resets_in": 3600})
    work = tmp_path / "w"
    work.mkdir()
    # plan_rerun off: the refusal comes back instead of waiting for the window, to be read here.
    result = session_agent(plan_rerun=False).run("go", workspace=work)
    extra = result.extra
    assert extra["served"] is False and extra["failed"] is True and result.reply == ""
    assert extra[kind] is True
    assert sum(bool(extra[k]) for k in ("refused", "unavailable", "login_failed", "rejected")) == (kind != "failed")
    if case != "always_cut":                               # (a message cut at the limit is one the model did write)
        assert not any(s.kind == "message" for s in result.trajectory)
    if kind == "failed":
        return
    assert extra["failure"].strip()


def test_a_plain_throttle_is_no_usage_limit():
    """The CLI's text for a plain 429 holds the words `usage limit`; a limit is what its own field says. Holding every
    session for an hour on a throttle would stop a run for nothing."""
    assert base_cli.served("claude", (STREAMS / "throttled.jsonl").read_text()) is False
    assert "not your usage limit" in (STREAMS / "throttled.jsonl").read_text()


def test_the_cli_exits_zero_with_no_result_and_that_is_not_served(stand, tmp_path):
    stand.play("interrupted")
    assert INDEX["interrupted"]["exit"] == 0
    work = tmp_path / "w"
    work.mkdir()
    result = make("claude", {"web_search": False, "home_files": []}).run("go", workspace=work)
    assert result.exit_code == 0 and result.extra["failed"] is True and result.extra["served"] is False


def test_words_a_session_read_are_not_its_failure(stand, tmp_path):
    """A served session whose reply quotes a limit and an outage is served (`notices` reads the CLI's reports only)."""
    stand.play({"case": "reply", "text": "You've hit your 5-hour limit, API Error: 529 Overloaded, Not logged in"})
    work = tmp_path / "w"
    work.mkdir()
    result = session_agent().run("go", workspace=work)
    assert result.extra["served"] and not result.extra["refused"] and not result.extra["unavailable"]
    assert not result.extra["login_failed"]


def test_a_finish_reason_says_how_the_reply_ended(stand, tmp_path):
    stand.play("pause_turn")
    work = tmp_path / "w"
    work.mkdir()
    assert session_agent().run("go", workspace=work).finish_reason == "length"
    stand.play("reply")
    assert session_agent().run("go", workspace=work).finish_reason == "stop"


def test_a_prompt_the_cli_ran_as_a_command_is_not_served(tmp_path):
    raw = json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 0, "result": "Total cost: $0"})
    assert base_cli.served("claude", raw + "\n") is False


# ---- the plan --------------------------------------------------------------------------------------------------------

def test_a_limit_holds_every_session_until_its_reset_and_the_session_runs_again(stand, tmp_path, monkeypatch):
    """The plan's refusal is never an answer: it holds the login's plan until the reset it names, and the session runs
    again after it. The plan's report is kept: each window's use and reset."""
    stand.play({"case": "limit_five_hour", "resets_in": 7200}, "reply")
    waits, kept = [], {}

    def wait(plan=None, cancel=None, log=None, max_s=None, credits=False):   # what it would see, then the reset
        waits.append(subscription.held(plan))
        if (Path(plan) / "plan.json").exists():
            kept.update(json.loads((Path(plan) / "plan.json").read_text()))
            (Path(plan) / "plan.json").unlink()
        return ""
    monkeypatch.setattr(subscription, "wait", wait)
    work = tmp_path / "w"
    work.mkdir()
    result = session_agent().run("go", workspace=work)
    assert result.reply == "mock-reply" and len(stand.seen()) == 2
    assert waits[0] == "" and "refused" in waits[1]
    assert kept["rate_limit_reached_type"] == "session_refused" and kept["windows"]["five_hour"]["used_percent"] == 100
    assert 7100 < kept["held_until"] - time.time() < 7300      # the reset the refusal named, not an hour by default
    assert not (tmp_path / "codex-plan").exists()              # each login has its own plan


def test_the_plans_files_steer_a_claude_session_as_they_do_a_codex_one(stand, tmp_path):
    """PAUSE and a usage threshold hold a session back; past plan_max_wait_s it is `unavailable`, with no reply. A window
    is read from the stream's report."""
    plan = tmp_path / "plan"
    plan.mkdir()
    work = tmp_path / "w"
    work.mkdir()
    (plan / "PAUSE").write_text("")
    result = session_agent(plan_max_wait_s=0).run("go", workspace=work)
    assert result.extra["unavailable"] and not result.extra["started"] and stand.seen() == []
    (plan / "PAUSE").unlink()
    stand.play({"case": "window_warning", "resets_in": 3600})
    assert session_agent().run("go", workspace=work).extra["served"]
    (plan / "MAX_USED").write_text("50")
    assert "plan usage 81%" in subscription.held(plan)
    calls = len(stand.seen())
    assert session_agent(plan_max_wait_s=0).run("go", workspace=work).extra["unavailable"] and len(stand.seen()) == calls


def test_a_session_on_usage_credits_holds_the_next_ones_unless_allowed(stand, tmp_path, monkeypatch):
    """Past the plan's limit an account with usage credits keeps being served, billed apart: the CLI has no switch against
    it. The session that did it is served; the ones after it wait for the plan's window, unless the user allowed credits."""
    stand.play({"case": "usage_credits", "resets_in": 9000})
    work = tmp_path / "w"
    work.mkdir()
    result = session_agent(usage_credits=True).run("go", workspace=work)
    assert result.extra["served"] and subscription.held(tmp_path / "plan") == ""
    result = session_agent().run("go", workspace=work)
    assert result.extra["served"] and "refused" in subscription.held(tmp_path / "plan")
    plan = json.loads((tmp_path / "plan" / "plan.json").read_text())
    assert plan["rate_limit_reached_type"] == "usage_credits" and 8900 < plan["held_until"] - time.time() < 9100
    # a run that allows credits (another run's hold, the same plan) is not held by it; the others still are
    assert subscription.held(tmp_path / "plan", credits=True) == ""
    result = session_agent(usage_credits=True, plan_max_wait_s=0).run("go", workspace=work)
    assert result.extra["served"] and "refused" in subscription.held(tmp_path / "plan")


def test_a_credits_hold_is_written_before_the_session_gives_its_slot_back(stand, tmp_path, monkeypatch):
    """With SLOTS: 1 the session waiting for the slot reads the plan the moment it has it: the hold of a session that
    drew credits is in the plan before that session's slot is given back, so the next one waits instead of drawing them
    too (the hold was written after, and a session that took the slot in between ran on credits as well)."""
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "SLOTS").write_text("1")
    stand.play({"case": "usage_credits", "resets_in": 9000})
    at_release, slot = [], subscription.slot

    @contextmanager
    def watched(*args, **kwargs):
        with slot(*args, **kwargs) as got:
            yield got
            at_release.append(subscription.held(plan))     # what the next session sees when it takes this slot

    monkeypatch.setattr(subscription, "slot", watched)
    work = tmp_path / "w"
    work.mkdir()
    assert session_agent().run("go", workspace=work).extra["served"]
    assert len(at_release) == 1 and "refused" in at_release[0]


def test_a_refusal_after_a_credits_hold_holds_the_sessions_allowed_credits(tmp_path, monkeypatch):
    """A credits hold lets the sessions allowed credits through; a refusal holds every session. A session allowed credits
    that is refused while another run's credits hold stands waits for the plan, never reruns at once (one refused session
    here, then the wait ends at its limit; before, the reason stayed `usage_credits` and the session reran in a loop)."""
    plan = tmp_path / "plan"
    subscription.hold(plan, time.time() + 3600, "usage_credits", max_s=0.0)
    assert subscription.held(plan, credits=True) == "" and "refused" in subscription.held(plan)
    subscription.hold(plan, time.time() + 7200, "session_refused", max_s=0.0)
    assert json.loads((plan / "plan.json").read_text())["rate_limit_reached_type"] == "session_refused"
    assert "refused" in subscription.held(plan, credits=True)
    subscription.hold(plan, time.time() + 600, "usage_credits", max_s=0.0)      # a weaker hold never replaces it
    assert json.loads((plan / "plan.json").read_text())["rate_limit_reached_type"] == "session_refused"

    (plan / "plan.json").unlink()
    subscription.hold(plan, time.time() + 3600, "usage_credits", max_s=0.0)
    calls = []

    def refused(self, prompt, **_):
        calls.append(prompt)
        assert len(calls) < 3, "a refused session ran again without waiting for the plan"
        return Result(reply="", exit_code=1, extra={"served": False, "refused": True, "unavailable": False,
                                                    "failed": True, "held_until": time.time() + 7200})
    monkeypatch.setenv(subscription.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(base_cli.CliAgent, "_session", refused)
    result = session_agent(usage_credits=True, plan_dir=str(plan), plan_max_wait_s=0).run("go", workspace=tmp_path)
    assert len(calls) == 1 and not result.extra.get("served") and "refused" in subscription.held(plan, credits=True)


def test_a_served_session_never_waits_for_a_hold_it_cannot_write(stand, tmp_path, monkeypatch):
    """The hold a session on usage credits leaves is for the sessions after it: when it cannot be written (the plan's
    lock held past its wait, a read-only plan) the served reply still comes back at once, not five minutes later."""
    stand.play({"case": "usage_credits", "resets_in": 9000})
    monkeypatch.setattr(subscription, "_hold", lambda *_, **__: (_ for _ in ()).throw(OSError("read-only")))
    work = tmp_path / "w"
    work.mkdir()
    started = time.time()
    result = session_agent(plan_max_wait_s=None).run("go", workspace=work)      # no deadline of its own either
    assert result.extra["served"] and time.time() - started < 60


def test_a_hold_outlasts_the_reports_of_sessions_that_started_before_it(stand, tmp_path):
    """Sessions run side by side: one drew credits (or was refused) and held the plan, and another that started before
    the hold ends after it with an ordinary report. The hold stands until its own end."""
    stand.play({"case": "usage_credits", "resets_in": 3600})
    work = tmp_path / "w"
    work.mkdir()
    session_agent().run("go", workspace=work)
    assert "refused" in subscription.held(tmp_path / "plan")
    later = subscription.claude_report([json.loads(line) for line in (STREAMS / "window_warning.jsonl").read_text()
                                        .splitlines() if line.startswith("{")])
    subscription.note_usage(tmp_path / "plan", None, later)
    plan = json.loads((tmp_path / "plan" / "plan.json").read_text())
    assert plan["time"] == later["time"] and plan["rate_limit_reached_type"] == "usage_credits"
    assert "refused" in subscription.held(tmp_path / "plan")


def test_a_hold_in_force_is_never_shortened(tmp_path):
    """A session that started before a long hold can end on a short window's refusal: the long hold stands. plan.json is
    replaced whole (a reader without the lock never sees half of it)."""
    subscription.hold(tmp_path / "plan", time.time() + 120 * 3600, "session_refused")
    subscription.hold(tmp_path / "plan", time.time() + 2 * 3600, "usage_credits")
    report = json.loads((tmp_path / "plan" / "plan.json").read_text())
    assert report["held_until"] > time.time() + 119 * 3600 and report["rate_limit_reached_type"] == "session_refused"
    assert not list((tmp_path / "plan").glob("*.tmp"))


def test_the_plans_files_are_never_written_through_what_a_session_left(tmp_path):
    """The plan's directory is shared with the sessions it steers, and written by processes outside any sandbox: a
    link, a fifo or a directory a session left at one of its names is replaced, never followed; the file it points to
    stays as it was."""
    plan, victim, elsewhere = tmp_path / "plan", tmp_path / "victim.txt", tmp_path / "elsewhere"
    plan.mkdir()
    elsewhere.mkdir()
    victim.write_text("keep me\n")
    for name in (".lock", "plan.json", "plan.jsonl", f"plan.{os.getpid()}.{threading.get_ident()}.tmp"):
        (plan / name).symlink_to(victim)
    (plan / subscription.LOCKS).symlink_to(elsewhere)
    (plan / "SLOTS").write_text("2")
    report = {"time": "2026-11-05T00:00:00+00:00", "windows": {"seven_day": {"used_percent": 50, "resets_at": 1}}}
    subscription.note_usage(plan, None, report)
    subscription.hold(plan, time.time() + 3600)
    with subscription.slot(plan) as got:
        assert got is True
    assert victim.read_text() == "keep me\n" and not list(elsewhere.iterdir())
    for name in (".lock", "plan.json", "plan.jsonl"):
        assert (plan / name).is_file() and not (plan / name).is_symlink()
    assert json.loads((plan / "plan.json").read_text())["held_until"] > time.time()
    (plan / "plan.json").unlink()
    os.mkfifo(plan / "plan.json")                                       # read without the lock: it never blocks
    started = time.time()
    assert subscription.held(plan) == "" and time.time() - started < 5


@pytest.mark.parametrize("report", ["[]", '{"held_until": "soon", "rate_limit_reached_type": "x"}',
                                    '{"held_until": 1e300, "rate_limit_reached_type": "x"}',
                                    '{"windows": {"w": {"used_percent": "abc", "resets_at": 1e300}}}',
                                    '{"windows": {"w": {"used_percent": 100, "resets_at": 1e300}}}', "not json"])
def test_a_plan_report_that_is_not_one_stops_nothing(tmp_path, report):
    """What the plan's files say is checked: a report of the wrong shape or with numbers past any reset neither crashes
    the sessions nor holds them forever; SLOTS that is not a small number is no limit."""
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "plan.json").write_text(report)
    (plan / "MAX_USED").write_text("90")
    reason = subscription.held(plan)
    assert isinstance(reason, str)
    if reason:                                                          # held: until a time that can be said
        assert "until" in reason
    subscription.hold(plan, 1e300)                                      # a reset past any window's
    assert json.loads((plan / "plan.json").read_text())["held_until"] < time.time() + 32 * 86400
    for slots in ("nan", "1e400", "-3"):
        (plan / "SLOTS").write_text(slots)
        with subscription.slot(plan) as got:
            assert got is True


def test_the_plans_lock_held_by_another_process_is_given_up(tmp_path, monkeypatch):
    """A command of a session could hold the plan's lock: a writer gives up past its wait instead of blocking forever
    (a report not kept, a hold not written: the session itself then waits)."""
    import fcntl
    plan = tmp_path / "plan"
    plan.mkdir()
    holder = os.open(plan / ".lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(OSError, match="held past"):
            with subscription._plan_lock(plan, wait_s=0.2):
                pass
        monkeypatch.setattr(subscription, "LOCK_WAIT_S", 0.2)
        assert subscription.note_usage(plan, None, {"time": "2026-11-05T00:00:00+00:00"}) is None
    finally:
        os.close(holder)


def test_a_sessions_commands_do_not_see_the_plan(tmp_path, monkeypatch):
    """Without isolation a session on the login runs in a mount namespace of its own where the login's plan is an empty
    directory of its own: its commands cannot pause, lift, lock or forge the holds every other session obeys."""
    if base_cli._bwrap_binds() is None:
        pytest.skip("needs a working bubblewrap")
    plan, work, bin_dir, user = tmp_path / "plan", tmp_path / "work", tmp_path / "bin", tmp_path / "user"
    for d in (work, bin_dir, user):
        d.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(f'#!/bin/sh\ncat > /dev/null\nls -A {plan} > {work}/seen.txt\n'
                    f'touch {plan}/PAUSE; rm -f {plan}/MAX_USED; ln -s /etc/passwd {plan}/.lock\n'
                    'echo \'{"type":"system","subtype":"init","apiKeySource":"none","model":"m","tools":[]}\'\n'
                    'echo \'{"type":"result","subtype":"success","is_error":false,"result":"ok","num_turns":1}\'\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv(subscription.TOKEN_ENV, TOKEN)
    agent = session_agent(plan_dir=str(plan), model="m")
    result = agent.run("go", workspace=work)                        # the first session: the plan's directory is made
    assert result.reply == "ok" and result.extra["served"] and plan.is_dir()
    (plan / "MAX_USED").write_text("90")
    result = agent.run("go", workspace=work)
    assert result.reply == "ok" and result.extra["served"]
    assert (work / "seen.txt").read_text() == ""                    # none of the plan's files in the session's view
    assert not (plan / "PAUSE").exists() and (plan / "MAX_USED").read_text() == "90" and not (plan / ".lock").is_symlink()


def test_a_task_sandbox_that_cannot_cover_the_plan_runs_no_session_when_its_sandbox_is_required(tmp_path, monkeypatch):
    """Where bubblewrap cannot nest, the session's commands would reach the plan every session on the machine obeys. In
    the task sandbox (its worker sets the run's enforce) a required sandbox refuses the session; best_effort, as
    outside a sandbox, warns and runs it as it is."""
    monkeypatch.setattr(base_cli, "_bwrap_binds", lambda: None)
    plan, command = tmp_path / "plan", ["claude", "-p"]
    monkeypatch.setenv("HA_ADAPTER_SANDBOX_ENFORCE", "required")
    with pytest.raises(Unsupported, match="cannot be kept from the session's commands"):
        base_cli._bound(command, [], tmp_path, [plan])
    assert base_cli._bound(command, [], tmp_path, []) == command    # nothing to cover: nothing refused
    monkeypatch.setenv("HA_ADAPTER_SANDBOX_ENFORCE", "best_effort")
    assert base_cli._bound(command, [], tmp_path, [plan]) == command
    monkeypatch.delenv("HA_ADAPTER_SANDBOX_ENFORCE")
    assert base_cli._bound(command, [], tmp_path, [plan]) == command


def test_a_refusal_without_a_usable_reset_holds_for_an_hour(tmp_path):
    for until in (None, "soon", time.time() - 10):
        subscription.hold(tmp_path / "plan", until)
        report = json.loads((tmp_path / "plan" / "plan.json").read_text())
        assert 3500 < report["held_until"] - time.time() < 3700
        (tmp_path / "plan" / "plan.json").unlink()


def test_a_report_without_windows_or_with_junk_is_harmless():
    events = [{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {"a": {"utilization": "x"},
                                                                                                       "b": None}}}]
    report = subscription.claude_report(events)
    assert report["windows"] == {} and report["using_credits"] is False
    assert subscription.claude_report([{"type": "assistant"}]) is None


# ---- a chat request: the model contract --------------------------------------------------------------------------------

def test_a_chat_request_is_one_session_that_can_only_reply(stand):
    """The system messages replace the CLI's instructions, the rest is the prompt on stdin; the session has no tools, no
    MCP servers, no settings, no commands and no memory, works in an empty directory and keeps no transcript; the reply is
    the content, with usage counted in full, no cost and the effort asked for."""
    with text_endpoint() as endpoint:
        message, usage = client(endpoint).chat([{"role": "system", "content": "Be terse."},
                                                {"role": "user", "content": "Sum it."}], reasoning_effort="low")
    saw = stand.seen()[0]
    args = saw["args"]
    assert message["content"] == "mock-reply" and message["finish_reason"] == "stop"
    assert saw["prompt"] == "Sum it." and saw["system"] == "Be terse." and "--append-system-prompt" not in args
    assert args[args.index("--tools") + 1] == "" and args[args.index("--permission-mode") + 1] == "default"
    for flag in ("--restricted", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in args
    assert args[args.index("--effort") + 1] == "low" and saw["work"] == []
    for name in ("CLAUDE_CODE_DISABLE_ATTACHMENTS", "CLAUDE_CODE_DISABLE_CLAUDE_MDS", "CLAUDE_CODE_DISABLE_AUTO_MEMORY"):
        assert saw["env"][name] == "1"
    assert usage.input_tokens == 279 and usage.cached_input_tokens == 89 and usage.output_tokens == 45
    assert usage.cost_usd is None


def test_several_turns_are_written_out_as_one_prompt(stand):
    """A session starts from its one prompt: earlier turns are text in it, not real turns (the CLI has no way in for them
    in print mode). A retry's history reaches the model this way."""
    with text_endpoint() as endpoint:
        client(endpoint).chat([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                               {"role": "user", "content": "c"}])
    saw = stand.seen()[0]
    assert saw["prompt"].endswith("[user]\na\n\n[assistant]\nb\n\n[user]\nc") and saw["system"] == ""


def test_the_reply_is_the_whole_message(stand):
    """`result` holds only the last text block: a message with two, or one the CLI continued past its output limit,
    comes back whole."""
    with text_endpoint() as endpoint:
        stand.play("two_blocks")
        two, _ = client(endpoint).chat([{"role": "user", "content": "go"}])
        stand.play("cut_then_continued")
        cut, usage = client(endpoint).chat([{"role": "user", "content": "go"}])
    stream = [json.loads(l) for l in (STREAMS / "two_blocks.jsonl").read_text().splitlines()]
    blocks = [b["text"] for e in stream if e.get("type") == "assistant" for b in e["message"]["content"] if b["type"] == "text"]
    assert two["content"] == "".join(blocks) and len(blocks) == 2
    stream = [json.loads(l) for l in (STREAMS / "cut_then_continued.jsonl").read_text().splitlines()]
    blocks = [b["text"] for e in stream if e.get("type") == "assistant" for b in e["message"]["content"] if b["type"] == "text"]
    assert cut["content"] == "".join(blocks) and len(blocks) >= 2
    assert next(e for e in reversed(stream) if e.get("type") == "result")["result"] != cut["content"]


def test_a_working_sessions_reply_is_its_last_turns_whole_message():
    """The acting reply: a message the CLI continued past its output limit is one text again, joined where it was cut
    (an answer cut inside its tags still reads); text before a tool call, or before a result that closed an earlier
    turn (a sub-agent working in the background hands its result back in a turn of its own), is not the reply."""
    from hermit.agent.base.cli import _final_turn
    stream = [json.loads(l) for l in (STREAMS / "cut_then_continued.jsonl").read_text().splitlines()]
    blocks = [b["text"] for e in stream if e.get("type") == "assistant" for b in e["message"]["content"] if b["type"] == "text"]
    assert _final_turn(stream) == "".join(blocks) and len(blocks) >= 2

    def say(*texts, **extra):
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": x} for x in texts]}, **extra}
    cut = {"type": "user", "message": {"content": [{"type": "text", "text": "Output token limit hit. Resume directly"}]}}
    call = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t", "name": "Task", "input": {}}]}}
    done = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "launched"}]}}
    assert _final_turn([say("The total is <answer>12"), cut, say("34</answer>")]) == "The total is <answer>1234</answer>"
    assert _final_turn([say("one", "two")]) == "one\n\ntwo"                 # two blocks of one message
    stop = {"type": "user", "message": {"content": [{"type": "text", "text": "Stop hook feedback:\ncheck the units"}]}}
    assert _final_turn([say("FIRST <answer>1</answer>"), stop, say("SECOND <answer>2</answer>")]) == \
        "SECOND <answer>2</answer>"                                     # a Stop hook sent it back: the answer it gave then
    assert _final_turn([say("plan"), call, done, say("waiting for it"), {"type": "result", "result": "waiting for it"},
                        {"type": "system", "subtype": "task_notification", "status": "completed"},
                        say("from the agent", parent_tool_use_id="t"), say("<answer>7</answer>"),
                        {"type": "result", "result": "<answer>7</answer>"}]) == "<answer>7</answer>"


def test_max_tokens_is_accepted_and_said_not_to_apply(stand):
    """Every method sends it as a cap on what it pays for; the CLI's cap would cut the reply and continue it by itself.
    It is not applied, and the reply says so."""
    with text_endpoint() as endpoint:
        request = urllib.request.Request(endpoint.url + "/chat/completions", data=json.dumps(
            {"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "go"}], "max_tokens": 7}).encode())
        body = json.loads(urllib.request.urlopen(request, timeout=30).read())
    assert body["unapplied"] == ["max_tokens"] and body["choices"][0]["message"]["content"] == "mock-reply"
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in stand.seen()[0]["env"]


@pytest.mark.parametrize("fields,messages,match", [
    ({"tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}]}, None, "tool calls"),
    ({"tool_choice": {"type": "function", "function": {"name": "f"}}}, None, "tool_choice"),
    ({"n": 2}, None, "one reply"),
    ({"response_format": {"type": "json_object"}}, None, "plain text"),
    ({"temperature": 0.2}, None, "temperature"),
    ({"top_p": 0.5}, None, "top_p"),
    ({"stop": ["x"]}, None, "stop"),
    ({"seed": 1}, None, "seed"),
    ({"logprobs": True}, None, "logprobs"),
    ({"model": "claude-opus-5-5"}, None, "sessions run"),
    ({"reasoning_effort": "minimal"}, None, "reasoning_effort"),
    ({}, [{"role": "tool", "content": "42", "tool_call_id": "c"}], "tool results"),
    ({}, [{"role": "assistant", "content": "", "tool_calls": [{"id": "c"}]}], "tool calls"),
    ({}, [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}], "not text"),
    ({}, [], "non-empty"),
])
def test_what_a_session_cannot_do_is_refused_and_no_session_starts(stand, fields, messages, match):
    with text_endpoint() as endpoint:
        with pytest.raises(ChatHTTPError, match=match) as raised:
            client(endpoint).chat(messages if messages is not None else [{"role": "user", "content": "go"}], **fields)
    assert raised.value.code == 400 and stand.seen() == []


def test_what_asks_for_nothing_is_no_refusal(stand):
    with text_endpoint() as endpoint:
        message, _ = client(endpoint).chat([{"role": "user", "content": [{"type": "text", "text": "go"}]}], tools=[],
                                           tool_choice="none", n=1, temperature=None, parallel_tool_calls=False,
                                           response_format={"type": "text"}, model="claude-sonnet-5-5")
    assert message["content"] == "mock-reply" and stand.seen()[0]["prompt"] == "go"


def test_the_user_can_accept_a_sampling_field_going_unapplied(stand):
    with text_endpoint(unapplied=["temperature"]) as endpoint:
        request = urllib.request.Request(endpoint.url + "/chat/completions", data=json.dumps(
            {"messages": [{"role": "user", "content": "go"}], "temperature": 0.3}).encode())
        body = json.loads(urllib.request.urlopen(request, timeout=30).read())
        with pytest.raises(ChatHTTPError, match="top_p"):
            client(endpoint).chat([{"role": "user", "content": "go"}], top_p=0.3)
    assert body["unapplied"] == ["temperature"]


STATUSES = {"limit_five_hour": 429, "throttled": 503, "overloaded": 503, "server_error": 503, "connection_refused": 503,
            "bad_token": 401, "no_access": 401, "not_logged_in": 401, "credit_balance": 401, "too_long": 400,
            "no_model": 400, "model_refusal": 400, "always_cut": 400, "terminated": 502, "empty_reply": 502}


@pytest.mark.parametrize("case,status", STATUSES.items())
def test_a_failed_session_is_never_a_successful_reply(stand, case, status):
    """Through the endpoint and the real client: the status tells the caller what to do with it (the client retries 429
    and 5xx), and the CLI's failure text is never the content."""
    stand.play({"case": case, "resets_in": 3600})
    with text_endpoint(plan_rerun=False) as endpoint:
        with pytest.raises(ChatHTTPError) as raised:
            client(endpoint).chat([{"role": "user", "content": "go"}])
    assert raised.value.code == status
    error = json.loads(raised.value.body)["error"]
    assert "gave no reply" in error["message"]
    assert (error.get("code") == "hermit_login_failed") == (status == 401)


def test_the_stream_form_carries_a_reply_and_a_failure(stand):
    def post(endpoint):
        request = urllib.request.Request(endpoint.url + "/chat/completions", data=json.dumps(
            {"messages": [{"role": "user", "content": "go"}], "stream": True}).encode())
        return urllib.request.urlopen(request, timeout=30)
    with text_endpoint() as endpoint:
        lines = post(endpoint).read().decode().splitlines()
        assert any('"mock-reply"' in line for line in lines) and "data: [DONE]" in lines
        stand.play("bad_token")
        with pytest.raises(urllib.error.HTTPError) as raised:
            post(endpoint)
    assert raised.value.code == 401


def test_a_session_past_its_clock_answers_504_and_leaves_no_process(stand):
    stand.play({"case": "reply", "sleep": 30})
    with text_endpoint(timeout_s=1) as endpoint:
        started = time.time()
        with pytest.raises(ChatHTTPError) as raised:
            client(endpoint).chat([{"role": "user", "content": "go"}])
    assert raised.value.code == 504 and time.time() - started < 25
    assert not _stand_alive(stand)
    stand.play({"case": "reply", "sleep": 30})
    with text_endpoint(timeout_s=1) as endpoint:                        # a client that retries: not this one
        with pytest.raises(ChatHTTPError) as raised:
            client(endpoint, max_retries=2, retry_wait=0.1).chat([{"role": "user", "content": "go"}])
    assert raised.value.code == 504 and raised.value.headers.get("x-should-retry") == "false"
    assert len(stand.seen()) == 1                                       # one session, not run again


def _stand_alive(stand) -> bool:
    out = subprocess.run(["pgrep", "-f", str(stand.dir / "claude")], capture_output=True, text=True).stdout.split()
    return bool(out)


def test_a_call_given_up_ends_its_session(stand):
    """The caller's `abort` (a stopped Evolver, a hung-up client) reaches the session: no answer, no process left."""
    stand.play({"case": "reply", "sleep": 30})
    abort = threading.Event()
    threading.Timer(1.0, abort.set).start()
    with text_endpoint() as endpoint:
        started = time.time()
        with pytest.raises(ChatAborted):
            client(endpoint).chat([{"role": "user", "content": "go"}], abort=abort)
        for _ in range(60):
            if not _stand_alive(stand):
                break
            time.sleep(0.5)
    assert time.time() - started < 25 and not _stand_alive(stand)


def test_a_session_waiting_for_the_plan_is_given_up_without_starting(stand, tmp_path):
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "PAUSE").write_text("")
    abort = threading.Event()
    threading.Timer(0.5, abort.set).start()
    with text_endpoint() as endpoint:
        with pytest.raises(ChatAborted):
            client(endpoint).chat([{"role": "user", "content": "go"}], abort=abort)
    assert stand.seen() == []


def test_a_stream_is_kept_with_the_run_when_asked_and_a_home_never(stand, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    logs = tmp_path / "llm_sessions"
    with cli_endpoint({"cli": "claude", "model": "m", "login": "claude"}, logs, sandbox=sandbox_off()) as endpoint:
        client(endpoint).chat([{"role": "user", "content": "go"}])
    assert len(list(logs.glob("*-claude.jsonl"))) == 1
    assert list((tmp_path / "homes").iterdir()) == []
    assert TOKEN not in next(logs.glob("*-claude.jsonl")).read_text()


def test_the_trainers_client_meters_stops_and_keeps_what_a_failed_call_spent(stand):
    """What the Trainer gives an Evolver: `ask(ctx.agent, ...)` through MeteredModel. The tokens of a failed session count;
    the Trainer's stop ends a call in flight; a lost sign-in gets past a method's `except Exception`."""
    from hermit.agent.evolver.base import ask
    from hermit.tools import evolver_agent
    from hermit.trainer.budget import Budget
    model = cli_model({"cli": "claude", "model": "claude-sonnet-5-5", "login": "claude"}, sandbox=sandbox_off())
    model.max_retries = 0
    budget, metered = Budget(), []
    agent = evolver_agent(model, budget, metered)
    assert ask(agent, [{"role": "user", "content": "go"}], max_tokens=10)["content"] == "mock-reply"
    assert budget.snapshot()["tokens"]["evolver"] == 279 + 45 and budget.snapshot()["usd"]["evolver"] == 0
    assert budget.unknown_cost_calls == 1
    stand.play("model_refusal")
    with pytest.raises(ChatHTTPError):
        ask(agent, [{"role": "user", "content": "go"}])
    assert budget.snapshot()["tokens"]["evolver"] > 279 + 45 and len(metered) == 2
    stand.play("not_logged_in")
    with pytest.raises(LoginFailed, match="Not logged in"):
        ask(agent, [{"role": "user", "content": "go"}])
    stand.play({"case": "reply", "sleep": 30})
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.time()
    with pytest.raises(ChatAborted):
        ask(evolver_agent(model, budget, metered, cancel), [{"role": "user", "content": "go"}])
    assert time.time() - started < 25
    model.close()


def test_the_session_sandbox_shows_nothing_of_the_machine_and_passes_nothing_of_the_shell(stand, tmp_path, monkeypatch):
    """`isolation: sandbox` (the default), the bubblewrap sandbox the Trainer builds for the sessions (cli_sandbox):
    the user's home is gone, no variable passes but the session's own (the token by name, with the session alone), the
    workspace is its one place, the network reaches the model through a proxy that lets only it through."""
    from hermit.isolate import cli_sandbox
    if not bwrap_works():
        pytest.skip("needs a working bubblewrap (unprivileged user namespaces)")
    stand.play({"case": "reply", "echo": True})
    monkeypatch.setenv("SOME_SECRET_KEY", "shown-to-no-session")
    records = tmp_path / "sandbox-records"
    spec = {"cli": "claude", "model": "m", "login": "claude"}
    box, proxy = cli_sandbox(spec, records=records, enabled=True, enforce="required", probe_absent=[])
    try:
        assert box.network == "isolated" and box.allow == ["api.anthropic.com"] and proxy is not None
        assert box.env_allow == [] and not box.framework       # none of its own: the token goes with each session
        absent = [Path(p) for p in box.probe_absent]            # proven absent: the user's own sign-in and the plan
        assert {Path.home() / ".claude" / ".credentials.json", subscription.plan_dir(None, "claude")} <= set(absent)
        with cli_endpoint(spec, sandbox=box) as endpoint:
            message, _ = client(endpoint).chat([{"role": "system", "content": "S"}, {"role": "user", "content": "go"}])
    finally:
        for thing in (box, proxy):
            if thing is not None:
                thing.close()
    saw = json.loads(message["content"])
    # the session had the token (the stand-in printed it), and what the session left names it, never shows it
    assert saw["env"][subscription.TOKEN_ENV] == f"<{subscription.TOKEN_ENV}>" and "ANTHROPIC_API_KEY" not in saw["env"]
    assert saw["visible"] == {"~/.claude": False, "~/.ssh": False} and saw["user_login"] is False and saw["work"] == []
    assert saw["system"] == "S"
    record = json.loads(next(records.glob("llm-*.json")).read_text())
    assert "SOME_SECRET_KEY" not in record["environment"] and TOKEN not in json.dumps(record)
    assert record["enforced"] is True and subscription.TOKEN_ENV in record["environment"]


def test_a_session_sandbox_without_bubblewrap_is_refused_at_the_start(stand, tmp_path, monkeypatch):
    """Without bubblewrap the sandbox of a model only a CLI reaches is refused before its proxy starts or any session
    runs, unless the run says otherwise (sandbox.enforce: best_effort, unconfined and recorded; sandbox.enabled:
    false); a container of its own needs none."""
    from hermit.isolate import cli_sandbox
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None if name == "bwrap" else real(name, *a, **k))
    spec, records = {"cli": "claude", "model": "m", "login": "claude"}, tmp_path / "records"
    with pytest.raises((ValueError, RuntimeError), match="bubblewrap"):
        cli_sandbox(spec, records=records, enabled=True, enforce="required", probe_absent=[])
    for enabled, enforce in ((True, "best_effort"), (False, "required")):
        box, proxy = cli_sandbox(spec, records=records, enabled=enabled, enforce=enforce, probe_absent=[])
        assert box.enabled is enabled and box.enforce == enforce
        for thing in (box, proxy):
            if thing is not None:
                thing.close()
    assert cli_sandbox({**spec, "isolation": "container"}, records=records, enabled=True, enforce="required",
                       probe_absent=[]) == (None, None)
    assert stand.seen() == []


# ---- acting ---------------------------------------------------------------------------------------------------------

def acting_params(tmp_path, **more):
    return {"backend": "claude", "login": "claude", "model": "claude-haiku-5-5", "web_search": False,
            "prompt": "{question}", "workspaces": str(tmp_path / "ws"), "retries": 2, "retry_wait_s": 0, "timeout_s": 30, **more}


def scored(tmp_path, stand, **more):
    """One task through the acting agent, rollouts' run_one and a judge that counts its calls."""
    from hermit.task import JudgeResult
    from hermit.trainer.rollout import run_one
    judged = []

    def judge(task, output):
        judged.append(output.output)
        return JudgeResult(score=1.0, feedback="ok")

    def pipeline(task, adapter):
        return acting.run(task, adapter, params=acting_params(tmp_path, **more), base=tmp_path)
    return run_one({"id": "t", "question": "what?"}, None, pipeline, judge), judged


def test_an_acting_session_is_judged_when_served_and_its_answer_is_its_reply(stand, tmp_path):
    record, judged = scored(tmp_path, stand)
    assert record["status"] == "ok" and judged == ["mock-reply"] and record["tokens"] == 279 + 45
    assert record["cost_usd"] is None and record["pipeline_extra"]["attempts"] == 1
    assert record["log"] and Path(record["log"], "agent-claude.jsonl").is_file()


@pytest.mark.parametrize("case", ["model_refusal", "too_long", "always_cut", "interrupted", "terminated", "no_model"])
def test_a_failed_acting_session_is_no_attempt_the_judge_scores(stand, tmp_path, case):
    """The failure text is no answer: the judge is not called, the record is an error and says why, and what the session
    spent and its log are kept."""
    stand.play(case)
    record, judged = scored(tmp_path, stand)
    assert judged == [] and record["status"] == "error" and "session failed" in record["error"]
    assert record["output"] is None and record["log"] and record["pipeline_extra"]["session_failed"]
    if case == "model_refusal":
        assert record["tokens"] > 0


def test_a_provider_that_could_not_serve_is_run_again_then_an_error(stand, tmp_path):
    stand.play("overloaded", "reply")
    record, judged = scored(tmp_path, stand)
    assert record["status"] == "ok" and judged == ["mock-reply"] and record["pipeline_extra"]["attempts"] == 2
    stand.play("server_error")
    record, judged = scored(tmp_path, stand)
    assert judged == [] and record["status"] == "error" and "could not be served" in record["error"]


def test_a_session_past_its_clock_stays_an_attempt_that_found_no_answer(stand, tmp_path):
    """As for Codex: the agent had its time; the task's `answer` says so (the methods read it as the attempt it is)."""
    stand.play({"case": "reply", "sleep": 30})
    record, judged = scored(tmp_path, stand, timeout_s=1, answer="<a>(.*)</a>")
    assert record["status"] == "ok" and judged and judged[0]["fail_reason"].startswith("session timed out")


def test_a_lost_sign_in_ends_the_run_instead_of_failing_every_task(stand, tmp_path):
    from hermit.trainer.rollout import run_tasks
    stand.play("bad_token")
    calls = []

    def pipeline(task, adapter):
        calls.append(task["id"])
        return acting.run(task, adapter, params=acting_params(tmp_path), base=tmp_path)
    with pytest.raises(LoginFailed, match="Failed to authenticate"):
        run_tasks([{"id": f"t{i}", "question": "q"} for i in range(5)], None, pipeline, lambda t, o: 1.0, concurrency=1)
    assert len(calls) == 1


def test_a_lost_sign_in_that_crossed_a_worker_as_text_ends_the_run_too():
    from hermit.trainer.rollout import run_one

    def pipeline(task, adapter):
        raise RuntimeError("LoginFailed: agent claude: Not logged in [hermit_login_failed]\ntraceback")
    with pytest.raises(LoginFailed, match="Not logged in"):
        run_one({"id": "t"}, None, pipeline, lambda t, o: 1.0)


def _project(tmp_path, stand, config_name="run.yaml", **overrides):
    """The example in a directory of the test's own, the stand-in beside it (a sandbox shows a program's own directory)."""
    run = tmp_path / "example"
    shutil.copytree(EXAMPLE, run, dirs_exist_ok=True,           # not the records of a run made in the checkout
                    ignore=shutil.ignore_patterns("runs", ".logs", "__pycache__"))
    path = run / config_name
    data = yaml.safe_load(path.read_text())
    for key, value in overrides.items():
        section, _, sub = key.partition("__")
        if sub:
            data.setdefault(section, {})[sub] = value
        else:
            data[section] = value
    path.write_text(yaml.safe_dump(data))
    return path


NEEDS_BWRAP = pytest.mark.skipif(not bwrap_works(), reason="needs a working bubblewrap (unprivileged user namespaces)")


@NEEDS_BWRAP
def test_a_sandboxed_acting_session_gets_the_token_and_the_plan_not_the_login(stand, tmp_path):
    """The task sandbox: the token passes by name with the agent's call (the worker's, for its session: not the
    sandbox's other commands, nor its judge), its host is allowed by itself, no login file is copied into any home, the
    user's own is out of sight, the plan is the machine's (shared with the worker, written outside the call), and
    nothing of the token is in what the run keeps. The session's own process (and so every command it runs) finds the
    plan an empty directory of its own: what it writes there (a PAUSE, a MAX_USED removed) never reaches the
    machine's."""
    from hermit.agent.acting.agent import load_agent  # noqa: F401 (the worker loads it)
    from hermit.isolate import agent_pipeline
    path = _project(tmp_path, stand)
    config = Config.load(path)
    records = tmp_path / "records"
    plan = subscription.plan_dir(None, "claude")
    plan.mkdir(parents=True, exist_ok=True)
    (plan / "MAX_USED").write_text("99")
    pipeline, close = agent_pipeline(config, "rollout", records=records, work_root=tmp_path / "calls")
    try:
        assert pipeline.allow_proxy is not None and "api.anthropic.com" in pipeline.sandbox.allow
        assert subscription.TOKEN_ENV not in pipeline.sandbox.env_allow
        assert plan in [Path(w) for w in pipeline.sandbox.writable]
        stand.play({"case": "window_allowed", "resets_in": 3600, "echo": True, "probe": str(plan)})
        out = pipeline({"id": "train-001", "prompt": "Classify it."}, None)
    finally:
        close()
    saw = json.loads(out.output)
    assert saw["probe"]["listing"] == []                          # covered: the session sees none of the plan's files
    assert (plan / "MAX_USED").read_text() == "99" and not (plan / "PAUSE").exists()
    # the session had the token (the stand-in printed it), and what it left names it, never shows it
    assert saw["env"][subscription.TOKEN_ENV] == f"<{subscription.TOKEN_ENV}>" and "ANTHROPIC_API_KEY" not in saw["env"]
    assert saw["user_login"] is False and saw["credentials"] == [] and ".credentials.json" not in saw["home"]
    assert (tmp_path / "plan" / "plan.json").is_file()
    left = [p for p in (tmp_path / "calls").rglob(".credentials.json")]
    assert left == []
    record = json.loads(next(records.glob("rollout-*.json")).read_text())       # the worker's: the token by name
    assert subscription.TOKEN_ENV in record["environment"]
    kept = "".join(p.read_text(errors="replace") for p in records.glob("*") if p.is_file())
    assert TOKEN not in kept


def test_a_judge_beside_a_session_on_the_login_gets_neither_its_plan_nor_its_token(stand, tmp_path):
    """A judge in the task sandbox (`shared`) or in a fresh one (`separate`) runs the task's code: the machine's plan,
    which the agent's worker writes, and the token, which passes with the agent's call alone, are not its."""
    from hermit.isolate import agent_pipeline, judge_for
    path = _project(tmp_path, stand)
    plan = subscription.plan_dir(None, "claude")
    for mode in ("shared", "separate"):
        config = Config.load(path, [f"judge.mode={mode}"])
        pipeline, close = agent_pipeline(config, "rollout", records=None, work_root=tmp_path / "calls")
        try:
            judge = judge_for(config, pipeline)
            assert judge.mode == mode and plan in [Path(w) for w in pipeline.sandbox.writable]
            assert plan not in [Path(w) for w in judge.box.writable], mode
            assert subscription.TOKEN_ENV not in judge.box.env_allow, mode
        finally:
            close()


# ---- a run ----------------------------------------------------------------------------------------------------------

ACE_REPLY = json.dumps({"reasoning": "r", "error_identification": "e", "root_cause_analysis": "c", "correct_approach": "a",
                        "key_insight": "k", "bullet_tags": [], "operations": []})


def _no_sandbox(path):
    """The run with its sandbox off (sandbox.enabled: false): what it starts runs unconfined, the sessions of a model
    only a CLI reaches too (their sandbox is a disabled one)."""
    data = yaml.safe_load(path.read_text())
    data["sandbox"] = {"enabled": False}
    path.write_text(yaml.safe_dump(data))
    return path


def test_the_example_acts_and_learns_on_the_subscription(stand, tmp_path):
    """run.yaml end to end with the real Trainer: Claude Code acting in a workspace and ACE's text roles through the chat
    endpoint. Tokens are metered, no dollars are, each learner call's stream is kept with the run, and no credential is
    in anything the run keeps."""
    from hermit.evaluate import evaluate
    from hermit.trainer.trainer import Trainer
    stand.play({"case": "reply", "text": ACE_REPLY})
    path = _no_sandbox(_project(tmp_path, stand))
    config = Config.load(path)
    summary = Trainer(config, log=lambda *_: None).train()
    run = path.parent / "runs" / "claude-subscription"
    steps = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]
    assert summary["stop_reason"] == "completed all epochs" and steps and steps[0]["status"] == "done"
    assert list((run / "llm_sessions").glob("*-claude.jsonl"))
    assert summary["spending"]["tokens"]["rollout"] > 0 and summary["spending"]["tokens"]["evolver"] > 0
    assert summary["spending"]["usd"]["rollout"] == 0 and summary["spending"]["usd"]["evolver"] == 0
    seen = stand.seen()
    learner = [s for s in seen if "--restricted" in s["args"]]
    actor = [s for s in seen if "--restricted" not in s["args"]]
    assert learner and actor and all(s["args"][s["args"].index("--permission-mode") + 1] == "bypassPermissions" for s in actor)
    assert all(s["args"][s["args"].index("--model") + 1] == "claude-haiku-5-5" for s in seen)
    assert (stand.dir / "seen.jsonl").is_file()
    kept = [p for p in run.rglob("*") if p.is_file() and p.suffix in (".json", ".jsonl", ".yaml", ".md", ".txt")]
    assert kept and not any(TOKEN in p.read_text(errors="replace") for p in kept)
    assert evaluate(Config.load(path), "final", log=lambda *_: None)["errors"] == 0


def test_a_workspace_learner_runs_the_base_agents_own_path(stand, tmp_path):
    """run_workspace_learner.yaml: the `claude` Evolver, a session with tools that edits the adapter in its workspace."""
    from hermit.trainer.trainer import Trainer
    stand.play("reply", "reply", "reply",                     # the three rollouts, then the learner's session
               {"case": "reply", "text": "done", "edit": ["adapter/ADAPTER.md", "\nAnswer with JSON.\n"]})
    path = _no_sandbox(_project(tmp_path, stand, "run_workspace_learner.yaml"))
    summary = Trainer(Config.load(path, ["agent.concurrency=1"]), log=lambda *_: None).train()
    run = path.parent / "runs" / "claude-subscription-workspace"
    steps = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]
    rollouts = [json.loads(p.read_text()) for p in (run / "learning_material").rglob("tasks/*.json")]
    assert len(rollouts) == 3 and {r["status"] for r in rollouts} == {"ok"}     # the acting half served too
    assert summary["steps"] == 1 and steps[0]["status"] == "done" and steps[0]["changed"] is True
    assert "Answer with JSON." in (run / "checkout" / "ADAPTER.md").read_text()
    assert steps[0]["evolver"]["usage"][0]["cost_usd"] is None
    learner = stand.seen()[-1]
    assert "--mcp-config" in learner["args"] and "--restricted" not in learner["args"]
    assert learner["env"][subscription.TOKEN_ENV] == TOKEN and "ANTHROPIC_API_KEY" not in learner["env"]
    assert (run / "step_0001" / "output" / "agent-claude.jsonl").is_file()


def test_a_learner_session_that_failed_commits_nothing(stand, tmp_path):
    from hermit.trainer.trainer import Trainer
    path = _no_sandbox(_project(tmp_path, stand, "run_workspace_learner.yaml"))
    stand.play({"case": "reply"}, {"case": "reply"}, {"case": "reply"},
               {"case": "interrupted", "edit": ["adapter/ADAPTER.md", "half an edit"]})
    Trainer(Config.load(path, ["agent.concurrency=1"]), log=lambda *_: None).train()   # in the script's order
    run = path.parent / "runs" / "claude-subscription-workspace"
    step = json.loads((run / "steps.jsonl").read_text().splitlines()[0])
    assert step["status"] == "failed" and "without a result" in step["evolver"]["summary"] and step["changed"] is False
    assert "half an edit" not in (run / "checkout" / "ADAPTER.md").read_text()


@pytest.mark.parametrize("sandboxed", [False, True])
def test_an_acting_pipeline_takes_the_cli_as_its_model_endpoint(stand, tmp_path, sandboxed):
    """run_react.yaml: `react` asks an endpoint on the host, each request one session in a sandbox of its own (role
    agent-model, built as the llm's); it has no login and its URL is in nothing the run keeps."""
    if sandboxed and not bwrap_works():
        pytest.skip("needs a working bubblewrap (unprivileged user namespaces)")
    from hermit.isolate import agent_pipeline
    path = _project(tmp_path, stand, "run_react.yaml")
    if not sandboxed:
        path = _no_sandbox(path)
    config = Config.load(path)
    before = json.dumps(config.data, default=str)
    stand.play({"case": "reply", "echo": True})
    pipeline, close = agent_pipeline(config, "rollout", records=tmp_path / "records", work_root=tmp_path / "calls")
    try:
        out = pipeline({"id": "train-001", "prompt": "Classify it."}, None)
    finally:
        close()
    assert out.tokens == 279 + 45 and out.cost_usd is None and out.extra["turns"] == 1
    assert json.dumps(config.data, default=str) == before     # the endpoint's URL and its token are not in the config
    saw = json.loads(out.output)
    assert "--restricted" in saw["args"] and saw["prompt"] == "Classify it."
    assert saw["system"].startswith("Complete the user's request.")
    assert saw["env"][subscription.TOKEN_ENV] == f"<{subscription.TOKEN_ENV}>"      # had it; the reply never shows it
    if sandboxed:
        assert saw["user_login"] is False
        assert list((tmp_path / "records").glob("agent-model-*.json"))   # its records apart from the learner's llm


def test_a_pipeline_that_offers_tools_is_told_it_cannot(stand, tmp_path):
    """`react` with tools on a text-only model: the request is refused, not answered as if the tools had been offered."""
    from hermit.agent.base.tool import Tool
    with text_endpoint() as endpoint:
        agent = make("react", {"max_turns": 2}, ChatClient.from_spec(endpoint_spec(endpoint, {"cli": "claude"})))
        tool = Tool("f", "does f", lambda: "x", source="task", input_schema={"type": "object", "properties": {}})
        with pytest.raises(ChatHTTPError, match="tool calls"):
            agent.run([{"role": "user", "content": "go"}], tools=[tool])
    assert stand.seen() == []


def test_a_run_whose_sign_in_is_lost_stops_unfinished_and_resumes(stand, tmp_path):
    """Nothing is scored 0 against the adapter and nothing is committed: the run stops at once, its command line says why,
    and the same command resumes after the last finished step."""
    from hermit.trainer.trainer import Trainer
    path = _no_sandbox(_project(tmp_path, stand))
    data = yaml.safe_load(path.read_text())
    data["data"]["step_size"] = 1
    path.write_text(yaml.safe_dump(data))
    stand.play({"case": "bad_token"})
    with pytest.raises(SystemExit, match="Failed to authenticate"):
        Trainer(Config.load(path), log=lambda *_: None).train()
    run = path.parent / "runs" / "claude-subscription"
    done = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()] if (run / "steps.jsonl").exists() else []
    assert not (run / "summary.json").exists() and not [s for s in done if not s.get("bootstrap")]
    stand.play({"case": "reply", "text": ACE_REPLY})
    summary = Trainer(Config.load(path), log=lambda *_: None).train()
    assert summary["stop_reason"] == "completed all epochs" and summary["steps"] == 3


def test_a_lost_sign_in_is_read_past_a_long_failure(stand, tmp_path):
    """The endpoint's error names a lost sign-in before its message, and the client reads it from the whole body: its
    text keeps only the start, and what the CLI printed beside its events can make the message long."""
    from hermit.agent.evolver.base import ask
    from hermit.tools import evolver_agent
    from hermit.trainer.budget import Budget
    model = cli_model({"cli": "claude", "model": "claude-sonnet-5-5", "login": "claude"}, sandbox=sandbox_off())
    model.max_retries = 0
    stand.play({"case": "not_logged_in", "stderr": "a notice the CLI printed on its way out\n" * 30})
    with pytest.raises(LoginFailed, match="Not logged in"):
        ask(evolver_agent(model, Budget(), []), [{"role": "user", "content": "go"}])
    model.close()


def test_a_learners_lost_sign_in_stops_the_run_past_the_methods_handlers(stand, tmp_path):
    """ACE turns a failed call into an empty reflection and goes on: a lost sign-in must not be one of those."""
    from hermit.trainer.trainer import Trainer
    path = _no_sandbox(_project(tmp_path, stand))
    # The acting agent's three sessions are served; the first learner call finds the sign-in gone.
    stand.play({"case": "reply"}, {"case": "reply"}, {"case": "reply"}, {"case": "not_logged_in"})
    with pytest.raises(SystemExit, match="Not logged in"):
        Trainer(Config.load(path, ["agent.concurrency=1"]), log=lambda *_: None).train()
    run = path.parent / "runs" / "claude-subscription"
    assert not (run / "summary.json").exists()


def test_the_learners_sandboxes_reach_the_login_and_pass_no_token(stand, tmp_path):
    """The llm's sandbox (each text session's, role llm) and the evolver sandbox (a workspace learner's sessions)
    reach api.anthropic.com and pass no variable for the login: the token goes with each session, by name (the Base
    Agent's), never to their other commands. The llm's proves the user's own sign-in and the plan absent."""
    from hermit.trainer.trainer import Trainer
    # built as the Trainer builds it, never run: on a machine without bubblewrap too
    trainer = Trainer(Config.load(_project(tmp_path, stand), ["sandbox.enforce=best_effort"]), log=lambda *_: None)
    try:
        box = trainer._llm_box
        assert box.role == "llm" and box.allow == ["api.anthropic.com"] and trainer._llm_proxy is not None
        assert box.env_allow == [] and not box.framework
        absent = [Path(p) for p in box.probe_absent]
        assert {Path.home() / ".claude" / ".credentials.json", subscription.plan_dir(None, "claude")} <= set(absent)
    finally:
        trainer._close_sandboxes()
    trainer = Trainer(Config.load(_project(tmp_path, stand, "run_workspace_learner.yaml")), log=lambda *_: None)
    try:
        trainer._prepare()
        assert "api.anthropic.com" in trainer.sandbox.allow and trainer.proxy is not None
        assert subscription.TOKEN_ENV not in trainer.sandbox.env_allow
    finally:
        trainer._close_sandboxes()


def test_check_warns_of_a_public_network_on_the_token(stand, tmp_path):
    """In the task sandbox a command the session runs (an adapter's hook, say) can read the token from its parent
    process: a network that reaches any host could carry it away."""
    from hermit.trainer.trainer import warnings_for
    path = _project(tmp_path, stand)
    assert not any("login: claude" in w for w in warnings_for(Config.load(path), val_given=False))
    public = warnings_for(Config.load(path, ["environment.network=public"]), val_given=False)
    assert any("login: claude" in w and "network" in w for w in public)


def test_check_warns_of_an_unconfined_login_and_of_claude_code_with_no_login(stand, tmp_path):
    """The acting agent on the token where its pipeline runs on the host is warned of (no sandbox holds its sessions),
    not in a container of its own. Claude Code with no login copies the user's own sign-in wherever nothing refuses it:
    the acting agent on the host (the task sandbox refuses it when the config loads), the learner anywhere."""
    from hermit.trainer.trainer import warnings_for
    path = _project(tmp_path, stand, "run_workspace_learner.yaml")
    quiet = warnings_for(Config.load(path), val_given=False)
    assert not [w for w in quiet if "login: claude" in w or "Claude Code" in w]
    for host in ("environment.runner=host", "sandbox.enabled=false"):
        assert any("login: claude" in w for w in warnings_for(Config.load(path, [host]), val_given=False)), host
    boxed = warnings_for(Config.load(path, ["environment.image=officeqa-claude:2.1.318"]), val_given=False)
    assert not [w for w in boxed if "login: claude" in w]               # a container of its own holds each session
    unsigned = warnings_for(Config.load(path, ["environment.runner=host", "agent.model.login=null",
                                               "evolver.model.login=null"]), val_given=False)
    assert any(w.startswith("Claude Code (agent, evolver) runs with no login") for w in unsigned)
    learner = warnings_for(Config.load(path, ["evolver.model.login=null"]), val_given=False)
    assert any(w.startswith("Claude Code (evolver) runs with no login") for w in learner)


# ---- the roles that used to be Codex's -----------------------------------------------------------------------------------

# ---- the real CLI, on a scripted local model ----------------------------------------------------------------------------
# No login and no real host: a fake token, every proxy at a closed port, the model on 127.0.0.1. Claude Code must not be
# told to leave its host for these: the login's lock blanks ANTHROPIC_BASE_URL, so here the lock and the dropped variables
# leave that one name out. Everything else is what a real session runs.

def REAL_CLI(test):
    """An integration test (tests/README.md): it runs the claude CLI installed on the machine."""
    return pytest.mark.integration(pytest.mark.skipif(not shutil.which("claude"), reason="needs the claude CLI")(test))


def _sse(text="real-reply", stop="end_turn"):
    events = [("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                           "usage": {"output_tokens": 3}}),
        ("message_stop", {"type": "message_stop"})]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def _tool_turns(*blocks):
    """The model's side of a working session: each call answers with the next tool call, then with "finished"."""
    calls = iter([*blocks, None])

    def answer(body):
        block = next(calls)
        if block is None:
            return 200, {"content-type": "text/event-stream"}, _sse("finished")
        events = [("message_start", {"type": "message_start", "message": {
            "id": "msg_2", "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 1}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                               "usage": {"output_tokens": 3}}),
            ("message_stop", {"type": "message_stop"})]
        data = "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()
        return 200, {"content-type": "text/event-stream"}, data
    return answer


def _mock_model(answer, host="127.0.0.1"):
    """A scripted model on `host` (a container's network gateway, for a session in a container): (the server, its URL,
    the bodies of the model requests). `server.headers` holds each model request's headers."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    requests, sent = [], []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(404)
            self.send_header("content-length", "0")
            self.end_headers()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            if "/messages" in self.path:
                requests.append(body)
                sent.append({k.lower(): v for k, v in self.headers.items()})
            status, headers, data = answer(body)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer((host, 0), Handler)
    server.headers = sent
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{server.server_address[1]}", requests


@pytest.fixture
def real_cli(tmp_path, monkeypatch):
    """The real CLI in a world of its own: a fresh HOME, a fake token, closed proxies; the model's URL allowed through."""
    home = tmp_path / "real-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(subscription.TOKEN_ENV, TOKEN)
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("CLAUDE_CODE_MAX_RETRIES", "0")
    monkeypatch.setattr(subscription, "CLAUDE_PLAN", tmp_path / "plan")
    for name in ("TMUX", "TMUX_PANE"):                     # no live terminal multiplexer of the developer's to query
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "run").mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))   # its sockets in the test's own directory
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=False, capture_output=True)   # the project root
    allowed = tuple(n for n in base_cli.CLAUDE_BILLING if n != "ANTHROPIC_BASE_URL")
    monkeypatch.setattr(base_cli, "CLAUDE_BILLING", allowed)
    monkeypatch.setattr(base_cli, "CLAUDE_LOCK", json.dumps({"apiKeyHelper": "", "env": {n: "" for n in allowed}}))
    monkeypatch.setattr(base_cli, "CLAUDE_INHERITED", tuple(n for n in base_cli.CLAUDE_INHERITED if n != "ANTHROPIC_BASE_URL"))
    return tmp_path


@REAL_CLI
def test_the_real_cli_takes_the_text_sessions_flags_and_keeps_its_contract(real_cli):
    """Every flag of a text session on the real binary, and what reaches the model: no tools, the caller's system text, an
    `@path` left as text, a command name left as text, the token as the credential."""
    secret = real_cli / "secret.txt"
    secret.write_text("the-file-text")
    server, url, requests = _mock_model(lambda body: (200, {"content-type": "text/event-stream"}, _sse()))
    spec = {"cli": "claude", "model": "claude-haiku-5-5", "login": "claude", "env": {"ANTHROPIC_BASE_URL": url},
            "timeout_s": 60}
    try:
        with cli_endpoint(spec, sandbox=sandbox_off()) as endpoint:      # the CLI's flags here, not the sandbox
            message, usage = client(endpoint).chat([{"role": "system", "content": "SYSTEM-MARK"},
                                                    {"role": "user", "content": f"read @{secret} /cost"}],
                                                   reasoning_effort="low")
            slash, _ = client(endpoint).chat([{"role": "user", "content": "/cost now"}])
    finally:
        server.shutdown()
    assert message["content"] == "real-reply" and usage.input_tokens > 0 and usage.cost_usd is None
    assert slash["content"] == "real-reply" and len(requests) == 2       # the model was asked: no command ran
    first = requests[0]
    assert first["tools"] == [] and first["model"] == "claude-haiku-5-5" and first["output_config"]["effort"] == "low"
    system = json.dumps(first["system"])
    assert "SYSTEM-MARK" in system and "tool" not in system.lower().replace("no tools", "")
    wire = json.dumps(first["messages"])
    assert "the-file-text" not in wire and f"@{secret}" in wire
    assert "/cost now" in json.dumps(requests[1]["messages"])


@REAL_CLI
def test_the_real_clis_failures_read_as_what_they_are(real_cli):
    limit = {"anthropic-ratelimit-unified-status": "rejected",
             "anthropic-ratelimit-unified-reset": str(int(time.time()) + 3600),
             "anthropic-ratelimit-unified-representative-claim": "five_hour", "content-type": "application/json"}
    error = lambda kind, text: json.dumps({"type": "error", "error": {"type": kind, "message": text}}).encode()
    answers = iter([(429, limit, error("rate_limit_error", "limited")),
                    (529, {"content-type": "application/json"}, error("overloaded_error", "busy")),
                    (401, {"content-type": "application/json"}, error("authentication_error", "bad token")),
                    (400, {"content-type": "application/json"}, error("invalid_request_error", "nope"))])
    current = {}

    def answer(body):
        return current["reply"]
    server, url, requests = _mock_model(answer)
    spec = {"cli": "claude", "model": "claude-haiku-5-5", "login": "claude", "plan_rerun": False,
            "env": {"ANTHROPIC_BASE_URL": url}, "timeout_s": 60}
    statuses = []
    try:
        with cli_endpoint(spec, sandbox=sandbox_off()) as endpoint:
            for reply in answers:
                current["reply"] = reply
                with pytest.raises(ChatHTTPError) as raised:
                    client(endpoint).chat([{"role": "user", "content": "go"}])
                statuses.append(raised.value.code)
                if raised.value.code == 429:           # the hold it left: the next session would wait for the window
                    held = json.loads((real_cli / "plan" / "plan.json").read_text())
                    (real_cli / "plan" / "plan.json").unlink()
    finally:
        server.shutdown()
    assert statuses == [429, 503, 401, 400]
    assert held["rate_limit_reached_type"] == "session_refused" and held["held_until"] > time.time() + 3000


@REAL_CLI
def test_the_real_cli_on_the_token_stays_out_of_the_bash_tools_environment(real_cli, monkeypatch):
    """What a session on the token does to a workspace session: its own commands do not get the token in their environment
    (the CLI's own rule), and an API key dropped by the Base Agent is not there to get."""
    work = real_cli / "work"
    work.mkdir()
    server, url, _ = _mock_model(_tool_turns(
        {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "env > env.txt", "description": "env"}}))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-would-bill")
    try:
        result = session_agent(model="claude-haiku-5-5", env={"ANTHROPIC_BASE_URL": url}, timeout_s=60).run(
            "run the command", workspace=work)
    finally:
        server.shutdown()
    assert result.extra["served"] and result.reply == "finished"
    env = (work / "env.txt").read_text()
    assert TOKEN not in env and "sk-would-bill" not in env
    events = [json.loads(line) for line in Path(result.log).read_text().splitlines() if line.startswith("{")]
    init = next(e for e in events if e.get("type") == "system" and e.get("subtype") == "init")
    # the denied tools are not the session's: web search off, and nothing whose work outlives the session
    assert "Bash" in init["tools"] and not {"WebSearch", "WebFetch", *base_cli.CLAUDE_BEYOND_SESSION} & set(init["tools"])
    assert any(s.kind == "command" and s.exit_code == 0 for s in result.trajectory)


@REAL_CLI
def test_the_real_cli_takes_a_deployed_trees_skill_sub_agent_and_hook(real_cli):
    """A Harness² tree (the harness2-codex template) deployed on Claude Code as the acting agent deploys it: its skill and
    its sub-agent are the session's own, and its PostToolUse hook runs after a Bash call; what the hook printed is in the
    session's log, and in the trajectory the editors read."""
    from hermit.hosts import claude_code
    adapter, work, marker = real_cli / "adapter", real_cli / "work", real_cli / "hook-fired"
    shutil.copytree(ROOT / "hermit" / "adapter" / "templates" / "harness2_codex", adapter,
                    ignore=shutil.ignore_patterns("__pycache__"))
    text = "Do the work.\n- When a table must be read, read .agents/skills/tables/SKILL.md and follow it.\n"
    for name in ("AGENTS.md", "ADAPTER.md"):
        (adapter / name).write_text(text)
    (adapter / ".agents" / "skills" / "tables").mkdir(parents=True)
    (adapter / ".agents" / "skills" / "tables" / "SKILL.md").write_text(
        "---\nname: tables\ndescription: reading a table of a bulletin\n---\nRead the table.\n")
    (adapter / "sub_agents" / "reader").mkdir(parents=True)
    (adapter / "sub_agents" / "reader" / "AGENT.md").write_text("---\ndescription: reads long files\n---\nSummarise.\n")
    block = json.dumps({"decision": "block", "reason": "check the units"})
    (adapter / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {"PostToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": f"touch {marker} && echo '{block}'"}]}]}}))
    work.mkdir()
    claude_code.prepare(adapter, work, sys.executable)
    server, url, _ = _mock_model(_tool_turns(
        {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "echo hi", "description": "say hi"}}))
    try:
        result = session_agent(model="claude-haiku-5-5", env={"ANTHROPIC_BASE_URL": url}, timeout_s=60).run(
            "go", workspace=work)
    finally:
        server.shutdown()
    assert result.extra["served"] and result.reply == "finished"
    events = [json.loads(line) for line in Path(result.log).read_text().splitlines() if line.startswith("{")]
    init = next(e for e in events if e.get("type") == "system" and e.get("subtype") == "init")
    assert "tables" in init["skills"] and "reader" in init["agents"]
    assert marker.exists()                                        # the hook ran after the Bash call
    from hermit.agent.evolver.methods.harness2_upstream.render import render_claude_events
    rendered = render_claude_events(Path(result.log), obs_trunc=1000, max_chars=100000).splitlines()
    assert f"[HARNESS] hook PostToolUse:Bash (exit 0, success): {block}" in rendered



@REAL_CLI
def test_the_real_clis_reply_is_the_message_a_stop_hook_let_stand(real_cli):
    """An adapter's Stop hook blocks the first final message and the model answers again: the reply is the second
    message, as the CLI's own result says, not both."""
    work, marker = real_cli / "work", real_cli / "stop-fired"
    (work / ".claude").mkdir(parents=True)
    hook = (f"if [ ! -f {marker} ]; then touch {marker}; "
            f"echo '{json.dumps({'decision': 'block', 'reason': 'check the units again'})}'; fi")
    (work / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": hook}]}]}}))
    answers = iter(["FIRST <answer>1</answer>", "SECOND <answer>2</answer>", "THIRD"])

    def answer(body):                                   # a side request (no tools) is not the session's turn
        return 200, {"content-type": "text/event-stream"}, _sse(next(answers) if body.get("tools") else "ok")
    server, url, _ = _mock_model(answer)
    try:
        result = session_agent(model="claude-haiku-5-5", env={"ANTHROPIC_BASE_URL": url}, timeout_s=60).run(
            "answer", workspace=work)
    finally:
        server.shutdown()
    assert marker.exists() and result.extra["served"]
    assert result.reply == "SECOND <answer>2</answer>"


def test_a_chat_client_on_a_login_waits_out_the_plan_with_its_session():
    """The session waits for the plan inside the request (up to plan_max_wait_s): the client's clock covers that wait,
    or a hold would make it hang up, retry and fail a call the plan would serve after the hold."""
    from types import SimpleNamespace as NS
    from hermit.agent.base.login import MAX_WAIT_S

    def timeout(**settings):
        endpoint = NS(url="http://127.0.0.1:1/t", agent=NS(settings={"model": "m", **settings}))
        return endpoint_spec(endpoint, {"cli": "claude", "timeout": 100})["timeout"]
    assert timeout() == 100                                             # no login: no plan to wait for
    assert timeout(login="claude") == 100 + MAX_WAIT_S
    assert timeout(login="claude", plan_max_wait_s=60) == 160
    assert timeout(login="claude", plan_max_wait_s=None) == 100 + 30 * 86400


def test_a_lost_sign_in_streamed_as_an_error_keeps_its_code():
    """A stream that waited past the first bytes sends its failure as an event: the code that ends the run stays."""
    from hermit.agent.base.endpoint import _chunks
    from hermit.agent.base.interface import LOGIN_FAILED, login_failure
    body = json.dumps({"error": {"code": LOGIN_FAILED, "message": "the token was revoked"}}).encode()
    event = json.loads(next(_chunks(401, body, "m", False)))
    assert event["error"] == {"code": LOGIN_FAILED, "message": "the token was revoked", "status": 401}
    assert login_failure(ChatHTTPError(401, json.dumps(event).encode(), {})) == "the token was revoked"
    assert json.loads(next(_chunks(502, b'{"error": {"message": "x"}}', "m", False)))["error"]["code"] == 502


def test_what_a_session_leaves_never_shows_the_token(tmp_path, monkeypatch):
    """A command can read the token from a parent's environment and print it: the stream, the stderr log and the CLI's
    transcripts in a kept home name it instead. A link a session left at one of those names is not followed."""
    monkeypatch.setenv(subscription.TOKEN_ENV, TOKEN)
    stream, err, outside = tmp_path / "agent-claude.jsonl", tmp_path / "err.log", tmp_path / "outside.txt"
    stream.write_text(json.dumps({"type": "user", "message": {"content": f"PATH=/bin {subscription.TOKEN_ENV}={TOKEN}"}}))
    err.write_bytes(b"\xff" + TOKEN.encode() + b" x")
    outside.write_text(f"not the session's: {TOKEN}")
    (tmp_path / "linked.jsonl").symlink_to(outside)
    base_cli._scrub_token([stream, err, tmp_path / "linked.jsonl", tmp_path / "missing.jsonl"])
    assert TOKEN not in stream.read_text() and f"<{subscription.TOKEN_ENV}>" in stream.read_text()
    assert err.read_bytes() == b"\xff<" + subscription.TOKEN_ENV.encode() + b"> x"
    assert TOKEN in outside.read_text()                                  # never written through a link


@pytest.mark.parametrize("web_search", [False, True])
def test_every_claude_session_is_denied_what_outlives_it_or_reaches_past_it(tmp_path, web_search):
    """Web search's tools follow `web_search`; the tools whose work outlives the session or reaches the user's other
    sessions are denied in every session, on every route."""
    for settings in ({}, {"login": "claude", "model": "m"}, {"text_only": True}):
        args = make("claude", {"web_search": web_search, **settings}).command("go", tmp_path, tmp_path / "last.txt")
        denied = set(args[args.index("--disallowedTools") + 1].split(","))
        assert denied == {*base_cli.CLAUDE_BEYOND_SESSION, *([] if web_search else ["WebSearch", "WebFetch"])}
        assert {"RemoteTrigger", "CronCreate", "ScheduleWakeup", "SendMessage", "ListAgents"} <= denied


# ---- the real CLI in its container, on a scripted model at the network's gateway ----------------------------------------

@pytest.fixture
def real_container(real_cli):
    """Claude Code's image on its network (HERMIT_CLAUDE_IMAGE and HERMIT_CLAUDE_NETWORK; docker/claude/ builds it): (the
    container settings, the network's gateway, where a scripted model listens for the container). The model's URL is
    plain HTTP on the gateway, as the tools' server is, so the sessions are told to reach it directly (NO_PROXY), and
    to try once (the host's variables do not reach a container)."""
    from hermit.agent.base import container
    conf = container.settings({}, "claude")
    if not shutil.which("docker") or container.check(conf, "claude"):
        pytest.skip("needs docker, Claude Code's image and its network (HERMIT_CLAUDE_IMAGE, HERMIT_CLAUDE_NETWORK)")
    base_cli._ready.clear()
    return conf, container.gateway(conf)


def _tool_output(request: dict) -> str:
    """The text of the tool results in a model request: what the session's commands printed (the CLI may put a message
    of its own after them)."""
    out = []
    for message in request["messages"]:
        for block in message["content"] if isinstance(message["content"], list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                out += [content] if isinstance(content, str) else [b.get("text", "") for b in content or []]
    return "\n".join(out)


@pytest.mark.integration
def test_the_real_cli_in_its_container_keeps_the_token_and_the_network_from_its_commands(real_container, tmp_path):
    """A working session in the real image: the token reaches the model as the session's credential (by a descriptor:
    no environment holds it), and the session's commands, in Claude Code's own sandbox, find it in no environment they
    can read, see no process but their own, reach no host, and write the workspace and /tmp but not the home."""
    conf, gateway = real_container
    probe = ("echo env=$(env | grep -c setup-token-never); "
             "echo environ=$(cat /proc/[0-9]*/environ 2>/dev/null | tr '\\0' '\\n' | grep -c setup-token-never); "
             "echo procs=$(ls /proc | grep -cE '^[0-9]+$'); "
             "python3 -c \"import urllib.request as u; u.urlopen('https://api.anthropic.com', timeout=5)\" 2>&1 | tail -1; "
             "touch /tmp/t && echo tmp-ok; touch w-ok && echo work-ok; touch \"$HOME/h\" 2>/dev/null || echo home-refused")
    server, url, requests = _mock_model(_tool_turns({"id": "toolu_1", "name": "Bash",
                                                     "input": {"command": probe, "description": "probe"}}), host=gateway)
    work = tmp_path / "w"
    work.mkdir()
    try:
        result = session_agent(model="claude-haiku-5-5", isolation="container", container=conf, timeout_s=180,
                               env={"ANTHROPIC_BASE_URL": url, "NO_PROXY": gateway, "CLAUDE_CODE_MAX_RETRIES": "0"}
                               ).run("probe", workspace=work)
    finally:
        server.shutdown()
    assert result.extra["served"] and result.reply == "finished", result.extra.get("failure")
    assert server.headers and all(h.get("authorization") == f"Bearer {TOKEN}" for h in server.headers)
    said = _tool_output(requests[-1])
    for line in ("env=0", "environ=0", "tmp-ok", "work-ok", "home-refused", "host is not on the allow list"):
        assert line in said, said
    assert int(said.split("procs=")[1].split()[0]) < 10, said            # the sandbox's own few, not the CLI's
    assert (work / "w-ok").is_file() and TOKEN not in result.log.read_text()


@pytest.mark.integration
def test_the_real_cli_answers_a_chat_call_from_its_container(real_container):
    """A session that only replies, in the real image as Codex's learner runs in its: no tools, the caller's system
    text, the token as the credential."""
    conf, gateway = real_container
    server, url, requests = _mock_model(lambda body: (200, {"content-type": "text/event-stream"}, _sse()), host=gateway)
    spec = {"cli": "claude", "model": "claude-haiku-5-5", "login": "claude", "isolation": "container", "container": conf,
            "env": {"ANTHROPIC_BASE_URL": url, "NO_PROXY": gateway, "CLAUDE_CODE_MAX_RETRIES": "0"}, "timeout_s": 180}
    try:
        with cli_endpoint(spec) as endpoint:
            message, usage = client(endpoint).chat([{"role": "system", "content": "SYSTEM-MARK"},
                                                    {"role": "user", "content": "hi"}])
    finally:
        server.shutdown()
    assert message["content"] == "real-reply" and usage.input_tokens > 0
    assert requests[0]["tools"] == [] and "SYSTEM-MARK" in json.dumps(requests[0]["system"])
    assert server.headers[0].get("authorization") == f"Bearer {TOKEN}"


HOOK_PROBE = """import sys
def attempt(label, fn):
    try:
        fn()
        print(label + ": allowed")
    except PermissionError:
        print(label + ": denied")
    except OSError as e:
        print(label + ": " + type(e).__name__)
attempt("mem", lambda: open("/proc/1/mem", "rb").close())
attempt("environ", lambda: open("/proc/1/environ", "rb").read())
attempt("fd1", lambda: open("/proc/1/fd/1", "w").close())
print("comm", open("/proc/1/comm").read().strip())
"""


@pytest.mark.integration
def test_the_real_cli_in_its_container_keeps_the_token_from_hooks_and_its_settings_from_the_session(real_container,
                                                                                                    tmp_path):
    """What runs beside Claude Code, outside its sandbox (a deployed hook here), finds the image's CLI undumpable: its
    memory (the token) and its descriptors (the session's stream) are closed to it. And the session's own Write tool,
    which runs outside the sandbox too, may not give itself such a hook: its Claude Code settings are denied to it."""
    conf, gateway = real_container
    work = tmp_path / "w"
    (work / ".claude").mkdir(parents=True)
    (work / "probe.py").write_text(HOOK_PROBE)
    settings = work / ".claude" / "settings.json"
    deployed = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": f"python3 {work / 'probe.py'} >> {work / 'hook.txt'} 2>&1"}]}]}}
    settings.write_text(json.dumps(deployed))
    server, url, requests = _mock_model(_tool_turns(
        {"id": "toolu_1", "name": "Write", "input": {"file_path": str(settings), "content": "{}"}},
        {"id": "toolu_2", "name": "Bash", "input": {"command": "echo hi", "description": "say hi"}}), host=gateway)
    try:
        result = session_agent(model="claude-haiku-5-5", isolation="container", container=conf, timeout_s=180,
                               env={"ANTHROPIC_BASE_URL": url, "NO_PROXY": gateway, "CLAUDE_CODE_MAX_RETRIES": "0"}
                               ).run("probe", workspace=work)
    finally:
        server.shutdown()
    assert result.extra["served"], result.extra.get("failure")
    hook = (work / "hook.txt").read_text()
    assert "comm claude" in hook and "allowed" not in hook, hook                  # the hook ran, beside the CLI
    assert {"mem: denied", "environ: denied", "fd1: denied"} <= set(hook.splitlines()), hook
    assert json.loads(settings.read_text()) == deployed                            # the Write did not land
    assert "denied" in _tool_output(requests[-1]) and TOKEN not in hook


def test_a_lost_sign_in_at_finalize_leaves_a_run_unfinished_only_when_a_resume_would_finalize_again(tmp_path):
    """A run that completed its epochs and loses the sign-in in finalize ends unfinished: `hermit train` comes straight
    back to finalize. One that stopped early (the Evolver, a budget) would go back into training on a resume, past the
    stop: it ends as any failed finalize does, the last adapter final."""
    from toy_task import make_task
    from hermit.trainer.trainer import Trainer
    path = make_task(tmp_path, evolver={"name": "toy", "params": {"mode": "stop"}})
    with open(tmp_path / "toy" / "evolver.py", "a") as f:
        f.write("\n\nclass SignedOut(Toy):\n    def finalize(self, ctx):\n"
                "        from hermit.agent.base.interface import LoginFailed\n"
                "        raise LoginFailed('agent claude: Not logged in [hermit_login_failed]')\n")
    (tmp_path / "toy" / "evolver.yaml").write_text("class: evolver.py:SignedOut\nneeds: []\nparams: {mode: learn}\n")
    summary = Trainer(Config.load(path, ["trainer.allow_early_stop=true"]), log=lambda *_: None).train()
    assert summary["steps"] == 1 and "stopped the run" in summary["stop_reason"]
    assert "finalize failed" in summary["finalize"] and (tmp_path / "runs" / "toy-run" / "summary.json").is_file()
    with pytest.raises(LoginFailed, match="Not logged in"):
        Trainer(Config.load(path, ["trainer.run_name=all-epochs", "evolver.params.mode=learn"]),
                log=lambda *_: None).train()
    assert not (tmp_path / "runs" / "all-epochs" / "summary.json").exists()


def test_a_kept_home_never_shows_the_token_in_a_tool_result_the_cli_kept(stand, tmp_path):
    """Claude Code keeps a long tool result as a file of its own beside the transcript (projects/<project>/<session>/
    tool-results/): a token a command printed there is scrubbed like the transcripts."""
    work, home = tmp_path / "w", tmp_path / "home"
    work.mkdir()
    kept = home / "projects" / "p" / "s" / "tool-results" / "r.txt"
    kept.parent.mkdir(parents=True)
    stand.play({"case": "reply", "edit": [str(kept), f"printed: {TOKEN}"]})
    session_agent(home=home).run("go", workspace=work)
    assert kept.read_text() == f"printed: <{subscription.TOKEN_ENV}>"


def test_a_session_stopped_by_an_exception_leaves_no_token_in_its_stream(stand, tmp_path, monkeypatch):
    """The scrub runs however the session ends: an interrupt after the CLI printed the token leaves its name."""
    work, log = tmp_path / "w", tmp_path / "log" / "agent-claude.jsonl"
    work.mkdir()

    def interrupted(command, *, stream, **_):
        Path(stream).write_text(f"{TOKEN}\n")
        raise KeyboardInterrupt
    monkeypatch.setattr(base_cli, "run_process", interrupted)
    with pytest.raises(KeyboardInterrupt):
        session_agent(home=tmp_path / "home", log=str(log)).run("go", workspace=work)
    assert TOKEN not in log.read_text() and f"<{subscription.TOKEN_ENV}>" in log.read_text()


def test_a_session_that_paid_another_way_is_returned_not_held_though_also_refused(stand, tmp_path, monkeypatch):
    """A failed sign-in (another payer) that also reads as the plan's refusal ends the session: no wait restores it,
    so it is neither held for nor run again, and it keeps saying the sign-in failed."""
    calls = []

    def session(self, *args, **kwargs):
        calls.append(1)
        return Result(reply="", extra={"served": False, "refused": True, "login_failed": True, "failed": True,
                                       "held_until": time.time() + 3600})
    monkeypatch.setattr(base_cli.Claude, "_session", session)
    result = session_agent(plan_max_wait_s=0).run("go", workspace=tmp_path)
    assert calls == [1] and result.extra["login_failed"]
    assert not subscription.held(subscription.plan_dir(None, "claude"), False)


def test_a_failed_call_whose_usage_is_no_count_keeps_its_error():
    from hermit.tools import failed_usage
    assert failed_usage(ChatHTTPError(500, b'{"usage": {"prompt_tokens": 1e400, "completion_tokens": 0}}', {}), "m") \
        is None


def test_a_login_that_fails_in_a_learners_tool_call_ends_the_run(tmp_path):
    """A learner's `llm` or `run_agent` call runs in the tool server's thread, where a failed sign-in would reach the
    learner as a failed call: the Trainer keeps it, stops the learner, and ends the run after the call."""
    from hermit.trainer.trainer import Trainer
    trainer = Trainer.__new__(Trainer)
    trainer._login_failed = None
    cancel = threading.Event()

    def call(**arguments):
        raise LoginFailed("the token was revoked")
    with pytest.raises(LoginFailed):
        trainer._latched(call, cancel)(prompt="go")
    assert cancel.is_set()
    with pytest.raises(LoginFailed, match="revoked"):
        trainer._raise_login_failed({"value": "the learner went on"})


@NEEDS_BWRAP
def test_the_task_sandbox_grants_and_covers_the_plan_under_its_real_name(stand, tmp_path, monkeypatch):
    """A plan under a linked directory (a ~/.cache that is a link) is granted and covered by its real name only: the
    sandbox would show a linked path under both names, and the session's cover hides the one it is given."""
    from hermit.isolate import agent_pipeline
    real = tmp_path / "disk"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real)
    monkeypatch.setattr(subscription, "CLAUDE_PLAN", tmp_path / "alias" / "plan")
    config = Config.load(_project(tmp_path, stand))
    pipeline, close = agent_pipeline(config, "rollout", records=tmp_path / "records", work_root=tmp_path / "calls")
    try:
        assert pipeline.plan == real / "plan"
        assert real / "plan" in [Path(w) for w in pipeline.sandbox.writable]
        assert not any("alias" in str(w) for w in pipeline.sandbox.writable)
    finally:
        close()


def test_usage_credits_opt_in_only_on_a_real_yes():
    assert [subscription.opted_in(v) for v in (True, "true", "Yes", 1)] == [True, True, True, True]
    assert [subscription.opted_in(v) for v in (False, "false", "no", 0, None, "")] == [False] * 6


def test_a_session_that_drew_credits_on_an_earlier_request_still_says_so():
    """Only the last rate-limit event used to be read: a later request on the plan hid a credit an earlier one drew."""
    events = [{"type": "rate_limit_event", "rate_limit_info": {"isUsingOverage": True, "resetsAt": 100}},
              {"type": "rate_limit_event", "rate_limit_info": {"isUsingOverage": False, "resetsAt": 200}}]
    report = subscription.claude_report(events)
    assert report["using_credits"] and report["credits_resets_at"] == 100 and report["resets_at"] == 200


def test_a_learner_on_the_subscription_with_a_backend_reference_is_refused_before_the_first_step():
    problems = subscription.evolver_problems({"login": "claude", "backend": "claude", "model": "m",
                                              "binding": {"ref": "vllm/test-model"}})
    assert any("evolver.model.name" in p and "reference" in p for p in problems)


def test_a_session_that_paid_another_way_and_then_hung_still_ends_the_run(tmp_path, monkeypatch):
    """A failed sign-in outranks the timeout that followed it: the attempt is not judged as one."""
    def hung(self, prompt, workspace=None, tools=(), limits=None, session=None, roles=()):
        return Result(reply="", timed_out=True, extra={"served": False, "login_failed": True, "unavailable": False,
                                                       "started": True, "failure": "the session paid with apiKey"})
    monkeypatch.setattr(base_cli.Claude, "run", hung)
    with pytest.raises(LoginFailed, match="paid with apiKey"):
        acting.run({"id": "q", "question": "q"}, None, params=acting_params(tmp_path), base=tmp_path)


def test_an_agent_evolvers_sessions_use_the_login_root_its_preflight_checked(tmp_path):
    """Preflight reads the learner's login_root; the sessions it starts sign in from the same one."""
    from types import SimpleNamespace as NS
    from hermit.agent.evolver.agent import AgentEvolver
    ws = NS(state=tmp_path / "state", output=tmp_path / "out")
    ctx = NS(params={"login": "chatgpt", "login_root": str(tmp_path / "root"), "model": "gpt-x", "web_search": False},
             sandbox=None, workspace=ws, cancel=None)
    settings = AgentEvolver.settings(AgentEvolver.__new__(AgentEvolver), ctx, "codex")
    assert settings["login_root"] == str(tmp_path / "root")


def test_an_agent_evolvers_failed_sign_in_outranks_the_stop_that_followed(tmp_path, monkeypatch):
    """A session that reported a lost sign-in and then hung until the Trainer (or a method's own deadline) stopped it
    ends the run: it is no ordinary failed step, whose edits a method could adopt."""
    import threading
    from hermit.agent.evolver import agent as evolver_agent
    from hermit.agent.evolver.base import Access, StepContext, Workspace
    ws = Workspace(*(tmp_path / n for n in ("adapter", "lm", "info", "hist", "state", "scratch", "out")))
    for d in (ws.adapter, ws.state, ws.output):
        d.mkdir(parents=True)
    (tmp_path / "START.md").write_text("go")
    cancel = threading.Event()
    ctx = StepContext(workspace=ws, access=Access(), tools={}, cancel=cancel,
                      params={"backend": "claude", "login": "claude", "model": "m", "web_search": False,
                              "prompt": "START.md"})

    class Hung:
        def run(self, *args, **kwargs):
            cancel.set()
            return Result(reply="", extra={"login_failed": True, "failure": "Not logged in"})
    monkeypatch.setattr(evolver_agent, "make", lambda backend, settings: Hung())
    evolver = evolver_agent.AgentEvolver()
    evolver.directory = tmp_path
    with pytest.raises(LoginFailed, match="Not logged in"):
        evolver.step(ctx)
