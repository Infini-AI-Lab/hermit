"""The `opencode` Base Agent (hermit/agent/base/cli.py), its model binding (hermit/agent/model/backends.py) and the
Layer 3 code that runs it: the acting agent (hermit/agent/acting/workspace.py) and its deployment
(hermit/hosts/opencode.py), the config's `opencode` base, the `opencode` Evolver.

Stand-ins: a fake `opencode` on PATH that replays a stream recorded from OpenCode 2.2.6 (tests/fixtures/opencode,
whose README says how) and records what it was given; and, for the tests marked integration, the installed
`opencode` against a scripted chat-completions model on the loopback (`serve_chat`), never a provider."""
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import threading

import pytest

from hermit.agent.base import cli as base_cli
from hermit.agent.base import trajectory
from hermit.agent.base.interface import Unsupported, make
from hermit.agent.base.tool import Tool
from hermit.agent.model.backends import bind_agent_params, registry

ROOT = Path(__file__).resolve().parents[2]
STREAMS = ROOT / "tests" / "fixtures" / "opencode"
FILE_ADAPTER = ROOT / "docs" / "_examples" / "file_adapter"
SESSION = "ses_e40d3f481ffePQb8uaj39iK60v"          # the recorded success stream's session
OUTAGES = ("failure_500.jsonl", "failure_429.jsonl", "failure_refused.jsonl")

