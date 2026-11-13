"""Backend contract tests; no API calls or Codex processes are started here."""
import sys
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "examples/agentdyn/utils"))  # bridge, victim, auditor
from victim import model_params, script_settings


def test_model_backend_uses_local_connection_settings(tmp_path, monkeypatch):
    import yaml
    from hermit.config import Config

    local = tmp_path / "backends.yaml"
    local.write_text(yaml.safe_dump({"vllm": {
        "base_url": "https://model-service.example.invalid/v1", "api_key_env": "VICTIM_TEST_KEY",
        "key_required": True, "apis": ["chat"],
        "models": {"nvidia/nvidia/nemotron-3.5-lightning": "provider/nemotron"},
        "prices": {"provider/nemotron": {"input": 1, "output": 2}}, "extra_body": {"top_k": 50},
    }}))
    monkeypatch.setenv("HA_BACKENDS", str(local))
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    root = Path(__file__).resolve().parents[3]
    config = Config.load(root / "examples/agentdyn/evosafe/run_nemotron_claude.yaml")
    original = config["agent"]["params"]
    params = model_params({**original, "extra_body": {"top_k": 20}})
    assert params["model"] == "provider/nemotron"
    assert params["base_url"] == "https://model-service.example.invalid/v1"
    assert params["api_key_env"] == "VICTIM_TEST_KEY" and params["key_required"]
    assert params["price"] == {"input": 1, "output": 2}
    assert params["extra_body"] == {"top_k": 20}
    assert isinstance(original["model"], dict)  # Adapter does not mutate the loaded config.


def test_api_script_is_parsed_without_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("VICTIM_TEST_KEY", "dummy-secret")
    script = tmp_path / "api.py"
    script.write_text('''
import os
from openai import OpenAI
URL = "https://example.invalid/v1"
KEY = os.environ["VICTIM_TEST_KEY"]
client = OpenAI(base_url=URL, api_key=KEY)
raise RuntimeError("must not execute this module")
''')
    assert script_settings(script) == {"base_url": "https://example.invalid/v1", "api_key": "dummy-secret"}
    script.write_text('client = OpenAI(base_url=compute_url(), api_key="dummy")')
    with pytest.raises(ValueError, match="literals"):
        script_settings(script)


