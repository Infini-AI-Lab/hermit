"""The `mini-swe-agent` Base Agent (hermit.agent.base.cli.MiniSweAgent, its process
hermit.agent.base.mini_swe_agent_run), the `mini-swe-agent` acting agent built on it (hermit.agent.acting.workspace)
and the deploy host (hermit.hosts.mini_swe_agent). The stand-in is the model: an OpenAI-compatible chat/completions
server answering from scripted assistant messages, its payload in the shape OpenAI's API returns; mini-swe-agent's
own loop, environment and litellm run unchanged. Covered: the messages, the tools as the `hermit-tool` command, what
the commands' environment holds, the stream read into the reply, trajectory and usage, the limits, retries, what is
refused, and the deployed config giving `mini` the system message the Base Agent sends."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

from jinja2 import StrictUndefined, Template
import pytest
import yaml

from hermit.agent.acting.agent import load_agent
from hermit.agent.base import mini_swe_agent as mini
from hermit.agent.base.interface import Unsupported, make
from hermit.agent.base.trajectory import log_steps
from hermit.agent.model.backends import bind_agent_params, registry
from hermit.hosts import mini_swe_agent as host
from test_isolate import needs_bwrap  # noqa: F401  (a marker, not a fixture: the bubblewrap check it carries)
from toy_task import make_task

pytestmark = pytest.mark.skipif(importlib.util.find_spec("minisweagent") is None,
                                reason="needs mini-swe-agent: python -m pip install -e '.[mini-swe-agent]'")

ROOT = Path(__file__).resolve().parents[2]
FILE_ADAPTER = ROOT / "docs" / "_examples" / "file_adapter"
KEY = "sk-test-not-a-real-key-7f3a"
PRICE = {"input": 1.0, "cached_input": 0.5, "output": 2.0}


class Scripted:
    """An OpenAI chat/completions server answering from scripted assistant messages; it records each request's body
    and Authorization header. A reply `(status, message)` is an error response."""

    def __init__(self, replies):
        self.replies, self.requests, self.auth = list(replies), [], []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                outer.auth.append(self.headers.get("Authorization"))
                message = outer.replies.pop(0)
                if isinstance(message, tuple):
                    status, payload = message[0], {"error": {"message": message[1], "type": "invalid_request_error"}}
                else:
                    status = 200
                    payload = {"id": f"r{len(outer.requests)}", "object": "chat.completion", "created": 0, "model": "m",
                               "choices": [{"index": 0, "message": message,
                                            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                               "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                                         "prompt_tokens_details": {"cached_tokens": 40}}}
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def scripted():
    servers = []

    def start(replies):
        servers.append(Scripted(replies))
        return servers[-1]
    yield start
    for server in servers:
        server.close()


def bash(command, cid):
    return {"role": "assistant", "content": f"Running {cid}.", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": command})}}]}


def text(command):
    return {"role": "assistant", "content": f"THOUGHT: next.\n\n```mswea_bash_command\n{command}\n```"}


def submit(cid="done"):
    return bash("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat answer.txt", cid)


def backends(server):
    return registry({"scripted": {"base_url": server.url, "api_key_env": "HERMIT_TEST_KEY", "litellm": "openai",
                                  "apis": ["chat"], "prices": {"m": PRICE}}}, user_file=None)


def params(server, **more):
    """The acting agent's params as a run config gives them: the model reference bound as normalize_config binds it."""
    return bind_agent_params({"model": "scripted/m", "web_search": False, "workspaces": "ws", **more},
                             "mini-swe-agent", backends(server))


def adapter_copy(tmp_path):
    adapter = tmp_path / "adapter"
    shutil.copytree(FILE_ADAPTER, adapter)
    return adapter