FAKE_OPENCODE = f'''#!{sys.executable}
"""A stand-in for `opencode run --format json ...`: records its arguments, OpenCode's variables, its home's files and
its stdin, then prints FAKE_OPENCODE_STREAM and exits FAKE_OPENCODE_EXIT."""
import json, os, sys
from pathlib import Path
home = Path(os.environ["OPENCODE_CONFIG"]).parent
files = {{p.name: p.read_text() for p in sorted(home.iterdir()) if p.is_file() and p.suffix in (".json", ".md")}}
seen = {{"args": sys.argv[1:], "env": {{k: v for k, v in os.environ.items()
                                     if k.startswith(("OPENCODE_", "XDG_")) or k in ("HOME", "PWD", "FAKE_KEY")}},
         "files": files, "stdin": sys.stdin.read()}}
with open(os.environ["FAKE_OPENCODE_SEEN"], "a") as f:
    f.write(json.dumps(seen) + "\\n")
sys.stdout.write(Path(os.environ["FAKE_OPENCODE_STREAM"]).read_text())
sys.exit(int(os.environ.get("FAKE_OPENCODE_EXIT", "0")))
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "opencode"
    exe.write_text(FAKE_OPENCODE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_OPENCODE_SEEN", str(tmp_path / "seen.jsonl"))
    monkeypatch.setenv("FAKE_OPENCODE_STREAM", str(STREAMS / "success.jsonl"))
    monkeypatch.setenv("FAKE_KEY", "sk-fake")
    return tmp_path


def replay(monkeypatch, name: str, code: int = 0) -> None:
    monkeypatch.setenv("FAKE_OPENCODE_STREAM", str(STREAMS / name))
    monkeypatch.setenv("FAKE_OPENCODE_EXIT", str(code))


def seen(tmp_path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "seen.jsonl").read_text().splitlines()]


def bound(**backend) -> dict:
    """An `opencode` agent's params with its model bound to a backend of its own (`fake/m-1`)."""
    spec = {"base_url": "http://127.0.0.1:9/v1/", "api_key_env": "FAKE_KEY", "apis": ["chat"],
            "prices": {"m-1": {"input": 1.0, "cached_input": 0.1, "output": 2.0}}, **backend}
    return bind_agent_params({"model": "fake/m-1"}, "opencode", registry({"fake": spec}))


def agent(fake, **settings):
    return make("opencode", {**bound(), "web_search": False, "home": str(fake / "home"), **settings})


def echo() -> Tool:
    return Tool("echo", "Echo the text back.", lambda text="": {"echoed": text},
                input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})


def test_a_bound_model_is_opencodes_provider_with_its_key_as_a_variable():
    """The key reaches OpenCode only as `{env:VARIABLE}` (the records keep its name, never the key)."""
    params = bound()
    assert params["model"] == "m-1" and params["binding"]["ref"] == "fake/m-1"
    assert params["provider"] == {"npm": "@ai-sdk/openai-compatible", "name": "hermit",
                                  "options": {"baseURL": "http://127.0.0.1:9/v1", "apiKey": "{env:FAKE_KEY}"},
                                  "models": {"m-1": {"name": "m-1"}}}
    assert bound(context_window=32000)["provider"]["models"]["m-1"]["limit"] == {"context": 32000}
    free = bound(api_key_env="NO_SUCH_KEY_VAR", key_required=False)["provider"]
    assert free["options"]["apiKey"] == "local"           # a server that needs no key gets the placeholder


def test_opencode_runs_in_its_home_with_its_model_held_and_the_tools_offered_directly(fake, monkeypatch):
    """What the session must hold (the provider, the permissions, no sharing) is the inline config, which outranks a
    workspace's opencode.json; the call's tools are the home's config, offered directly; the caller's own OpenCode
    settings do not reach the session."""
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(fake / "elsewhere"))
    monkeypatch.setenv("OPENCODE_PERMISSION", '{"*": "ask"}')
    home, work = fake / "home", fake / "work"
    work.mkdir()
    result = agent(fake, effort="high", log=str(fake / "agent-opencode.jsonl")).run(
        "-a prompt that starts with a dash", workspace=work, tools=[echo()])
    [call] = seen(fake)
    # a server of its own (never OpenCode's background service, which other sessions would share)
    assert call["args"][:7] == ["run", "--standalone", "--format", "json", "-m", "hermit/m-1", "--title"]
    assert "--thinking" in call["args"] and call["args"][-2:] == ["--", "-a prompt that starts with a dash"]
    env = call["env"]
    assert env["HOME"] == str(home) and env["XDG_DATA_HOME"] == str(home / "data")
    assert env["OPENCODE_CONFIG"] == str(home / "opencode.json") and env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    assert "OPENCODE_CONFIG_DIR" not in env and "OPENCODE_PERMISSION" not in env
    assert "PWD" not in env                     # OpenCode works where PWD names, before where it is started
    held = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert held["model"] == "hermit/m-1" and held["enabled_providers"] == ["hermit"] and held["share"] == "disabled"
    assert held["provider"]["hermit"]["options"]["apiKey"] == "{env:FAKE_KEY}"
    assert held["provider"]["hermit"]["models"]["m-1"]["options"] == {"reasoningEffort": "high"}
    assert {held["permission"][t] for t in ("question", "subagent", "execute", "webfetch", "websearch")} == {"deny"}
    # the agent the session runs holds them too (a workspace's config could give it its own), and no skill is allowed
    assert held["default_agent"] == "build" and held["agent"]["build"]["permission"] == held["permission"]
    assert held["permission"]["skill"] == {"*": "deny"}
    assert "sk-fake" not in env["OPENCODE_CONFIG_CONTENT"] + json.dumps(call["files"])
    [(server, entry)] = json.loads(call["files"]["opencode.json"])["mcp"]["servers"].items()
    assert server == "hermit_tools" and entry["codemode"] is False and entry["url"].startswith("http://127.0.0.1:")
    assert entry["timeout"] == {"execution": 1800000}          # a Trainer tool runs longer than OpenCode's 30 s
    assert result.reply == "the answer" and result.exit_code == 0 and result.extra["served"]
    assert not result.extra["failed"] and result.session == SESSION
    [usage] = result.usage
    assert (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens, usage.calls) == (4000, 1600, 80, 4)
    assert usage.cost_usd == pytest.approx(0.00272)             # the price's, not OpenCode's $0 for a custom model
    assert [s.kind for s in result.trajectory] == ["command", "tool_call", "file_change", "reasoning", "message"]
    command, tool = result.trajectory[0], json.loads(result.trajectory[1].text)
    assert (command.command, command.exit_code, command.output) == ("echo hi && exit 3", 3, "hi\n")
    assert tool == {"tool": "hermit_tools_echo", "input": {"text": "from opencode"}}
    assert json.loads(result.trajectory[1].output) == {"echoed": "from opencode"}


@pytest.mark.parametrize("stream", OUTAGES)
def test_a_provider_that_could_not_serve_is_an_outage_read_from_opencodes_own_error(fake, monkeypatch, stream):
    """A 5xx, a 429 and a connection that never reached the provider each end in OpenCode's error event, whose
    `type` says so: `unavailable` (the acting agent runs the task again), never read from the error's wording."""
    replay(monkeypatch, stream, code=1)
    work = fake / "work"
    work.mkdir()
    result = agent(fake).run("go", workspace=work)
    assert result.exit_code == 1 and result.extra["failed"] and not result.extra["served"]
    assert result.extra["unavailable"] and not result.extra["rejected"]
    assert result.usage == [] and result.trajectory[-1].kind == "error"


@pytest.mark.parametrize("stream,kind", [("failure_401.jsonl", "provider.auth"),
                                         ("failure_402.jsonl", "provider.quota")])
def test_a_request_the_provider_refuses_is_rejected_not_an_outage(fake, monkeypatch, stream, kind):
    """A 401 (a wrong key) or a 402 (the account's quota) fails the same way however often it is sent: no retry."""
    replay(monkeypatch, stream, code=1)
    work = fake / "work"
    work.mkdir()
    result = agent(fake).run("go", workspace=work)
    assert result.extra["failed"] and result.extra["rejected"] and not result.extra["unavailable"]
    assert result.extra["failure"].startswith(kind)


def test_the_acting_agent_runs_a_task_again_when_the_provider_could_not_serve_then_reports_an_error(fake, monkeypatch):
    """Through the caller that reads `unavailable`: a provider down for every attempt is an error, not an answer."""
    from hermit.agent.acting import workspace as acting
    replay(monkeypatch, "failure_500.jsonl", code=1)
    params = {**bound(), "backend": "opencode", "web_search": False, "prompt": "go", "workspaces": "ws",
              "retries": 2, "retry_wait_s": 0}
    with pytest.raises(RuntimeError, match="could not be served after 2 attempts"):
        acting.run({"id": "q1"}, None, params=params, base=fake)
    assert len(seen(fake)) == 2


def test_a_resumed_session_is_continued_by_its_id_and_counts_only_its_new_usage(fake, monkeypatch):
    replay(monkeypatch, "resume.jsonl")
    work = fake / "work"
    work.mkdir()
    opencode = agent(fake)
    result = opencode.run("again", workspace=work, session=SESSION)
    args = seen(fake)[0]["args"]
    assert args[args.index("--session") + 1] == SESSION and result.session == SESSION
    [usage] = result.usage
    assert (usage.input_tokens, usage.calls) == (1000, 1)
    opencode.run("again", workspace=work, session="../not-an-id")     # no OpenCode session id: a new session
    assert "--session" not in seen(fake)[1]["args"]


def test_opencode_runs_only_a_model_reference_and_refuses_roles_web_search_a_login_and_a_container(fake):
    """Each is refused before OpenCode starts, never ignored: no sign-in of the user's is ever copied."""
    work = fake / "work"
    work.mkdir()
    with pytest.raises(Unsupported, match="runs a <backend>/<model> reference"):
        make("opencode", {"model": "some-model", "web_search": False, "home": str(fake / "home")}).run(
            "go", workspace=work)
    opencode = agent(fake)
    role = {"name": "reviewer", "description": "Review.", "instructions": "Check it."}
    with pytest.raises(Unsupported, match="no roles"):
        opencode.run("go", workspace=work, roles=[role])
    with pytest.raises(Unsupported, match="no web search"):
        opencode.configured(web_search=True).run("go", workspace=work)
    with pytest.raises(Unsupported, match="login"):
        opencode.configured(login="chatgpt").run("go", workspace=work)
    with pytest.raises(Unsupported, match="takes isolation none or sandbox"):
        opencode.configured(isolation="container").run("go", workspace=work)
    assert not (fake / "seen.jsonl").exists()
    from hermit.agent.acting.workspace import _check
    with pytest.raises(ValueError, match="agent opencode takes no login"):
        _check("opencode", {"login": "chatgpt", "web_search": False})


def test_system_instructions_are_a_file_the_inline_config_names(fake):
    """OpenCode has no flag for them: a file in the home, named by its path in the inline config (a variable, which
    a sandbox spells as it is inside)."""
    work = fake / "work"
    work.mkdir()
    opencode = agent(fake)
    own, prompt = opencode.system_settings("Be exact.", "go")
    assert own == {"append_system_prompt": "Be exact."} and prompt == "go"
    opencode.configured(**own).run(prompt, workspace=work)
    [call] = seen(fake)
    assert call["files"]["system.md"] == "Be exact."
    assert json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"])["instructions"] == [str(fake / "home" / "system.md")]


def test_an_opencode_stream_is_read_back_by_its_name_or_by_its_events(tmp_path):
    """The learning material keeps the acting agent's log: an Evolver reads OpenCode's steps from it like Codex's."""
    named = tmp_path / "log"
    named.mkdir()
    shutil.copy(STREAMS / "success.jsonl", named / trajectory.OPENCODE_STREAM)
    other = tmp_path / "events.jsonl"
    shutil.copy(STREAMS / "success.jsonl", other)
    for log in (named, other):
        assert [s.kind for s in trajectory.log_steps(log)] == ["command", "tool_call", "file_change", "reasoning",
                                                               "message"]
    assert base_cli.served("opencode", (STREAMS / "success.jsonl").read_text())
    raw = (STREAMS / "failure_500.jsonl").read_text()
    assert not base_cli.served("opencode", raw) and base_cli.notices("opencode", raw).startswith("provider.internal")


def test_the_opencode_acting_agent_deploys_the_adapter_where_opencode_reads_it(fake):
    """The adapter's tools are the project's opencode.json, offered directly; its skills are .opencode/skills; its
    roles stay files AGENTS.md names, as OpenCode takes none."""
    from hermit.agent.acting import workspace as acting
    params = {**bound(), "backend": "opencode", "web_search": False, "prompt": "go", "workspaces": "ws"}
    out = acting.run({"id": "q1"}, FILE_ADAPTER, params=params, base=fake)
    work = Path(out.extra["workspace"])
    [call] = seen(fake)
    assert out.output == "the answer" and out.extra["backend"] == "opencode" and "--approve" not in call["args"]
    held = json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"])
    assert held["permission"]["skill"] == {"*": "deny", "verify-output": "allow"}     # the deployment's skills only
    [server] = json.loads((work / "opencode.json").read_text())["mcp"]["servers"].values()
    assert server["type"] == "local" and server["codemode"] is False
    assert "hermit.runtime.mcp_server" in server["command"]
    assert (work / ".opencode" / "skills" / "verify-output" / "SKILL.md").is_file()
    assert "do it yourself" in (work / "AGENTS.md").read_text()
    assert (out.log / "agent-opencode.jsonl").is_file()


def test_hermit_deploy_writes_an_opencode_workspace(tmp_path, capsys):
    """`hermit deploy --host opencode`: the command a person runs, writing what OpenCode reads."""
    from hermit.cli import main
    assert main(["deploy", "--adapter", str(FILE_ADAPTER), "--workspace", str(tmp_path / "ws"),
                 "--host", "opencode"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["host"] == "opencode" and record["skills"] == ["verify-output"]
    assert set(json.loads((tmp_path / "ws" / "opencode.json").read_text())["mcp"]["servers"]) == {"file_example"}


def test_opencode_is_a_base_of_the_config_and_the_opencode_evolver_binds_its_model():
    from hermit.agent.base.login import evolver_problems
    from hermit.agent.evolver.loader import find, instantiate
    from hermit.config import layered
    data = {"agent": {"base": "opencode", "model": {"ref": "fake/m-1", "effort": "high"},
                      "opencode": {"timeout_s": 60}, "params": {"prompt": "go"}},
            "evolver": {"name": "opencode", "base": "opencode", "model": "fake/m-1", "params": {}}, "llm": None}
    layered(data)
    assert data["agent"]["name"] == "opencode" and data["agent"]["params"]["effort"] == "high"
    assert data["agent"]["params"]["timeout_s"] == 60 and data["evolver"]["params"]["backend"] == "opencode"
    backends = registry({"fake": {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "FAKE_KEY", "apis": ["chat"]}})
    _, params = instantiate(find("opencode", ROOT), data["evolver"]["params"], backends=backends,
                            default_model="fake/m-1")
    assert params["backend"] == "opencode" and params["home"] == [] and params["web_search"] is False
    assert params["provider"]["options"]["apiKey"] == "{env:FAKE_KEY}"
    assert evolver_problems({"backend": "opencode", "login": "chatgpt"})


# ---------- the installed opencode (integration) ----------

def serve_chat(script, status: int | None = None, seen: list | None = None):
    """A chat-completions model on the loopback: `script(request body) -> (tool call (name, arguments) | None)`;
    None answers `the answer`, with a reasoning text first. Every response is streamed, with usage (1000 prompt
    tokens, 400 of them cached; 20 completion tokens, 5 of them reasoning). With `status`, every request is answered
    that error. `seen` gets each request's body and Authorization header."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def chunk(body):
        return f"data: {json.dumps(body)}\n\n".encode()

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            if seen is not None:
                seen.append({"body": body, "authorization": self.headers.get("authorization")})
            if status is not None:
                data = json.dumps({"error": {"message": "the model is down", "type": "server_error"}}).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            call = script(body)
            deltas = [{"role": "assistant", "tool_calls": [{"index": 0, "id": f"call_{len(body['messages'])}",
                                                            "type": "function",
                                                            "function": {"name": call[0],
                                                                         "arguments": json.dumps(call[1])}}]}] \
                if call else [{"role": "assistant", "reasoning_content": "The work is done."},
                              {"content": "the answer"}]
            data = b"".join([*(chunk({"choices": [{"index": 0, "delta": d, "finish_reason": None}]}) for d in deltas),
                             chunk({"choices": [{"index": 0, "delta": {}, "finish_reason":
                                                 "tool_calls" if call else "stop"}]}),
                             chunk({"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 20,
                                                             "prompt_tokens_details": {"cached_tokens": 400},
                                                             "completion_tokens_details": {"reasoning_tokens": 5}}}),
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
    import subprocess
    return bool(shutil.which("bwrap")) and subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--unshare-net", "true"], capture_output=True).returncode == 0


def mock_params(url: str) -> dict:
    return bind_agent_params({"model": "mock/m-1"}, "opencode",
                             registry({"mock": {"base_url": url, "api_key_env": "MOCK_KEY", "apis": ["chat"]}}))


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("opencode"), reason="needs the opencode CLI")
@pytest.mark.parametrize("isolation", ["none", "sandbox"])
def test_with_the_opencode_cli_a_session_reaches_its_model_and_its_tools(tmp_path, monkeypatch, isolation):
    """The installed OpenCode, bound to a scripted model: it calls the call's MCP tool (offered directly), writes a
    file, answers; in the run's sandbox (isolated network) only its model and the tool server are reached. The key
    reaches the model from its variable, the effort reaches the request, and a parent folder (a Git repository's
    root, with a skill and a config that allows the denied tools again) gives the session nothing."""
    from hermit.sandbox import Sandbox
    if isolation == "sandbox" and not _bwrap_works():
        pytest.skip("needs a working bubblewrap")
    requests = []

    def script(body):
        done = sum(1 for m in body.get("messages") or [] if m.get("role") == "tool")
        names = [t["function"]["name"] for t in body.get("tools") or []]
        plan = [("hermit_tools_echo", {"text": "hi"}), ("write", {"path": "note.txt", "content": "ok\n"})]
        assert plan[0][0] in names                       # the tool is offered directly, not through code mode
        return plan[done] if done < len(plan) else None

    (tmp_path / ".git").mkdir()                          # the parent folder: a repository's root, as a checkout is
    (tmp_path / "opencode.json").write_text(json.dumps(
        {"agent": {"build": {"permission": {"webfetch": "allow", "question": "allow"}}}}))
    (tmp_path / ".agents" / "skills" / "parent-skill").mkdir(parents=True)
    (tmp_path / ".agents" / "skills" / "parent-skill" / "SKILL.md").write_text(
        "---\nname: parent-skill\ndescription: A skill of the parent folder.\n---\nNot the adapter's.\n")
    server, url = serve_chat(script, seen=requests)
    monkeypatch.setenv("MOCK_KEY", "sk-mock")
    (tmp_path / "caller").mkdir()
    monkeypatch.setenv("PWD", str(tmp_path / "caller"))      # where OpenCode would work if it went by PWD
    work = tmp_path / "work"
    work.mkdir()
    settings = {**mock_params(url), "web_search": False, "home": str(tmp_path / "home"), "isolation": isolation,
                "timeout_s": 300, "effort": "high"}
    if isolation == "sandbox":
        settings["sandbox"] = Sandbox(network="isolated", env_allow=[], writable=[work], role="agent",
                                      record_dir=tmp_path / "records")
    try:
        result = make("opencode", settings).run("Do the task.", workspace=work, tools=[echo()])
    finally:
        server.shutdown()
    assert result.exit_code == 0 and result.extra["served"] and result.reply == "the answer"
    assert (work / "note.txt").read_text() == "ok\n" and not any((tmp_path / "caller").iterdir())
    assert [s.kind for s in result.trajectory] == ["tool_call", "file_change", "reasoning", "message"]
    assert json.loads(result.trajectory[0].output) == {"echoed": "hi"}
    assert (result.usage[0].calls, result.usage[0].output_tokens) == (3, 60)    # the reasoning counted as output
    assert {r["authorization"] for r in requests} == {"Bearer sk-mock"}
    assert {r["body"].get("reasoning_effort") for r in requests} == {"high"}
    offered = {t["function"]["name"] for r in requests for t in r["body"].get("tools") or []}
    assert not offered & {"question", "subagent", "execute", "webfetch", "websearch"}
    assert "parent-skill" not in json.dumps([r["body"] for r in requests])
    if isolation == "sandbox":
        [record] = (tmp_path / "records").glob("agent-*.json")
        data = json.loads(record.read_text())
        assert data["network"] == "isolated" and data["probe"]["passed"]
        assert {"OPENCODE_CONFIG_CONTENT", "XDG_DATA_HOME", "MOCK_KEY"} <= set(data["environment"])


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("opencode"), reason="needs the opencode CLI")
def test_with_the_opencode_cli_the_acting_agent_uses_the_deployed_adapters_tools(tmp_path, monkeypatch):
    """The deployment as OpenCode reads it: the project's opencode.json starts the adapter's tool server, whose tool
    the model is offered directly and calls."""
    from hermit.agent.acting import workspace as acting
    calls, bodies = [], []

    def script(body):
        names = [t["function"]["name"] for t in body.get("tools") or []]
        done = any(m.get("role") == "tool" for m in body.get("messages") or [])
        calls.append(names)
        bodies.append(body)
        return None if done else ("file_example_count_words", {"text": "a b c"})

    server, url = serve_chat(script)
    monkeypatch.setenv("MOCK_KEY", "sk-mock")
    try:
        out = acting.run({"id": "q1"}, FILE_ADAPTER, base=tmp_path,
                         params={**mock_params(url), "backend": "opencode", "web_search": False, "prompt": "Count.",
                                 "workspaces": "ws", "timeout_s": 300, "retries": 1})
    finally:
        server.shutdown()
    assert out.output == "the answer" and "file_example_count_words" in calls[0]
    assert "verify-output" in json.dumps(bodies[0])          # the deployment's skill, the one the session may load
    audit = Path(out.extra["workspace"]) / "tool-calls.jsonl"
    assert '"count_words"' in audit.read_text()
