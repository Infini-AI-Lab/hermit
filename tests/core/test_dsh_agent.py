"""The `dsh` Base Agent (DeepSeek Harness, hermit/agent/base/cli.py), its model binding
(hermit/agent/model/backends.py), its stream's reader (hermit/agent/base/trajectory.py) and the Layer 3 code that runs
it: the acting agent (hermit/agent/acting/workspace.py) with its deployment (hermit/hosts/dsh.py), the config's `dsh`
base, the `dsh` Evolver.

Stand-ins: a fake `dsh` on PATH that replays a stream recorded from DeepSeek Harness 0.2.2-rc.1 (tests/fixtures/dsh,
whose README says how) and records what it was given; and, for the tests marked integration, the installed `dsh`
against a scripted chat-completions model on the loopback (`serve_chat`), never a provider."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading

import pytest

from hermit.agent.base import cli as base_cli
from hermit.agent.base import trajectory
from hermit.agent.base.interface import Unsupported, make
from hermit.agent.base.tool import Tool
from hermit.agent.model.backends import bind_agent_params, registry

ROOT = Path(__file__).resolve().parents[2]
STREAMS = ROOT / "tests" / "fixtures" / "dsh"
FILE_ADAPTER = ROOT / "docs" / "_examples" / "file_adapter"
SESSION = "session-b2fbf9bf-a321-4065-8f8a-ee344b66b5f3"          # the recorded success stream's session

FAKE_DSH = f'''#!{sys.executable}
"""A stand-in for `dsh --profile headless ...`: records its arguments, dsh's variables, the patch layers it is given
and its stdin, runs FAKE_DSH_RUN in its working directory, then prints FAKE_DSH_STREAM and exits FAKE_DSH_EXIT."""
import json, os, subprocess, sys
args = sys.argv[1:]
patches = {{args[i + 1]: open(args[i + 1]).read() for i, a in enumerate(args[:-1]) if a == "--patch"}}
names = ("DSH_HOME", "HOME", "DSH_AGENTS_HOME", "DSH_TELEMETRY_DISABLED", "{base_cli.DSH_NO_KEY}", "FAKE_KEY",
         "DSH_BUNDLED_SKILL_DIR", "DEEPSEEK_BASE_URL", "DEEPSEEK_SEARCH_BASE_URL")
seen = {{"args": args, "env": {{k: os.environ.get(k) for k in names}}, "patches": patches, "stdin": sys.stdin.read()}}
with open(os.environ["FAKE_DSH_SEEN"], "a") as f:
    f.write(json.dumps(seen) + "\\n")
if os.environ.get("FAKE_DSH_RUN"):
    subprocess.run(os.environ["FAKE_DSH_RUN"], shell=True)
sys.stdout.write(open(os.environ["FAKE_DSH_STREAM"]).read())
sys.exit(int(os.environ.get("FAKE_DSH_EXIT", "0")))
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "dsh"
    exe.write_text(FAKE_DSH)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DSH_SEEN", str(tmp_path / "seen.jsonl"))
    monkeypatch.setenv("FAKE_DSH_STREAM", str(STREAMS / "success.jsonl"))
    monkeypatch.setenv("FAKE_KEY", "sk-fake")
    return tmp_path


def seen(tmp_path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "seen.jsonl").read_text().splitlines()]


def layer_rows(text: str) -> list[dict]:
    """A patch layer's rows, read as dsh reads it (YAML), a `!!js` expression as {"!!js": its text}."""
    import yaml

    class Loader(yaml.SafeLoader):
        pass
    Loader.add_constructor("tag:yaml.org,2002:js", lambda loader, node: {"!!js": loader.construct_scalar(node)})
    return yaml.load(text, Loader=Loader)


def rows(call: dict, home: Path) -> list[dict]:
    """The session's own patch layer, as the fake read it."""
    return layer_rows(call["patches"][str(home / base_cli.DSH_PATCH)])


def row(layer: list[dict], row_id: str) -> dict:
    [found] = [r for r in layer if r.get("id") == row_id]
    return found


def bound(**backend) -> dict:
    """A `dsh` agent's params with its model bound to a backend of its own (`fake/m-1`)."""
    spec = {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "FAKE_KEY", "apis": ["chat"],
            "prices": {"m-1": {"input": 1.0, "cached_input": 0.1, "output": 2.0}}, **backend}
    return bind_agent_params({"model": "fake/m-1"}, "dsh", registry({"fake": spec}))


def echo() -> Tool:
    return Tool("echo", "Echo the text back.", lambda text="": {"echoed": text},
                input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})


def test_a_bound_model_is_a_dsh_route_with_its_key_variable_in_the_dialect_the_backend_serves():
    """The key reaches dsh only as the name of its variable (`apiKeyEnv`): the records keep the name, never the key."""
    params = bound()
    assert params["model"] == "m-1" and params["binding"]["ref"] == "fake/m-1"
    assert params["provider"] == {"api": "openai-completions", "baseURL": "http://127.0.0.1:9/v1",
                                  "apiKeyEnv": "FAKE_KEY", "models": [{"id": "m-1"}]}
    assert params["price"] == {"input": 1.0, "cached_input": 0.1, "output": 2.0}
    messages = bound(apis=["messages"])["provider"]
    assert messages["api"] == "anthropic-messages" and messages["baseURL"] == "http://127.0.0.1:9"
    system = bound(developer_role="system", context_window=32000)["provider"]
    assert system["compat"] == {"supportsDeveloperRole": False} and system["models"][0]["contextWindow"] == 32000
    free = bound(api_key_env="NO_SUCH_KEY_VAR", key_required=False)["provider"]
    assert "apiKeyEnv" not in free                 # a server that needs no key: the session names the placeholder


def test_dsh_runs_headless_in_its_home_with_its_model_and_the_tools_as_its_own(fake, monkeypatch):
    """Without the sandbox a session gets this process's environment: a dsh variable of the caller's (here a folder
    of skills, and where DeepSeek's API is reached, for dsh's own provider and for its web search) does not reach it,
    so the session's settings are its own home's; one the session is given (`env`, here the caller's by name) does.
    The call's tools are an MCP row whose server, if dsh cannot start it, fails the session (failOnStartupError), and
    whose calls may take as long as a Trainer tool's rollouts."""
    tmp_path = fake
    monkeypatch.setenv("DSH_BUNDLED_SKILL_DIR", str(tmp_path / "callers-skills"))
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("DEEPSEEK_SEARCH_BASE_URL", "http://127.0.0.1:9")
    home, work = tmp_path / "home", tmp_path / "work"
    work.mkdir()
    agent = make("dsh", {**bound(), "effort": "high", "web_search": False, "home": str(home),
                         "log": str(tmp_path / "agent-dsh.jsonl")})
    result = agent.run("-a prompt that starts with a dash", workspace=work, tools=[echo()])
    [call] = seen(tmp_path)
    assert call["args"][:4] == ["--profile", "headless", "--patch", str(home / base_cli.DSH_PATCH)]
    assert call["args"][4:9] == ["--json", "--permission-mode", "workspace-write", "--approval-policy", "never"]
    assert call["args"][-2:] == ["--", "-a prompt that starts with a dash"]
    assert call["env"] == {"DSH_HOME": str(home), "HOME": str(home), "DSH_AGENTS_HOME": str(home / ".agents"),
                           "DSH_TELEMETRY_DISABLED": "1", base_cli.DSH_NO_KEY: "local",
                           "FAKE_KEY": "sk-fake", "DSH_BUNDLED_SKILL_DIR": None,   # none of the user's own skills
                           "DEEPSEEK_BASE_URL": None, "DEEPSEEK_SEARCH_BASE_URL": None}
    layer = rows(call, home)
    assert "sk-fake" not in json.dumps(layer)
    assert {r["id"] for r in layer if r.get("disabled")} == {"session-telemetry-otel", "session-title-llm",
                                                             "plugin-package-inventory-deepseek", "tool-subagent",
                                                             "tool-subagent-fork", "tool-subagent-control",
                                                             "tool-workflow", "tool-web"}
    assert row(layer, "session-log-deepseek")["config"] == {"enabled": False}
    route = row(layer, "llm-pi-ai")["config"]["providers"][base_cli.DSH_PROVIDER]
    assert route["apiKeyEnv"] == "FAKE_KEY" and route["models"] == [{"id": "m-1", "reasoningEfforts": {"high": "high"}}]
    assert row(layer, "agent-default-model")["config"] == {"provider": base_cli.DSH_PROVIDER, "model": "m-1",
                                                           "reasoningEffort": "high"}
    [server] = [r for r in layer if "insert" in r]
    [entry] = server["insert"]
    assert entry["name"] == "@deepseek-ai/dsh-mcp-client" and entry["config"]["serverName"] == "hermit_tools"
    assert entry["config"]["transport"] == "streamable-http" and entry["config"]["url"].startswith("http://127.0.0.1:")
    assert entry["config"]["failOnStartupError"] is True and entry["config"]["toolCallTimeoutMs"] == 1_800_000
    assert result.reply == "the answer" and result.exit_code == 0 and result.extra["served"]
    assert not result.extra["failed"] and result.session == SESSION
    [usage] = result.usage
    assert (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens, usage.calls) == (6000, 2400, 120, 6)
    assert usage.cost_usd == pytest.approx(0.00408)                       # dsh reports none: the backend's price
    kinds = [s.kind for s in result.trajectory if s.kind != "reasoning"]
    assert kinds == ["command", "command", "tool_call", "file_change", "file_change", "message"]
    failed, passed, tool = result.trajectory[1], result.trajectory[3], json.loads(result.trajectory[5].text)
    assert (failed.command, failed.exit_code, failed.output) == ("echo hi && exit 3", 3, "hi")
    assert (passed.command, passed.exit_code, passed.output) == ("echo ok", 0, "ok\n")     # no marker: exit 0
    assert tool == {"arguments": {"text": "from dsh"}, "server": "hermit_tools", "tool": "echo"}
    assert json.loads(result.trajectory[5].output) == {"echoed": "from dsh"}
    assert result.trajectory[0] == trajectory.Step("reasoning", "Thinking it over.")
    agent.configured(env={"DEEPSEEK_BASE_URL": "$DEEPSEEK_BASE_URL"}).run("go", workspace=work)
    assert seen(tmp_path)[-1]["env"]["DEEPSEEK_BASE_URL"] == "http://127.0.0.1:9"


