"""The built-in acting agents: the react loop against a scripted chat server, the CLI agent against fake CLIs,
and pipeline.params reaching a pipeline that asks for them."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import stat
import sys
import threading

import pytest

from hermit.task import load_pipeline

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "docs" / "_examples" / "file_adapter"

TOOLS_PY = '''
from hermit.agents import tool

@tool("Add two integers.", {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
      "required": ["a", "b"]})
def add(a: int, b: int) -> dict:
    return {"sum": a + b}

@tool("The task id.")
def whoami(_task) -> str:
    return _task["id"]
'''


class Scripted:
    """A chat/completions server that answers from a list of scripted assistant messages and records requests."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                message = outer.replies.pop(0)
                payload = {"choices": [{"message": message, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 100, "completion_tokens": 10}}
                data = json.dumps(payload).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

            def log_message(self, *args):
                pass
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


def call(name, arguments, cid):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def test_react_loop(tmp_path):
    (tmp_path / "tools.py").write_text(TOOLS_PY)
    (tmp_path / "prompt.md").write_text("You add numbers.\n\n{context}\n")
    server = Scripted([
        {"role": "assistant", "content": None, "tool_calls": [call("add", {"a": 2, "b": 3}, "c1"), call("whoami", {}, "c2")]},
        {"role": "assistant", "content": None, "tool_calls": [call("count_words", {"text": "x y z"}, "c3"), call("nope", {}, "c4")]},
        {"role": "assistant", "content": "The sum is 5."},
        {"role": "assistant", "content": "<FINAL_ANSWER>5</FINAL_ANSWER>"},
    ])
    params = {"model": {"base_url": server.url, "model": "m", "price": {"input": 1, "output": 2}},
              "prompt": "prompt.md", "tools": "tools.py", "user": "Q: {question}", "answer_tag": "FINAL_ANSWER",
              "logs": "logs"}
    run = load_pipeline("hermit.agents.react:run", tmp_path, params)
    out = run({"id": "t1", "question": "2+3?"}, EXAMPLE)

    first = server.requests[0]
    assert first["messages"][0]["content"].startswith("You add numbers.") and "Review the task" in first["messages"][0]["content"]
    assert first["messages"][1] == {"role": "user", "content": "Q: 2+3?"}
    assert [t["function"]["name"] for t in first["tools"]] == ["add", "whoami", "count_words"]
    tool_msgs = [m for m in out.messages if m["role"] == "tool"]
    assert [m["content"] for m in tool_msgs] == ['{"sum": 5}', '"t1"', '{"count": 3}', "Error: unknown tool nope"]
    assert "tools" not in server.requests[3]          # the answer-tag retry goes without tools
    assert out.output == "<FINAL_ANSWER>5</FINAL_ANSWER>" and out.extra == {"turns": 4, "tool_calls": 4}
    assert out.tokens == 440 and out.cost_usd == pytest.approx((400 * 1 + 40 * 2) / 1e6)
    assert out.log.parent == tmp_path / "logs" and json.loads(out.log.read_text())["task"] == "t1"
    server.server.shutdown()


def test_react_without_adapter_or_tools(tmp_path):
    server = Scripted([{"role": "assistant", "content": "hello"}])
    run = load_pipeline("hermit.agents.react:run", tmp_path, {"model": {"base_url": server.url, "model": "m"},
                                                                       "prompt": "Be brief."})
    out = run({"id": "t2", "prompt": "hi"}, None)
    assert out.output == "hello" and out.cost_usd is None and "tools" not in server.requests[0]
    assert server.requests[0]["messages"][0]["content"] == "Be brief.\n"
    server.server.shutdown()


FAKE_CODEX = '''#!/bin/sh
# a stand-in for `codex exec --json ... -o LAST PROMPT`: records the arguments, writes events and the last message
last=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && last="$a"; prev="$a"; done
printf '%s\\n' "$*" > "$(dirname "$last")/.args"
echo '{"type":"thread.started","thread_id":"th1"}'
echo '{"type":"turn.completed","usage":{"input_tokens":1000,"cached_input_tokens":200,"output_tokens":50}}'
printf 'ANSWER 42' > "$last"
'''
FAKE_CLAUDE = '''#!/bin/sh
printf '%s\\n' "$*" > .args
echo '{"type":"system","session_id":"s1"}'
echo '{"type":"result","result":"ANSWER 43","num_turns":3,"total_cost_usd":0.5,"usage":{"input_tokens":10,"output_tokens":5,"cache_read_input_tokens":0}}'
'''


@pytest.mark.parametrize("backend,script,answer", [("codex", FAKE_CODEX, "ANSWER 42"), ("claude-code", FAKE_CLAUDE, "ANSWER 43")])
def test_cli_agent_with_fake_cli(tmp_path, monkeypatch, backend, script, answer):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / ("codex" if backend == "codex" else "claude")
    exe.write_text(script)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    (tmp_path / "prompt.md").write_text("Solve {question} in this workspace.")
    params = {"backend": backend, "prompt": "prompt.md", "price": {"input": 1, "cached_input": 0.5, "output": 2},
              "workspaces": "ws", "model": "m1"}
    run = load_pipeline("hermit.agents.cli:run", tmp_path, params)
    out = run({"id": "q7", "question": "6*7"}, EXAMPLE)

    ws = out.log
    assert ws.parent == tmp_path / "ws" and ws.name.startswith("q7-") and out.output == answer
    context_file = ws / ("AGENTS.md" if backend == "codex" else "CLAUDE.md")
    assert "hermit:file-example:start" in context_file.read_text()     # the adapter was deployed
    args = (ws / ".args").read_text()
    assert "Solve 6*7 in this workspace." in args and "m1" in args
    assert (ws / f"agent-{'codex' if backend == 'codex' else 'claude'}.jsonl").is_file()
    if backend == "codex":
        assert out.tokens == 1050 and out.cost_usd == pytest.approx((800 * 1 + 200 * 0.5 + 50 * 2) / 1e6)
    else:
        assert out.cost_usd == 0.5 and out.tokens == 15
    assert out.extra["exit_code"] == 0 and out.extra["backend"] in ("codex", "claude")


def test_load_pipeline_binds_only_what_is_asked(tmp_path):
    (tmp_path / "p.py").write_text(
        "def plain(task, adapter):\n    return ('plain', task, adapter)\n"
        "def wants(task, adapter, *, params, base):\n    return (params, base)\n")
    plain = load_pipeline("p.py:plain", tmp_path, {"k": 1})
    assert plain({"id": 1}, None) == ("plain", {"id": 1}, None)
    wants = load_pipeline("p.py:wants", tmp_path, {"k": 1})
    assert wants({"id": 1}, None) == ({"k": 1}, tmp_path)


def test_load_agent_builtin_class_function_and_alias(tmp_path):
    from hermit.agents import Agent, FunctionAgent, load_agent
    from hermit.agents.react import ReactAgent
    from hermit.agents.cli import CliAgent
    from hermit.config import Config
    assert isinstance(load_agent("react", {"model": {}}, tmp_path), ReactAgent)
    claude = load_agent("claude-code", {"model": "m"}, tmp_path)
    assert isinstance(claude, CliAgent) and claude.params["backend"] == "claude" and claude.base == tmp_path.resolve()
    (tmp_path / "agent.py").write_text(
        "from hermit.agents import Agent\nfrom hermit.task import RunOutput\n"
        "class Mine(Agent):\n    def run(self, task, adapter):\n        return RunOutput(output=(self.params['k'], str(self.base.name), adapter))\n"
        "def plain(task, adapter):\n    return RunOutput(output='plain')\n")
    mine = load_agent("agent.py:Mine", {"k": 1}, tmp_path)
    assert isinstance(mine, Agent) and mine.run({"id": 1}, None).output == (1, tmp_path.name, None)
    plain = load_agent("agent.py:plain", {}, tmp_path)
    assert isinstance(plain, FunctionAgent) and plain.run({}, None).output == "plain"
    with pytest.raises(ValueError):
        load_agent("", {}, tmp_path)
    # the older `pipeline:` spelling still configures the agent
    (tmp_path / "tasks.jsonl").write_text('{"id": "a"}\n'); (tmp_path / "train.txt").write_text("a\n")
    (tmp_path / "judge.py").write_text("def score(task, output):\n    return 1.0\n")
    (tmp_path / "run.yaml").write_text(
        "data: {tasks: tasks.jsonl, train: train.txt}\npipeline: {run: 'agent.py:plain', concurrency: 2, params: {x: 1}}\n"
        "judge: {path: judge.py}\ntemplate: prompt-only@1.0\nevolver: {name: gepa}\n")
    cfg = Config.load(tmp_path / "run.yaml", [])
    assert cfg["agent"] == {"name": "agent.py:plain", "concurrency": 2, "timeout_min": None, "params": {"x": 1}}
