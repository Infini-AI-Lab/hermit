"""The `pi` Base Agent (hermit/agent/base/cli.py), its model binding (hermit/agent/model/backends.py) and the Layer 3
code that runs it: the acting agent (hermit/agent/acting/workspace.py), the config's `pi` base, the `pi` Evolver.

Stand-ins: a fake `pi` on PATH that replays a stream recorded from Pi 1.5.0 (tests/fixtures/pi, whose README says
how) and records what it was given; and, for the tests marked integration, the installed `pi` against a scripted
chat-completions model on the loopback (`serve_chat`), never a provider."""
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
STREAMS = ROOT / "tests" / "fixtures" / "pi"
FILE_ADAPTER = ROOT / "docs" / "_examples" / "file_adapter"
SESSION = "01a1b44b-6699-759a-a7f2-22c02fcee4c5"          # the recorded success stream's session

FAKE_PI = f'''#!{sys.executable}
"""A stand-in for `pi --mode json ...`: records its arguments, Pi's variables, its home's files and its stdin, then
prints FAKE_PI_STREAM and exits FAKE_PI_EXIT."""
import json, os, sys
from pathlib import Path
home = Path(os.environ["PI_CODING_AGENT_DIR"])
files = {{p.name: p.read_text() for p in sorted(home.glob("*.json*")) if p.is_file()}}
names = ("PI_CODING_AGENT_DIR", "HOME", "PI_OFFLINE", "PI_TELEMETRY", "FAKE_KEY")
seen = {{"args": sys.argv[1:], "env": {{k: os.environ.get(k) for k in names}}, "files": files,
         "stdin": sys.stdin.read()}}
with open(os.environ["FAKE_PI_SEEN"], "a") as f:
    f.write(json.dumps(seen) + "\\n")
sys.stdout.write(Path(os.environ["FAKE_PI_STREAM"]).read_text())
sys.exit(int(os.environ.get("FAKE_PI_EXIT", "0")))
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "pi"
    exe.write_text(FAKE_PI)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PI_SEEN", str(tmp_path / "seen.jsonl"))
    monkeypatch.setenv("FAKE_PI_STREAM", str(STREAMS / "success.jsonl"))
    monkeypatch.setenv("FAKE_KEY", "sk-fake")
    return tmp_path


def seen(tmp_path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "seen.jsonl").read_text().splitlines()]


def bound(**backend) -> dict:
    """A `pi` agent's params with its model bound to a backend of its own (`fake/m-1`)."""
    spec = {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "FAKE_KEY", "apis": ["chat"],
            "prices": {"m-1": {"input": 1.0, "cached_input": 0.1, "output": 2.0}}, **backend}
    return bind_agent_params({"model": "fake/m-1"}, "pi", registry({"fake": spec}))


def echo() -> Tool:
    return Tool("echo", "Echo the text back.", lambda text="": {"echoed": text},
                input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})


def test_a_bound_model_is_pis_provider_with_its_key_as_a_variable_in_the_dialect_the_backend_serves():
    """The key reaches Pi only as `$VARIABLE` (the records keep its name, never the key): models.json is written
    in the home a run keeps."""
    params = bound()
    assert params["model"] == "m-1" and params["binding"]["ref"] == "fake/m-1"
    assert params["provider"] == {"baseUrl": "http://127.0.0.1:9/v1", "api": "openai-completions",
                                  "apiKey": "$FAKE_KEY",
                                  "models": [{"id": "m-1", "cost": {"input": 1.0, "output": 2.0, "cacheRead": 0.1,
                                                                    "cacheWrite": 1.0}}]}
    messages = bound(apis=["messages"], base_url="http://127.0.0.1:9/v1")["provider"]
    assert messages["api"] == "anthropic-messages" and messages["baseUrl"] == "http://127.0.0.1:9"
    system = bound(developer_role="system", context_window=32000)["provider"]
    assert system["compat"] == {"supportsDeveloperRole": False} and system["models"][0]["contextWindow"] == 32000
    free = bound(api_key_env="NO_SUCH_KEY_VAR", key_required=False)["provider"]
    assert free["apiKey"] == "local"                       # a server that needs no key gets the placeholder


def test_pi_runs_offline_in_its_home_with_its_model_and_the_tools_offered_directly(fake):
    tmp_path = fake
    home, work = tmp_path / "home", tmp_path / "work"
    work.mkdir()
    agent = make("pi", {**bound(), "web_search": False, "home": str(home), "log": str(tmp_path / "agent-pi.jsonl")})
    result = agent.run("-a prompt that starts with a dash", workspace=work, tools=[echo()])
    [call] = seen(tmp_path)
    assert call["args"][:6] == ["--mode", "json", "--provider", "hermit", "--model", "m-1"]
    assert call["args"][-2:] == ["--", "-a prompt that starts with a dash"]
    assert call["env"] == {"PI_CODING_AGENT_DIR": str(home), "HOME": str(home), "PI_OFFLINE": "1",
                           "PI_TELEMETRY": "0", "FAKE_KEY": "sk-fake"}
    models = json.loads(call["files"]["models.json"])["providers"]["hermit"]
    assert models["apiKey"] == "$FAKE_KEY" and "sk-fake" not in json.dumps(call["files"])
    [(server, entry)] = json.loads(call["files"]["mcp.json"])["mcpServers"].items()
    assert server == "hermit_tools" and entry["exposure"] == "direct" and entry["url"].startswith("http://127.0.0.1:")
    assert json.loads(call["files"]["settings.json"])["cacheWarming"] == "off"
    assert result.reply == "the answer" and result.exit_code == 0 and result.extra["served"]
    assert result.session == SESSION
    [usage] = result.usage
    assert (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens, usage.calls) == (4000, 1600, 80, 4)
    assert usage.cost_usd == pytest.approx(0.00272)
    kinds = [s.kind for s in result.trajectory]
    assert kinds == ["command", "tool_call", "file_change", "message"]
    command, tool = result.trajectory[0], json.loads(result.trajectory[1].text)
    assert (command.command, command.exit_code, command.output) == ("echo hi && exit 3", 3, "hi\n")
    assert tool == {"arguments": {"text": "from pi"}, "server": "hermit_tools", "tool": "echo"}
    assert json.loads(result.trajectory[1].output) == {"echoed": "from pi"}


def test_a_failed_request_fails_the_session_although_pi_exits_zero(fake, monkeypatch):
    """Pi's JSON mode exits 0 when its last request failed: the session is `failed` (exit 0 with no result), so a
    failed learner step is not taken for a done one, and the provider's outage is retried by the acting agent."""
    monkeypatch.setenv("FAKE_PI_STREAM", str(STREAMS / "failure.jsonl"))
    work = fake / "work"
    work.mkdir()
    result = make("pi", {**bound(), "web_search": False, "home": str(fake / "home")}).run("go", workspace=work)
    assert result.exit_code == 0 and result.extra["failed"]
    assert not result.extra["served"] and result.extra["unavailable"]
    assert result.usage == []                              # retried requests that used no tokens are no calls
    assert [s.kind for s in result.trajectory] == ["error"] * 4
    assert result.trajectory[0].text.startswith("503: ")


def test_a_resumed_session_is_opened_by_its_file_and_counts_only_its_new_usage(fake, monkeypatch):
    monkeypatch.setenv("FAKE_PI_STREAM", str(STREAMS / "resume.jsonl"))
    home, work = fake / "home", fake / "work"
    work.mkdir()
    kept = home / "sessions" / "--run-work--" / f"2026-11-07T04-36-42-777Z_{SESSION}.jsonl"
    kept.parent.mkdir(parents=True)
    kept.write_text("{}\n")
    agent = make("pi", {**bound(), "web_search": False, "home": str(home)})
    result = agent.run("again", workspace=work, session=SESSION)
    args = seen(fake)[0]["args"]
    assert args[args.index("--session") + 1] == str(kept)
    [usage] = result.usage
    assert (usage.input_tokens, usage.calls) == (1000, 1)
    agent.run("again", workspace=work, session="*")       # no file of that session: a new one, no glob
    assert "--session" not in seen(fake)[1]["args"]


def test_pi_refuses_roles_web_search_and_a_container(fake):
    """Pi has no sub-agents and no web search, and no container image: each is refused, never ignored."""
    work = fake / "work"
    work.mkdir()
    agent = make("pi", {**bound(), "web_search": False, "home": str(fake / "home")})
    role = {"name": "reviewer", "description": "Review.", "instructions": "Check it."}
    with pytest.raises(Unsupported, match="no sub-agents"):
        agent.run("go", workspace=work, roles=[role])
    with pytest.raises(Unsupported, match="no web search"):
        agent.configured(web_search=True).run("go", workspace=work)
    with pytest.raises(Unsupported, match="takes isolation none or sandbox"):
        agent.configured(isolation="container").run("go", workspace=work)


def test_system_instructions_are_appended_and_a_long_one_goes_in_a_file(fake):
    work = fake / "work"
    work.mkdir()
    agent = make("pi", {**bound(), "web_search": False, "home": str(fake / "home")})
    own, prompt = agent.system_settings("Be exact.", "go")
    assert own == {"append_system_prompt": "Be exact."} and prompt == "go"
    agent.configured(**own).run(prompt, workspace=work)
    args = seen(fake)[0]["args"]
    assert args[args.index("--append-system-prompt") + 1] == "Be exact."
    long = "x" * (base_cli.ARG_MAX + 1)
    agent.configured(append_system_prompt=long).run("go", workspace=work)
    args = seen(fake)[1]["args"]
    path = Path(args[args.index("--append-system-prompt") + 1])
    assert path.parent == fake / "home" and path.read_text() == long


def test_a_pi_stream_is_read_back_by_its_name_or_by_its_events(tmp_path):
    """The learning material keeps the acting agent's log: an Evolver reads Pi's steps from it like Codex's."""
    named = tmp_path / "log"
    named.mkdir()
    shutil.copy(STREAMS / "success.jsonl", named / trajectory.PI_STREAM)
    other = tmp_path / "events.jsonl"
    shutil.copy(STREAMS / "success.jsonl", other)
    for log in (named, other):
        assert [s.kind for s in trajectory.log_steps(log)] == ["command", "tool_call", "file_change", "message"]
    raw = (STREAMS / "failure.jsonl").read_text()
    assert not base_cli.served("pi", raw) and base_cli.notices("pi", raw).startswith("503: ")
    assert base_cli.served("pi", (STREAMS / "success.jsonl").read_text())


def test_the_pi_acting_agent_deploys_the_adapter_and_trusts_its_project_for_the_session(fake):
    """Pi loads a workspace's .pi/ (the adapter's tools and skills) only in a trusted project: the session passes
    --approve, as a Codex session is told its deployed workspace is trusted. The tools are offered directly."""
    from hermit.agent.acting import workspace as acting
    params = {**bound(), "backend": "pi", "web_search": False, "prompt": "go", "workspaces": "ws"}
    out = acting.run({"id": "q1"}, FILE_ADAPTER, params=params, base=fake)
    work = Path(out.extra["workspace"])
    [call] = seen(fake)
    assert "--approve" in call["args"] and out.output == "the answer" and out.extra["backend"] == "pi"
    mcp = json.loads((work / ".pi" / "mcp.json").read_text())["mcpServers"]
    assert [entry["exposure"] for entry in mcp.values()] == ["direct"]
    assert (work / ".pi" / "skills" / "verify-output" / "SKILL.md").is_file()
    assert "do it yourself" in (work / "AGENTS.md").read_text()      # roles stay files: Pi has no sub-agents
    assert (out.log / "agent-pi.jsonl").is_file()


def test_pi_is_a_base_of_the_config_and_the_pi_evolver_binds_its_model():
    from hermit.agent.evolver.loader import find, instantiate
    from hermit.config import layered
    data = {"agent": {"base": "pi", "model": {"ref": "fake/m-1", "effort": "high"}, "pi": {"timeout_s": 60},
                      "params": {"prompt": "go"}},
            "evolver": {"name": "pi", "base": "pi", "model": "fake/m-1", "params": {}}, "llm": None}
    layered(data)
    assert data["agent"]["name"] == "pi" and data["agent"]["params"]["effort"] == "high"
    assert data["agent"]["params"]["timeout_s"] == 60 and data["evolver"]["params"]["backend"] == "pi"
    spec = find("pi", ROOT)
    backends = registry({"fake": {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "FAKE_KEY", "apis": ["chat"]}})
    _, params = instantiate(spec, data["evolver"]["params"], backends=backends, default_model="fake/m-1")
    assert params["backend"] == "pi" and params["home"] == [] and params["provider"]["apiKey"] == "$FAKE_KEY"


# ---------- the installed pi (integration) ----------

def serve_chat(script):
    """A chat-completions model on the loopback: `script(request body) -> (tool call (name, arguments) | None)`;
    None answers `the answer`. Every response is streamed, with usage."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def chunk(body):
        return f"data: {json.dumps(body)}\n\n".encode()

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
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
    import subprocess
    return bool(shutil.which("bwrap")) and subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--unshare-net", "true"], capture_output=True).returncode == 0


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("pi"), reason="needs the pi CLI")
@pytest.mark.parametrize("isolation", ["none", "sandbox"])
def test_with_the_pi_cli_a_session_reaches_its_model_and_its_tools(tmp_path, monkeypatch, isolation):
    """The installed Pi, bound to a scripted model: it calls the call's MCP tool, writes a file, answers; in the
    run's sandbox (isolated network) only its model and the tool server are reached."""
    from hermit.sandbox import Sandbox
    if isolation == "sandbox" and not _bwrap_works():
        pytest.skip("needs a working bubblewrap")

    def script(body):
        done = sum(1 for m in body.get("messages") or [] if m.get("role") == "tool")
        names = [t["function"]["name"] for t in body.get("tools") or []]
        plan = [("mcp__hermit_tools__echo", {"text": "hi"}), ("write", {"path": "note.txt", "content": "ok\n"})]
        assert plan[0][0] in names                       # the tool is offered directly, not through code mode
        return plan[done] if done < len(plan) else None

    server, url = serve_chat(script)
    monkeypatch.setenv("MOCK_KEY", "sk-mock")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    params = bind_agent_params({"model": "mock/m-1"}, "pi",
                               registry({"mock": {"base_url": url, "api_key_env": "MOCK_KEY", "apis": ["chat"]}}))
    work = tmp_path / "work"
    work.mkdir()
    settings = {**params, "web_search": False, "home": str(tmp_path / "home"), "isolation": isolation,
                "timeout_s": 300}
    if isolation == "sandbox":
        settings["sandbox"] = Sandbox(network="isolated", env_allow=[], writable=[work], role="agent",
                                      record_dir=tmp_path / "records")
    try:
        result = make("pi", settings).run("Do the task.", workspace=work, tools=[echo()])
    finally:
        server.shutdown()
    assert result.exit_code == 0 and result.extra["served"] and result.reply == "the answer"
    assert (work / "note.txt").read_text() == "ok\n"
    assert [s.kind for s in result.trajectory] == ["tool_call", "file_change", "message"]
    assert json.loads(result.trajectory[0].output) == {"echoed": "hi"}
    assert result.usage[0].calls == 3
    if isolation == "sandbox":
        [record] = (tmp_path / "records").glob("agent-*.json")
        data = json.loads(record.read_text())
        assert data["network"] == "isolated" and data["probe"]["passed"]
        assert {"PI_CODING_AGENT_DIR", "PI_OFFLINE", "MOCK_KEY"} <= set(data["environment"])