def test_an_effort_is_a_level_the_bound_model_lists_and_off_asks_it_for_none(fake):
    """dsh refuses an effort its route does not list for the model (UNSUPPORTED_REASONING_EFFORT), and a list of
    `off` alone (INVALID_CONFIG): effort off declares the model with no levels and gives no effort, so dsh asks for
    no reasoning level, which leaves the server's default (only dsh's own provider has a way to turn thinking off)."""
    work = fake / "work"
    work.mkdir()
    agent = make("dsh", {**bound(), "web_search": False, "home": str(fake / "home")})
    for effort, efforts, default in (("high", {"high": "high"}, "high"), ("off", False, None), (None, None, None)):
        agent.configured(effort=effort).run("go", workspace=work)
        layer = rows(seen(fake)[-1], fake / "home")
        [model] = row(layer, "llm-pi-ai")["config"]["providers"][base_cli.DSH_PROVIDER]["models"]
        assert model.get("reasoningEfforts") == efforts
        assert row(layer, "agent-default-model")["config"].get("reasoningEffort") == default


def test_a_failed_run_is_an_error_and_only_an_outage_is_unavailable(fake, monkeypatch):
    """dsh exits 1 for a run that did not complete; its error code tells an outage (the codes it retries, and a
    stream the provider closed) from a refusal, and only an outage is `unavailable`: the acting agent runs the task
    again only when the provider could not serve it. An overload a provider sends inside a 200 stream (recorded:
    overloaded.jsonl, the Messages API's `overloaded_error` event) has no status, and dsh leaves it unclassified
    (pi-ai's PI_AI_ERROR): it is told by its words, as Pi's are read, and pi-ai's own refusal so coded is no outage."""
    monkeypatch.setenv("FAKE_DSH_STREAM", str(STREAMS / "failure.jsonl"))
    monkeypatch.setenv("FAKE_DSH_EXIT", "1")
    work = fake / "work"
    work.mkdir()
    result = make("dsh", {**bound(), "web_search": False, "home": str(fake / "home")}).run("go", workspace=work)
    assert result.exit_code == 1 and result.extra["failed"] and not result.extra["served"]
    assert result.extra["unavailable"] and result.extra["failure"].startswith("dsh: SERVER: 503: ")
    assert result.usage == []                              # retried requests that used no tokens are no calls
    assert [s.kind for s in result.trajectory] == ["error"] and result.trajectory[0].text.startswith("503: ")
    refused = (STREAMS / "failure.jsonl").read_text().replace('"SERVER"', '"AUTH"').replace("dsh: SERVER:",
                                                                                         "dsh: AUTH:")
    assert not base_cli.Dsh.outage(refused)                                   # a bad key is no outage
    for code in ("RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT", "EMPTY_RESPONSE", "STREAM_CLOSED"):
        assert base_cli.Dsh.outage((STREAMS / "failure.jsonl").read_text().replace("SERVER", code)), code
    monkeypatch.setenv("FAKE_DSH_STREAM", str(STREAMS / "overloaded.jsonl"))
    result = make("dsh", {**bound(), "web_search": False, "home": str(fake / "home")}).run("go", workspace=work)
    assert result.extra["failed"] and result.extra["unavailable"] and "dsh: PI_AI_ERROR: " in result.extra["failure"]
    [end] = [json.loads(line) for line in (STREAMS / "overloaded.jsonl").read_text().splitlines()
             if line.startswith('{"type":"final"')]
    no_key = {"kind": "error", "error": {"code": "PI_AI_ERROR", "message": "No API key for provider: hermit"}}
    assert not base_cli.Dsh.outage(json.dumps({**end, "reason": no_key}))


def test_a_tool_servers_words_of_an_outage_are_no_outage(fake, monkeypatch):
    """dsh starts a deployed stdio tool server on its own stderr, which the Base Agent keeps in the stream: a tool's
    line with an outage's words (written here by hand into the recorded stream of a run that ended at the token
    limit) is the session's own work, not the provider's end. The acting agent runs the task once and records the
    session's failure, not an outage it would run `retries` times and then leave at an unknown cost."""
    from hermit.agent.acting import workspace as acting
    lines = (STREAMS / "max_tokens.jsonl").read_text().splitlines(keepends=True)
    tool = "requests.exceptions.HTTPError: 503 Server Error: Service Unavailable for url: https://example.com/v1\n"
    stream = fake / "tool_stderr.jsonl"
    stream.write_text("".join(lines[:3]) + tool + "".join(lines[3:]))
    monkeypatch.setenv("FAKE_DSH_STREAM", str(stream))
    monkeypatch.setenv("FAKE_DSH_EXIT", "1")
    work = fake / "work"
    work.mkdir()
    result = make("dsh", {**bound(), "web_search": False, "home": str(fake / "home")}).run("go", workspace=work)
    assert result.extra["failed"] and not result.extra["served"] and not result.extra["unavailable"]
    out = acting.run({"id": "q1"}, None, params={**bound(), "backend": "dsh", "web_search": False, "prompt": "go",
                                                 "workspaces": "ws", "retries": 3, "retry_wait_s": 0}, base=fake)
    assert out.extra["attempts"] == 1 and "run max-tokens" in out.extra["session_failed"]


def test_a_run_dsh_was_stopped_in_is_no_success_although_dsh_exits_0(fake, monkeypatch, tmp_path):
    """dsh exits 0 when SIGTERM stops it (recorded: stopped.jsonl, a session stopped while its command ran): its own
    rule for a completed run is exit 0 and a completed `final`, so a run without one has failed. The acting agent
    records no answer, and an AgentEvolver's step is a failed one; so is a session whose tools did not start. The
    stopped step's request is not counted (dsh writes a step's usage at its end, which a stopped session never
    writes): the session has no usage."""
    from hermit.agent.acting import workspace as acting
    from hermit.agent.evolver.agent import AgentEvolver
    from hermit.agent.evolver.base import Access, StepContext, Workspace
    monkeypatch.setenv("FAKE_DSH_STREAM", str(STREAMS / "stopped.jsonl"))
    work = fake / "work"
    work.mkdir()
    result = make("dsh", {**bound(), "web_search": False, "home": str(fake / "home")}).run("go", workspace=work)
    assert result.exit_code == 0 and result.extra["failed"] and not result.extra["served"] and result.extra["started"]
    assert not result.extra["unavailable"] and result.reply == "" and "closed handle" in result.extra["failure"]
    assert [s.kind for s in result.trajectory] == ["command", "error"] and result.trajectory[0].exit_code is None
    assert result.usage == []
    out = acting.run({"id": "q1"}, None, params={**bound(), "backend": "dsh", "web_search": False, "prompt": "go",
                                                 "workspaces": "ws"}, base=fake)
    assert out.output is None and "closed handle" in out.extra["session_failed"]
    ws = Workspace(*(tmp_path / "evolver" / n for n in ("adapter", "lm", "info", "hist", "state", "scratch", "out")))
    for folder in (ws.adapter, ws.state, ws.output):
        folder.mkdir(parents=True)
    (tmp_path / "START.md").write_text("go")
    evolver = AgentEvolver()
    evolver.directory = tmp_path
    for stream, says in (("stopped.jsonl", "closed handle"), ("tools_down.jsonl", "did not start"),
                         ("success.jsonl", None)):
        monkeypatch.setenv("FAKE_DSH_STREAM", str(STREAMS / stream))
        step = evolver.step(StepContext(workspace=ws, access=Access(), tools={}, params={
            **bound(), "backend": "dsh", "web_search": False, "prompt": "START.md", "home": []}))
        assert (step.status, says is None or says in step.summary) == ("failed" if says else "done", True)


def test_a_resumed_session_is_adopted_by_its_id_and_counts_only_its_new_usage(fake, monkeypatch):
    monkeypatch.setenv("FAKE_DSH_STREAM", str(STREAMS / "resume.jsonl"))
    home, work = fake / "home", fake / "work"
    work.mkdir()
    (home / "sessions" / "--run-work--" / SESSION).mkdir(parents=True)
    agent = make("dsh", {**bound(), "web_search": False, "home": str(home)})
    result = agent.run("again", workspace=work, session=SESSION)
    args = seen(fake)[0]["args"]
    assert args[args.index("--session-id") + 1] == SESSION
    [usage] = result.usage
    assert (usage.input_tokens, usage.calls) == (1000, 1)
    agent.run("again", workspace=work, session="session-gone")     # no such session kept: a new one
    agent.run("again", workspace=work, session="*")                 # nor a glob
    assert all("--session-id" not in call["args"] for call in seen(fake)[1:])


def test_dsh_refuses_roles_and_a_container_and_searches_only_on_deepseeks_own_provider(fake):
    """Roles and a container are refused, never ignored. Web search on: dsh's search is DeepSeek's own service (its
    Messages API on DEEPSEEK_API_KEY), so a session whose model is another backend's would search on a provider and
    key the run did not bind, outside its usage: it gets fetch only, as Codex's hosted search is off for a backend
    other than OpenAI's. A model of DeepSeek's API, or of dsh's own provider, keeps dsh's web tools as they are."""
    work = fake / "work"
    work.mkdir()
    agent = make("dsh", {**bound(), "web_search": False, "home": str(fake / "home")})
    role = {"name": "reviewer", "description": "Review.", "instructions": "Check it."}
    with pytest.raises(Unsupported, match="roles"):
        agent.run("go", workspace=work, roles=[role])
    with pytest.raises(Unsupported, match="takes isolation none or sandbox"):
        agent.configured(isolation="container").run("go", workspace=work)
    with pytest.raises(ValueError, match="permission_mode"):
        agent.configured(permission_mode="bypass").run("go", workspace=work)
    agent.configured(web_search=True).run("go", workspace=work)
    assert row(rows(seen(fake)[-1], fake / "home"), "tool-web") == {"id": "tool-web", "config": {"search": False,
                                                                                               "fetch": True}}
    own = dict(user_file=None, environ={})              # the built-ins alone: not the developer's own backends
    deepseek = bind_agent_params({"model": "deepseek/deepseek-v4-flash"}, "dsh", registry({}, **own))
    named = bind_agent_params({"model": "ds/deepseek-v4-flash"}, "dsh", registry({"ds": {"from": "deepseek"}}, **own))
    for params in (deepseek, named, {}):                  # DeepSeek's API on its key, under any name; dsh's own
        make("dsh", {**params, "web_search": True, "home": str(fake / "home")}).run("go", workspace=work)
        assert all(r.get("id") != "tool-web" for r in rows(seen(fake)[-1], fake / "home"))
    relayed = bind_agent_params({"model": "deepseek/deepseek-v4-flash"}, "dsh", registry(
        {}, user_file=None, environ={"DEEPSEEK_BASE_URL": "http://127.0.0.1:9/v1"}))   # the variable moves the backend
    make("dsh", {**relayed, "web_search": True, "home": str(fake / "home")}).run("go", workspace=work)
    assert row(rows(seen(fake)[-1], fake / "home"), "tool-web")["config"] == {"search": False, "fetch": True}


