"""The mini-swe-agent acting agent and deploy host: the agent against a scripted chat server (mini-swe-agent's own loop
and litellm, no model), the deployed config, and the `hermit-tool` command."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import threading

from jinja2 import StrictUndefined, Template
import pytest
import yaml

from hermit.agents import load_agent
from hermit.hosts import mini_swe_agent as host
from test_isolate import needs_bwrap
from test_training import make_task

pytestmark = pytest.mark.skipif(importlib.util.find_spec("minisweagent") is None,
                                reason="needs the mini-swe-agent dependency group")

ROOT = Path(__file__).resolve().parents[1]
FILE_ADAPTER = ROOT / "docs" / "_examples" / "file_adapter"
KEY = "sk-test-not-a-real-key-7f3a"


class Scripted:
    """An OpenAI chat/completions server answering from scripted assistant messages; it records each request's body
    and Authorization header."""

    def __init__(self, replies):
        self.replies, self.requests, self.auth = list(replies), [], []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                outer.auth.append(self.headers.get("Authorization"))
                message = outer.replies.pop(0)
                if isinstance(message, tuple):            # (status, error message): an error response
                    data = json.dumps({"error": {"message": message[1], "type": "invalid_request_error"}}).encode()
                    self.send_response(message[0]); self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
                    return
                payload = {"id": f"r{len(outer.requests)}", "object": "chat.completion", "created": 0, "model": "m",
                           "choices": [{"index": 0, "message": message,
                                        "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                           "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                                     "prompt_tokens_details": {"cached_tokens": 40}}}
                data = json.dumps(payload).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

            def log_message(self, *args):
                pass
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"


def bash(command, cid):
    return {"role": "assistant", "content": f"Running {cid}.", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": command})}}]}


def text(command):
    return {"role": "assistant", "content": f"THOUGHT: next.\n\n```mswea_bash_command\n{command}\n```"}


def tool_adapter(tmp_path):
    """The file adapter (ADAPTER.md, the count_words tool, a skill) without its role: mini-swe-agent has no subagents."""
    adapter = tmp_path / "adapter"
    shutil.copytree(FILE_ADAPTER, adapter)
    shutil.rmtree(adapter / "agents")
    exports = yaml.safe_load((adapter / "exports.yaml").read_text())
    del exports["agents"]
    (adapter / "exports.yaml").write_text(yaml.safe_dump(exports, sort_keys=False))
    return adapter


def spec(server):
    return {"base_url": server.url, "model": "m", "api_key_env": "HERMIT_TEST_KEY", "litellm": "openai/m",
            "price": {"input": 1.0, "cached_input": 0.5, "output": 2.0}}


def test_agent_runs_the_adapter_tools_and_skills_and_submits(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-leak")
    monkeypatch.setenv("MY_TOKEN", "tok-expanded-value")
    adapter = tool_adapter(tmp_path)
    server = Scripted([
        bash("hermit-tool count_words '{\"text\": \"one two three\"}'", "c1"),
        bash("hermit-tool nope '{}'", "c2"),
        bash("head -3 .hermit/skills/verify-output/SKILL.md", "c3"),
        bash("echo key=${HERMIT_TEST_KEY:-unset} secret=${UNRELATED_SECRET:-unset} extra=$EXTRA token=$TOKEN", "c4"),
        bash("printf '42\\n' > answer.txt", "c5"),
        bash("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat answer.txt", "c6"),
    ])
    agent = load_agent("mini-swe-agent", {"model": spec(server), "user": "Q: {question}", "env": {"EXTRA": "x", "TOKEN": "$MY_TOKEN"},
                                          "workspaces": "ws"}, tmp_path)
    out = agent.run({"id": "t1", "question": "What is six times seven?"}, adapter)

    assert out.output == "42" and out.extra["exit_status"] == "Submitted"
    system, user = server.requests[0]["messages"][:2]
    assert (adapter / "ADAPTER.md").read_text().strip().splitlines()[0] in system["content"]
    assert "hermit-tool <name>" in system["content"] and "`count_words`" in system["content"]
    assert "`verify-output`" in system["content"]
    assert "Please solve this task: Q: What is six times seven?" in user["content"]
    assert [t["function"]["name"] for t in server.requests[0]["tools"]] == ["bash"]
    assert server.auth == [f"Bearer {KEY}"] * 6
    tool_results = [m["content"] for m in out.messages if m["role"] == "tool"]
    assert '\\"count\\": 3' in tool_results[0] and "Unknown tool: nope" in tool_results[1]
    assert "name: verify-output" in tool_results[2]
    assert "key=unset secret=unset extra=x token=tok-expanded-value" in tool_results[3]
    assert all(set(m) <= {"role", "content", "tool_calls", "tool_call_id"} for m in out.messages)
    # Six calls of 100 prompt tokens (40 cached) and 10 output tokens, at the binding's price.
    assert out.tokens == 660 and out.api_speed["model_calls"] == 6
    assert out.cost_usd == pytest.approx((6 * 60 * 1.0 + 6 * 40 * 0.5 + 6 * 10 * 2.0) / 1e6)
    workspace = Path(out.log)
    assert workspace.parent == tmp_path / "ws" and (workspace / "answer.txt").read_text() == "42\n"
    info = json.loads((workspace / "trajectory.traj.json").read_text())["info"]
    assert "tok-expanded-value" not in json.dumps(info)        # the commands' env is not in the recorded config
    for path in workspace.rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text(errors="replace"), path


def test_text_based_model_class(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    server = Scripted([text("echo hello > answer.txt"), text("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat answer.txt")])
    agent = load_agent("mini-swe-agent", {"model": spec(server), "model_class": "litellm_textbased"}, tmp_path)
    out = agent.run({"id": "t2", "prompt": "Say hello."}, None)
    assert out.output == "hello" and "tools" not in server.requests[0]
    assert "mswea_bash_command" in server.requests[0]["messages"][0]["content"]
    assert "Instructions from the harness adapter" not in server.requests[0]["messages"][0]["content"]


def test_step_limit_without_a_submission_returns_the_last_message(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    (tmp_path / "mini.yaml").write_text("agent: {step_limit: 1}\nmodel: {cost_tracking: default}\n")
    server = Scripted([bash("ls", "c1")])
    out = load_agent("mini-swe-agent", {"model": spec(server), "config": "mini.yaml"}, tmp_path).run(
        {"id": "t3", "prompt": "List the files."}, None)
    assert out.extra["exit_status"] == "LimitsExceeded" and out.output == "Running c1."


def test_config_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    adapter = tool_adapter(tmp_path)
    server = Scripted([])
    (tmp_path / "docker.yaml").write_text("environment: {environment_class: docker, image: python:3.12}\n")
    agent = load_agent("mini-swe-agent", {"model": spec(server), "config": "docker.yaml"}, tmp_path)
    with pytest.raises(ValueError, match="tools and skills reach the local environment's workspace only"):
        agent.run({"id": "t4", "prompt": "x"}, adapter)
    (tmp_path / "model.yaml").write_text("model: {model_name: openai/gpt-x}\n")
    with pytest.raises(ValueError, match="agent.params.model"):
        load_agent("mini-swe-agent", {"model": spec(server), "config": "model.yaml"}, tmp_path).run({"id": "t5", "prompt": "x"}, None)
    monkeypatch.delenv("HERMIT_TEST_KEY")
    with pytest.raises(RuntimeError, match="HERMIT_TEST_KEY is not set"):
        load_agent("mini-swe-agent", {"model": spec(server)}, tmp_path).run({"id": "t6", "prompt": "x"}, None)
    assert server.requests == []


def test_deployed_config_renders_the_agents_system_message(tmp_path, monkeypatch):
    """A deployed workspace's system message is the acting agent's, and its environment answers `hermit-tool` through
    the adapter it names, as mini-swe-agent's CLI builds it."""
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    adapter = tool_adapter(tmp_path)
    (adapter / "ADAPTER.md").write_text("Use {{ braces }} and {% raw %} literally: {% endraw %} stays text.\n")
    result = host.prepare(adapter, tmp_path / "w", sys.executable)
    config = yaml.safe_load((tmp_path / "w" / host.CONFIG_FILE).read_text())
    assert result["environment_class"] == host.ENVIRONMENT_CLASS and result["tools"] == ["count_words"]
    assert (tmp_path / "w" / ".hermit" / "skills" / "verify-output" / "SKILL.md").is_file()
    deployed = Template(config["agent"]["system_template"], undefined=StrictUndefined).render()

    server = Scripted([bash("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "c1")])
    load_agent("mini-swe-agent", {"model": spec(server)}, tmp_path).run({"id": "t7", "prompt": "x"}, adapter)
    assert server.requests[0]["messages"][0]["content"] == deployed
    assert "{% endraw %} stays text" in deployed

    host.load_minisweagent()
    from minisweagent.environments import get_environment
    env = get_environment(dict(config["environment"], cwd=str(tmp_path / "w")))
    listed = env.execute({"command": "hermit-tool"})
    called = env.execute({"command": "hermit-tool count_words '{\"text\": \"a b\"}'"})
    joined = env.execute({"command": "hermit-tool count_words '{}' && ls"})
    shell = env.execute({"command": "echo plain"})
    assert listed["returncode"] == 0 and listed["output"].startswith("count_words:")
    assert called["returncode"] == 0 and json.loads(called["output"]) == {"count": 2}
    assert joined["returncode"] == 1 and "command of its own" in joined["output"]
    assert shell == {"output": "plain\n", "returncode": 0, "exception_info": ""}
    audit = [json.loads(line) for line in (tmp_path / "w" / "tool-calls.jsonl").read_text().splitlines()]
    assert [r["tool"] for r in audit] == ["count_words"] and audit[0]["status"] == "ok"
    # Redeploying is allowed; another host's workspace is not.
    host.prepare(adapter, tmp_path / "w", sys.executable)
    from hermit.hosts.codex import prepare as codex_prepare
    with pytest.raises(ValueError, match="generated for another host"):
        codex_prepare(adapter, tmp_path / "w", sys.executable)


def test_deploy_without_tools_keeps_the_local_environment_and_refuses_roles(tmp_path):
    result = host.prepare(ROOT / "hermit" / "algorithms" / "templates" / "prompt_only", tmp_path / "p", sys.executable)
    config = yaml.safe_load((tmp_path / "p" / host.CONFIG_FILE).read_text())
    assert result["environment_class"] == "local" and "environment_class" not in config["environment"]
    assert config["environment"]["env"]["PAGER"] == "cat" and config["model"]["observation_template"]
    with pytest.raises(ValueError, match="no subagents.*reviewer"):
        host.prepare(FILE_ADAPTER, tmp_path / "f", sys.executable)
    assert not (tmp_path / "f").exists()


@needs_bwrap
def test_a_sandboxed_rollout_runs_mini_swe_agent_with_the_adapter_tools(tmp_path, monkeypatch):
    """train/eval run each task in the rollout sandbox: mini-swe-agent reaches the scripted model through the
    forwarded loopback port, and `hermit-tool` reaches the adapter's tools in their own sandbox."""
    from hermit.config import Config
    from hermit.isolate import SandboxedAgent
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    server = Scripted([bash("hermit-tool count_words '{\"text\": \"x y z w\"}'", "c1"),
                       bash("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && echo 4", "c2")])
    path = make_task(tmp_path, sandbox={"enabled": True, "network": "isolated"},
                     agent={"name": "mini-swe-agent", "params": {"model": spec(server), "user": "Add {a} and {b}."}})
    config = Config.load(path, [])
    adapter = tool_adapter(tmp_path / "runs" / "r")
    agent = SandboxedAgent(config, "rollout", records=tmp_path / "records")
    try:
        out = agent({"id": "t1", "a": 1, "b": 2}, adapter)
    finally:
        agent.close()
    assert out.output == "4" and out.extra["exit_status"] == "Submitted"
    assert '\\"count\\": 4' in [m["content"] for m in out.messages if m["role"] == "tool"][0]
    assert server.auth == [f"Bearer {KEY}"] * 2
    assert "Please solve this task: Add 1 and 2." in server.requests[0]["messages"][1]["content"]
    assert json.loads((tmp_path / "records" / "rollout-1.json").read_text())["probe"]["passed"]


def test_a_failed_call_ends_the_task_with_the_calls_so_far(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    server = Scripted([bash("ls", "c1"), (400, "This model's maximum context length is 8192 tokens. However, your "
                                                "messages resulted in 9000 tokens.")])
    out = load_agent("mini-swe-agent", {"model": spec(server)}, tmp_path).run({"id": "t8", "prompt": "x"}, None)
    assert out.extra["exit_status"] == "ContextWindowExceededError" and "context" in out.extra["error"].lower()
    assert out.tokens == 110 and out.api_speed["model_calls"] == 2 and out.output == "Running c1."
    assert (Path(out.log) / "trajectory.traj.json").is_file()
    server = Scripted([(401, "Incorrect API key provided")])
    with pytest.raises(Exception, match="(?i)api key"):
        load_agent("mini-swe-agent", {"model": spec(server)}, tmp_path).run({"id": "t9", "prompt": "x"}, None)


def test_the_context_tool_adds_this_tasks_context(tmp_path, monkeypatch):
    from test_context_tool import EXPORTS, INTERFACE, META
    monkeypatch.setenv("HERMIT_TEST_KEY", KEY)
    adapter = tmp_path / "ctx"
    (adapter / ".hermit").mkdir(parents=True)
    (adapter / ".hermit" / "adapter.yaml").write_text(META)
    (adapter / "exports.yaml").write_text(EXPORTS)
    (adapter / "ADAPTER.md").write_text("# Static instructions\n")
    (adapter / "interface.py").write_text(INTERFACE)
    server = Scripted([bash("hermit-tool lookup '{}'", "c1"), bash("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "c2")])
    out = load_agent("mini-swe-agent", {"model": spec(server)}, tmp_path).run({"id": "t10", "prompt": "find it"}, adapter)
    system = server.requests[0]["messages"][0]["content"]
    assert "# Static instructions" in system and "[NOTES FOR t10]\nFIND IT" in system
    assert "`calc`" in system and "`lookup`" not in system          # the context tool is the harness's
    assert out.extra["context_tool"]["retrieved"] == ["t10"]
    assert "Unknown tool: lookup" in [m["content"] for m in out.messages if m["role"] == "tool"][0]


@pytest.mark.parametrize("closing", ["{% endraw %}", "{%- endraw -%}", "{%+ endraw %}", "{% endraw +%}", "{%+endraw+%}"])
def test_baked_context_stays_text(closing):
    block = f"a {closing}{{{{ SECRET }}}}{{% raw %}} b {{# c #}}"
    rendered = Template(host.baked("S\n{{ adapter_context }}\n", block), undefined=StrictUndefined).render(SECRET="x")
    assert rendered == "S\n" + block


def test_the_base_config_is_mini_swe_agents_own(tmp_path, monkeypatch):
    """A mini.yaml in the working directory or MSWEA_CONFIG_DIR is not mini-swe-agent's base config."""
    (tmp_path / "mini.yaml").write_text("agent: {step_limit: 1}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MSWEA_CONFIG_DIR", str(tmp_path))
    config = host.render_config("mini")
    assert config["model"]["observation_template"] and config["environment"]["env"]["PAGER"] == "cat"
    assert config["agent"]["step_limit"] == 0 and config["agent"]["cost_limit"] == 3.0   # mini-swe-agent's own
