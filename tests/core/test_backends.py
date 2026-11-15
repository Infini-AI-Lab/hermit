"""Model backends: references resolve through the three layers, the CLI agents get provider settings from a binding,
the run config is normalized, the preflight finds a dialect the backend does not serve, and the probe speaks each
dialect to a fake server."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest
import yaml

from hermit.agent.model import backends
from hermit.agent.model.backends import (Binding, bind_agent_params, bindings_of, is_ref, normalize_config, probe,
                                     problems_of, registry, resolve, shell_lines)
from hermit.config import Config
from hermit.agent.model.llm import ChatClient

ROOT = Path(__file__).resolve().parents[2]
GLM = "accounts/fireworks/models/glm-5p3-flash"


def reg(declared=None, user=None, environ=None):
    return registry(declared, user_file=user, environ=environ if environ is not None else {})


class FakeServer:
    """One server speaking the three dialects: /v1/models, /v1/chat/completions, /v1/responses, /v1/messages."""

    def __init__(self, model="m", key=None, reject_namespace=False):
        self.requests, outer = [], self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

            def do_GET(self):
                if key and self.headers.get("Authorization") != f"Bearer {key}":
                    return self._send(401, {"error": "unauthorized"})
                if self.path == "/v1/models":
                    return self._send(200, {"data": [{"id": model}]})
                self._send(404, {"error": "no"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                if key and self.headers.get("Authorization") != f"Bearer {key}" and self.headers.get("x-api-key") != key:
                    return self._send(401, {"error": "unauthorized"})
                if body.get("model") != model:
                    return self._send(404, {"error": f"model {body.get('model')} not found"})
                if self.path == "/v1/chat/completions":
                    return self._send(200, {"choices": [{"message": {"role": "assistant", "content": "pong"},
                                                         "finish_reason": "stop"}],
                                            "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
                if self.path == "/v1/responses":
                    if reject_namespace and any(t.get("type") == "namespace" for t in body.get("tools") or []):
                        return self._send(400, {"error": {"message": "tool type 'namespace' is not supported."}})
                    if not body.get("stream"):
                        return self._send(200, {"id": "r", "output": [], "usage": {"input_tokens": 3, "output_tokens": 1}})
                    data = (b'event: response.created\ndata: {"type":"response.created"}\n\n'
                            b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"r"}}\n\n')
                    self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
                    return
                if self.path == "/v1/messages":
                    return self._send(200, {"id": "m", "content": [{"type": "text", "text": "pong"}],
                                            "usage": {"input_tokens": 3, "output_tokens": 1}})
                self._send(404, {"error": "no such route"})

            def log_message(self, *args):
                pass
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"


# ---- the registry and references -----------------------------------------------------------------------------------

def test_builtins_and_reference_parsing():
    r = reg()
    assert {"fireworks", "openai", "anthropic", "deepseek", "vllm", "sglang", "ollama"} <= set(r)
    b = resolve(f"fireworks/{GLM}", r)
    assert b.model == GLM and b.base_url == "https://api.fireworks.ai/inference/v1"
    assert b.api_key_env == "FIREWORKS_API_KEY" and b.price == {"input": 0.15, "cached_input": 0.03, "output": 0.50}
    assert b.serves("chat") and b.serves("responses") and b.serves("messages")
    assert b.backend.messages_root == "https://api.fireworks.ai/inference"
    assert b.litellm_model == f"fireworks_ai/{GLM}" and b.litellm_key_env == "FIREWORKS_AI_API_KEY"
    assert b.backend.origin == "https://api.fireworks.ai" and b.backend.path == "/inference/v1"
    v = resolve("vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8", r)
    assert v.model == "Qwen/Qwen3-30B-A3B-Instruct-2507-FP8" and v.price is None and not v.backend.key_required
    assert v.backend.messages_root == "http://127.0.0.1:8000"
    assert is_ref(f"fireworks/{GLM}", r) and is_ref({"backend": "vllm", "model": "x"}, r)
    assert not is_ref("gpt-5.4", r) and not is_ref(GLM, r) and not is_ref({"base_url": "http://x/v1", "model": "m"}, r)
    with pytest.raises(ValueError, match="names no backend"):
        resolve("nowhere/x", r)


def test_layers_from_and_environment(tmp_path):
    user = tmp_path / "backends.yaml"
    user.write_text(yaml.safe_dump({"vllm": {"base_url": "http://gpu-box:8000/v1", "api_key_env": "LAB_KEY",
                                             "prices": {"q": {"input": 0, "output": 0}}},
                                    "lab": {"from": "vllm", "base_url": "http://lab:9000/v1", "models": {"big": "Org/Big"}}}))
    r = reg({"meter": {"from": "fireworks", "base_url": "http://127.0.0.1:3452/run_o0/inference/v1"},
             "sglang": {"apis": ["chat"]}}, user=user, environ={"OPENAI_BASE_URL": "http://proxy:4000/v1"})
    assert r["vllm"].base_url == "http://gpu-box:8000/v1" and r["vllm"].api_key_env == "LAB_KEY"
    assert r["vllm"].litellm == "hosted_vllm" and not r["vllm"].key_required         # the built-in's other fields stay
    assert r["lab"].base_url == "http://lab:9000/v1" and r["lab"].api_key_env == "LAB_KEY"
    assert resolve("lab/big", r).model == "Org/Big"                                 # an alias
    m = resolve(f"meter/{GLM}", r)
    assert m.api_key_env == "FIREWORKS_API_KEY" and m.price["input"] == 0.15 and m.serves("responses")
    assert m.backend.origin == "http://127.0.0.1:3452" and m.backend.path == "/run_o0/inference/v1"
    assert r["sglang"].apis == ("chat",) and r["sglang"].base_url == "http://127.0.0.1:30000/v1"
    assert r["openai"].base_url == "http://proxy:4000/v1"                             # <NAME>_BASE_URL moves it
    with pytest.raises(ValueError, match="from 'nothing' names no backend"):
        reg({"x": {"from": "nothing", "base_url": "http://x/v1"}})
    # A copy keeps its source's key variable (the default is taken by the source's name, once), copies may chain in
    # any order within a layer, and a copy at another base URL does not inherit the source's Messages root.
    r = reg({"b": {"from": "a", "base_url": "http://b/v1"}, "a": {"from": "mybox", "base_url": "http://a/v1"},
             "mybox": {"base_url": "http://x/v1"}, "relay": {"from": "deepseek", "base_url": "http://127.0.0.1:1/t"}})
    assert r["mybox"].api_key_env == "MYBOX_API_KEY" and r["a"].api_key_env == "MYBOX_API_KEY"
    assert r["b"].api_key_env == "MYBOX_API_KEY" and r["b"].base_url == "http://b/v1"
    assert r["relay"].messages_root == "http://127.0.0.1:1/t" and r["deepseek"].messages_root == "https://api.deepseek.com/anthropic"
    with pytest.raises(ValueError, match="unknown fields"):
        reg({"x": {"base_url": "http://x/v1", "url": "http://x"}})
    with pytest.raises(ValueError, match="unknown apis"):
        reg({"x": {"base_url": "http://x/v1", "apis": ["grpc"]}})


def test_mapping_forms_and_spec_round_trip():
    r = reg()
    b = resolve({"backend": "vllm", "model": "q", "price": {"input": 1, "output": 2}, "extra_body": {"reasoning_effort": "low"}}, r)
    assert b.price == {"input": 1, "output": 2} and b.extra_body == {"reasoning_effort": "low"}
    spec = b.spec()
    assert spec["ref"] == "vllm/q" and spec["apis"] == ["chat", "responses", "messages"] and spec["litellm"] == "hosted_vllm/q"
    again = Binding.from_spec(spec)
    assert again.ref == b.ref and again.base_url == b.base_url and again.price == b.price and again.serves("messages")
    assert again.backend.litellm == "hosted_vllm" and not again.backend.key_required
    legacy = resolve({"base_url": "https://api.fireworks.ai/inference/v1", "model": GLM, "api_key_env": "FIREWORKS_API_KEY",
                      "price": {"input": 0.15}}, r)
    assert legacy.backend.name == "api.fireworks.ai" and legacy.serves("chat") and not legacy.serves("responses")
    assert legacy.api_key_env == "FIREWORKS_API_KEY" and legacy.price == {"input": 0.15}


# ---- the CLI agents' provider settings ------------------------------------------------------------------------------

def test_codex_args_and_claude_env(monkeypatch):
    r = reg()
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-key")
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    fw = resolve(f"fireworks/{GLM}", r)
    args = fw.codex_args(base_url="http://127.0.0.1:3452/b/inference/v1")
    assert args == ["-c", 'model_provider="fireworks"', "-c", 'model_providers.fireworks.name="fireworks"',
                    "-c", 'model_providers.fireworks.base_url="http://127.0.0.1:3452/b/inference/v1"',
                    "-c", 'model_providers.fireworks.wire_api="responses"',
                    "-c", 'model_providers.fireworks.env_key="FIREWORKS_API_KEY"', "-c", 'web_search="disabled"']
    assert "model_context_window" not in " ".join(args)                    # no window built in: the CLI's own default
    sized = resolve("lab/m", reg({"lab": {"from": "fireworks", "context_window": 131072}}))
    assert "model_context_window=131072" in sized.codex_args()               # a declared window is passed on
    v = resolve("vllm/q", r)
    assert 'model_providers.vllm.env_key' not in " ".join(v.codex_args())      # no key: Codex is given none to read
    monkeypatch.setenv("VLLM_API_KEY", "k")
    assert 'model_providers.vllm.env_key="VLLM_API_KEY"' in v.codex_args()       # a key that is set is passed on
    env = v.claude_env()
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8000" and env["ANTHROPIC_AUTH_TOKEN"] == "$VLLM_API_KEY"
    assert v.claude_env(expand=True)["ANTHROPIC_AUTH_TOKEN"] == "k"          # the value only on request
    assert env["ANTHROPIC_MODEL"] == "q" and env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "q" and env["ANTHROPIC_API_KEY"] == ""
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "q" and env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in env                     # no window declared: Claude Code's own
    assert sized.claude_env()["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "131072"  # a declared window is passed on
    monkeypatch.delenv("VLLM_API_KEY")
    assert v.claude_env()["ANTHROPIC_AUTH_TOKEN"] == backends.PLACEHOLDER_KEY
    assert resolve("deepseek/deepseek-chat", r).claude_env()["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"


def test_bind_agent_params(monkeypatch):
    r = reg()
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-key")
    out = bind_agent_params({"backend": "codex", "model": f"fireworks/{GLM}", "args": ["-c", 'model_reasoning_effort="high"'],
                             "home": []}, "codex", r)
    assert out["model"] == GLM and out["price"]["output"] == 0.5 and out["binding"]["backend"] == "fireworks"
    assert out["args"][:2] == ["-c", 'model_provider="fireworks"'] and out["args"][-2:] == ["-c", 'model_reasoning_effort="high"']
    assert out["home"] == []
    out = bind_agent_params({"backend": "claude", "model": None, "env": {"X": "1"}}, "claude", r, default="vllm/q")
    assert out["model"] == "q" and out["env"]["X"] == "1" and out["env"]["ANTHROPIC_MODEL"] == "q" and out["price"] is None
    assert "fw-key" not in json.dumps(out)                                   # a run's records carry no key value
    mapped = bind_agent_params({"backend": "codex", "model": {"model": "vllm/q", "price": {"input": 2}}}, "codex", r)
    assert mapped["model"] == "q" and mapped["price"] == {"input": 2} and mapped["args"][1] == 'model_provider="vllm"'
    plain = {"backend": "codex", "model": "gpt-5.4", "args": []}
    assert bind_agent_params(plain, "codex", r) is plain                   # the CLI's own provider: untouched
    assert bind_agent_params({"backend": "codex"}, "codex", r) == {"backend": "codex"}
    kept = bind_agent_params({"backend": "codex", "model": "vllm/q", "price": {"input": 9}}, "codex", r)
    assert kept["price"] == {"input": 9}                                     # a price given stays


def test_claude_cost_counts_only_on_anthropic():
    from hermit.agent.base.records import price_usage
    from hermit.agent.evolver.base import Usage
    r = reg()
    reported = lambda: [Usage(model="q", input_tokens=2_000_000, output_tokens=1_000_000, calls=3, cost_usd=3.6)]
    local = bind_agent_params({"backend": "claude", "model": {"model": "vllm/q", "price": {"input": 0, "output": 0}}},
                              "claude", r)
    usage = reported(); price_usage(usage, local)
    assert usage[0].cost_usd == 0                       # the backend's price, not Claude Code's figure
    usage = reported(); price_usage(usage, bind_agent_params({"backend": "claude", "model": "vllm/q"}, "claude", r))
    assert usage[0].cost_usd is None                    # no price on that backend: unknown
    usage = reported(); price_usage(usage, bind_agent_params({"backend": "claude", "model": "anthropic/claude-x"}, "claude", r))
    assert usage[0].cost_usd == 3.6                     # Anthropic's own models: Claude Code's figure
    usage = reported(); price_usage(usage, {"backend": "claude", "model": "sonnet", "price": {"input": 1}})
    assert usage[0].cost_usd == 3.6                     # no binding: as before


# ---- the run config ------------------------------------------------------------------------------------------------

def test_normalize_config_resolves_llm_agent_and_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HA_BACKENDS", str(tmp_path / "no-user-file.yaml"))
    data = {"model": "vllm/q", "backends": {"vllm": {"base_url": "http://box:8000/v1"}}, "llm": None,
            "agent": {"name": "react", "params": {"prompt": "p"}}}
    normalize_config(data)
    assert data["llm"]["base_url"] == "http://box:8000/v1" and data["llm"]["model"] == "q"
    assert data["agent"]["params"]["model"]["ref"] == "vllm/q" and data["agent"]["params"]["prompt"] == "p"
    data = {"model": None, "backends": {}, "llm": {"model": f"fireworks/{GLM}", "extra_body": {"reasoning_effort": "high"}},
            "agent": {"name": "codex", "params": {"model": "vllm/q", "args": ["-c", "x=1"]}}}
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    normalize_config(data)
    assert data["llm"]["model"] == GLM and data["llm"]["extra_body"] == {"reasoning_effort": "high"}
    p = data["agent"]["params"]
    assert p["model"] == "q" and p["args"][1] == 'model_provider="vllm"' and p["args"][-2:] == ["-c", "x=1"]
    assert p["binding"]["ref"] == "vllm/q"
    data = {"model": None, "backends": {}, "llm": {"base_url": "http://x/v1", "model": "m"},
            "agent": {"name": "task.py:Agent", "params": {"model": "accounts/fireworks/models/x"}}}
    normalize_config(data)                                                   # the older forms pass through unchanged
    assert data["llm"] == {"base_url": "http://x/v1", "model": "m"}
    assert data["agent"]["params"]["model"] == "accounts/fireworks/models/x"
    with pytest.raises(ValueError, match="names no backend"):
        normalize_config({"model": "nope/x", "backends": {}, "llm": None, "agent": {"name": "react", "params": {}}})
    data = {"model": "anthropic/claude-x", "backends": {}, "llm": None, "agent": {"name": "claude-code", "params": {}}}
    normalize_config(data)                                                   # a Messages-only default makes no llm tool
    assert data["llm"] is None and data["agent"]["params"]["binding"]["ref"] == "anthropic/claude-x"
    assert [api for _, _, api in bindings_of(data)] == ["messages"]


def test_config_load_with_references(tmp_path, monkeypatch):
    monkeypatch.setenv("HA_BACKENDS", str(tmp_path / "no-user-file.yaml"))     # the developer's own file stays out
    for name in ("tasks.jsonl", "train.txt"):
        (tmp_path / name).write_text('{"id": "t1", "question": "q"}\n' if name.endswith("jsonl") else "t1\n")
    (tmp_path / "judge.py").write_text("def score(task, output):\n    return 1.0\n")
    (tmp_path / "run.yaml").write_text(yaml.safe_dump({
        "data": {"tasks": "tasks.jsonl", "train": "train.txt"}, "judge": {"path": "judge.py"},
        "template": "prompt-only@1.0", "evolver": {"name": "gepa"}, "model": "vllm/q",
        "backends": {"vllm": {"base_url": "http://box:8000/v1"}},
        "agent": {"name": "react", "params": {"prompt": "hi"}}}))
    config = Config.load(tmp_path / "run.yaml")
    assert config["llm"]["base_url"] == "http://box:8000/v1" and config["agent"]["params"]["model"]["model"] == "q"
    config = Config.load(tmp_path / "run.yaml", ["backends.vllm.base_url=http://other:1/v1", f"llm=fireworks/{GLM}",
                                                 "agent.params.model=vllm/alias"])
    assert config["llm"]["model"] == GLM and config["agent"]["params"]["model"]["base_url"] == "http://other:1/v1"
    assert config["agent"]["params"]["model"]["model"] == "alias"
    assert bindings_of(config.data)[0][0] == "llm tool" and bindings_of(config.data)[1][2] == "chat"


# ---- the preflight and the probe --------------------------------------------------------------------------------------

def test_problems_dialect_and_key(monkeypatch):
    r = reg()
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    fw = resolve(f"fireworks/{GLM}", r)
    assert [p for p in problems_of(fw, "chat") if "FIREWORKS_API_KEY is not set" in p]
    assert backends.warnings_of(fw, "messages") == []                         # priced: nothing to warn about
    assert any("no price" in w for w in backends.warnings_of(resolve("openai/gpt-x", r), "chat"))
    assert problems_of(fw, "messages") == [p for p in problems_of(fw, "messages") if "FIREWORKS_API_KEY" in p]
    monkeypatch.setenv("FIREWORKS_API_KEY", "k")
    assert problems_of(fw, "responses") == []
    only_chat = resolve("c/m", reg({"c": {"base_url": "http://x/v1", "key_required": False}}))
    assert any("Codex speaks the OpenAI Responses API only" in p for p in problems_of(only_chat, "responses"))
    assert problems_of(only_chat, "chat") == []                               # an optional key may be absent


def test_probe_each_dialect_and_chat_client(monkeypatch):
    server = FakeServer(model="m", key="sk")
    r = reg({"local": {"base_url": server.url, "api_key_env": "LOCAL_KEY", "apis": ["chat", "responses", "messages"]}})
    monkeypatch.setenv("LOCAL_KEY", "sk")
    b = resolve("local/m", r)
    assert backends.list_models(b) == ["m"]
    for api in ("chat", "responses", "messages"):
        result = probe(b, api)
        assert result["ok"], result
    paths = [req[0] for req in server.requests]
    assert paths == ["/v1/chat/completions", "/v1/responses", "/v1/messages"]
    assert server.requests[2][1].get("x-api-key") == "sk" and server.requests[2][1].get("anthropic-version")
    assert not probe(resolve("local/other", r), "chat")["ok"]                 # a model the server does not have
    assert any(t.get("type") == "namespace" for t in server.requests[1][2]["tools"])   # shaped as Codex sends it
    strict = FakeServer(model="m", reject_namespace=True)                      # vLLM 0.31's gpt-oss path
    refused = probe(resolve("s/m", reg({"s": {"base_url": strict.url, "key_required": False}})), "responses")
    assert not refused["ok"] and "refuses a `namespace` tool" in refused["detail"]
    client = ChatClient.from_spec(b.spec())
    message, usage = client.chat([{"role": "user", "content": "hi"}])
    assert message["content"] == "pong" and usage.input_tokens == 3 and client.api_key_env == "LOCAL_KEY"
    # a keyless local server: no Authorization header is sent when the optional key is absent
    open_server = FakeServer(model="m")
    monkeypatch.delenv("NOKEY", raising=False)
    spec = resolve("open/m", reg({"open": {"base_url": open_server.url, "api_key_env": "NOKEY", "key_required": False}})).spec()
    assert ChatClient.from_spec(spec).api_key_env is None
    assert ChatClient.from_spec(spec).chat([{"role": "user", "content": "hi"}])[0]["content"] == "pong"
    assert "authorization" not in open_server.requests[0][1]


def test_shell_lines_and_cli(capsys, monkeypatch):
    monkeypatch.setenv("HA_BACKENDS", "/nonexistent/backends.yaml")
    lines = shell_lines(resolve(f"fireworks/{GLM}", reg()))
    assert "HA_ORIGIN=https://api.fireworks.ai" in lines and "HA_PATH=/inference/v1" in lines
    assert f"HA_LITELLM_MODEL=fireworks_ai/{GLM}" in lines and "HA_PRICE_INPUT=0.15" in lines
    assert "HA_REWRITE_DEVELOPER_ROLE=1" in lines                              # Fireworks: renamed, as before
    assert "HA_REWRITE_DEVELOPER_ROLE=0" in shell_lines(resolve("vllm/q", reg()))   # a local server keeps the role
    with pytest.raises(ValueError, match="developer_role must be one of"):
        reg({"x": {"base_url": "http://x/v1", "developer_role": "user"}})
    assert backends.main(["list"]) == 0 and "vllm" in capsys.readouterr().out
    assert backends.main(["shell", "--backends", "{m: {from: vllm, base_url: 'http://127.0.0.1:1/b/v1'}}", "m/q"]) == 0
    out = capsys.readouterr().out
    assert "HA_ORIGIN=http://127.0.0.1:1" in out and "HA_PATH=/b/v1" in out and "HA_KEY_REQUIRED=0" in out
    assert backends.main(["show", "nowhere/x"]) == 2


# ---- the Evolver side, the check command, the CLI dispatch and the meter -----------------------------------------------

def test_instantiate_binds_an_agent_evolver(monkeypatch):
    from hermit.agent.evolver.loader import find, instantiate
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-key")
    _, params = instantiate(find("codex"), {"model": f"fireworks/{GLM}", "args": ["-c", "x=1"]}, backends=reg())
    assert params["model"] == GLM and params["args"][1] == 'model_provider="fireworks"' and params["args"][-1] == "x=1"
    assert params["binding"]["ref"] == f"fireworks/{GLM}" and params["price"]["input"] == 0.15
    _, params = instantiate(find("claude"), {}, backends=reg(), default_model="vllm/q")
    assert params["model"] == "q" and params["env"]["ANTHROPIC_MODEL"] == "q" and params["env"]["ANTHROPIC_AUTH_TOKEN"] == "local"
    _, params = instantiate(find("codex"), {"model": "gpt-5.4"}, backends=reg())
    assert params["model"] == "gpt-5.4" and "binding" not in params             # the CLI's own provider
    _, params = instantiate(find("gepa"), {}, backends=reg(), default_model="vllm/q")
    assert "binding" not in params                                             # not an agent Evolver


def _tiny_run(tmp_path, **overrides):
    for name, text in (("tasks.jsonl", '{"id": "t1", "question": "q"}\n'), ("train.txt", "t1\n"),
                       ("judge.py", "def score(task, output):\n    return 1.0\n")):
        (tmp_path / name).write_text(text)
    (tmp_path / "task-info").mkdir(exist_ok=True)
    (tmp_path / "task-info" / "README.md").write_text("task\n")
    data = {"data": {"tasks": "tasks.jsonl", "train": "train.txt"}, "judge": {"path": "judge.py"}, "task_info": "task-info",
            "template": "prompt-only@1.0", "evolver": {"name": "codex"}, "model": "vllm/q",
            "backends": {"vllm": {"base_url": "http://127.0.0.1:1/v1"}},
            "agent": {"name": "react", "params": {"prompt": "hi"}}, "sandbox": {"enabled": True},
            "trainer": {"budget": {"hours": 1}}, **overrides}
    (tmp_path / "run.yaml").write_text(yaml.safe_dump(data))
    return tmp_path / "run.yaml"


def test_check_reports_models_and_train_refuses_a_missing_dialect(tmp_path, monkeypatch):
    from hermit.settings_check import run_check
    from hermit.trainer.trainer import Trainer
    monkeypatch.setenv("HA_BACKENDS", str(tmp_path / "no-user-file.yaml"))
    lines = []
    config = Config.load(_tiny_run(tmp_path))
    assert run_check(config, probe=False, log=lines.append)
    text = "\n".join(lines)
    assert "[ok] model: llm tool — vllm/q at http://127.0.0.1:1/v1 (chat)" in text
    assert "[ok] model: agent (react) — vllm/q at http://127.0.0.1:1/v1 (chat)" in text
    assert "[ok] model: evolver — vllm/q at http://127.0.0.1:1/v1 (responses)" in text
    lines.clear()
    config = Config.load(_tiny_run(tmp_path), ["evolver.name=claude", "backends.vllm.apis=[chat]"])
    assert not run_check(config, probe=False, log=lines.append)
    assert any("[PROBLEM] model: evolver" in l for l in lines) and any("not 'messages'" in l for l in lines)
    trainer = Trainer(config, log=lambda *a: None)
    with pytest.raises(SystemExit, match="train refuses to start: evolver: backend 'vllm' serves"):
        trainer._prepare()


def test_evolver_binding_reaches_the_sandbox(tmp_path, monkeypatch):
    """The learner reaches the Evolver's endpoint as Harbor's agents reach theirs: its key variable passes into the
    learner's sandbox and its host is allowed; the key's value is never written into the run's records."""
    from hermit.trainer.trainer import Trainer
    monkeypatch.setenv("HA_BACKENDS", str(tmp_path / "no-user-file.yaml"))
    monkeypatch.setenv("LAB_KEY", "sekret-123")
    config = Config.load(_tiny_run(tmp_path, backends={"vllm": {"base_url": "http://box:8000/v1", "api_key_env": "LAB_KEY"}},
                                   sandbox={"enabled": False}))
    trainer = Trainer(config, log=lambda *a: None)
    trainer._prepare()
    # as Harbor gives its agents their keys: the key variable passes, the provider's host is allowed
    assert "LAB_KEY" in trainer.sandbox.env_allow and trainer.sandbox.allow == ["box"]
    assert trainer.params["binding"]["api_key_env"] == "LAB_KEY" and "sekret-123" not in json.dumps(trainer.params)
    recorded = yaml.safe_load((trainer.run_dir / "config.yaml").read_text())
    assert "sekret-123" not in json.dumps(recorded["evolver"]["params"])