def test_dsh_takes_no_login_and_no_container_wherever_the_run_is_checked():
    """dsh signs in through a model reference (or a model of its own provider): a ChatGPT or Claude login is refused
    by the acting agent, the run config and the Evolver's check, never passed on or ignored; a container runs Codex
    or Claude Code."""
    from hermit.agent.acting import workspace as acting
    from hermit.agent.base.login import evolver_problems
    from hermit.config import Config
    with pytest.raises(ValueError, match="agent dsh takes no login"):
        acting._check("dsh", {"web_search": False, "login": "chatgpt"})
    with pytest.raises(ValueError, match="agent dsh takes isolation: none"):
        acting._check("dsh", {"web_search": False, "isolation": "container"})
    assert acting._check("dsh", {"web_search": False}) == "dsh"
    assert any("the dsh Evolver signs in through its model reference" in problem
               for problem in evolver_problems({"backend": "dsh", "login": "claude"}))
    run = ROOT / "docs" / "_examples" / "first_training" / "run.yaml"
    fake = "backends={fake: {base_url: 'http://127.0.0.1:9/v1', api_key_env: FAKE_KEY, apis: [chat]}}"
    with pytest.raises(ValueError, match="agent dsh takes no login"):
        Config.load(run, [fake, "agent.name=null", "agent.base=dsh", "agent.model={ref: fake/m-1, login: chatgpt}",
                          "agent.params={prompt: go, web_search: false}"])


def test_an_effort_or_a_sandbox_dsh_has_not_is_refused_before_any_session(tmp_path, monkeypatch):
    """dsh's effort levels and permission modes are its own: a value it has not would fail every session, one task
    (or one learner step, after its rollouts were paid for) at a time. The acting agent's are refused as the run
    config loads (as a Claude Code effort is); the dsh Evolver's by `hermit check` and before `train`'s first step,
    from its params as the Trainer has them, whichever way the config names it (evolver.base: dsh, or the dsh method
    with its params) and with the run's model as its own. dsh's own provider (a model of no binding) has fewer
    levels than a bound model's route. YAML reads a bare `off` as false, which is `off`. A setting an agent does not
    take is refused, not dropped: the Evolver runs dsh's own sandbox off and gives its sessions no AGENTS.md (its own
    danger-full-access and instruction_files false are taken, another value refused), and the acting agent's
    sessions read the adapter's context as their AGENTS.md (instruction_files false is refused)."""
    from hermit.agent.base.login import evolver_problems
    from hermit.agent.evolver.loader import find, instantiate
    from hermit.config import Config
    from hermit.settings_check import run_check
    from hermit.trainer.trainer import Trainer
    monkeypatch.setenv("HA_BACKENDS", str(tmp_path / "no-user-file.yaml"))
    monkeypatch.setenv("FAKE_KEY", "sk-fake")
    run = ROOT / "docs" / "_examples" / "first_training" / "run.yaml"
    fake = "backends={fake: {base_url: 'http://127.0.0.1:9/v1', api_key_env: FAKE_KEY, apis: [chat]}}"
    load = lambda *more: Config.load(run, [fake, f"trainer.output_dir={tmp_path / 'runs'}", "agent.name=null",
                                           "agent.base=dsh", "agent.params={prompt: go, web_search: false}", *more])
    loaded = load("agent.model={ref: fake/m-1, effort: xhigh}", "agent.dsh={permission_mode: read-only}")
    assert loaded.data["agent"]["params"]["effort"] == "xhigh"
    assert load("agent.model={ref: fake/m-1, effort: off}").data["agent"]["params"]["effort"] is False
    with pytest.raises(ValueError, match="agent.model.effort is one of off, minimal, low, medium, high, xhigh, max"):
        load("agent.model={ref: fake/m-1, effort: ultra}")
    with pytest.raises(ValueError, match="one of off, low, high, max for a model of dsh's own provider"):
        load("agent.model={name: deepseek-v4-flash, effort: medium}", "sandbox.enabled=false")
    assert load("agent.model={name: deepseek-v4-flash, effort: max}", "sandbox.enabled=false")
    with pytest.raises(ValueError, match="agent.dsh.permission_mode is one of read-only, workspace-write"):
        load("agent.model=fake/m-1", "agent.dsh={permission_mode: bypass}")
    assert load("agent.model=fake/m-1", "agent.dsh={instruction_files: true}")
    with pytest.raises(ValueError, match="agent.dsh.instruction_files is not taken: the adapter's context reaches"):
        load("agent.model=fake/m-1", "agent.dsh={instruction_files: false}")

    def problems(*more) -> list[str]:          # the Evolver's, from its params as the Trainer and the check have them
        config = load("agent.model=fake/m-1", *more)
        d = config.data
        _, params = instantiate(find(d["evolver"]["name"], config.base), d["evolver"]["params"],
                                backends=registry(d.get("backends")), default_model=d.get("model"))
        return evolver_problems(params)

    layered, flat = ("evolver.name=dsh", "evolver.base=dsh"), ("evolver.name=dsh",)
    assert problems(*layered, "evolver.model={ref: fake/m-1, effort: off}") == []
    assert problems(*flat, "evolver.params={model: fake/m-1, effort: xhigh}") == []
    assert problems("model=fake/m-1", *layered, "evolver.dsh={effort: high}") == []    # the run's model, its own
    assert problems(*layered, "evolver.model=fake/m-1",                                    # what the Evolver uses
                    "evolver.dsh={permission_mode: danger-full-access, instruction_files: false}") == []
    for more, said in (((*layered, "evolver.model={ref: fake/m-1, effort: ultra}"), "is one of off, minimal, low"),
                       ((*flat, "evolver.params={model: fake/m-1, effort: ultra}"), "is one of off, minimal, low"),
                       ((*flat, "evolver.params={effort: high}"), "evolver.model.effort needs evolver.model"),
                       ((*flat, "evolver.params={model: deepseek-v4-flash, effort: medium}"),
                        "is one of off, low, high, max for a model of dsh's own provider"),
                       ((*layered, "evolver.model=fake/m-1", "evolver.dsh={permission_mode: workspace-write}"),
                        "evolver.dsh.permission_mode is not taken"),
                       ((*flat, "evolver.params={model: fake/m-1, instruction_files: true}"),
                        "evolver.dsh.instruction_files is not taken")):
        assert any(said in problem for problem in problems(*more)), more
    flat_ultra = load("agent.model=fake/m-1", *flat, "evolver.params={model: fake/m-1, effort: ultra}")
    lines = []
    assert run_check(flat_ultra, probe=False, log=lines.append) is False
    at = lines.index("[PROBLEM] evolver")
    assert "evolver.model.effort is one of off, minimal, low, medium, high, xhigh, max" in lines[at + 1]
    with pytest.raises(SystemExit, match="train refuses to start: evolver: evolver.model.effort is one of off, min"):
        Trainer(flat_ultra, log=lambda *_: None)._prepare()
    lines.clear()
    run_check(load("agent.model=fake/m-1", "model=fake/m-1", *layered, "evolver.dsh={effort: high}"), probe=False,
              log=lines.append)
    assert any(line.startswith("[ok] evolver — dsh") for line in lines)


def test_a_model_that_takes_images_is_declared_so_to_dsh(fake):
    """dsh's pi-ai route offers a model it does not know text only, as Pi does: `image_input` (agent.dsh or
    evolver.dsh) declares the bound model's images, through the acting agent's and the Evolver's settings."""
    from hermit.agent.acting.workspace import base_agent
    from hermit.agent.evolver.loader import find, instantiate
    from hermit.config import layered
    data = {"agent": {"base": "dsh", "model": "fake/m-1", "dsh": {"image_input": True}},
            "evolver": {"name": "dsh", "base": "dsh", "model": "fake/m-1", "dsh": {"image_input": True}}, "llm": None}
    layered(data)
    assert base_agent("dsh", {**data["agent"]["params"], **bound(), "web_search": False}).settings["image_input"]
    _, params = instantiate(find("dsh", ROOT), data["evolver"]["params"], default_model="fake/m-1",
                            backends=registry({"fake": {"base_url": "http://127.0.0.1:9/v1", "apis": ["chat"]}}))
    assert params["image_input"] is True
    work = fake / "work"
    work.mkdir()
    for image_input, declared in ((True, ["text", "image"]), (False, None)):
        make("dsh", {**bound(), "web_search": False, "home": str(fake / "home"), "image_input": image_input}).run(
            "go", workspace=work)
        route = row(rows(seen(fake)[-1], fake / "home"), "llm-pi-ai")["config"]["providers"][base_cli.DSH_PROVIDER]
        assert route["models"][0].get("input") == declared