def test_compatible_victim_tools_usage_and_error_redaction(tmp_path, monkeypatch):
    openai = pytest.importorskip("openai")
    pytest.importorskip("agentdojo")
    from openai.types.chat import ChatCompletion
    from victim import make_victim
    monkeypatch.setenv("VICTIM_TEST_KEY", "dummy-secret")
    requests = []
    response = {"id": "test", "created": 0, "object": "chat.completion", "model": "nemotron-test",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ready"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}

    def create(self, messages, **kwargs):
        requests.append(kwargs)
        if len(requests) == 2:
            raise RuntimeError("private request: dummy-secret")
        from hermit.agent.model.usage import Usage
        return {"content": "ready", "_response": response}, Usage("nemotron-test", input_tokens=10, output_tokens=2, calls=1)

    monkeypatch.setattr("hermit.agent.model.llm.ChatClient.chat", create)
    events = []
    victim = make_victim({"model": "nemotron-test", "base_url": "https://example.invalid/v1",
                          "api_key_env": "VICTIM_TEST_KEY", "max_tokens": 123}, tmp_path, on_call=events.append)
    out = victim.query("hi", SimpleNamespace(functions={}), {}, messages=[])
    assert out[3][-1]["content"][0]["content"] == "ready"
    assert requests[0]["max_tokens"] == 123
    assert "tools" not in requests[0]
    assert victim.usage == {"calls": 1, "input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2,
                            "cost_usd": 0.0, "unknown_usage_calls": 0, "unknown_cost_calls": 1}
    with pytest.raises(RuntimeError, match="victim API failed") as error:
        victim.query("hi", SimpleNamespace(functions={}), {})
    assert "dummy-secret" not in str(error.value)
    assert events[0]["finish_reason"] == "stop" and events[1]["status"] == "error"
    assert victim.usage["calls"] == 2 and victim.usage["unknown_usage_calls"] == 1
    assert "dummy-secret" not in json.dumps(events)
    victim.close()


@pytest.mark.parametrize("failure", ["truncated", "truncated_text", "malformed", "textual", "no_calls"])
def test_compatible_victim_rejects_partial_responses(tmp_path, monkeypatch, failure):
    openai = pytest.importorskip("openai")
    pytest.importorskip("agentdojo")
    from openai.types.chat import ChatCompletion
    from victim import make_victim
    monkeypatch.setenv("VICTIM_TEST_KEY", "dummy")
    message = {"role": "assistant", "content": "<TOOLCALL>invalid</TOOLCALL>" if failure == "textual"
               else "Done." if failure in ("no_calls", "truncated_text") else None}
    if failure == "malformed":
        message["tool_calls"] = [{"id": "call", "type": "function",
                                  "function": {"name": "echo", "arguments": "not json"}}]
    response = ChatCompletion.model_validate({"id": "test", "created": 0, "object": "chat.completion",
        "model": "nemotron-test", "choices": [{"index": 0, "message": message,
        "finish_reason": "length" if failure.startswith("truncated")
        else "tool_calls" if failure in ("malformed", "no_calls") else "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15} if failure == "textual" else None})
    from hermit.agent.model.usage import Usage
    monkeypatch.setattr("hermit.agent.model.llm.ChatClient.chat", lambda self, messages, **kw:
                        ({"_response": response.model_dump()}, Usage("nemotron-test", calls=1,
                            input_tokens=10 if failure == "textual" else 0,
                            output_tokens=5 if failure == "textual" else 0)))
    events = []
    victim = make_victim({"model": "nemotron-test", "base_url": "https://example.invalid/v1",
                          "api_key_env": "VICTIM_TEST_KEY"}, tmp_path, events.append)
    expected = {"truncated": "truncated", "truncated_text": "truncated", "malformed": "malformed",
                "no_calls": "missing_structured_tool_calls"}
    with pytest.raises(RuntimeError, match=expected.get(failure, "unparsed_textual_tool_call")):
        victim.query("task", SimpleNamespace(functions={}), {})
    assert victim.usage["calls"] == 1 and len(events) == 1
    assert events[0]["status"] == "error"  # the call's event agrees with the raised failure
    if failure == "textual":
        assert victim.usage["input_tokens"] == 10 and victim.usage["output_tokens"] == 5
        assert events[0]["status"] == "error"
    else:
        assert victim.usage["unknown_usage_calls"] == 1


@pytest.mark.parametrize('content,calls,expected', [
    ('Done.', False, None),
    ('<think>private reasoning</think>Done.', False, None),
    ('<think>unfinished', False, 'unfinished_reasoning'),
    ('<think>complete</think>', False, 'missing_terminal_answer'),
    ('<TOOLCALL>{invalid}</TOOLCALL>', False, 'unparsed_textual_tool_call'),
    ('<function=search_product>{"product_name": "watch"}</function>', False, 'unparsed_textual_tool_call'),
    ('<tool_call>{"name": "search_product"}</tool_call>', False, 'unparsed_textual_tool_call'),
    (None, False, 'missing_terminal_answer'),
    (None, True, None),
    ([{'type': 'text', 'text': 'Done.'}], False, None),
])
def test_terminal_protocol_validation(content, calls, expected):
    from victim import terminal_protocol_error
    assert terminal_protocol_error(content, calls) == expected


def test_victim_uses_direct_and_preserves_wire_request(tmp_path, monkeypatch):
    """Exercise the real direct + ChatClient path against a loopback provider."""
    pytest.importorskip("agentdojo")
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from hermit.agent.base import interface
    from victim import make_victim
    seen, bases = [], []
    original = interface.make
    def make(name, settings, model):
        bases.append(name)
        return original(name, settings, model)
    monkeypatch.setattr(interface, "make", make)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            seen.append((self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            body = json.dumps({"id": "fixture", "created": 0, "object": "chat.completion", "model": "fixture",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ready"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("VICTIM_FIXTURE_KEY", "fixture-key")
    try:
        victim = make_victim({"model": "fixture", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                              "api_key_env": "VICTIM_FIXTURE_KEY", "max_tokens": 41, "top_p": 0.8,
                              "extra_body": {"reasoning_effort": "low"}}, tmp_path)
        victim.query("", SimpleNamespace(functions={}), {}, messages=[])
        victim.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert bases == ["direct"]
    key, body = seen[0]
    assert key == "Bearer fixture-key"
    assert body["model"] == "fixture" and body["max_tokens"] == 41 and body["top_p"] == 0.8
    assert body["reasoning_effort"] == "low" and "extra_body" not in body and "tools" not in body
    assert victim.usage["input_tokens"] == 5 and victim.usage["output_tokens"] == 2


@pytest.fixture
def usage_provider():
    """Local provider exercising the real shared ChatClient accounting."""
    pytest.importorskip("agentdojo")
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    pending = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps(pending.pop(0)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    params = {"model": "fixture", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
              "key_required": False, "max_retries": 0,
              "price": {"input": 10, "cached_input": 1, "output": 20}}
    try:
        yield pending, params
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def usage_response():
    return {"id": "fixture", "created": 0, "object": "chat.completion", "model": "fixture",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant",
                         "content": '{"has_injected_instruction": false, "reason": "fixture"}'}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
                      "prompt_tokens_details": {"cached_tokens": 900}}}


def test_cached_victim_and_classifier_usage_reaches_budget(tmp_path, usage_provider):
    from victim import make_victim
    from auditor import OutputClassifier
    from bridge import metered, score
    from hermit.task import RunOutput
    from hermit.trainer.rollout import run_tasks
    from hermit.trainer.budget import Budget
    pending, params = usage_provider
    pending.extend([usage_response(), usage_response()])
    victim = make_victim(params, tmp_path)
    classifier = OutputClassifier(params, tmp_path)
    assert metered(classifier.usage) == (0, 0.0)
    def pipeline(task, adapter):
        victim.query("", SimpleNamespace(functions={}), {}, messages=[])
        assert victim.usage["cost_usd"] == pytest.approx(0.0039)
        assert classifier.classify("fixture", "fixture")["status"] == "ok"
        for key, value in classifier.usage.items():
            victim.usage[key] += value  # the episode bridge's merge
        tokens, cost = metered(victim.usage)
        return RunOutput(output={"utility": True, "attack_success": None}, tokens=tokens, cost_usd=cost)
    budget = Budget(usd=0.01)
    rows = run_tasks([{"id": "fixture", "kind": "clean"}], None, pipeline, score,
                     concurrency=1, budget=budget)
    assert rows[0]["status"] == "ok"
    assert rows[0]["cost_usd"] == pytest.approx(0.0078)
    assert rows[0]["tokens"] == 2200
    assert victim.usage["cached_input_tokens"] == 1800
    assert victim.usage["calls"] == 2
    assert budget.snapshot()["usd"]["rollout"] == pytest.approx(0.0078)
    assert budget.exceeded() is None
    victim.close()
    classifier.close()


@pytest.mark.parametrize("mode", ["missing", "empty", "partial", "no_price", "zero", "malformed"])
def test_shared_usage_preserves_unknowns_and_billed_parse_failures(tmp_path, usage_provider, mode):
    from victim import make_victim
    from bridge import metered
    pending, params = usage_provider
    response = usage_response()
    if mode == "missing":
        response.pop("usage")
    elif mode == "empty":
        response["usage"] = {}
    elif mode == "partial":
        response["usage"] = {"prompt_tokens": 1000}
    elif mode == "no_price":
        params.pop("price")
    elif mode == "zero":
        response["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    elif mode == "malformed":
        response["choices"][0]["message"]["content"] = {"invalid": "content type"}
    pending.append(response)
    victim = make_victim(params, tmp_path)
    if mode in ("empty", "partial", "malformed"):
        with pytest.raises(RuntimeError, match="victim API failed"):
            victim.query("", SimpleNamespace(functions={}), {})
    else:
        victim.query("", SimpleNamespace(functions={}), {})
    assert victim.usage["calls"] == 1  # parsing failures do not double-count
    if mode in ("missing", "empty", "partial"):
        assert metered(victim.usage) == (None, None)
        assert victim.usage["unknown_usage_calls"] == victim.usage["unknown_cost_calls"] == 1
    elif mode == "no_price":
        assert metered(victim.usage) == (1100, None)
        assert victim.usage["unknown_usage_calls"] == 0 and victim.usage["unknown_cost_calls"] == 1
    elif mode == "zero":
        assert metered(victim.usage) == (0, 0.0)
        assert victim.usage["unknown_usage_calls"] == victim.usage["unknown_cost_calls"] == 0
    else:
        tokens, cost = metered(victim.usage)
        assert tokens == 1100 and cost == pytest.approx(0.0039)
        assert victim.usage["unknown_usage_calls"] == victim.usage["unknown_cost_calls"] == 0
    victim.close()


def test_transport_error_keeps_known_subtotal_but_marks_episode_unknown(tmp_path, usage_provider, monkeypatch):
    from victim import make_victim
    from bridge import metered
    pending, params = usage_provider
    pending.append(usage_response())
    victim = make_victim(params, tmp_path)
    victim.query("", SimpleNamespace(functions={}), {})
    def failed(*args, **kwargs):
        raise RuntimeError("fixture transport failure")
    monkeypatch.setattr("hermit.agent.model.llm.ChatClient.chat", failed)
    with pytest.raises(RuntimeError, match="victim API failed"):
        victim.query("", SimpleNamespace(functions={}), {})
    assert victim.usage["calls"] == 2
    assert victim.usage["cost_usd"] == pytest.approx(0.0039)
    assert victim.usage["input_tokens"] == 1000
    assert victim.usage["unknown_usage_calls"] == victim.usage["unknown_cost_calls"] == 1
    assert metered(victim.usage) == (None, None)
    victim.close()


def test_victim_without_an_optional_key_sends_no_authorization(tmp_path):
    pytest.importorskip("agentdojo")
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from victim import make_victim
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            seen.append(self.headers.get("Authorization"))
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"id": "fixture", "created": 0, "object": "chat.completion", "model": "fixture",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ready"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        victim = make_victim({"model": "fixture", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                              "key_required": False}, tmp_path)
        victim.query("", SimpleNamespace(functions={}), {}, messages=[])
    finally:
        server.shutdown()
    assert seen == [None]