def test_cli_dispatch_and_meter_buckets(capsys):
    from hermit.cli import main as cli_main
    from hermit.runtime.wire_meter import split_bucket
    assert cli_main(["backends", "list"]) == 0 and "fireworks" in capsys.readouterr().out
    assert cli_main(["backends", "show", f"fireworks/{GLM}"]) == 0 and '"ref"' in capsys.readouterr().out
    assert split_bucket("/run_o0/inference/v1/chat/completions") == ("run_o0", "/inference/v1/chat/completions")
    assert split_bucket("/run_o0/v1/responses") == ("run_o0", "/v1/responses")
    assert split_bucket("/run_o0/chat/completions") == ("run_o0", "/chat/completions")     # a backend at its origin
    assert split_bucket("/v1/chat/completions") == ("unattributed", "/v1/chat/completions")
    assert split_bucket("/anthropic/v1/messages") == ("unattributed", "/anthropic/v1/messages")
    assert split_bucket("/health") == ("unattributed", "/health")


def test_a_meter_carries_on_the_ledger_an_earlier_one_left(tmp_path):
    """A launcher stops its meter when its run ends and the next run on the port starts another on the same ledger
    (#82): with LEDGER_RESUME=1 (the launchers set it) the new meter keeps the earlier totals instead of writing an
    empty ledger over them; without it a meter starts empty, as lean_swarm's rerun of an attempt expects."""
    ledger = tmp_path / "wire_ledger.json"
    earlier = {"run_o0": {"input": 1000, "cacheRead": 200, "output": 50, "calls": 3}}
    ledger.write_text(json.dumps({"ledger": earlier, "health": {}, "ts": 1}))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2]), "UPSTREAM": "http://127.0.0.1:9",
           "PORT": str(port), "LEDGER": str(ledger), "FLUSH_S": "3600"}
    env.pop("UPSTREAM_KEY_ENV", None)

    def meter(**extra) -> dict:
        """The ledger a meter serves once up, and the one it writes when stopped as a launcher stops it (SIGTERM)."""
        proc = subprocess.Popen([sys.executable, "-m", "hermit.runtime.wire_meter"], env={**env, **extra},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/ledger", timeout=2) as response:
                        live = json.loads(response.read())["ledger"]
                    break
                except OSError:
                    time.sleep(0.2)
            else:
                raise AssertionError("the meter did not come up")
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
        assert json.loads(ledger.read_text())["ledger"] == live
        return live

    assert meter(LEDGER_RESUME="1") == earlier
    assert meter() == {}


def test_a_task_agent_on_a_cli_base_is_bound_and_checked_like_the_cli():
    """A task's own agent on agent.base: codex with a model reference is bound as Codex's model (not a dictionary for
    -m), and the run's preflight checks that binding for the Responses API."""
    data = {"agent": {"name": "trials.py:TrialCodexAgent",
                      "params": {"base": "codex", "model": "fireworks/accounts/fireworks/models/glm-5p3-flash"}}}
    normalize_config(data)
    params = data["agent"]["params"]
    assert params["model"] == "accounts/fireworks/models/glm-5p3-flash" and params["binding"]
    assert [(who, api) for who, _, api in bindings_of(data)] == [("agent (trials.py:TrialCodexAgent)", "responses")]


# ---- a Harbor task set's acting agent -------------------------------------------------------------------------------

def _harbor(acting: dict, model, **params) -> dict:
    """A loaded config of a Harbor task set (agent.name: harbor) whose acting agent is `acting`, normalized."""
    data = {"model": None, "backends": {}, "llm": None,
            "agent": {"name": "harbor", "params": {"tasks_dir": "tasks", "acting_agent": acting, "model": model,
                                                   **params}}}
    normalize_config(data)
    return data


def test_a_harbor_acting_agents_model_is_bound_for_the_cli_it_runs_in_the_tasks_container(monkeypatch, tmp_path):
    """agent.name: harbor runs a HermitXXX (one CLI Base Agent) in each task's container: its model is checked in the
    dialect of the CLI it runs (Claude Code the Messages API, Codex the Responses API, Pi and dsh their pi-ai dialect,
    OpenCode, mini-swe-agent and the answer-only agent chat completions), which its `cli` or its table import path
    names (Codex when it names neither), and the params keep the whole binding (its developer_role too), which makes
    the same binding again for the runner to give the CLI its provider settings; the model is the endpoint's id, and
    no CLI's settings land among the runner's own. Normalized again, the params stay as they are."""
    import copy
    monkeypatch.setenv("HA_BACKENDS", str(tmp_path / "no-user-file.yaml"))
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    r = reg()
    cases = [({"cli": "claude"}, "anthropic/claude-haiku-5-5", "claude", "messages"),
             ({"import_path": "harborside.claude:HermitClaude", "kwargs": {}}, "anthropic/claude-haiku-5-5", "claude",
              "messages"),
             ({}, f"fireworks/{GLM}", "codex", "responses"),
             ({"import_path": "harborside.codex:HermitCodex"}, f"fireworks/{GLM}", "codex", "responses"),
             ({"cli": "pi"}, "anthropic/claude-haiku-5-5", "pi", "messages"),
             ({"cli": "dsh"}, f"fireworks/{GLM}", "dsh", "chat"),
             ({"cli": "opencode"}, "vllm/q", "opencode", "chat"),
             ({"import_path": "harborside.mini_swe_agent:HermitMiniSweAgent"}, "vllm/openai/gpt-oss-120b",
              "mini-swe-agent", "chat"),
             ({"cli": "answer"}, "vllm/q", "answer", "chat")]
    for acting, ref, cli, api in cases:
        data = _harbor(acting, ref)
        params, binding = data["agent"]["params"], resolve(ref, r)
        assert backends.harbor_cli(params) == cli and params["acting_agent"] == acting
        assert params["model"] == binding.model and params["binding"] == binding.spec()
        assert params["price"] == binding.price and not {"args", "env", "provider"} & set(params)
        [(who, bound, dialect)] = bindings_of(data)
        assert (who, bound.ref, dialect) == (f"agent (harbor, {cli})", ref, api)
        assert not [p for p in problems_of(bound, dialect) if " serves " in p], (cli, problems_of(bound, dialect))
        again = copy.deepcopy(data)
        normalize_config(again)
        assert again["agent"] == data["agent"], cli          # the id (openai/gpt-oss-120b) is not bound again
        assert Binding.from_spec(params["binding"]).spec() == params["binding"]
    assert _harbor({}, f"fireworks/{GLM}")["agent"]["params"]["binding"]["developer_role"] == "system"


def test_harbors_own_agents_and_a_clis_own_model_name_keep_their_forms(monkeypatch, tmp_path):
    """One of Harbor's own agents (by name, or a class outside harborside) keeps the endpoint form Harbor is given
    ({model, base_url, api_key_env} among the spec) and is checked as chat completions; a CLI's own model name (with
    its login) is no reference and stays as given, with no binding to check; an acting CLI the table has not is
    refused when the config loads."""
    monkeypatch.setenv("HA_BACKENDS", str(tmp_path / "no-user-file.yaml"))
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    for acting in ({"name": "terminus-2"}, {"import_path": "examples.x.agents:MyAgent"},
                   {"import_path": "x:HermitCodex"}):
        data = _harbor(acting, "vllm/q")
        params = data["agent"]["params"]
        assert backends.harbor_cli(params) is None and "binding" not in params
        assert params["model"]["model"] == "q" and params["model"]["base_url"] == "http://127.0.0.1:8000/v1"
        assert [(who, api) for who, _, api in bindings_of(data)] == [("agent (harbor)", "chat")]
    data = _harbor({"cli": "claude"}, "claude-haiku-5-5", login="claude")
    assert data["agent"]["params"]["model"] == "claude-haiku-5-5" and "binding" not in data["agent"]["params"]
    assert bindings_of(data) == []
    with pytest.raises(ValueError, match="acting_agent.cli must be one of codex, claude, pi"):
        _harbor({"cli": "claude-code"}, "vllm/q")


def test_a_binding_keeps_its_developer_role_through_its_plain_form():
    """A run records a binding in its plain form (Binding.spec), which Harbor's runner, an Evolver's sessions and the
    run's checks make a binding again (Binding.from_spec): a backend that refuses the developer role still says so,
    which Pi's and dsh's provider entries follow; the older endpoint form keeps the role; a spec naming another role
    is refused."""
    fw = resolve(f"fireworks/{GLM}", reg())
    again = Binding.from_spec(fw.spec())
    assert fw.spec()["developer_role"] == "system" and again.spec() == fw.spec()
    assert again.pi_provider()["compat"] == {"supportsDeveloperRole": False} == again.dsh_provider()["compat"]
    assert Binding.from_spec({"base_url": "http://x/v1", "model": "m"}).backend.developer_role == "keep"
    with pytest.raises(ValueError, match="developer_role must be one of"):
        Binding.from_spec({**fw.spec(), "developer_role": "user"})


def test_the_harbor_acting_agents_this_layer_names_are_the_runners_table():
    """The model layer names a Harbor task set's acting agents by a copy of hermit.harbor.clis's table (it imports no
    layer above it): every entry's import path names that entry's CLI, and the copy names every entry."""
    from hermit.harbor.clis import HARBOR_CLIS
    assert sorted(HARBOR_CLIS) == sorted(backends.HARBOR_ACTING.values())
    for cli, entry in HARBOR_CLIS.items():
        assert backends.harbor_cli({"acting_agent": {"import_path": entry["import_path"]}}) == cli
        assert backends.harbor_cli({"acting_agent": {"cli": cli}}) == cli