def test_system_instructions_go_at_the_top_of_the_prompt(fake):
    """dsh's persona rows interpolate `{{name}}`, which instructions may hold: they are given in the prompt."""
    work = fake / "work"
    work.mkdir()
    agent = make("dsh", {**bound(), "web_search": False, "home": str(fake / "home")})
    own, prompt = agent.system_settings("Fill {{name}} in.", "go")
    assert own == {} and prompt.startswith("# SYSTEM INSTRUCTIONS\n\nFill {{name}} in.\n\n# END") and \
        prompt.endswith("\n\ngo")
    agent.configured(stdin=True).run(prompt, workspace=work)
    call = seen(fake)[0]
    assert call["args"][-1] == "-" and call["stdin"] == prompt


def test_the_learners_sessions_take_no_instruction_file_and_every_transcript_is_plain_text(fake, tmp_path):
    """dsh adds a folder's AGENTS.md (or CLAUDE.md) when its file tools touch a file there: the learner, which reads
    and edits an adapter whose AGENTS.md is the acting agent's policy, would take that policy, or its own text, for
    instructions (Codex's learner is given no such file). The dsh Evolver's sessions take none. dsh keeps its
    transcripts zstd-compressed, which hermit.redact cannot scrub a key from: every session keeps them in plain JSON
    lines, under dsh's own root for them (its home's sessions folder, as dsh sees it in a sandbox too)."""
    from hermit.agent.evolver.agent import AgentEvolver

    class Ctx:
        params = {**bound(), "web_search": False}
        sandbox = cancel = None
        workspace = type("W", (), {"state": tmp_path / "state", "output": tmp_path / "output"})
    settings = AgentEvolver().settings(Ctx(), "dsh")
    assert settings["instruction_files"] is False and "instruction_files" not in AgentEvolver().settings(Ctx(), "pi")
    work = fake / "work"
    work.mkdir()
    for given, budget in (({}, 65536), ({"instruction_files": False}, 0)):
        make("dsh", {**bound(), "web_search": False, "home": str(fake / "home"), **given}).run("go", workspace=work)
        layer = rows(seen(fake)[-1], fake / "home")
        assert row(layer, "agent-instructions")["config"]["maxBytes"] == budget
        assert row(layer, "session-persistence-jsonl")["config"] == {"root": {"!!js": "dshHomePath('sessions')"},
                                                                     "compression": "none"}


def test_the_workspace_is_the_sessions_project_wherever_it_lies(fake):
    """dsh takes the nearest folder above with a .git for the project: a run's workspace inside a repository would
    get the repository's skills (.dsh/skills, .agents/skills) and instructions, which outrank the adapter's, and lose
    its own skills. The session's layer gives dsh the workspace's .dsh/skills alone, and no root marker, so its
    instructions are the working directory's: none above it, a repository's or a deployment's (any host's deployment
    leaves a .hermit-deploy.json, which a run without an adapter below it took for its root), as the run's sandbox
    shows nothing above the workspace."""
    work = fake / "work"
    work.mkdir()
    make("dsh", {**bound(), "web_search": False, "home": str(fake / "home")}).run("go", workspace=work)
    layer = rows(seen(fake)[0], fake / "home")
    assert row(layer, "skill-filesystem")["config"] == {"includeDefaultRoots": False, "customSkillDirs": [".dsh/skills"]}
    assert row(layer, "agent-instructions")["config"] == {"maxBytes": 65536, "projectRootMarkers": []}


def test_what_dsh_cannot_use_is_refused_rather_than_dropped(fake, monkeypatch, tmp_path):
    """An effort without a model reaches no model (it is the default model's reasoningEffort), and dsh's own provider
    (a model of no binding, DeepSeek's API) takes fewer levels than a bound model's route; a writable path dsh's
    sandbox cannot take (a link, a missing folder) failed every command and write of the session: dsh requires an
    existing directory by its real path, which the session is given. The user's own dsh home is where its .env is
    copied from."""
    work = fake / "work"
    work.mkdir()
    with pytest.raises(ValueError, match="an effort needs its model"):
        make("dsh", {"effort": "high", "web_search": False, "home": str(fake / "home")}).run("go", workspace=work)
    with pytest.raises(ValueError, match="an effort is one of off, minimal, low, medium, high, xhigh, max"):
        make("dsh", {**bound(), "effort": "ultra", "web_search": False, "home": str(fake / "home")}).run(
            "go", workspace=work)
    with pytest.raises(ValueError, match="an effort is one of off, low, high, max for a model of its own provider"):
        make("dsh", {"model": "deepseek-v4-flash", "effort": "medium", "web_search": False,
                     "home": str(fake / "home")}).run("go", workspace=work)
    make("dsh", {"model": "deepseek-v4-flash", "effort": "max", "web_search": False, "home": str(fake / "home")}).run(
        "go", workspace=work)
    assert row(rows(seen(fake)[-1], fake / "home"), "agent-default-model")["config"] == {
        "provider": base_cli.DSH_ROUTE, "model": "deepseek-v4-flash", "reasoningEffort": "max"}
    make("dsh", {**bound(), "effort": False, "web_search": False, "home": str(fake / "home")}).run(
        "go", workspace=work)                                      # YAML's bare `off`
    [model] = row(rows(seen(fake)[-1], fake / "home"), "llm-pi-ai")["config"]["providers"][base_cli.DSH_PROVIDER][
        "models"]
    assert model["reasoningEfforts"] is False
    real = tmp_path / "data"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    agent = make("dsh", {**bound(), "web_search": False, "home": str(fake / "home")})
    agent.configured(writable=[str(tmp_path / "link")]).run("go", workspace=work)
    args = seen(fake)[-1]["args"]
    assert args[args.index("--add-dir") + 1] == os.path.realpath(real)
    with pytest.raises(ValueError, match="a writable path is an existing directory"):
        agent.configured(writable=[str(tmp_path / "missing")]).run("go", workspace=work)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "users-dsh"))
    assert base_cli.Dsh.user_home() == tmp_path / "users-dsh"


def test_a_tasks_data_is_bound_in_the_workspace_where_bubblewrap_can_bind_it(fake, monkeypatch, tmp_path):
    """dsh's glob and grep run ripgrep without --follow: without isolation a task's data inside the workspace is
    bound there, a real read-only directory that a search walks into (as for Claude Code), not linked."""
    from hermit.agent.base.interface import Limits, Mount
    if base_cli._bwrap_binds() is None:
        pytest.skip("needs a working bubblewrap")
    data = tmp_path / "data"
    (data / "deep").mkdir(parents=True)
    (data / "deep" / "a.txt").write_text("1934 557\n")
    work = fake / "work"
    work.mkdir()
    monkeypatch.setenv("FAKE_DSH_RUN", 'find docs -name "*.txt" > found.txt; [ -L docs ] && echo link >> found.txt')
    result = make("dsh", {**bound(), "web_search": False, "home": str(fake / "home")}).run(
        "go", workspace=work, limits=Limits(readable=[Mount(data, Path("docs"))]))
    assert result.extra["mounts"] == {str(work / "docs"): "bound"}
    assert (work / "found.txt").read_text().split() == ["docs/deep/a.txt"]


def test_a_run_whose_tools_did_not_start_has_failed_although_dsh_completed_it(fake, monkeypatch):
    """dsh reports an MCP row it could not start on stderr and runs on without its tools (recorded: tools_down.jsonl,
    a server dsh could not reach, exit 0): the session ran without what it was given, so it has failed."""
    monkeypatch.setenv("FAKE_DSH_STREAM", str(STREAMS / "tools_down.jsonl"))
    work = fake / "work"
    work.mkdir()
    result = make("dsh", {**bound(), "web_search": False, "home": str(fake / "home")}).run("go", workspace=work,
                                                                                         tools=[echo()])
    assert result.exit_code == 0 and result.extra["served"] and result.extra["failed"]
    assert result.extra["failure"].startswith("mcp-hermit_tools did not start, so the session ran without its tools")


def test_a_run_dsh_refused_to_start_is_no_session(fake, monkeypatch):
    """A usage error is an `error` event before any session (recorded: bad_option.jsonl), and a .env in the working
    directory that sets a name dsh keeps for its launch stops it before any event (dotenv.jsonl): neither started
    a session (the usage error's event read as one), so the acting agent reports a CLI that exited before starting
    one (each task's error record says so), rather than a session that failed."""
    from hermit.agent.acting import workspace as acting
    monkeypatch.setenv("FAKE_DSH_EXIT", "1")
    work = fake / "work"
    work.mkdir()
    agent = make("dsh", {**bound(), "web_search": False, "home": str(fake / "home")})
    for stream, says in (("bad_option.jsonl", "unknown option"), ("dotenv.jsonl", 'sets "PYTHONPATH"')):
        monkeypatch.setenv("FAKE_DSH_STREAM", str(STREAMS / stream))
        result = agent.run("go", workspace=work)
        assert result.exit_code == 1 and not result.extra["started"] and says in result.extra["output_tail"]
    monkeypatch.setenv("FAKE_DSH_STREAM", str(STREAMS / "bad_option.jsonl"))
    with pytest.raises(RuntimeError, match="before starting a session"):
        acting.run({"id": "q1"}, None, params={**bound(), "backend": "dsh", "web_search": False, "prompt": "go",
                                               "workspaces": "ws"}, base=fake)


def test_dsh_permission_mode_reaches_the_acting_agents_session_or_is_refused(fake):
    """agent.dsh.permission_mode picks dsh's own sandbox for the acting agent (it was dropped, so the session always
    ran workspace-write); a mode dsh has not is refused when the call is checked."""
    from hermit.agent.acting import workspace as acting
    from hermit.config import layered
    data = {"agent": {"base": "dsh", "model": "fake/m-1", "dsh": {"permission_mode": "danger-full-access"},
                      "params": {"prompt": "go"}},
            "evolver": {"name": "dsh", "base": "dsh", "model": "fake/m-1"}, "llm": None}
    layered(data)
    params = {**data["agent"]["params"], **bound(), "backend": "dsh", "web_search": False, "workspaces": "ws"}
    acting.run({"id": "q1"}, None, params=params, base=fake)
    args = seen(fake)[-1]["args"]
    assert args[args.index("--permission-mode") + 1] == "danger-full-access"
    with pytest.raises(ValueError, match="permission_mode is one of read-only, workspace-write"):
        acting._check("dsh", {**params, "permission_mode": "bypassPermissions"})


