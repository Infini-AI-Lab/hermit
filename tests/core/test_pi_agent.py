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
from hermit.agent.model.backends import Binding, bind_agent_params, registry

ROOT = Path(__file__).resolve().parents[2]
STREAMS = ROOT / "tests" / "fixtures" / "pi"
FILE_ADAPTER = ROOT / "docs" / "_examples" / "file_adapter"
SESSION = "01a1b44b-6699-759a-a7f2-22c02fcee4c5"          # the recorded success stream's session
FAILED = '503: {"message":"mock: service unavailable"}'    # the recorded failure stream's error, each request's

FAKE_PI = f'''#!{sys.executable}
"""A stand-in for `pi --mode json ...`: records its arguments, Pi's variables, its home's files and its stdin, then
prints FAKE_PI_STREAM and exits FAKE_PI_EXIT."""
import json, os, sys
from pathlib import Path
home = Path(os.environ["PI_CODING_AGENT_DIR"])
files = {{p.name: p.read_text() for p in sorted(home.glob("*.json*")) if p.is_file()}}
names = ("PI_CODING_AGENT_DIR", "HOME", "PI_OFFLINE", "PI_TELEMETRY", "PI_CODING_AGENT_SESSION_DIR", "FAKE_KEY")
seen = {{"args": sys.argv[1:], "env": {{k: os.environ.get(k) for k in names}}, "files": files,
         "extensions": {{p.name: p.read_text() for p in sorted(home.glob("extensions/*")) if p.is_file()}},
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
    assert "maxTokens" not in params["provider"]["models"][0]          # Pi's own default, unless the backend says
    long = bound(max_output_tokens=64000)
    assert long["provider"]["models"][0]["maxTokens"] == 64000
    assert Binding.from_spec(long["binding"]).pi_provider()["models"][0]["maxTokens"] == 64000   # a role's own binding
    # a backend's extra_body goes with every request to an OpenAI-compatible API; the Messages API takes none
    assert bound(extra_body={"reasoning_effort": "low"})["provider"]["models"][0]["samplingParams"] == {
        "reasoning_effort": "low"}
    assert "samplingParams" not in bound(apis=["messages"], extra_body={"x": 1})["provider"]["models"][0]
    free = bound(api_key_env="NO_SUCH_KEY_VAR", key_required=False)["provider"]
    assert free["apiKey"] == "local"                       # a server that needs no key gets the placeholder


def test_pi_runs_offline_in_its_home_with_its_model_and_the_tools_offered_directly(fake, monkeypatch):
    tmp_path = fake
    home, work = tmp_path / "home", tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(tmp_path / "elsewhere"))   # the caller's: not taken
    agent = make("pi", {**bound(), "web_search": False, "home": str(home), "log": str(tmp_path / "agent-pi.jsonl")})
    result = agent.run("-a prompt that starts with a dash", workspace=work, tools=[echo()])
    [call] = seen(tmp_path)
    assert call["args"][:6] == ["--mode", "json", "--provider", "hermit", "--model", "m-1"]
    assert call["args"][-2:] == ["--", "-a prompt that starts with a dash"]
    assert call["env"] == {"PI_CODING_AGENT_DIR": str(home), "HOME": str(home), "PI_OFFLINE": "1",
                           "PI_TELEMETRY": "0", "PI_CODING_AGENT_SESSION_DIR": "", "FAKE_KEY": "sk-fake"}
    models = json.loads(call["files"]["models.json"])["providers"]["hermit"]
    assert models["apiKey"] == "$FAKE_KEY" and "sk-fake" not in json.dumps(call["files"])
    [(server, entry)] = json.loads(call["files"]["mcp.json"])["mcpServers"].items()
    assert server == "hermit_tools" and entry["exposure"] == "direct" and entry["url"].startswith("http://127.0.0.1:")
    assert entry["timeout"] == 1800                         # a Trainer tool runs longer than Pi's 60 s
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



@pytest.mark.parametrize("event, old, new", [("message_end", '"stopReason":"stop"', '"stopReason":"length"'),
                                             ("agent_settled", '"aborted":false', '"aborted":true')],
                         ids=["cut at the limit", "aborted"])
def test_a_cut_or_aborted_session_fails_although_pi_exits_zero(fake, monkeypatch, event, old, new):
    """Pi's text mode exits 1 for a reply cut at the output limit and for an abort; its JSON mode exits 0."""
    lines = (STREAMS / "success.jsonl").read_text().splitlines(keepends=True)
    last = max(i for i, line in enumerate(lines) if f'"type":"{event}"' in line)
    lines[last] = lines[last].replace(old, new)
    (fake / "changed.jsonl").write_text("".join(lines))
    monkeypatch.setenv("FAKE_PI_STREAM", str(fake / "changed.jsonl"))
    work = fake / "work"
    work.mkdir()
    result = make("pi", {**bound(), "web_search": False, "home": str(fake / "home")}).run("go", workspace=work)
    assert result.exit_code == 0 and result.extra["failed"]


def test_a_model_bound_to_anthropic_costs_its_price_not_pis_figure(fake, monkeypatch):
    """Claude Code's own cost is kept for an Anthropic model; Pi's is not: Pi counts a model its models.json gives
    no cost as free, and the caller's price is not in that file."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    work = fake / "work"
    work.mkdir()
    for price, cost in ((None, None), ({"input": 3.0, "cached_input": 0.3, "output": 15.0}, 0.00888)):
        params = bind_agent_params({"model": "anthropic/claude-sonnet-4-5", "price": price}, "pi", registry())
        result = make("pi", {**params, "web_search": False, "home": str(fake / "home")}).run("go", workspace=work)
        assert result.usage[0].cost_usd == (pytest.approx(cost) if cost else None)


def test_a_compactions_request_counts_as_a_call(tmp_path):
    """Pi keeps the usage of a compaction's summary request in its `compaction_end`, not in a message."""
    log = tmp_path / "events.jsonl"
    usage = {"input": 900, "output": 100, "cacheRead": 100, "cacheWrite": 0, "cost": {"total": 0.5}}
    log.write_text(json.dumps({"type": "compaction_end", "reason": "threshold", "aborted": False,
                               "result": {"summary": "s", "usage": usage}}) + "\n")
    [u] = base_cli.Pi.parse(log, "m")[1]
    assert (u.input_tokens, u.cached_input_tokens, u.output_tokens, u.calls, u.cost_usd) == (1000, 100, 100, 1, 0.5)

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
    """The learning material keeps the acting agent's log: an Evolver reads Pi's steps from it like Codex's, and
    records.parse_log reads its session and usage as Pi's (it read a Pi stream as Claude Code's: none of either)."""
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
    from hermit.agent.base.records import parse_log
    session, [usage] = parse_log(STREAMS / "success.jsonl", "pi", "m")
    assert session == SESSION and (usage.calls, usage.input_tokens, usage.output_tokens) == (4, 4000, 80)


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
    assert [(entry["exposure"], entry["timeout"]) for entry in mcp.values()] == [("direct", 120)]
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



def test_a_model_that_takes_images_is_declared_so_to_pi(fake):
    """Pi offers a model it does not know text only, and drops a tool's image for it: `image_input` (agent.pi or
    evolver.pi) declares the bound model's images, through the acting agent's and the Evolver's settings."""
    from hermit.agent.acting.workspace import base_agent
    from hermit.agent.evolver.loader import find, instantiate
    from hermit.config import layered
    data = {"agent": {"base": "pi", "model": "fake/m-1", "pi": {"image_input": True}},
            "evolver": {"name": "pi", "base": "pi", "model": "fake/m-1", "pi": {"image_input": True}}, "llm": None}
    layered(data)
    acting = base_agent("pi", {**data["agent"]["params"], **bound(), "web_search": False})
    assert acting.settings["image_input"] is True
    _, params = instantiate(find("pi", ROOT), data["evolver"]["params"], default_model="fake/m-1",
                            backends=registry({"fake": {"base_url": "http://127.0.0.1:9/v1", "apis": ["chat"]}}))
    assert params["image_input"] is True
    work = fake / "work"
    work.mkdir()
    for image_input, declared in ((True, ["text", "image"]), (False, None)):
        make("pi", {**bound(), "web_search": False, "home": str(fake / "home"), "image_input": image_input}).run(
            "go", workspace=work)
        [model] = json.loads(seen(fake)[-1]["files"]["models.json"])["providers"]["hermit"]["models"]
        assert model.get("input") == declared

def test_a_session_that_only_replies_is_given_nothing_else_and_one_that_ran_a_tool_has_failed(fake, monkeypatch):
    """`text_only`: a role that answers from its prompt, or a model behind a chat endpoint. The recorded stream ran three
    tools, so that session is no reply; the one without tool calls is."""
    work = fake / "work"
    work.mkdir()
    agent = make("pi", {**bound(), "web_search": False, "home": str(fake / "home"), "text_only": True, "stdin": True})
    result = agent.run("go", workspace=work)
    args = seen(fake)[0]["args"]
    assert {"--no-tools", "--no-mcp", "--no-extensions", "--no-skills", "--no-prompt-templates",
            "--no-context-files"} <= set(args)
    assert result.extra["failed"] and not result.extra["served"] and "no tools" in result.extra["failure"]
    monkeypatch.setenv("FAKE_PI_STREAM", str(STREAMS / "resume.jsonl"))
    assert agent.run("go", workspace=work).extra["served"]


def test_allowed_tools_are_pis_own_and_keep_the_calls_tools(fake):
    work = fake / "work"
    work.mkdir()
    agent = make("pi", {**bound(), "web_search": False, "home": str(fake / "home"), "allowed_tools": ["read", "grep"]})
    agent.run("go", workspace=work)
    agent.run("go", workspace=work, tools=[echo()])
    agent.configured(allowed_tools=[]).run("go", workspace=work, tools=[echo()])
    alone, beside, none = [c["args"] for c in seen(fake)]
    assert alone[alone.index("--tools") + 1] == "read,grep"
    assert beside[beside.index("--tools") + 1] == "read,grep,mcp__hermit_tools__*"
    assert "--no-builtin-tools" in none and "--tools" not in none


def test_pi_refuses_an_effort_it_does_not_take_and_keeps_no_session_when_told(fake):
    """Pi 1.5.0 takes off, minimal, low, medium, high, xhigh and max (clamped to what the model supports); it warns
    about any other --thinking level and goes on at its default: refused, as for Claude Code."""
    work = fake / "work"
    work.mkdir()
    agent = make("pi", {**bound(), "web_search": False, "home": str(fake / "home")})
    with pytest.raises(Unsupported, match="Pi takes effort .*not 'bogus'"):
        agent.configured(effort="bogus").run("go", workspace=work)
    agent.configured(persist_session=False, effort="max").run("go", workspace=work)
    args = seen(fake)[0]["args"]
    assert "--no-session" in args and args[args.index("--thinking") + 1] == "max"
    own = make("pi", {"model": "its-own", "web_search": False, "home": str(fake / "own"), "home_files": []})
    with pytest.raises(Unsupported, match="Pi takes effort .*not 'bogus'"):     # a model of Pi's own catalog
        own.configured(effort="bogus").run("go", workspace=work)


def test_the_sessions_effort_is_sent_as_named_and_over_the_backends_extra_body(fake):
    """A session's effort is the call's: a backend's extra_body.reasoning_effort, which Pi applies last (its
    samplingParams), no longer replaces it. xhigh and max, which Pi sends as high for a model with no map of its own,
    are mapped to themselves on an OpenAI-compatible API; on the Messages API, where Pi gives either high's thinking
    budget, they are refused. extra_body goes with a chat request only, and stands where no effort is given."""
    work = fake / "work"
    work.mkdir()

    def model_of(call):
        return json.loads(call["files"]["models.json"])["providers"]["hermit"]["models"][0]

    chat = bound(extra_body={"reasoning_effort": "low", "top_k": 20})
    for effort in (None, "high", "xhigh", "max", "off"):
        make("pi", {**chat, "web_search": False, "home": str(fake / "home"),
                    **({"effort": effort} if effort else {})}).run("go", workspace=work)
    plain, high, xhigh, top, off = [model_of(c) for c in seen(fake)]
    assert plain["samplingParams"] == {"reasoning_effort": "low", "top_k": 20} and "thinkingLevelMap" not in plain
    assert high["samplingParams"] == off["samplingParams"] == {"top_k": 20} and "thinkingLevelMap" not in high
    assert xhigh["thinkingLevelMap"] == {"xhigh": "xhigh"} and top["thinkingLevelMap"] == {"max": "max"}
    responses = bound(apis=["responses"], extra_body={"reasoning_effort": "low", "top_k": 20})
    make("pi", {**responses, "web_search": False, "home": str(fake / "home"), "effort": "max"}).run("go", workspace=work)
    model = model_of(seen(fake)[-1])
    assert "samplingParams" not in model and model["thinkingLevelMap"] == {"max": "max"}
    messages = make("pi", {**bound(apis=["messages"]), "web_search": False, "home": str(fake / "home")})
    for effort in ("xhigh", "max"):
        with pytest.raises(Unsupported, match=f"Messages API high's thinking budget for effort {effort}"):
            messages.configured(effort=effort).run("go", workspace=work)
    messages.configured(effort="high").run("go", workspace=work)
    assert "thinkingLevelMap" not in model_of(seen(fake)[-1])


def ended(fake, name: str, **message) -> str:
    """The recorded resume stream (one request) with these fields of the request's message (its `message_end`), one
    given as None removed."""
    lines = (STREAMS / "resume.jsonl").read_text().splitlines(keepends=True)
    last = max(i for i, line in enumerate(lines) if '"type":"message_end"' in line and '"assistant"' in line)
    event = json.loads(lines[last])
    event["message"] = {k: v for k, v in {**event["message"], **message}.items() if v is not None}
    lines[last] = json.dumps(event, separators=(",", ":")) + "\n"
    (fake / name).write_text("".join(lines))
    return str(fake / name)


def retried(fake, error: str) -> str:
    """An error Pi retries (a 429, a 5xx, a lost connection): the recorded failure stream with `error` for its 503,
    each of its four requests failed, retried three times (`auto_retry_start`), then given up (`auto_retry_end`)."""
    (fake / "retried.jsonl").write_text((STREAMS / "failure.jsonl").read_text().replace(
        json.dumps(FAILED)[1:-1], json.dumps(error)[1:-1]))
    return str(fake / "retried.jsonl")


def once(fake, error: str) -> str:
    """An error Pi sends once (a 4xx but a 429): the resume stream's one request failed with `error`, with no retry
    event and, no answer having come, no stop reason of the provider's."""
    return ended(fake, "once.jsonl", stopReason="error", errorMessage=error, rawStopReason=None, content=[])


def stopped(fake, error: str) -> str:
    """A finish reason Pi does not retry and that is no refusal (DeepSeek's `insufficient_system_resource`): the resume
    stream's one request stopped with it, sent once, with no status."""
    return ended(fake, "stopped.jsonl", stopReason="error", errorMessage=error, rawStopReason=error.split(": ")[-1])


def given_up(fake, error: str, raw: str | None = None) -> str:
    """Outages, then an error Pi does not retry: the recorded failure stream to its third request, which failed with
    `error` (and `raw`, the provider's stop reason, for an answer it declined), the two 503s before it retried
    (`auto_retry_start`), then given up (`auto_retry_end` with `error`)."""
    lines = (STREAMS / "failure.jsonl").read_text().splitlines(keepends=True)
    second, third = [i for i, line in enumerate(lines) if '"type":"auto_retry_start"' in line][1:]
    events = [json.loads(line) for line in lines[second + 1:third]]
    for event in events:
        for message in [event.get("message"), *event.get("messages", [])]:
            if isinstance(message, dict) and message.get("role") == "assistant":
                message.update(errorMessage=error, **({"rawStopReason": raw} if raw else {}))
        if event["type"] == "agent_end":
            event["willRetry"] = False
    events.append({"type": "auto_retry_end", "success": False, "attempt": 2, "finalError": error})
    (fake / "given-up.jsonl").write_text("".join([*lines[:second + 1], *(json.dumps(e, separators=(",", ":")) + "\n"
                                                                         for e in events), lines[-1]]))
    return str(fake / "given-up.jsonl")


@pytest.mark.parametrize("sent, error, unavailable, rejected", [
    (retried, '503: {"message":"busy"}', True, False), (retried, "503 status code (no body)", True, False),
    (retried, "503 <html><body>Service Unavailable</body></html>", True, False),
    (retried, '503 {"error":{"message":"busy"}}', True, False),
    (retried, 'hermit API error (503): {"error":{"message":"busy"}}', True, False),
    (retried, '500: {"message":"internal error"}', True, False),
    (retried, '529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}', True, False),
    (retried, '429 {"error":{"message":"slow down"}}', True, False), (retried, "Connection error.", True, False),
    (once, '400: {"message":"no such field"}', False, True), (once, '401 {"error":{"message":"bad key"}}', False, True),
    (once, 'hermit API error (404): {"error":{"message":"no such model"}}', False, True),
    (once, '408: {"message":"too slow"}', False, False), (once, '409: {"message":"conflict"}', False, False),
    (stopped, "Provider finish_reason: insufficient_system_resource", False, False),
    (given_up, '400: {"message":"no such field"}', False, True)],
    ids=["outage", "outage, no body", "outage, a proxy's page", "outage, Messages API", "outage, Responses API",
         "server error", "overloaded, Messages API", "rate limit, Messages API", "no connection, retries given up",
         "bad request", "bad key, Messages API", "no such model, Responses API", "request timeout", "conflict",
         "stopped by the provider, no status", "outages, then a bad request"])
def test_a_failed_requests_status_says_whether_a_retry_can_help(fake, monkeypatch, sent, error, unavailable, rejected):
    """Pi reports a provider's status in its own forms (those Pi 1.5.0 printed): `503: {...}` or `503 status code (no
    body)` from chat completions, `503 {...}` from the Messages API, `<provider> API error (503): ...` from the
    Responses API; it retries a 429, a 500 to 504, an overload and a lost connection, and sends a 4xx once. An outage
    (any 5xx) or a rate limit is `unavailable` (retried; the chat endpoint's 503), and so is an error with no status
    that Pi retried until it gave up (a dropped connection), but not one it sent once (a finish reason); a status no
    retry can help is `rejected` (the chat endpoint's 400, which a client does not retry); a timeout or a conflict is
    neither. The last request's status decides: outages that ended in a bad request are rejected."""
    from hermit.agent.base.endpoint import _no_reply
    monkeypatch.setenv("FAKE_PI_STREAM", sent(fake, error))
    raw = Path(os.environ["FAKE_PI_STREAM"]).read_text()
    assert base_cli.Pi._last_assistant(raw)["errorMessage"] == error
    assert ('"auto_retry_start"' in raw) == (sent in (retried, given_up))
    work = fake / "work"
    work.mkdir()
    agent = make("pi", {**bound(), "web_search": False, "home": str(fake / "home")})
    result = agent.run("go", workspace=work)
    assert result.extra["failed"] and (result.extra["unavailable"], result.extra["rejected"]) == (unavailable, rejected)
    assert not result.extra["model_refusal"] and _no_reply(agent, result)[0] == (
        503 if unavailable else 400 if rejected else 502)


@pytest.mark.parametrize("error, raw", [("The model refused to complete the request", "refusal"),
                                        ("501 words of the answer are withheld by the usage policy", "refusal"),
                                        ("Provider stopped with: sensitive", "sensitive"),
                                        ("Provider finish_reason: content_filter", "content_filter"),
                                        ("Response incomplete: content_filter", "incomplete.content_filter")],
                         ids=["refusal, Messages API", "refusal, explained with a number first",
                              "sensitive, Messages API", "content filter, chat completions",
                              "content filter, Responses API"])
def test_an_answer_the_provider_declined_is_a_refusal_no_retry_can_help(fake, monkeypatch, error, raw):
    """Pi 1.5.0 ends a request whose answer the provider stopped as an error, sent once, with the provider's stop
    reason (`rawStopReason`) and its words (on the Messages API the refusal's explanation, when it gives one):
    `rejected` and `model_refusal`, as on Claude Code, and never `unavailable` (the chat endpoint's 400), whatever
    Pi retried before it and whatever number the words start with."""
    from hermit.agent.base.endpoint import _no_reply
    work = fake / "work"
    work.mkdir()
    agent = make("pi", {**bound(), "web_search": False, "home": str(fake / "home")})
    for stream in (ended(fake, "declined.jsonl", stopReason="error", errorMessage=error, rawStopReason=raw),
                   given_up(fake, error, raw)):
        monkeypatch.setenv("FAKE_PI_STREAM", stream)
        result = agent.run("go", workspace=work)
        assert result.extra["failed"] and result.extra["rejected"] and result.extra["model_refusal"]
        assert not result.extra["unavailable"] and _no_reply(agent, result)[0] == 400


def test_command_timeout_s_from_the_config_reaches_the_acting_agents_and_the_learners_sessions(fake):
    """`agent.pi` and `evolver.pi` give Pi's settings (PI_PARAMS) to the acting agent's and the learner's sessions:
    with command_timeout_s 0, neither session's home has Hermit's extension."""
    from types import SimpleNamespace as NS
    from hermit.agent.acting.workspace import base_agent
    from hermit.agent.evolver.loader import find, instantiate
    from hermit.config import layered
    data = {"agent": {"base": "pi", "model": "fake/m-1", "pi": {"command_timeout_s": 0}},
            "evolver": {"name": "pi", "base": "pi", "model": "fake/m-1", "pi": {"command_timeout_s": 0}}, "llm": None}
    layered(data)
    work = fake / "work"
    work.mkdir()
    acting = base_agent("pi", {**data["agent"]["params"], **bound(), "web_search": False})
    assert acting.settings["command_timeout_s"] == 0
    acting.configured(home=str(fake / "acting")).run("go", workspace=work)
    evolver, params = instantiate(find("pi", ROOT), data["evolver"]["params"], default_model="fake/m-1",
                                  backends=registry({"fake": {"base_url": "http://127.0.0.1:9/v1",
                                                              "api_key_env": "FAKE_KEY", "apis": ["chat"]}}))
    ws = NS(state=fake / "state" / "now", output=fake / "out")
    ws.output.mkdir()
    settings = evolver.settings(NS(params=params, sandbox=None, workspace=ws, cancel=None), "pi")
    assert settings["command_timeout_s"] == 0
    make("pi", settings).run("go", workspace=work)
    assert [call["extensions"] for call in seen(fake)] == [{}, {}]


def test_a_command_the_model_gives_no_time_limit_gets_the_sessions_and_only_hermits_extension_is_in_the_home(fake):
    """Pi's bash runs a command until it ends unless the model gives it a limit: Hermit's extension in the session's
    home gives the others `command_timeout_s` (600 s unless set; 0 for none). The home's extensions folder is
    Hermit's alone, so what a session left there is gone before the next one."""
    work, home = fake / "work", fake / "home"
    work.mkdir()
    (home / "extensions").mkdir(parents=True)
    (home / "extensions" / "left.js").write_text("export default function () {}\n")
    agent = make("pi", {**bound(), "web_search": False, "home": str(home)})
    agent.run("go", workspace=work)
    agent.configured(command_timeout_s=30).run("go", workspace=work)
    agent.configured(command_timeout_s=0).run("go", workspace=work)
    default, thirty, none = [c["extensions"] for c in seen(fake)]
    assert list(default) == ["hermit-command-limit.js"]
    assert 'event.toolName === "bash" && !(event.input.timeout > 0)) event.input.timeout = 600;' in \
        default["hermit-command-limit.js"]
    assert "event.input.timeout = 30;" in thirty["hermit-command-limit.js"]
    assert none == {} and not (home / "extensions").exists()
    (home / "extensions").write_text("a file, not a folder")
    agent.run("go", workspace=work)
    assert list(seen(fake)[-1]["extensions"]) == ["hermit-command-limit.js"]


def test_a_model_through_pi_answers_each_request_with_a_session_that_only_replies(fake, monkeypatch):
    from hermit.agent.base.endpoint import cli_answer
    monkeypatch.setenv("FAKE_PI_STREAM", str(STREAMS / "resume.jsonl"))
    agent = make("pi", {**bound(), "web_search": False})
    status, body, _ = cli_answer(agent, {"messages": [{"role": "system", "content": "Be exact."},
                                                      {"role": "user", "content": "hello"}], "max_tokens": 10})
    reply = json.loads(body)
    [call] = seen(fake)
    assert status == 200 and reply["choices"][0]["message"]["content"] == "the answer" and reply["unapplied"] == ["max_tokens"]
    assert call["stdin"] == "hello" and "--no-tools" in call["args"]
    assert call["args"][call["args"].index("--append-system-prompt") + 1] == "Be exact."


def cut_stream(fake, text: bool = True):
    """The recorded resume stream with its reply ended at the output limit, and with no text but its thinking."""
    return ended(fake, "cut.jsonl", stopReason="length",
                 **({} if text else {"content": [{"type": "thinking", "thinking": "a long thought"}]}))


def test_a_reply_cut_at_the_output_limit_comes_back_through_the_chat_endpoint_marked_so(fake, monkeypatch):
    """As from an API: the methods that call the run's model read `finish_reason: length` (GEPA, SkillOpt and ReHarness
    discard or retry such a reply), and one with no text is no error to retry the same way."""
    from hermit.agent.base.endpoint import cli_answer
    agent = make("pi", {**bound(), "web_search": False})
    request = {"messages": [{"role": "user", "content": "hello"}]}
    for text, content in ((True, "the answer"), (False, "")):
        monkeypatch.setenv("FAKE_PI_STREAM", cut_stream(fake, text))
        status, body, _ = cli_answer(agent, request)
        choice = json.loads(body)["choices"][0]
        assert status == 200 and choice["finish_reason"] == "length" and choice["message"]["content"] == content
    work = fake / "work"
    work.mkdir()
    result = make("pi", {**bound(), "web_search": False, "home": str(fake / "home")}).run("go", workspace=work)
    assert result.finish_reason == "length" and result.extra["failed"]


def test_a_requests_sampling_fields_reach_pis_model_where_its_api_takes_them(fake, monkeypatch):
    """Pi sends a model's samplingParams to an OpenAI-compatible API: a request's temperature and top_p are applied
    there, not refused; on the Messages API Pi would drop them, so there they are refused, as on the other CLIs."""
    from hermit.agent.base.endpoint import cli_answer
    monkeypatch.setenv("FAKE_PI_STREAM", str(STREAMS / "resume.jsonl"))
    request = {"messages": [{"role": "user", "content": "hello"}], "temperature": 0.3, "top_p": 0.9}
    status, body, _ = cli_answer(make("pi", {**bound(extra_body={"seed": 1}), "web_search": False}), request)
    [model] = json.loads(seen(fake)[0]["files"]["models.json"])["providers"]["hermit"]["models"]
    assert status == 200 and "unapplied" not in json.loads(body)
    assert model["samplingParams"] == {"seed": 1, "temperature": 0.3, "top_p": 0.9}
    status, body, _ = cli_answer(make("pi", {**bound(apis=["responses"]), "web_search": False}), request)
    [model] = json.loads(seen(fake)[1]["files"]["models.json"])["providers"]["hermit"]["models"]
    assert status == 200 and model["samplingParams"] == {"temperature": 0.3, "top_p": 0.9}
    status, body, _ = cli_answer(make("pi", {**bound(apis=["messages"]), "web_search": False}), request)
    assert status == 400 and "temperature" in json.loads(body)["error"]["message"] and len(seen(fake)) == 2


@pytest.mark.parametrize("spec, match", [({}, "needs a <backend>/<model> reference"),
                                         ({"login": "chatgpt"}, "not login: chatgpt"),
                                         ({"binding": {"x": 1}, "isolation": "container"}, "needs isolation: sandbox"),
                                         ({"binding": {"x": 1}, "web_search": True}, "web_search cannot be on"),
                                         ({"binding": {"x": 1}, "effort": "bogus"}, "takes effort off")])
def test_what_a_model_through_pi_cannot_be_is_said_before_any_session(spec, match):
    from hermit.agent.base.endpoint import cli_problems
    assert any(match in problem for problem in cli_problems({"cli": "pi", **spec}))


def test_evolver_base_pi_is_the_run_model_of_a_method_that_calls_it_and_the_learner_of_one_that_has_a_cli():
    """One setting for every method: GEPA, SkillOpt and the rest call the run's model, so on Pi that model is a Pi session
    (`llm: {cli: pi}`); mwm, EvoSafe and the others run a CLI of their own, and Pi is that CLI."""
    from hermit.agent.model.backends import bindings_of, normalize_config
    from hermit.config import layered
    fake_backend = {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "FAKE_KEY", "apis": ["chat"]}
    data = {"agent": {}, "llm": None, "backends": {"fake": fake_backend},
            "evolver": {"name": "gepa", "base": "pi", "model": "fake/m-1", "pi": {"unapplied": ["temperature"]}, "params": {}}}
    layered(data)
    normalize_config(data)
    assert data["llm"]["cli"] == "pi" and data["llm"]["provider"]["apiKey"] == "$FAKE_KEY"
    assert data["llm"]["unapplied"] == ["temperature"] and data["llm"]["model"] == "m-1"
    assert "backend" not in data["evolver"]["params"]
    [(who, _, dialect)] = bindings_of(data)
    assert who == "llm tool through pi" and dialect == "chat"
    data = {"agent": {}, "llm": None, "backends": {"fake": fake_backend},     # a model mapping binds the same way
            "evolver": {"name": "ace", "base": "pi", "model": {"ref": "fake/m-1", "extra_body": {"top_k": 20}},
                        "params": {}}}
    layered(data)
    normalize_config(data)
    assert data["llm"]["model"] == "m-1" and data["llm"]["provider"]["apiKey"] == "$FAKE_KEY"
    assert data["llm"]["provider"]["models"][0]["samplingParams"] == {"top_k": 20}
    for name in ("mwm", "evosafe", "rrsi_adapter", "ahe_adapter", "harness2_upstream", "meta_harness_adapter", "aspire"):
        data = {"agent": {}, "llm": None, "evolver": {"name": name, "base": "pi", "model": "fake/m-1", "params": {}}}
        layered(data)
        assert data["evolver"]["params"]["backend"] == "pi" and data["llm"] is None, name
    with pytest.raises(ValueError, match="llm names it too"):
        layered({"agent": {}, "llm": "fake/m-1", "evolver": {"name": "opro", "base": "pi", "model": "fake/m-1", "params": {}}})
    data = {"agent": {}, "llm": None, "model": "fake/m-1", "evolver": {"name": "ace", "base": "pi", "params": {}}}
    layered(data)                                         # the run's default model, when evolver.model is not given
    assert data["llm"] == {"cli": "pi", "model": "fake/m-1"}
    data = {"agent": {}, "llm": None, "backends": {"anth": {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "FAKE_KEY",
                                                            "apis": ["messages"]}},
            "evolver": {"name": "gepa", "base": "pi", "model": "anth/m-2", "params": {}}}
    layered(data)
    normalize_config(data)
    [(_, _, dialect)] = bindings_of(data)                  # the dialect Pi speaks to this backend
    assert dialect == "messages" and data["llm"]["provider"]["api"] == "anthropic-messages"


def test_a_custom_evolver_takes_evolver_base_pi_as_its_cli_unless_its_manifest_says_it_calls_the_runs_model(tmp_path,
                                                                                                         monkeypatch):
    """A launcher of the agent Evolver (docs/framework/custom-evolver.md) names no backend in its manifest: Pi is its
    CLI, as before. One whose manifest says `needs: [llm]` calls the run's model, which becomes Pi; a relative name is
    found beside the config."""
    from toy_task import make_task
    from hermit.config import Config
    monkeypatch.setenv("FAKE_KEY", "sk-fake")
    for name, needs in (("agentic", []), ("chatty", ["llm"])):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "evolver.yaml").write_text("class: hermit.agent.evolver.agent:AgentEvolver\n"
                                             f"needs: {json.dumps(needs)}\nparams: {{prompt: START.md, web_search: false}}\n")
    backends = {"fake": {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "FAKE_KEY", "apis": ["chat"]}}
    path = make_task(tmp_path, evolver={"name": "agentic", "base": "pi", "model": "fake/m-1"}, backends=backends)
    agentic = Config.load(path).data
    assert agentic["evolver"]["params"]["backend"] == "pi" and not agentic.get("llm")
    chatty = Config.load(path, ["evolver.name=chatty"]).data
    assert chatty["llm"]["cli"] == "pi" and "backend" not in chatty["evolver"]["params"]


def test_a_pipelines_model_through_pi_is_bound_as_the_learners_is():
    from hermit.agent.model.backends import normalize_config
    data = {"agent": {"name": "react", "params": {"model": {"cli": "pi", "model": "fake/m-1"}}},
            "backends": {"fake": {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "FAKE_KEY", "apis": ["chat"]}}}
    normalize_config(data)
    spec = data["agent"]["params"]["model"]
    assert spec["cli"] == "pi" and spec["provider"]["apiKey"] == "$FAKE_KEY" and spec["model"] == "m-1"


# ---------- the installed pi (integration) ----------

def serve_chat(script):
    """A chat-completions model on the loopback: `script(request body) -> (tool call (name, arguments) | None)`;
    None answers `the answer`, a status (an int) is an error with that status, and a finish reason (a str, such as
    `content_filter`) ends an answer that has no text. Every answer is streamed, with usage."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def chunk(body):
        return f"data: {json.dumps(body)}\n\n".encode()

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            call = script(body)
            if isinstance(call, int):
                error = json.dumps({"error": {"message": f"scripted {call}"}}).encode()
                self.send_response(call)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(error)))
                self.end_headers()
                self.wfile.write(error)
                return
            tool = isinstance(call, tuple)
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                          "function": {"name": call[0],
                                                                       "arguments": json.dumps(call[1])}}]} \
                if tool else {"role": "assistant", "content": "" if call else "the answer"}
            data = b"".join([chunk({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}),
                             chunk({"choices": [{"index": 0, "delta": {}, "finish_reason":
                                                 "tool_calls" if tool else call or "stop"}]}),
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


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("pi"), reason="needs the pi CLI")
@pytest.mark.parametrize("limit, offered", [({"text_only": True, "stdin": True}, []),
                                            ({"allowed_tools": []}, ["mcp__hermit_tools__echo"]),
                                            ({"allowed_tools": ["read", "grep"]},
                                             ["grep", "mcp__hermit_tools__echo", "read"])])
def test_with_the_pi_cli_a_limited_session_is_offered_only_what_it_may_use(tmp_path, monkeypatch, limit, offered):
    """The installed Pi, bound to a scripted model: what the model is offered is what a role or a text session can do.
    A role's read-only tools keep the call's tools, and none of them is offered to a session that only replies."""
    asked = []

    def script(body):
        asked.append(sorted(t["function"]["name"] for t in body.get("tools") or []))

    server, url = serve_chat(script)
    monkeypatch.setenv("MOCK_KEY", "sk-mock")
    params = bind_agent_params({"model": "mock/m-1"}, "pi",
                               registry({"mock": {"base_url": url, "api_key_env": "MOCK_KEY", "apis": ["chat"]}}))
    work = tmp_path / "work"
    work.mkdir()
    settings = {**params, "web_search": False, "home": str(tmp_path / "home"), "timeout_s": 300, **limit}
    try:
        result = make("pi", settings).run("Do the task.", workspace=work, tools=[] if limit.get("text_only") else [echo()])
    finally:
        server.shutdown()
    assert result.extra["served"] and result.reply == "the answer" and asked == [offered]


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("pi"), reason="needs the pi CLI")
@pytest.mark.parametrize("isolation", ["none", "sandbox"])
def test_with_the_pi_cli_a_command_without_a_limit_stops_at_the_sessions_and_one_with_a_limit_keeps_it(
        tmp_path, monkeypatch, isolation):
    """The installed Pi, bound to a scripted model that runs two commands: one with a limit of its own, which it keeps,
    and one without, which Hermit's extension stops after `command_timeout_s`; the session goes on to its answer."""
    import time
    from hermit.sandbox import Sandbox
    if isolation == "sandbox" and not _bwrap_works():
        pytest.skip("needs a working bubblewrap")

    def script(body):
        done = sum(1 for m in body.get("messages") or [] if m.get("role") == "tool")
        plan = [("bash", {"command": "sleep 3; echo kept", "timeout": 30}), ("bash", {"command": "sleep 60; echo late"})]
        return plan[done] if done < len(plan) else None

    server, url = serve_chat(script)
    monkeypatch.setenv("MOCK_KEY", "sk-mock")
    params = bind_agent_params({"model": "mock/m-1"}, "pi",
                               registry({"mock": {"base_url": url, "api_key_env": "MOCK_KEY", "apis": ["chat"]}}))
    work = tmp_path / "work"
    work.mkdir()
    settings = {**params, "web_search": False, "home": str(tmp_path / "home"), "isolation": isolation,
                "timeout_s": 300, "command_timeout_s": 2}
    if isolation == "sandbox":
        settings["sandbox"] = Sandbox(network="isolated", env_allow=[], writable=[work], role="agent",
                                      record_dir=tmp_path / "records")
    start = time.monotonic()
    try:
        result = make("pi", settings).run("Do the task.", workspace=work)
    finally:
        server.shutdown()
    assert result.extra["served"] and result.reply == "the answer" and time.monotonic() - start < 45
    kept, stopped = [s for s in result.trajectory if s.kind == "command"]
    assert "kept" in kept.output and "late" not in stopped.output and "timed out" in stopped.output.lower()


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("pi"), reason="needs the pi CLI")
def test_with_the_pi_cli_a_requests_temperature_and_the_backends_extra_body_reach_the_model(tmp_path, monkeypatch):
    """The installed Pi behind the chat endpoint: what it sends is what the method asked and the backend adds."""
    from hermit.agent.base.endpoint import cli_answer
    bodies = []

    def script(body):
        bodies.append(body)

    server, url = serve_chat(script)
    monkeypatch.setenv("MOCK_KEY", "sk-mock")
    params = bind_agent_params({"model": "mock/m-1"}, "pi", registry({"mock": {
        "base_url": url, "api_key_env": "MOCK_KEY", "apis": ["chat"], "extra_body": {"reasoning_effort": "low"}}}))
    try:
        status, body, _ = cli_answer(make("pi", {**params, "web_search": False, "home": str(tmp_path / "home"),
                                                 "timeout_s": 300}),
                                     {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.3})
    finally:
        server.shutdown()
    [sent] = bodies
    assert status == 200 and sent["temperature"] == 0.3 and sent["reasoning_effort"] == "low" and not sent.get("tools")


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("pi"), reason="needs the pi CLI")
@pytest.mark.parametrize("asked, effort, sent", [("high", None, "high"), (None, "max", "max"), (None, "xhigh", "xhigh"),
                                                 (None, None, "low")],
                         ids=["the request's", "max", "xhigh", "the backend's"])
def test_with_the_pi_cli_the_model_is_sent_the_calls_effort_over_the_backends(tmp_path, monkeypatch, asked, effort,
                                                                              sent):
    """The installed Pi behind the chat endpoint, its backend's extra_body asking for low: the request's or the
    session's effort is what the model is sent, xhigh and max as named; with neither, the backend's."""
    from hermit.agent.base.endpoint import cli_answer
    bodies = []

    def script(body):
        bodies.append(body)

    server, url = serve_chat(script)
    monkeypatch.setenv("MOCK_KEY", "sk-mock")
    params = bind_agent_params({"model": "mock/m-1"}, "pi", registry({"mock": {
        "base_url": url, "api_key_env": "MOCK_KEY", "apis": ["chat"], "extra_body": {"reasoning_effort": "low"}}}))
    agent = make("pi", {**params, "web_search": False, "home": str(tmp_path / "home"), "timeout_s": 300,
                        **({"effort": effort} if effort else {})})
    try:
        status, _, _ = cli_answer(agent, {"messages": [{"role": "user", "content": "hi"}],
                                          **({"reasoning_effort": asked} if asked else {})})
    finally:
        server.shutdown()
    assert status == 200 and [b.get("reasoning_effort") for b in bodies] == [sent]


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("pi"), reason="needs the pi CLI")
@pytest.mark.parametrize("answers, retries, rejected, refusal, status", [
    ([503, 503, 400], ["auto_retry_start", "auto_retry_start", "auto_retry_end"], True, False, 400),
    ([409], [], False, False, 502), (["content_filter"], [], True, True, 400)],
    ids=["outages, then a bad request", "a conflict", "a content filter"])
def test_with_the_pi_cli_the_last_requests_status_or_stop_reason_says_whether_a_retry_can_help(
        tmp_path, monkeypatch, answers, retries, rejected, refusal, status):
    """The installed Pi, bound to a scripted model that answers each request in turn: Pi retries a 503 (after 2 s,
    then 4 s) and gives up at a 400, sends a 409 or a content filter once, and the session reads as the streams built
    for test_a_failed_requests_status_says_whether_a_retry_can_help and the refusal test do."""
    from hermit.agent.base.endpoint import _no_reply
    asked = []

    def script(body):
        asked.append(body)
        return answers[min(len(asked), len(answers)) - 1]

    server, url = serve_chat(script)
    monkeypatch.setenv("MOCK_KEY", "sk-mock")
    params = bind_agent_params({"model": "mock/m-1"}, "pi",
                               registry({"mock": {"base_url": url, "api_key_env": "MOCK_KEY", "apis": ["chat"]}}))
    work = tmp_path / "work"
    work.mkdir()
    agent = make("pi", {**params, "web_search": False, "home": str(tmp_path / "home"), "timeout_s": 300,
                        "log": str(tmp_path / "agent-pi.jsonl")})
    try:
        result = agent.run("Do the task.", workspace=work)
    finally:
        server.shutdown()
    events = [json.loads(line) for line in (tmp_path / "agent-pi.jsonl").read_text().splitlines() if line.strip()]
    assert len(asked) == len(answers) and [e["type"] for e in events if e["type"].startswith("auto_retry")] == retries
    assert result.extra["failed"] and not result.extra["unavailable"]
    assert (result.extra["rejected"], result.extra["model_refusal"]) == (rejected, refusal)
    assert _no_reply(agent, result)[0] == status