def test_the_acting_agent_runs_the_adapters_tools_without_the_key_and_submits(tmp_path, monkeypatch, scripted):
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-leak")
    monkeypatch.setenv("MY_TOKEN", "tok-expanded-value")
    server = scripted([
        bash("hermit-tool count_words '{\"text\": \"one two three\"}'", "c1"),
        bash("hermit-tool nope '{}'", "c2"),
        bash("echo key=${HERMIT_TEST_KEY:-unset} secret=${UNRELATED_SECRET:-unset} token=$TOKEN", "c3"),
        bash("head -3 hermit/file-example/skills/verify-output/SKILL.md", "c4"),
        bash("printf '42\\n' > answer.txt", "c5"),
        submit(),
    ])
    agent = load_agent("mini-swe-agent", params(server, prompt="Q: {question}", env={"TOKEN": "$MY_TOKEN"}),
                       tmp_path)
    out = agent.run({"id": "t1", "question": "What is six times seven?"}, adapter_copy(tmp_path))

    assert out.output == "42" and out.extra["exit_code"] == 0 and out.extra["backend"] == "mini-swe-agent"
    system, user = server.requests[0]["messages"][:2]
    assert "Review the task requirements before acting." in system["content"]           # ADAPTER.md
    assert "`hermit/file-example/skills/verify-output/SKILL.md`" in system["content"]   # its skills and roles, named
    assert "`reviewer`" in system["content"] and "`count_words`" in system["content"]
    assert "Please solve this task: Q: What is six times seven?" in user["content"]
    assert [t["function"]["name"] for t in server.requests[0]["tools"]] == ["bash"]
    assert server.auth == [f"Bearer {KEY}"] * 6                         # the key goes with each request
    steps = log_steps(out.log)
    commands = [s for s in steps if s.kind == "command"]
    assert [s.exit_code for s in commands] == [0, 1, 0, 0, 0, 0]
    assert json.loads(commands[0].output) == {"count": 3} and "Unknown tool: nope" in commands[1].output
    assert "key=unset secret=unset token=tok-expanded-value" in commands[2].output
    assert "name: verify-output" in commands[3].output and commands[5].output == "42\n"
    # Six responses of 100 prompt tokens (40 cached) and 10 output tokens, at the backend's price.
    assert out.tokens == 660 and out.api_speed == {"model_calls": 6}
    assert out.cost_usd == pytest.approx((6 * 60 * 1.0 + 6 * 40 * 0.5 + 6 * 10 * 2.0) / 1e6)
    run_dir = Path(out.log).parent
    assert (run_dir / "work" / "answer.txt").read_text() == "42\n"
    for path in run_dir.rglob("*"):                  # the stream, the deployment, the workspace: no key, no token
        if path.is_file():
            assert KEY not in path.read_text(errors="replace"), path
            if path.name != "agent-mini-swe-agent.jsonl":    # the stream holds the command's output, as it ran
                assert "tok-expanded-value" not in path.read_text(errors="replace"), path


def test_the_text_based_model_class_answers_in_bash_blocks_with_no_adapter(tmp_path, monkeypatch, scripted):
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    server = scripted([text("echo hello > answer.txt"),
                       text("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat answer.txt")])
    out = load_agent("mini-swe-agent", params(server, model_class="litellm_textbased"), tmp_path).run(
        {"id": "t2", "prompt": "Say hello."}, None)
    assert out.output == "hello" and "tools" not in server.requests[0]
    assert "mswea_bash_command" in server.requests[0]["messages"][0]["content"]
    assert mini.CONTEXT_HEADING not in server.requests[0]["messages"][0]["content"]


def test_the_step_limit_ends_a_task_with_its_last_message_as_the_answer(tmp_path, monkeypatch, scripted):
    """The config file's agent.step_limit wins over the setting, and its model section is merged (cost_tracking
    there does not collide with Hermit's own)."""
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    server = scripted([bash("echo one", "c1"), bash("echo two", "c2"), bash("echo three", "c3")])
    (tmp_path / "mini.yaml").write_text("agent:\n  step_limit: 2\nmodel:\n  cost_tracking: default\n")
    out = load_agent("mini-swe-agent", params(server, config="mini.yaml", step_limit=9), tmp_path).run(
        {"id": "t3", "prompt": "Count."}, None)
    assert len(server.requests) == 2 and out.output == "Running c2." and out.extra["exit_code"] == 0
    assert [s.text for s in log_steps(out.log) if s.kind == "error"] == ["LimitsExceeded: LimitsExceeded"]


