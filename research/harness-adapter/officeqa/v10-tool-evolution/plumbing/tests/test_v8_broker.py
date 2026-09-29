"""Offline tests of the v8 broker fixes against a local fake Fireworks server.

No credentials or network: broker.URL is pointed at 127.0.0.1 and every
canned response is synthetic.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import urllib.request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import broker  # noqa: E402
import rate_control  # noqa: E402

MODEL = broker.MODEL
GOOD_MESSAGE = {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'FINAL'}]}
GOOD_CALL = {'type': 'function_call', 'name': 'exec_command', 'call_id': 'c1', 'arguments': '{"cmd": "ls"}'}
REASONING = {'type': 'reasoning', 'summary': [], 'encrypted_content': 'x'}
USAGE = {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}
ZERO = {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}


def final(status, output, usage, reason=None):
    value = {'id': 'resp', 'model': MODEL, 'status': status, 'output': output, 'usage': usage}
    if reason:
        value['incomplete_details'] = {'reason': reason}
    return value


def sse(response):
    kind = {'completed': 'response.completed', 'incomplete': 'response.incomplete', 'failed': 'response.failed'}[response['status']]
    events = [{'type': 'response.created', 'response': {'id': 'resp', 'model': MODEL, 'status': 'in_progress'}},
              {'type': 'response.reasoning_text.delta', 'delta': 'thinking'},
              {'type': kind, 'response': response}]
    return b''.join(b'data: ' + json.dumps(e).encode() + b'\n\n' for e in events)


class Upstream:
    """Serves the queued responses in order and records every request body."""
    def __init__(self, responses):
        self.responses, self.bodies = list(responses), []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                upstream.bodies.append(body)
                # An exhausted queue fails the call (never retried) instead of hanging the test.
                data = sse(upstream.responses.pop(0) if upstream.responses else final('failed', [], USAGE))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1/responses'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def relay(tmp_path, monkeypatch, responses, **options):
    upstream = Upstream(responses)
    monkeypatch.setattr(broker, 'URL', upstream.url)
    scheduler = rate_control.RateScheduler(max_in_flight=float('inf'), min_interval=0, backoffs=(0,))
    request = {'model': MODEL, 'stream': True, 'input': [{'role': 'user', 'content': 'hi'}],
               'tools': [{'type': 'function', 'name': 'exec_command', 'parameters': {'type': 'object'}}],
               'reasoning': {'effort': 'high', 'summary': 'auto'}, 'max_output_tokens': 999}
    unbounded = {'max_completion': None, 'max_response': None, 'request_timeout': None, 'max_attempts': None}
    with broker.Broker('test-key', tmp_path / 'api', scheduler=scheduler, **unbounded, **options) as b:
        http = urllib.request.Request(b.endpoint + '/responses', data=json.dumps(request).encode(),
                                      headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(http, timeout=30) as response:
            wire = response.read()
    upstream.close()
    forwarded = [json.loads(line[5:]) for line in wire.splitlines() if line.startswith(b'data:')]
    terminal = [e['response'] for e in forwarded if e.get('type') in ('response.completed', 'response.incomplete')]
    transport = json.loads((tmp_path / 'api/001/transport.json').read_text())
    return upstream, terminal, transport


@pytest.mark.parametrize('defect,bad', [
    ('completed_without_usage', final('completed', [], ZERO)),
    ('invalid_tool_arguments', final('completed', [dict(GOOD_CALL, arguments='{"cmd": "gre')], ZERO)),
    ('completed_without_output', final('completed', [REASONING], USAGE)),
    ('incomplete_without_output', final('incomplete', [REASONING], {'input_tokens': 100, 'output_tokens': 65536}, 'max_output_tokens')),
    ('invalid_tool_arguments', final('completed', [dict(GOOD_CALL, arguments='{"cmd": "gre')], USAGE)),
    ('invalid_tool_arguments', final('incomplete', [REASONING, dict(GOOD_CALL, arguments='{"cmd": "gre')],
                                     {'input_tokens': 100, 'output_tokens': 65536}, 'max_output_tokens')),
    ('invalid_tool_arguments', final('completed', [dict(GOOD_CALL, arguments='["ls"]')], USAGE)),
])
def test_defect_is_resent_and_never_forwarded(tmp_path, monkeypatch, defect, bad):
    assert broker.provider_defect(bad) == defect
    good = final('completed', [REASONING, GOOD_MESSAGE], USAGE)
    upstream, terminal, transport = relay(tmp_path, monkeypatch, [bad, good])
    assert len(upstream.bodies) == 2 and upstream.bodies[0] == upstream.bodies[1]
    assert terminal == [good]
    first, second = transport['attempts']
    assert first['status'] == 'transport_retry' and first['provider_defect'] == defect
    assert second['status'] == 'succeeded'


def test_repeated_defects_resent_until_good(tmp_path, monkeypatch):
    bad = final('completed', [], ZERO)
    good = final('completed', [GOOD_CALL], USAGE)
    upstream, terminal, transport = relay(tmp_path, monkeypatch, [bad, bad, bad, good])
    assert len(upstream.bodies) == 4 and terminal == [good]
    assert [a['status'] for a in transport['attempts']] == ['transport_retry'] * 3 + ['succeeded']
    assert all(a['usage_known'] is False for a in transport['attempts'][:3])


@pytest.mark.parametrize('good', [
    final('completed', [REASONING, GOOD_MESSAGE], USAGE),
    final('completed', [GOOD_CALL], USAGE),
    final('incomplete', [GOOD_MESSAGE], {'input_tokens': 100, 'output_tokens': 65536}, 'max_output_tokens'),
])
def test_usable_response_forwarded_once(tmp_path, monkeypatch, good):
    assert broker.provider_defect(good) is None
    upstream, terminal, transport = relay(tmp_path, monkeypatch, [good])
    assert len(upstream.bodies) == 1 and terminal == [good]


def test_reasoning_forwarded_and_output_caps_dropped(tmp_path, monkeypatch):
    upstream, _, transport = relay(tmp_path, monkeypatch, [final('completed', [GOOD_MESSAGE], USAGE)])
    sent = upstream.bodies[0]
    assert sent['reasoning'] == {'effort': 'high', 'summary': 'auto'}
    assert 'max_output_tokens' not in sent and sent['temperature'] == 0 and sent['store'] is False
    assert transport['changes']['reasoning_forwarded'] == {'effort': 'high', 'summary': 'auto'}
    assert transport['changes']['provider_default_reasoning'] is False


def test_retry_can_be_disabled(tmp_path, monkeypatch):
    bad = final('completed', [], ZERO)
    upstream, terminal, _ = relay(tmp_path, monkeypatch, [bad], retry_defects=False)
    assert len(upstream.bodies) == 1 and terminal == [bad]


def test_tool_call_on_a_request_without_tools_is_not_usable():
    # Codex compaction requests offer no tools; a tool call there would compact to an empty summary.
    call = final('completed', [GOOD_CALL], USAGE)
    assert broker.provider_defect(call, tools=True) is None
    assert broker.provider_defect(call, tools=False) == 'completed_without_output'
    assert broker.provider_defect(final('completed', [GOOD_MESSAGE], USAGE), tools=False) is None