def test_a_dsh_stream_is_read_back_by_its_name_or_by_its_events(tmp_path):
    """The learning material keeps the acting agent's log: an Evolver reads dsh's steps from it like Codex's."""
    named = tmp_path / "log"
    named.mkdir()
    shutil.copy(STREAMS / "success.jsonl", named / trajectory.DSH_STREAM)
    other = tmp_path / "events.jsonl"
    shutil.copy(STREAMS / "success.jsonl", other)
    for log in (named, other):
        assert [s.kind for s in trajectory.log_steps(log) if s.kind != "reasoning"] == \
            ["command", "command", "tool_call", "file_change", "file_change", "message"]
    raw = (STREAMS / "failure.jsonl").read_text()
    assert not base_cli.served("dsh", raw) and base_cli.notices("dsh", raw).startswith("dsh: SERVER: 503: ")
    assert base_cli.served("dsh", (STREAMS / "success.jsonl").read_text())


def test_a_bash_result_is_read_as_dsh_marks_its_end():
    """dsh marks a nonzero exit (`[exit code: N]`) or a signal after a command's output and nothing after a clean
    exit; `(no output)` stands for an empty output, also before a marker. A command stopped by its timeout has no
    exit code to trust, even one that trapped the signal and exited 0 (it read as a clean exit before), as a
    signal's has none: unknown, as Codex reports it. dsh prints the timeout it applied (the model's, else 60 s, at
    most 600 s), which need not be a whole number of milliseconds (`1000.5ms`, read as a clean exit before)."""
    stream = [json.loads(line) for line in (STREAMS / "bash.jsonl").read_text().splitlines()]
    commands = [(s.command, s.exit_code, s.output) for s in trajectory.dsh_steps(stream) if s.kind == "command"]
    assert commands == [("exit 3", 3, ""), ("printf ''", 0, ""), ("true", 0, ""), ("echo out; exit 4", 4, "out"),
                        ("kill -9 $$", None, "(no output)\n[killed by signal: SIGKILL]"),
                        ("printf 'no newline'; exit 5", 5, "no newline"),
                        ("trap 'exit 0' TERM; sleep 5 & wait", None, "(no output)\n[timed out after 1000ms]"),
                        ("sleep 5", None, "(no output)\n[timed out after 1000ms]\n[killed by signal: SIGTERM]"),
                        ("trap 'exit 0' TERM; sleep 5 & wait", None, "(no output)\n[timed out after 1000.5ms]")]


def test_a_command_whose_end_dsh_did_not_report_has_no_exit_code():
    """dsh's --json keeps 8 KiB of a string: a long result loses its end, where the marker is (recorded:
    big_bash.jsonl, 1,500 lines then exit 2), and read as exit 0 before; a command started in the background
    (background.jsonl) has no status yet, nor has one still running at its timeout (60 s by default), which dsh moves
    to a background job in a session unless its job limit is reached (promoted.jsonl): only DSH_BACKGROUND keeps it
    from reading as exit 0, also when its marker is the whole result (the command printed nothing yet) and when the
    timeout is no whole number of milliseconds (`1000.5ms`, read as exit 0 before). A command stopped from outside
    the call (the job stopped in dsh's UI) is marked `[stopped: <reason>]` before its signal or exit marker, and may
    have trapped the signal and exited 0: no headless session writes one, so its results are written here as dsh's
    bash tool renders them (tool-bash's render.ts, dsh 0.2.2-rc.1). A command that printed a marker of its own and
    exited 0 (fake_marker.jsonl) is a clean exit, as dsh reads it: the marker counts only at the very end. A call
    that failed to run (a result dsh marks `error`) has no exit status either, as dsh's own view of a bash result has
    none for it."""
    def commands(name):
        stream = [json.loads(line) for line in (STREAMS / name).read_text().splitlines() if line.startswith("{")]
        return [s for s in trajectory.dsh_steps(stream) if s.kind == "command"]

    def command(result, status="completed"):
        [step] = trajectory.dsh_steps([
            {"type": "tool_call", "callId": "call_0", "tool": "bash", "input": {"command": "make"}},
            {"type": "tool_result", "callId": "call_0", "status": status, "result": result}])
        return step
    [cut] = commands("big_bash.jsonl")
    assert cut.exit_code is None and cut.output.startswith("line 1 of a long build log\n")
    assert cut.output.endswith(trajectory.DSH_CUT) and "[exit code" not in cut.output
    [background] = commands("background.jsonl")
    assert background.exit_code is None and background.output.startswith("started background job ")
    [building, silent, fraction] = commands("promoted.jsonl")
    for step, start in ((building, "building\n[still running after 1000ms"), (silent, "[still running after 1000ms"),
                        (fraction, "building\n[still running after 1000.5ms")):
        assert step.exit_code is None and step.output.startswith(f"{start}; moved to background job bash-"), start
    for stopped in ("(no output)\n[stopped: cancelled by the user]",
                    "cleaning up\n[stopped: cancelled by the user]\n[exit code: 143]"):
        assert command(stopped).exit_code is None, stopped
    [printed] = commands("fake_marker.jsonl")
    assert (printed.exit_code, printed.output) == (0, "built\n[exit code: 7]\n")
    unrun = command("bash could not run", status="error")
    assert (unrun.kind, unrun.exit_code, unrun.output) == ("command", None, "bash could not run")


def test_a_session_is_resumed_only_from_a_directory_of_the_home_itself(tmp_path):
    """dsh keeps a session at sessions/<its directory>/<id>/ in its home, which the session's commands may write: a
    link there (in place of the session's folder, or of the folder it lies in) is no session to resume, so a call
    starts a new one; so is a handle that is no session id."""
    home, elsewhere = tmp_path / "home", tmp_path / "elsewhere"
    (home / "sessions" / "project" / SESSION).mkdir(parents=True)
    (elsewhere / SESSION).mkdir(parents=True)
    assert base_cli._dsh_session(home, SESSION) == SESSION
    assert base_cli._dsh_session(home, f"../{SESSION}") is None
    linked = tmp_path / "linked"
    (linked / "sessions" / "project").mkdir(parents=True)
    (linked / "sessions" / "project" / SESSION).symlink_to(elsewhere / SESSION)
    assert base_cli._dsh_session(linked, SESSION) is None
    parent = tmp_path / "parent"
    (parent / "sessions").mkdir(parents=True)
    (parent / "sessions" / "project").symlink_to(elsewhere)
    assert base_cli._dsh_session(parent, SESSION) is None


def without_usage(stream: str, tmp_path: Path) -> Path:
    """A recorded stream whose steps report no usage, as dsh writes a step one of whose attempts reported none (its
    json-stream.ts omits the partial sum): a stream that ended before its last event, a failed request of dsh's own
    provider."""
    out = tmp_path / f"no-usage-{stream}"
    out.write_text("".join(json.dumps({k: v for k, v in json.loads(line).items() if k != "usage"}) + "\n"
                           if '"step_end"' in line else line + "\n"
                           for line in (STREAMS / stream).read_text().splitlines()))
    return out


def test_a_step_counts_as_a_call_whenever_it_made_one(tmp_path):
    """A step whose requests all failed produced nothing and is no call (failure.jsonl), also when dsh reports no
    usage for it; one that answered is a call even when the provider reported no usage, which dsh reports as zeros
    (no_usage.jsonl), and so is one dsh reports no usage for at all. A step dsh retried after the provider refused a
    request (retried.jsonl: a 503, then 1000 prompt tokens, 900 of them cached) reports its input and output but not
    its cache counts, which its total holds: those tokens count as input, and the call as one whose tokens are known.
    parse_log reads a dsh stream as dsh's (it read one as Claude Code's)."""
    from hermit.agent.base.records import parse_dsh, parse_log
    assert parse_dsh(STREAMS / "failure.jsonl", "m")[1] == []
    assert parse_dsh(without_usage("failure.jsonl", tmp_path), "m")[1] == []
    _, [answered] = parse_dsh(STREAMS / "no_usage.jsonl", "m")
    assert (answered.calls, answered.unreported_calls, answered.input_tokens, answered.output_tokens) == (1, 1, 0, 0)
    session, [resumed] = parse_log(STREAMS / "resume.jsonl", "dsh", "m")
    assert session == SESSION and (resumed.calls, resumed.unreported_calls, resumed.input_tokens) == (1, 0, 1000)
    [unknown] = parse_dsh(without_usage("resume.jsonl", tmp_path), "m")[1]
    assert (unknown.calls, unknown.unreported_calls, unknown.input_tokens) == (1, 1, 0)
    [retried] = parse_dsh(STREAMS / "retried.jsonl", "m")[1]
    assert (retried.calls, retried.unreported_calls, retried.input_tokens, retried.cached_input_tokens,
            retried.output_tokens) == (1, 0, 1000, 0, 10)




def test_a_step_is_a_call_when_it_called_a_tool_or_thought_and_none_when_its_requests_then_failed(tmp_path):
    """Which steps whose tokens dsh did not report are calls, from the recorded streams: a step whose only output is a
    tool call (bash.jsonl's nine, recorded without reasoning) is one, and so is a step whose only output is a
    thought (resume.jsonl's step without its reply); a step whose requests all failed (failure.jsonl's) after a step
    that produced something is none: what a step produced does not carry over to the next."""
    from hermit.agent.base.records import parse_dsh

    def stream(name: str, events: list[dict]) -> Path:
        path = tmp_path / name
        path.write_text("".join(json.dumps(e) + "\n" for e in events))
        return path

    def read(name: str) -> list[dict]:
        return [json.loads(line) for line in (STREAMS / name).read_text().splitlines() if line.startswith("{")]
    [tools] = parse_dsh(without_usage("bash.jsonl", tmp_path), "m")[1]
    assert (tools.calls, tools.unreported_calls) == (10, 10)
    resume = read("resume.jsonl")
    thought = [{k: v for k, v in e.items() if k != "usage"} for e in resume if e.get("type") != "text"]
    [thinking] = parse_dsh(stream("thought.jsonl", thought), "m")[1]
    assert (thinking.calls, thinking.unreported_calls) == (1, 1)
    end = next(i for i, e in enumerate(resume) if e.get("phase") == "step_end")
    failed = resume[:end + 1] + [e for e in read("failure.jsonl") if e.get("phase") in ("step_start", "step_end")]
    [after] = parse_dsh(stream("failed_after.jsonl", failed), "m")[1]
    assert (after.calls, after.unreported_calls, after.input_tokens) == (1, 0, 1000)