def test_a_failed_model_call_fails_the_session_with_the_calls_it_made(tmp_path, monkeypatch, scripted):
    """A context overflow breaks the session off: it failed, with no answer to judge (an error record, as a CLI's
    failed session is), and the tokens it spent still count."""
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    server = scripted([bash("echo hi", "c1"), (400, "This model's maximum context length is 8192 tokens.")])
    out = load_agent("mini-swe-agent", params(server, answer="<a>(.*?)</a>"), tmp_path).run(
        {"id": "t4", "prompt": "Go."}, None)
    assert out.extra["exit_code"] == 1 and out.output is None
    assert "ContextWindowExceededError" in out.extra["session_failed"]
    assert out.tokens == 110 and out.api_speed == {"model_calls": 1}
    assert "ContextWindowExceededError" in [s for s in log_steps(out.log) if s.kind == "error"][-1].text


def test_a_process_cut_off_after_the_model_answered_fails_the_session(tmp_path, monkeypatch, scripted):
    """A session ends with mini-swe-agent's exit message; the process killed before it (here by its own command, as
    an out-of-memory kill would) leaves none, and the session failed: not an empty answer to judge."""
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    server = scripted([bash("kill -9 $PPID", "c1")])
    out = load_agent("mini-swe-agent", params(server), tmp_path).run({"id": "t12", "prompt": "Go."}, None)
    assert out.output is None and "session_failed" in out.extra and len(server.requests) == 1


def test_the_settings_mini_swe_agent_cannot_take_are_refused_before_any_task(tmp_path):
    from hermit.config import Config
    for more, match in ((dict(login="chatgpt"), "takes no login"), (dict(isolation="container"), "isolation")):
        with pytest.raises(ValueError, match=match):
            load_agent("mini-swe-agent", {"model": "m", "web_search": False, **more}, tmp_path).run(
                {"id": "t", "prompt": "x"}, None)
    path = make_task(tmp_path, sandbox={"enabled": True}, model="deepseek/deepseek-flash",
                     agent={"name": "mini-swe-agent", "params": {"web_search": False, "login": "chatgpt"}})
    with pytest.raises(ValueError, match="agent mini-swe-agent takes no login"):
        Config.load(path, [])


def test_a_provider_that_stops_serving_midway_is_tried_again_not_scored(tmp_path, monkeypatch, scripted):
    """A rate limit that mini-swe-agent's own retries could not get past, after the model has answered once, is the
    provider's failure, not the task's: the task runs again in a fresh workspace (CliAgent's UNAVAILABLE, through the
    event's `unavailable`), and the first attempt's mid-task text is never the answer."""
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    monkeypatch.setenv("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", "1")       # mini-swe-agent's own: once, no wait
    (tmp_path / "mini.yaml").write_text("model:\n  model_kwargs:\n    max_retries: 0\n")   # nor the client's
    server = scripted([bash("echo looking", "c0"), (429, "Rate limit reached for requests."),
                       bash("printf 'ok' > answer.txt", "c1"), submit()])
    out = load_agent("mini-swe-agent", params(server, config="mini.yaml", retries=2, retry_wait_s=0), tmp_path).run(
        {"id": "t7", "prompt": "Go."}, None)
    assert out.output == "ok" and out.extra["attempts"] == 2 and len(server.requests) == 4


@pytest.mark.parametrize("status,error", [(401, "Incorrect API key provided."), (404, "The model m does not exist."),
                                          (400, "Authentication Fails, Your api key is invalid")])   # DeepSeek's
