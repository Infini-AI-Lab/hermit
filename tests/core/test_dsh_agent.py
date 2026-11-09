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
and its stdin, then prints FAKE_DSH_STREAM and exits FAKE_DSH_EXIT."""
import json, os, sys
args = sys.argv[1:]
patches = {{args[i + 1]: open(args[i + 1]).read() for i, a in enumerate(args[:-1]) if a == "--patch"}}
names = ("DSH_HOME", "HOME", "DSH_AGENTS_HOME", "DSH_TELEMETRY_DISABLED", "{base_cli.DSH_NO_KEY}", "FAKE_KEY",
         "DSH_BUNDLED_SKILL_DIR")
seen = {{"args": args, "env": {{k: os.environ.get(k) for k in names}}, "patches": patches, "stdin": sys.stdin.read()}}
with open(os.environ["FAKE_DSH_SEEN"], "a") as f:
    f.write(json.dumps(seen) + "\\n")
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


def rows(call: dict, home: Path) -> list[dict]:
    """The session's own patch layer, as the fake read it."""
    return json.loads(call["patches"][str(home / base_cli.DSH_PATCH)])


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
    of skills) does not reach it, so the session's settings are its own home's."""
    tmp_path = fake
    monkeypatch.setenv("DSH_BUNDLED_SKILL_DIR", str(tmp_path / "callers-skills"))
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
                           "FAKE_KEY": "sk-fake", "DSH_BUNDLED_SKILL_DIR": None}   # none of the user's own skills
    layer = rows(call, home)
    assert "sk-fake" not in json.dumps(layer)
    assert {r["id"] for r in layer if r.get("disabled")} == {*base_cli.DSH_OFF, "tool-web"}
    assert {"tool-subagent", "session-telemetry-otel", "session-title-llm"} <= set(base_cli.DSH_OFF)
    assert row(layer, "session-log-deepseek")["config"] == {"enabled": False}
    route = row(layer, "llm-pi-ai")["config"]["providers"][base_cli.DSH_PROVIDER]
    assert route["apiKeyEnv"] == "FAKE_KEY" and route["models"] == [{"id": "m-1", "reasoningEfforts": {"high": "high"}}]
    assert row(layer, "agent-default-model")["config"] == {"provider": base_cli.DSH_PROVIDER, "model": "m-1",
                                                           "reasoningEffort": "high"}
    [server] = [r for r in layer if "insert" in r]
    [entry] = server["insert"]
    assert entry["name"] == "@deepseek-ai/dsh-mcp-client" and entry["config"]["serverName"] == "hermit_tools"
    assert entry["config"]["transport"] == "streamable-http" and entry["config"]["url"].startswith("http://127.0.0.1:")
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


def test_an_effort_is_a_level_the_bound_model_lists_and_off_is_a_model_that_does_not_reason(fake):
    """dsh refuses an effort its route does not list for the model (UNSUPPORTED_REASONING_EFFORT), and a list of
    `off` alone (INVALID_CONFIG): effort off declares a model that does not reason, and gives no effort."""
    work = fake / "work"
    work.mkdir()
    agent = make("dsh", {**bound(), "web_search": False, "home": str(fake / "home")})
    for effort, efforts, default in (("high", {"high": "high"}, "high"), ("off", False, None), (None, None, None)):
        agent.configured(effort=effort).run("go", workspace=work)
        layer = rows(seen(fake)[-1], fake / "home")
        [model] = row(layer, "llm-pi-ai")["config"]["providers"][base_cli.DSH_PROVIDER]["models"]
        assert model.get("reasoningEfforts") == efforts
        assert row(layer, "agent-default-model")["config"].get("reasoningEffort") == default


def test_a_failed_run_is_an_error_and_an_outage_is_retried(fake, monkeypatch):
    """dsh exits 1 for a run that did not complete; its error code tells an outage (SERVER, RATE_LIMIT, ...) from
    a refusal, so the acting agent runs the task again only when the provider could not serve it."""
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
    assert not base_cli.UNAVAILABLE.search(base_cli.notices("dsh", refused))   # a bad key is no outage
    closed = (STREAMS / "failure.jsonl").read_text().replace("SERVER", "STREAM_CLOSED")   # dsh does not retry it
    assert base_cli.UNAVAILABLE.search(base_cli.notices("dsh", closed))


def test_a_run_dsh_was_stopped_in_is_no_success_although_dsh_exits_0(fake, monkeypatch, tmp_path):
    """dsh exits 0 when SIGTERM stops it: its own rule for a completed run is exit 0 and a completed `final`, so a
    run without one has failed (the acting agent records no answer, an AgentEvolver's step is not a done one)."""
    cut = tmp_path / "cut.jsonl"
    cut.write_text("".join((STREAMS / "success.jsonl").read_text().splitlines(keepends=True)[:12]))
    monkeypatch.setenv("FAKE_DSH_STREAM", str(cut))
    work = fake / "work"
    work.mkdir()
    result = make("dsh", {**bound(), "web_search": False, "home": str(fake / "home")}).run("go", workspace=work)
    assert result.exit_code == 0 and result.extra["failed"] and not result.extra["served"]
    assert not result.extra["unavailable"] and result.reply == "" and "did not finish" in result.extra["failure"]


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


def test_dsh_refuses_roles_and_a_container_and_keeps_web_search_when_asked(fake):
    """Roles and a container are refused, never ignored; web search on leaves dsh's web tools as they are."""
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
    assert all(r.get("id") != "tool-web" for r in rows(seen(fake)[-1], fake / "home"))


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


def test_the_workspaces_skills_are_the_sessions_own_and_the_last_calls_copy_is_replaced(fake):
    """dsh reads a project's skills only at the project's root (the nearest folder above with a .git), so the
    workspace's are copied to the session's own ($DSH_AGENTS_HOME/skills), which it reads wherever it runs; a skill
    the last call copied is gone, and a link left in place of the folder is replaced, never written through."""
    home, work = fake / "home", fake / "work"
    (work / ".dsh" / "skills" / "check").mkdir(parents=True)
    (work / ".dsh" / "skills" / "check" / "SKILL.md").write_text("---\nname: check\ndescription: Check.\n---\n")
    (home / ".agents" / "skills" / "old").mkdir(parents=True)
    agent = make("dsh", {**bound(), "web_search": False, "home": str(home)})
    agent.run("go", workspace=work)
    assert [p.name for p in (home / ".agents" / "skills").iterdir()] == ["check"]
    outside = fake / "outside"
    outside.mkdir()
    shutil.rmtree(home / ".agents")
    (home / ".agents").symlink_to(outside)
    agent.run("go", workspace=work)
    assert not (home / ".agents").is_symlink() and list(outside.iterdir()) == []
    assert (home / ".agents" / "skills" / "check" / "SKILL.md").is_file()


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
    signal's has none: unknown, as Codex reports it."""
    stream = [json.loads(line) for line in (STREAMS / "bash.jsonl").read_text().splitlines()]
    commands = [(s.command, s.exit_code, s.output) for s in trajectory.dsh_steps(stream) if s.kind == "command"]
    assert commands == [("exit 3", 3, ""), ("printf ''", 0, ""), ("true", 0, ""), ("echo out; exit 4", 4, "out"),
                        ("kill -9 $$", None, "(no output)\n[killed by signal: SIGKILL]"),
                        ("printf 'no newline'; exit 5", 5, "no newline"),
                        ("trap 'exit 0' TERM; sleep 5 & wait", None, "(no output)\n[timed out after 1000ms]"),
                        ("sleep 5", None, "(no output)\n[timed out after 1000ms]\n[killed by signal: SIGTERM]")]


def test_a_run_that_ends_at_the_token_limit_is_an_error_the_learner_can_read():
    """dsh exits 1 for a run that ends `max-tokens` and writes nothing on stderr: the run is no success and no
    outage, and its end is a step of the trajectory and a notice (only errors and aborts were, before)."""
    raw = (STREAMS / "max_tokens.jsonl").read_text()
    assert not base_cli.served("dsh", raw) and base_cli.notices("dsh", raw) == "run max-tokens"
    assert not base_cli.UNAVAILABLE.search(base_cli.notices("dsh", raw))
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
    [layer] = json.loads((work / ".dsh" / "hermit.patch.yml").read_text())
    [server] = layer["insert"]
    assert server["config"]["transport"] == "stdio" and server["config"]["args"][:2] == ["-m",
                                                                                         "hermit.runtime.mcp_server"]
    skill = work / ".dsh" / "skills" / "verify-output" / "SKILL.md"
    assert (work.parent / "home" / ".agents" / "skills" / "verify-output" / "SKILL.md").read_text() == \
        skill.read_text()                                              # also the session's own: _dsh_skills
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
@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_with_the_dsh_cli_a_deployed_adapter_brings_its_skills_and_tools_into_a_git_repository(tmp_path, monkeypatch):
    """The acting agent with the installed dsh, its workspaces inside a git repository (a config kept in a project).
    dsh takes the repository for the project and reads project skills only from its .dsh/skills, so the deployed
    skills reach it as the session's own (_dsh_skills; before, they were not offered there). The adapter's own tool
    server (the deployment's stdio server, loaded from its patch layer) answers a call."""
    from hermit.agent.acting import workspace as acting
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
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
    assert "mcp__file_example__count_words" in [t["function"]["name"] for t in requests[0].get("tools") or []]
    [answer] = [m for m in requests[1]["messages"] if m.get("role") == "tool"]
    assert json.loads(answer["content"] if isinstance(answer["content"], str) else
                      "".join(p.get("text", "") for p in answer["content"])) == {"count": 3}


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
    agent = make("dsh", {**bound(), "effort": "high", "web_search": False})
    agent.prepare_home(home, (), "http://127.0.0.1:9/mcp", "hermit_tools")
    layer = json.loads((home / base_cli.DSH_PATCH).read_text())
    assert {r["id"] for r in layer if "id" in r} >= {*base_cli.DSH_OFF, "session-log-deepseek", "tool-web",
                                                     "llm-pi-ai", "agent-default-model"}
    work = tmp_path / "work"
    work.mkdir()
    env = {**os.environ, **agent.home_env(home)}
    done = subprocess.run(["dsh", "--profile", "headless", "--patch", str(home / base_cli.DSH_PATCH),
                           "--dump-config"], cwd=work, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "not found" not in done.stderr and "id: llm-pi-ai" in done.stdout