def test_calls_whose_tokens_were_not_reported_have_an_unknown_cost_not_a_free_one(fake, monkeypatch, tmp_path):
    """dsh reports no cost, so the backend's price makes one from the tokens. A session with a call whose tokens it
    did not report (a provider that reports no usage, an attempt that reported none) has an unknown cost (None), not
    $0.00 nor the price of the rest, which may be far less (the successful attempt of a step whose usage dsh left out
    goes unreported too): the run's budget counts its calls as of unknown cost, and none of its dollars. A session
    whose tokens dsh reported is priced from them; a step whose cache counts dsh left out after a retry is priced
    with those tokens as input, as a cache write is, never less than with the counts (the cached input being no
    dearer than the input), so that a USD budget counts the session. Only dsh's reader marks a call unreported:
    another CLI's call of no tokens (Claude Code's result of a refused request) keeps its price, $0.00."""
    from hermit.trainer.budget import Budget
    from hermit.agent.base.records import price_usage
    from hermit.agent.model.usage import Usage
    work = fake / "work"
    work.mkdir()
    agent = make("dsh", {**bound(), "web_search": False, "home": str(fake / "home")})
    partial = tmp_path / "partial.jsonl"
    lines = (STREAMS / "success.jsonl").read_text().splitlines()
    first = next(i for i, line in enumerate(lines) if '"step_end"' in line)
    lines[first] = json.dumps({k: v for k, v in json.loads(lines[first]).items() if k != "usage"})
    partial.write_text("\n".join(lines) + "\n")                  # one of six steps without usage
    for stream, tokens in ((STREAMS / "no_usage.jsonl", 0), (without_usage("success.jsonl", tmp_path), 0),
                           (partial, 5000)):
        monkeypatch.setenv("FAKE_DSH_STREAM", str(stream))
        [usage] = agent.run("go", workspace=work).usage
        assert usage.unreported_calls and usage.input_tokens == tokens and usage.cost_usd is None
    refused = [Usage(model="claude-x", calls=1)]
    price_usage(refused, {"price": {"input": 1.0, "output": 2.0}})
    assert refused[0].cost_usd == 0.0
    monkeypatch.setenv("FAKE_DSH_STREAM", str(STREAMS / "retried.jsonl"))
    [retried] = agent.run("go", workspace=work).usage
    assert not retried.unreported_calls and retried.cost_usd == pytest.approx((1000 * 1.0 + 10 * 2.0) / 1e6)
    budget = Budget(usd=0.001)
    budget.add("rollout", retried)
    assert budget.exceeded() and budget.unknown_cost_calls == 0
    monkeypatch.setenv("FAKE_DSH_STREAM", str(STREAMS / "resume.jsonl"))
    [usage] = agent.run("go", workspace=work).usage
    assert usage.input_tokens and usage.cost_usd == pytest.approx(
        ((usage.input_tokens - usage.cached_input_tokens) * 1.0 + usage.cached_input_tokens * 0.1
         + usage.output_tokens * 2.0) / 1e6)


def test_a_model_whose_backend_declares_no_context_window_fails_with_what_to_declare(fake, monkeypatch):
    """A model dsh's catalog does not know is taken for 1,000,000 tokens, asked for up to 128,000 output tokens at a
    time: a server sized below that (vLLM refuses a request past its model's length) fails every request (recorded:
    context_window.jsonl). The failure says to declare the backend's context_window, which dsh then sizes requests
    by; a backend that declared one gets dsh's error alone."""
    monkeypatch.setenv("FAKE_DSH_STREAM", str(STREAMS / "context_window.jsonl"))
    monkeypatch.setenv("FAKE_DSH_EXIT", "1")
    work = fake / "work"
    work.mkdir()
    said = base_cli.notices("dsh", (STREAMS / "context_window.jsonl").read_text())   # its error, twice
    for backend, hinted in (({}, True), ({"context_window": 32768}, False)):
        result = make("dsh", {**bound(**backend), "web_search": False, "home": str(fake / "home")}).run(
            "go", workspace=work)
        assert result.extra["failed"] and "CONTEXT_WINDOW_EXCEEDED" in result.extra["failure"]
        assert said.splitlines()[0] in result.extra["failure"]          # dsh's error line (this one fits whole)
        assert ("declare the backend's context_window" in result.extra["failure"]) is hinted
        assert result.usage == []                                  # the refused requests used nothing


def test_a_run_that_ends_at_the_token_limit_is_an_error_the_learner_can_read():
    """dsh exits 1 for a run that ends `max-tokens` and writes nothing on stderr: the run is no success and no
    outage, and its end is a step of the trajectory and a notice (only errors and aborts were, before)."""
    raw = (STREAMS / "max_tokens.jsonl").read_text()
    assert not base_cli.served("dsh", raw) and base_cli.notices("dsh", raw) == "run max-tokens"
    assert not base_cli.Dsh.outage(raw)
    steps = trajectory.dsh_steps([json.loads(line) for line in raw.splitlines()])
    assert [(s.kind, s.text) for s in steps] == [("message", "partial answ"), ("error", "run max-tokens")]


def test_a_server_that_needs_no_key_gets_the_placeholder_key(fake):
    """A route that names no key variable gets no key from pi-ai under the name `hermit`, and dsh refuses every
    request (PI_AI_ERROR "No API key"): the session names a variable that holds the placeholder key instead."""
    work = fake / "work"
    work.mkdir()
    params = bound(api_key_env="NO_SUCH_KEY_VAR", key_required=False)
    make("dsh", {**params, "web_search": False, "home": str(fake / "home")}).run("go", workspace=work)
    [call] = seen(fake)
    route = row(rows(call, fake / "home"), "llm-pi-ai")["config"]["providers"][base_cli.DSH_PROVIDER]
    assert route["apiKeyEnv"] == base_cli.DSH_NO_KEY and call["env"][base_cli.DSH_NO_KEY] == "local"


def test_the_dsh_acting_agent_deploys_the_adapter_and_loads_its_patch_layer(fake):
    """dsh has no project file for MCP servers: the deployment's patch layer (.dsh/hermit.patch.yml) declares the
    adapter's tool server, and the session loads it with --patch, before its own layer, which wins."""
    from hermit.agent.acting import workspace as acting
    params = {**bound(), "backend": "dsh", "web_search": False, "prompt": "go", "workspaces": "ws"}
    out = acting.run({"id": "q1"}, FILE_ADAPTER, params=params, base=fake)
    work = Path(out.extra["workspace"])
    [call] = seen(fake)
    patches = [call["args"][i + 1] for i, a in enumerate(call["args"]) if a == "--patch"]
    assert patches == [str(work / ".dsh" / "hermit.patch.yml"), str(work.parent / "home" / base_cli.DSH_PATCH)]
    assert call["args"].index("--json") > call["args"].index(patches[-1])  # the launcher's options come first
    layer = json.loads((work / ".dsh" / "hermit.patch.yml").read_text())
    [server] = [r["insert"][0] for r in layer if "insert" in r]
    assert server["config"]["transport"] == "stdio" and server["config"]["args"][:2] == ["-m",
                                                                                         "hermit.runtime.mcp_server"]
    assert (work / ".dsh" / "skills" / "verify-output" / "SKILL.md").is_file()
    assert (work / ".hermit-deploy.json").is_file()                   # the deployment's record
    assert "do it yourself" in (work / "AGENTS.md").read_text()       # roles stay files: none is passed to dsh
    assert out.output == "the answer" and out.extra["backend"] == "dsh"
    assert (out.log / "agent-dsh.jsonl").is_file()


def test_dsh_is_a_base_of_the_config_and_the_dsh_evolver_binds_its_model_with_its_sandbox_off():
    """In the evolver sandbox the run's isolation is the boundary, so dsh's own sandbox is off, as Codex's is."""
    from hermit.agent.evolver.agent import AgentEvolver
    from hermit.agent.evolver.loader import find, instantiate
    from hermit.config import layered
    data = {"agent": {"base": "dsh", "model": {"ref": "fake/m-1", "effort": "high"}, "dsh": {"timeout_s": 60},
                      "params": {"prompt": "go"}},
            "evolver": {"name": "dsh", "base": "dsh", "model": "fake/m-1", "params": {}}, "llm": None}
    layered(data)
    assert data["agent"]["name"] == "dsh" and data["agent"]["params"]["effort"] == "high"
    assert data["agent"]["params"]["timeout_s"] == 60 and data["evolver"]["params"]["backend"] == "dsh"
    spec = find("dsh", ROOT)
    backends = registry({"fake": {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "FAKE_KEY", "apis": ["chat"]}})
    _, params = instantiate(spec, data["evolver"]["params"], backends=backends, default_model="fake/m-1")
    assert params["backend"] == "dsh" and params["home"] == [] and params["provider"]["apiKeyEnv"] == "FAKE_KEY"

    class Ctx:                                  # the fields AgentEvolver.settings reads
        def __init__(self, root):
            self.params, self.sandbox, self.cancel = params, None, None
            self.workspace = type("W", (), {"state": root / "state", "output": root / "output"})
    settings = AgentEvolver().settings(Ctx(Path("/w")), "dsh")
    assert settings["permission_mode"] == "danger-full-access" and settings["provider"] == params["provider"]
    assert "permission_mode" not in AgentEvolver().settings(Ctx(Path("/w")), "claude")


# ---------- the installed dsh (integration) ----------