def test_a_wrong_key_or_model_on_the_first_call_stops_the_agent_rather_than_score(tmp_path, monkeypatch, scripted,
                                                                                  status, error):
    """A setting every task shares, found wrong by the first model call, is no task's wrong answer: no session starts
    (the events are held back until the model answers) and the acting agent raises."""
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    server = scripted([(status, error)])
    with pytest.raises(RuntimeError, match="before starting a session"):
        load_agent("mini-swe-agent", params(server), tmp_path).run({"id": "t10", "prompt": "Go."}, None)
    assert len(server.requests) == 1


def test_a_timeout_stops_the_command_that_is_running(tmp_path, monkeypatch, scripted):
    """A command runs in a session of its own, which the Base Agent's kill of the process group does not reach (on
    macOS nothing else finds it): the process kills it when it is stopped. The delay is what the test holds."""
    import time
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    marker = tmp_path / "still-running"
    server = scripted([bash(f"sleep 6; touch {marker}", "c1")])
    started = time.time()
    out = load_agent("mini-swe-agent", params(server, timeout_s=5), tmp_path).run({"id": "t11", "prompt": "Go."}, None)
    assert out.extra["timed_out"] is True and len(server.requests) == 1
    time.sleep(max(0.0, started + 9 - time.time()))           # past when the command would have finished
    assert not marker.exists()


def test_a_module_in_the_workspace_is_not_imported_by_minis_process(tmp_path, monkeypatch, scripted):
    """The process runs in the workspace; a task's file named like a module it imports (a repository with its own
    `yaml.py`) must not replace that module in the process that holds the model's key."""
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    (tmp_path / "yaml.py").write_text("raise SystemExit(7)\n")
    server = scripted([bash("printf 'ok' > answer.txt", "c1"), submit()])
    out = load_agent("mini-swe-agent", params(server, files=["yaml.py"]), tmp_path).run({"id": "t8", "prompt": "Go."},
                                                                                          None)
    assert out.output == "ok" and (Path(out.extra["workspace"]) / "yaml.py").is_file()


@pytest.mark.skipif(not sys.platform.startswith("linux") or os.geteuid() == 0,
                    reason="/proc and PR_SET_DUMPABLE are Linux's; root reads any process's environment")
def test_a_command_cannot_read_the_key_from_minis_process(tmp_path, monkeypatch, scripted):
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    server = scripted([bash("tr '\\0' '\\n' < /proc/$PPID/environ | grep -c HERMIT_TEST_KEY; true", "c1"),
                       bash("printf 'ok' > answer.txt", "c2"), submit()])
    out = load_agent("mini-swe-agent", params(server), tmp_path).run({"id": "t9", "prompt": "Go."}, None)
    first = [s for s in log_steps(out.log) if s.kind == "command"][0]
    assert out.output == "ok" and "Permission denied" in first.output and KEY not in first.output


def test_what_mini_swe_agent_cannot_do_is_refused(tmp_path, monkeypatch, scripted):
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    agent = make("mini-swe-agent", {"web_search": False, "model": "openai/m"})
    for call, match in ((dict(session="s1"), "resume"), (dict(roles=[{"name": "r"}]), "sub-agents")):
        with pytest.raises(Unsupported, match=match):
            agent.run("x", workspace=tmp_path, **call)
    for setting, match in ((dict(web_search=True), "web search"), (dict(login="chatgpt"), "no login"),
                           (dict(isolation="container"), "isolation")):
        with pytest.raises(Unsupported, match=match):
            agent.configured(**setting).run("x", workspace=tmp_path)
    with pytest.raises(ValueError, match="needs a model"):
        make("mini-swe-agent", {"web_search": False}).run("x", workspace=tmp_path)
    # another model class calls its own service with what it is given: never a binding's key and URL
    bound = make("mini-swe-agent", {"web_search": False, "binding": {"base_url": "http://x", "model": "m"},
                                    "model_class": "openrouter"})
    with pytest.raises(Unsupported, match="calls its own service"):
        bound.run("x", workspace=tmp_path)
    # a config that names the model or the environment class fails every task: no session starts, and the acting
    # agent reports the error rather than score an empty answer
    server = scripted([])
    (tmp_path / "bad.yaml").write_text("model:\n  model_name: other\nenvironment:\n  environment_class: docker\n")
    with pytest.raises(RuntimeError, match="model.model_name and environment.environment_class"):
        load_agent("mini-swe-agent", params(server, config="bad.yaml"), tmp_path).run({"id": "t5", "prompt": "x"}, None)
    assert server.requests == []


def system_message(config: dict, workspace: Path) -> str:
    """The system message `mini -c` renders from a deployed config: its template with mini-swe-agent's variables (the
    environment's include this process's variables)."""
    import platform
    return Template(config["agent"]["system_template"], undefined=StrictUndefined).render(
        **{**config["agent"], **platform.uname()._asdict(), **os.environ, "cwd": str(workspace)})


@pytest.mark.parametrize("endraw", ["{% endraw %}", "{%- endraw -%}", "{%+ endraw %}", "{%endraw+%}", "{%  endraw  %}"])
def test_a_deployed_config_renders_the_base_agents_system_message_whatever_the_context_holds(
        tmp_path, monkeypatch, scripted, endraw):
    """The adapter's context is literal text in the deployed template: braces, a raw block and every spelling of its
    end stay text, so the learner's ADAPTER.md cannot become template code (a variable there could print a key)."""
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    adapter = adapter_copy(tmp_path)
    (adapter / "ADAPTER.md").write_text("Use {{ HERMIT_TEST_KEY }} as {% raw %}{{ x }}" + endraw + " text.\n")
    server = scripted([bash("printf 'ok' > answer.txt", "c1"), submit()])
    agent = load_agent("mini-swe-agent", params(server), tmp_path)
    out = agent.run({"id": "t6", "prompt": "Go."}, adapter)
    sent = server.requests[0]["messages"][0]["content"]
    assert "Use {{ HERMIT_TEST_KEY }} as {% raw %}{{ x }}" + endraw + " text." in sent and KEY not in sent

    workspace = tmp_path / "deployed"
    deployed = host.prepare(adapter, workspace, sys.executable)
    config = yaml.safe_load((workspace / mini.CONFIG_FILE).read_text())
    work = Path(out.extra["workspace"])
    rendered = system_message(config, workspace)
    assert rendered == sent.replace(str(work), str(workspace))      # the same text, the deployed workspace's paths
    assert deployed["environment_class"] == host.ENVIRONMENT_CLASS and deployed["tools"] == ["count_words"]


def test_a_deployed_workspace_runs_the_adapters_tools_in_minis_own_environment(tmp_path):
    adapter = adapter_copy(tmp_path)
    workspace = tmp_path / "ws"
    host.prepare(adapter, workspace, sys.executable)
    config = yaml.safe_load((workspace / mini.CONFIG_FILE).read_text())
    mini.load_minisweagent()
    from minisweagent.environments import get_environment
    env = get_environment({**config["environment"], "cwd": str(workspace)})
    ran = env.execute({"command": "hermit-tool count_words '{\"text\": \"a b\"}'"})
    assert ran["returncode"] == 0 and json.loads(ran["output"]) == {"count": 2}
    assert env.execute({"command": "hermit-tool count_words '{}' && ls"})["returncode"] == 1    # one command only
    assert env.execute({"command": "echo plain"})["output"] == "plain\n"
    calls = [json.loads(line) for line in (workspace / "tool-calls.jsonl").read_text().splitlines()]
    assert [c["tool"] for c in calls] == ["count_words"]