def serve_chat(script, keys=None):
    """A chat-completions model on the loopback: `script(request body) -> (tool call (name, arguments) | None)`;
    None answers `the answer`. Every response is streamed, with usage. `keys` collects each request's
    Authorization header."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def chunk(body):
        return f"data: {json.dumps(body)}\n\n".encode()

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            if keys is not None:
                keys.append(self.headers.get("authorization"))
            call = script(body)
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                          "function": {"name": call[0],
                                                                       "arguments": json.dumps(call[1])}}]} \
                if call else {"role": "assistant", "content": "the answer"}
            data = b"".join([chunk({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}),
                             chunk({"choices": [{"index": 0, "delta": {}, "finish_reason":
                                                 "tool_calls" if call else "stop"}]}),
                             chunk({"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 5}}),
                             b"data: [DONE]\n\n"])
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


def _bwrap_works() -> bool:
    return bool(shutil.which("bwrap")) and subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--unshare-net", "true"], capture_output=True).returncode == 0


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("dsh"), reason="needs the dsh CLI")
@pytest.mark.parametrize("isolation", ["none", "sandbox"])
def test_with_the_dsh_cli_a_session_reaches_its_model_and_its_tools(tmp_path, monkeypatch, isolation):
    """The installed dsh, bound to a scripted model: it calls the call's MCP tool, writes a file, answers; in the
    run's sandbox (isolated network) only its model and the tool server are reached."""
    from hermit.sandbox import Sandbox
    if isolation == "sandbox" and not _bwrap_works():
        pytest.skip("needs a working bubblewrap")
    requests = []

    def script(body):
        requests.append(body)
        done = sum(1 for m in body.get("messages") or [] if m.get("role") == "tool")
        names = [t["function"]["name"] for t in body.get("tools") or []]
        plan = [("mcp__hermit_tools__echo", {"text": "hi"}), ("write", {"file_path": "note.txt", "content": "ok\n"})]
        assert plan[0][0] in names and "web_search" not in names     # the tool offered as dsh's own; no web tools
        return plan[done] if done < len(plan) else None

    server, url = serve_chat(script)
    monkeypatch.setenv("MOCK_KEY", "sk-mock")
    params = bind_agent_params({"model": "mock/m-1"}, "dsh",
                               registry({"mock": {"base_url": url, "api_key_env": "MOCK_KEY", "apis": ["chat"]}}))
    work = tmp_path / "work"
    work.mkdir()
    settings = {**params, "web_search": False, "home": str(tmp_path / "home"), "isolation": isolation,
                "timeout_s": 300}
    if isolation == "sandbox":
        settings["sandbox"] = Sandbox(network="isolated", env_allow=[], writable=[work], role="agent",
                                      record_dir=tmp_path / "records")
    try:
        result = make("dsh", settings).run("Do the task.", workspace=work, tools=[echo()])
    finally:
        server.shutdown()
    assert result.exit_code == 0 and result.extra["served"] and result.reply == "the answer"
    assert (work / "note.txt").read_text() == "ok\n"
    assert [s.kind for s in result.trajectory] == ["tool_call", "file_change", "message"]
    assert json.loads(result.trajectory[0].output) == {"echoed": "hi"}
    assert result.usage[0].calls == 3 == len(requests)          # no request besides the session's own
    if isolation == "sandbox":
        [record] = (tmp_path / "records").glob("agent-*.json")
        data = json.loads(record.read_text())
        assert data["network"] == "isolated" and data["probe"]["passed"]
        assert {"DSH_HOME", "HOME", "MOCK_KEY"} <= set(data["environment"])


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("dsh"), reason="needs the dsh CLI")
def test_with_the_dsh_cli_a_command_runs_in_dshs_own_sandbox_inside_the_runs(tmp_path, monkeypatch):
    """The acting agent keeps dsh's own sandbox (workspace-write) inside the run's: dsh starts it nested in the run's
    bubblewrap sandbox (bubblewrap, else Landlock), and a command of the session writes its workspace there, not the
    session's home, which the run's sandbox lets the session write (the home lies outside /tmp, which workspace-write
    lets a command write). Where neither can start, dsh refuses every command (SANDBOX_UNAVAILABLE), which this would
    show."""
    from hermit.sandbox import Sandbox
    if not _bwrap_works():
        pytest.skip("needs a working bubblewrap")
    base = Path.home() / ".cache" / "hermit-tests"        # outside /tmp, an empty tmpfs under bubblewrap
    base.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="dsh-home-", dir=base))
    outside = home / "outside.txt"
    commands = [{"command": "echo made > made.txt && cat made.txt", "description": "Write a file"},
                {"command": f"echo out > {outside}", "description": "Write the home"}]

    def script(body):
        done = sum(1 for m in body.get("messages") or [] if m.get("role") == "tool")
        return ("bash", commands[done]) if done < len(commands) else None

    server, url = serve_chat(script)
    monkeypatch.setenv("MOCK_KEY", "sk-mock")
    params = bind_agent_params({"model": "mock/m-1"}, "dsh",
                               registry({"mock": {"base_url": url, "api_key_env": "MOCK_KEY", "apis": ["chat"]}}))
    work = tmp_path / "work"
    work.mkdir()
    box = Sandbox(network="isolated", env_allow=[], writable=[work], role="agent", record_dir=tmp_path / "records")
    try:
        result = make("dsh", {**params, "web_search": False, "home": str(home), "isolation": "sandbox",
                              "sandbox": box, "timeout_s": 300}).run("Write the files.", workspace=work)
        escaped = outside.exists()
    finally:
        server.shutdown()
        shutil.rmtree(home, ignore_errors=True)
    assert result.exit_code == 0 and result.extra["served"] and not result.extra["failed"]
    [inside, written] = [s for s in result.trajectory if s.kind == "command"]
    assert (inside.exit_code, inside.output) == (0, "made\n") and (work / "made.txt").read_text() == "made\n"
    assert written.exit_code != 0 and not escaped


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("dsh"), reason="needs the dsh CLI")
@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_with_the_dsh_cli_a_deployed_adapter_brings_its_skills_and_tools_into_a_git_repository(tmp_path, monkeypatch):
    """The acting agent with the installed dsh, its workspaces inside a git repository (a config kept in a project,
    run without the sandbox). dsh takes the repository for the project: before, the deployed skill was not offered,
    and the repository's own skills and AGENTS.md were, ahead of the adapter's. Now the session's layer makes the
    workspace the project. The adapter's own tool server (the deployment's stdio server, loaded from its patch layer)
    answers a call."""
    from hermit.agent.acting import workspace as acting
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "AGENTS.md").write_text("REPO-INSTRUCTIONS-MARKER\n")
    for folder in (".dsh", ".agents"):
        (repo / folder / "skills" / "repo-skill").mkdir(parents=True)
        (repo / folder / "skills" / "repo-skill" / "SKILL.md").write_text(
            f"---\nname: repo-skill{folder}\ndescription: REPO-SKILL-MARKER\n---\nbody\n")
    requests = []

    def script(body):
        requests.append(body)
        done = sum(1 for m in body.get("messages") or [] if m.get("role") == "tool")
        return ("mcp__file_example__count_words", {"text": "one two three"}) if done == 0 else None

    server, url = serve_chat(script)
    monkeypatch.setenv("MOCK_KEY", "sk-mock")
    params = {**bind_agent_params({"model": "mock/m-1"}, "dsh",
                                  registry({"mock": {"base_url": url, "api_key_env": "MOCK_KEY", "apis": ["chat"]}})),
              "backend": "dsh", "web_search": False, "prompt": "go", "workspaces": "ws", "timeout_s": 300,
              "retries": 1}
    try:
        out = acting.run({"id": "q1"}, FILE_ADAPTER, params=params, base=repo)
    finally:
        server.shutdown()
    assert out.output == "the answer" and len(requests) == 2
    assert "Check a completed result against the task requirements." in json.dumps(requests[0])   # the skill
    assert "REPO-SKILL-MARKER" not in json.dumps(requests[0]) and "REPO-INSTRUCTIONS-MARKER" not in json.dumps(
        requests[0])                                                  # nothing of the repository the run lies in
    assert "mcp__file_example__count_words" in [t["function"]["name"] for t in requests[0].get("tools") or []]
    [answer] = [m for m in requests[1]["messages"] if m.get("role") == "tool"]
    assert json.loads(answer["content"] if isinstance(answer["content"], str) else
                      "".join(p.get("text", "") for p in answer["content"])) == {"count": 3}


def mock_model(monkeypatch, url: str) -> dict:
    """The params of a `dsh` agent bound to the scripted model at `url`."""
    monkeypatch.setenv("MOCK_KEY", "sk-mock")
    return bind_agent_params({"model": "mock/m-1"}, "dsh",
                             registry({"mock": {"base_url": url, "api_key_env": "MOCK_KEY", "apis": ["chat"]}}))