def test_deploy_names_the_roles_keeps_the_local_environment_without_tools_and_redeploys(tmp_path):
    adapter = adapter_copy(tmp_path)
    exports = yaml.safe_load((adapter / "exports.yaml").read_text())
    del exports["tools"]
    (adapter / "exports.yaml").write_text(yaml.safe_dump(exports, sort_keys=False))
    workspace = tmp_path / "ws"
    result = host.prepare(adapter, workspace, sys.executable)
    config = yaml.safe_load((workspace / mini.CONFIG_FILE).read_text())
    assert result["environment_class"] == "local" and "environment_class" not in config["environment"]
    assert "`reviewer`" in config["hermit"]["context"] and result["placed"]["agents"] == "files"
    assert (workspace / "hermit" / "file-example" / "agents" / "reviewer.md").is_file()
    (adapter / "ADAPTER.md").write_text("Changed.\n")
    host.prepare(adapter, workspace, sys.executable)                                       # a redeploy replaces it
    assert "Changed." in yaml.safe_load((workspace / mini.CONFIG_FILE).read_text())["hermit"]["context"]
    with pytest.raises(ValueError, match="another host"):
        from hermit.hosts import codex
        codex.prepare(adapter, workspace, sys.executable)
    run = subprocess.run([sys.executable, "-m", "hermit", "deploy", "--adapter", str(adapter), "--workspace",
                          str(tmp_path / "cli"), "--host", "mini-swe-agent"], capture_output=True, text=True, cwd=ROOT)
    assert run.returncode == 0 and json.loads(run.stdout)["host"] == "mini-swe-agent"


def test_the_prompts_come_from_minis_package_not_a_mini_yaml_where_it_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mini.yaml").write_text("agent:\n  system_template: hijacked\n")
    monkeypatch.setenv("MSWEA_CONFIG_DIR", str(tmp_path))
    config = mini.render_config("mini")
    assert config["agent"]["system_template"].startswith("You are a helpful assistant") and config["model"]
    assert mini.base_config("litellm_textbased") == "mini_textbased" and mini.base_config("litellm") == "mini"


def test_the_layered_config_names_mini_swe_agent_and_binds_its_model(tmp_path):
    from hermit.config import Config
    path = make_task(tmp_path, agent={"base": "mini-swe-agent", "model": "deepseek/deepseek-flash",
                                      "mini-swe-agent": {"web_search": False, "step_limit": 7}})
    agent = Config.load(path, ["agent.mini-swe-agent.model_class=litellm_textbased"]).data["agent"]
    assert agent["name"] == "mini-swe-agent"
    assert agent["params"]["binding"]["litellm"] == "deepseek/deepseek-flash" and agent["params"]["step_limit"] == 7
    assert agent["params"]["model_class"] == "litellm_textbased" and agent["params"]["model"] == "deepseek-flash"


@needs_bwrap
def test_a_rollout_in_the_task_sandbox_reaches_its_model_but_not_the_runs_data(tmp_path, monkeypatch, scripted):
    """In the task sandbox the Base Agent's process reaches its model on the loopback (forwarded), and a command it
    runs does not see the run's data."""
    from hermit.config import Config
    from hermit.isolate import SandboxedAgent
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    data = tmp_path / "tasks.jsonl"
    server = scripted([bash(f"cat {data} 2>&1 | head -1; printf '9' > answer.txt", "c1"), submit()])
    path = make_task(tmp_path, sandbox={"enabled": True}, model="scripted/m",
                     backends={"scripted": {"base_url": server.url, "api_key_env": "HERMIT_TEST_KEY",
                                            "litellm": "openai", "apis": ["chat"]}},
                     agent={"name": "mini-swe-agent", "concurrency": 1, "params": {"web_search": False}})
    agent = SandboxedAgent(Config.load(path, []), "rollout", records=None)
    try:
        out = agent({"id": "t1", "prompt": "Write 9."}, None)
    finally:
        agent.close()
    assert out.output == "9" and len(server.requests) == 2
    first = [s for s in log_steps(Path(out.log)) if s.kind == "command"][0]
    assert "No such file" in first.output and data.is_file()     # there outside, not shown inside