def tool_texts(body: dict) -> list[str]:
    """The tool results a request carries, as text."""
    return [m["content"] if isinstance(m["content"], str) else "".join(p.get("text", "") for p in m["content"])
            for m in body.get("messages") or [] if m.get("role") == "tool"]


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("dsh"), reason="needs the dsh CLI")
def test_with_the_dsh_cli_a_tasks_data_is_found_by_dshs_own_search(tmp_path, monkeypatch):
    """dsh's glob and grep run ripgrep without --follow, so a task's data linked into the workspace was never listed
    or matched. Without isolation it is bound there, a real directory: dsh's own search finds it, and dsh's own
    sandbox, nested in that mount namespace, runs a command that reads it."""
    from hermit.agent.base.interface import Limits, Mount
    if base_cli._bwrap_binds() is None:
        pytest.skip("needs a working bubblewrap")
    data, work = tmp_path / "data", tmp_path / "work"
    (data / "deep").mkdir(parents=True)
    work.mkdir()
    (data / "deep" / "facts.txt").write_text("1934 557\n")
    last = []

    def script(body):
        last[:] = tool_texts(body)
        plan = [("glob", {"pattern": "**/*.txt"}), ("grep", {"pattern": "1934"}),
                ("bash", {"command": "cat docs/deep/facts.txt", "description": "Read the facts"})]
        return plan[len(last)] if len(last) < len(plan) else None

    server, url = serve_chat(script)
    try:
        result = make("dsh", {**mock_model(monkeypatch, url), "web_search": False, "home": str(tmp_path / "home"),
                              "timeout_s": 300}).run("Find the facts.", workspace=work,
                                                     limits=Limits(readable=[Mount(data, Path("docs"))]))
    finally:
        server.shutdown()
    assert result.extra["served"] and result.extra["mounts"] == {str(work / "docs"): "bound"}
    found, matched, read = last
    assert "docs/deep/facts.txt" in found and "1934 557" in matched and "1934 557" in read
    [command] = [s for s in result.trajectory if s.kind == "command"]
    assert (command.exit_code, command.output) == (0, "1934 557\n")


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("dsh"), reason="needs the dsh CLI")
def test_with_the_dsh_cli_a_deployed_tool_server_that_does_not_start_fails_the_session(tmp_path, monkeypatch):
    """The deployment's tool server is a stdio row of its patch layer: one dsh could not start was retried in the
    background while the session ran on without the adapter's tools, and its answer was taken like any other. The
    row now fails at the session's start, which dsh reports, so the session has failed."""
    from hermit.hosts.dsh import prepare
    work = tmp_path / "ws"
    prepare(FILE_ADAPTER, work, sys.executable)
    patch = work / ".dsh" / "hermit.patch.yml"
    layer = json.loads(patch.read_text())
    [server] = [r["insert"][0] for r in layer if "insert" in r]
    server["config"]["command"] = str(tmp_path / "no-such-python")              # a server that cannot start
    patch.write_text(json.dumps(layer))
    server_http, url = serve_chat(lambda body: None)
    try:
        result = make("dsh", {**mock_model(monkeypatch, url), "web_search": False, "home": str(tmp_path / "home"),
                              "args": ["--patch", str(patch)], "timeout_s": 300}).run("go", workspace=work)
    finally:
        server_http.shutdown()
    assert result.extra["failed"] and result.extra["failure"].startswith("mcp-file_example did not start")


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("dsh"), reason="needs the dsh CLI")
def test_with_the_dsh_cli_no_instructions_above_the_workspace_are_read(tmp_path, monkeypatch):
    """A session whose workspace holds no deployment (a run without an adapter, an Evolver's) below a folder that
    does (a project deployed into with `hermit deploy`, of any host): dsh took that folder, the nearest with a
    .hermit-deploy.json, for the project's root and read its AGENTS.md too. Without a root marker the working
    directory is the root, as the run's sandbox shows nothing above it."""
    above = tmp_path / "deployed"
    work = above / "runs" / "work"
    work.mkdir(parents=True)
    (above / ".hermit-deploy.json").write_text("{}\n")
    (above / "AGENTS.md").write_text("ABOVE-INSTRUCTIONS-MARKER\n")
    (work / "AGENTS.md").write_text("OWN-INSTRUCTIONS-MARKER\n")
    requests = []
    server, url = serve_chat(lambda body: requests.append(body))
    try:
        make("dsh", {**mock_model(monkeypatch, url), "web_search": False, "home": str(tmp_path / "home"),
                     "timeout_s": 300}).run("go", workspace=work)
    finally:
        server.shutdown()
    sent = json.dumps(requests[0])
    assert "OWN-INSTRUCTIONS-MARKER" in sent and "ABOVE-INSTRUCTIONS-MARKER" not in sent


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("dsh"), reason="needs the dsh CLI")
def test_with_the_dsh_cli_a_learners_session_takes_no_instruction_file_and_its_transcript_is_scrubbed(tmp_path,
                                                                                                      monkeypatch):
    """The installed dsh adds a folder's AGENTS.md when its read tool touches a file there; a session with
    instruction_files false (the dsh Evolver's) is given none. Every session's transcript is plain JSON lines in its
    home, so a key a session wrote there is scrubbed (hermit.redact), and the session is resumed from it."""
    from hermit.redact import redact_path
    work = tmp_path / "work"
    (work / "adapter").mkdir(parents=True)
    (work / "adapter" / "AGENTS.md").write_text("NESTED-INSTRUCTIONS-MARKER\n")
    (work / "adapter" / "notes.txt").write_text("the notes\n")
    secret = "sk-mock-0123456789abcdef"
    requests = []

    def script(body):
        requests.append(body)
        return ("read", {"file_path": "adapter/notes.txt"}) if not tool_texts(body) else None

    server, url = serve_chat(script)
    params = mock_model(monkeypatch, url)
    monkeypatch.setenv("MOCK_KEY", secret)
    try:
        for given in ({}, {"instruction_files": False}):
            home = tmp_path / f"home-{len(given)}"
            agent = make("dsh", {**params, "web_search": False, "home": str(home), "timeout_s": 300, **given})
            first = len(requests)
            result = agent.run(f"Read the notes; the key is {secret}.", workspace=work)
            sent = json.dumps(requests[first:])
            assert result.extra["served"] and ("NESTED-INSTRUCTIONS-MARKER" in sent) is not bool(given)
            [transcript] = (home / "sessions").rglob("session.v4.jsonl")
            assert secret in transcript.read_text() and not list((home / "sessions").rglob("*.zstd"))
            redact_path(home)
            assert secret not in transcript.read_text() and "<MOCK_KEY>" in transcript.read_text()
        again = agent.run("Again.", workspace=work, session=result.session)
    finally:
        server.shutdown()
    assert again.extra["served"] and again.session == result.session


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("dsh"), reason="needs the dsh CLI")
def test_with_the_dsh_cli_an_agents_md_at_the_deploy_limit_reaches_the_model_whole(tmp_path, monkeypatch):
    """The deployment refuses an AGENTS.md past what dsh gives the model of a project's instructions, its frame
    included (DSH_INSTRUCTION_FRAME): one at that size reaches the model whole, and one byte more is cut, which dsh
    tells the model in a note of its budget."""
    from hermit.hosts.dsh import prepare
    adapter = tmp_path / "adapter"
    (adapter / ".hermit").mkdir(parents=True)
    (adapter / ".hermit" / "adapter.yaml").write_text("template: {id: edge, version: '1'}\n"
                                                      "structure: {learnable: [exports.yaml, ADAPTER.md]}\n")
    (adapter / "exports.yaml").write_text("{context: ADAPTER.md}\n")
    (adapter / "ADAPTER.md").write_text("LAST-LINE-MARKER\n")
    prepare(adapter, tmp_path / "small", sys.executable)
    rest = len((tmp_path / "small" / "AGENTS.md").read_bytes()) - len("LAST-LINE-MARKER\n")
    limit = base_cli.DSH_INSTRUCTION_BYTES - base_cli.DSH_INSTRUCTION_FRAME
    (adapter / "ADAPTER.md").write_text("x" * (limit - rest - len("\nLAST-LINE-MARKER\n")) + "\nLAST-LINE-MARKER\n")
    prepare(adapter, tmp_path / "edge", sys.executable)
    whole = tmp_path / "edge" / "AGENTS.md"
    assert len(whole.read_bytes()) == limit
    over = tmp_path / "over"
    over.mkdir()
    (over / "AGENTS.md").write_text(whole.read_text().replace("x", "xx", 1))       # one byte more, by hand
    requests = []
    server, url = serve_chat(lambda body: requests.append(body))
    try:
        for work in (tmp_path / "edge", over):
            make("dsh", {**mock_model(monkeypatch, url), "web_search": False, "home": str(tmp_path / "home"),
                         "timeout_s": 300}).run("go", workspace=work)
    finally:
        server.shutdown()
    edge, cut = json.dumps(requests[0]), json.dumps(requests[1])
    assert "LAST-LINE-MARKER" in edge and "Workspace instruction budget" not in edge
    assert "Workspace instruction budget 65536 bytes: truncated AGENTS.md" in cut


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("dsh"), reason="needs the dsh CLI")
def test_with_the_dsh_cli_a_server_that_needs_no_key_is_reached_with_the_placeholder(tmp_path, monkeypatch):
    """A backend that needs no key (a local server): the installed dsh refused every request of a route that named
    no key variable (PI_AI_ERROR "No API key"); the session's placeholder reaches the server, and a key of another
    provider in the environment (OPENAI_API_KEY) is never sent."""
    keys = []
    server, url = serve_chat(lambda body: None, keys)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-another-provider")
    params = bind_agent_params({"model": "local/m-1"}, "dsh", registry(
        {"local": {"base_url": url, "api_key_env": "NO_SUCH_KEY_VAR", "key_required": False, "apis": ["chat"]}}))
    work = tmp_path / "work"
    work.mkdir()
    try:
        result = make("dsh", {**params, "web_search": False, "home": str(tmp_path / "home"),
                              "timeout_s": 300}).run("Answer.", workspace=work)
    finally:
        server.shutdown()
    assert result.exit_code == 0 and result.reply == "the answer" and keys == ["Bearer local"]


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("dsh"), reason="needs the dsh CLI")
def test_the_installed_dsh_has_every_row_the_session_patches(tmp_path):
    """A patch row whose id dsh does not have is ignored, and dsh says so only when asked (--dump-config, which
    starts nothing and reaches no network): every row the session's layer names (telemetry, title, sub-agents, the
    web tools, the route, the default model) is one of the installed dsh's, so none is silently left on."""
    home = tmp_path / "home"
    home.mkdir()
    agent = make("dsh", {**bound(), "effort": "high", "web_search": True})       # web fetch only: a row's settings
    agent.prepare_home(home, (), "http://127.0.0.1:9/mcp", "hermit_tools")
    layer = layer_rows((home / base_cli.DSH_PATCH).read_text())
    assert {r["id"] for r in layer if "id" in r} >= {*base_cli.DSH_OFF, "session-log-deepseek", "tool-web",
                                                     "llm-pi-ai", "agent-default-model", "skill-filesystem",
                                                     "agent-instructions", "session-persistence-jsonl"}
    work = tmp_path / "work"
    work.mkdir()
    env = {**os.environ, **agent.home_env(home)}
    done = subprocess.run(["dsh", "--profile", "headless", "--patch", str(home / base_cli.DSH_PATCH),
                           "--dump-config"], cwd=work, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "not found" not in done.stderr and "id: llm-pi-ai" in done.stdout
    assert "  config:\n    search: false\n    fetch: true\n" in done.stdout                  # web fetch, no search
    assert "    projectRootMarkers: []\n" in done.stdout
    assert "    root: !!js dshHomePath('sessions')\n    compression: none\n" in done.stdout   # dsh's own root, plain
