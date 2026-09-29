"""Responder-driven fake of the Fireworks Responses endpoint (127.0.0.1 only, no credentials).

`Upstream(responder)` serves every POST by calling `responder(body, index)`, where `body` is the parsed
JSON request and `index` the 0-based request ordinal. The responder returns one of:
- a response dict (see `response()`)       -> served as a full SSE stream by `sse_full()`
- bytes                                     -> written verbatim as a text/event-stream body
- ('http', code, body_dict_or_bytes)        -> an HTTP error with that body
- 'drop'                                    -> response.created, then the connection closes (no terminal event)
- 'stall'                                   -> headers, then nothing until `close()` (or the client gives up)
`Upstream.bodies` keeps every parsed request, `Upstream.raw` the exact request bytes and
`Upstream.served` the (request index, SSE bytes) of every streamed reply, so tests can compare what the
broker sent upstream and forwarded to Codex byte for byte.

`sse_full(response)` emits the event sequence the real Codex needs to build its history:
response.created, response.in_progress, then per output item response.output_item.added, the item's
deltas (message text parts, function-call arguments, reasoning summary), response.output_item.done, and
finally the terminal response.completed / response.incomplete / response.failed carrying `id` and
`usage`. The v8 fake (`tests/test_v8_broker.py:sse`) emitted only created, one delta and the terminal
event, which a broker test accepts but Codex would not build history from (understand/broker.md §3c).
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import itertools
import json
import threading

MODEL = 'accounts/fireworks/models/glm-5p3-flash'
_ids = itertools.count(1)


def usage(input_tokens=100, output_tokens=20, cached=0, reasoning=0):
    return {'input_tokens': input_tokens, 'input_tokens_details': {'cached_tokens': cached},
            'output_tokens': output_tokens, 'output_tokens_details': {'reasoning_tokens': reasoning},
            'total_tokens': input_tokens + output_tokens}


def message(text, *, id=None, annotations=True):
    part = {'type': 'output_text', 'text': text}
    if annotations:
        part['annotations'] = []
    return {'type': 'message', 'id': id or f'msg_fake{next(_ids):04d}', 'status': 'completed',
            'role': 'assistant', 'content': [part]}


def call(cmd, *, call_id=None, id=None, name='exec_command', **extra):
    n = next(_ids)
    args = {'cmd': cmd, **extra} if name == 'exec_command' else extra
    return {'type': 'function_call', 'id': id or f'fc_fake{n:04d}', 'status': 'completed', 'name': name,
            'arguments': json.dumps(args), 'call_id': call_id or f'call_fake{n:04d}'}


def reasoning(summary='thinking', *, id=None, encrypted='enc'):
    item = {'type': 'reasoning', 'id': id or f'rs_fake{next(_ids):04d}',
            'summary': [{'type': 'summary_text', 'text': summary}] if summary else []}
    if encrypted is not None:
        item['encrypted_content'] = encrypted
    return item


def response(output, *, status='completed', use=None, id=None, reason=None, model=MODEL):
    value = {'id': id or f'resp_fake{next(_ids):04d}', 'object': 'response', 'model': model,
             'status': status, 'output': list(output), 'usage': usage() if use is None else use}
    if reason:
        value['incomplete_details'] = {'reason': reason}
    return value


def _event(seq, kind, **fields):
    return {'type': kind, 'sequence_number': seq, **fields}


def sse_events(resp):
    """The full event list for one response (see module docstring)."""
    seq = itertools.count(0)
    shell = {k: v for k, v in resp.items() if k not in ('output', 'usage', 'status')}
    events = [_event(next(seq), 'response.created', response={**shell, 'status': 'in_progress', 'output': []}),
              _event(next(seq), 'response.in_progress', response={**shell, 'status': 'in_progress', 'output': []})]
    for index, item in enumerate(resp.get('output') or []):
        kind = item.get('type')
        if kind == 'message':
            events.append(_event(next(seq), 'response.output_item.added', output_index=index,
                                 item={**item, 'status': 'in_progress', 'content': []}))
            for p, part in enumerate(item.get('content') or []):
                empty = {**part, 'text': ''}
                events.append(_event(next(seq), 'response.content_part.added', item_id=item.get('id'),
                                     output_index=index, content_index=p, part=empty))
                text = part.get('text') or ''
                for chunk in [text[i:i + 16] for i in range(0, len(text), 16)] or ['']:
                    events.append(_event(next(seq), 'response.output_text.delta', item_id=item.get('id'),
                                         output_index=index, content_index=p, delta=chunk))
                events.append(_event(next(seq), 'response.output_text.done', item_id=item.get('id'),
                                     output_index=index, content_index=p, text=text))
                events.append(_event(next(seq), 'response.content_part.done', item_id=item.get('id'),
                                     output_index=index, content_index=p, part=part))
        elif kind == 'function_call':
            events.append(_event(next(seq), 'response.output_item.added', output_index=index,
                                 item={**item, 'status': 'in_progress', 'arguments': ''}))
            args = item.get('arguments') or ''
            for chunk in [args[i:i + 24] for i in range(0, len(args), 24)] or ['']:
                events.append(_event(next(seq), 'response.function_call_arguments.delta', item_id=item.get('id'),
                                     output_index=index, delta=chunk))
            events.append(_event(next(seq), 'response.function_call_arguments.done', item_id=item.get('id'),
                                 output_index=index, arguments=args))
        elif kind == 'reasoning':
            events.append(_event(next(seq), 'response.output_item.added', output_index=index,
                                 item={**item, 'summary': []}))
            for s, part in enumerate(item.get('summary') or []):
                events.append(_event(next(seq), 'response.reasoning_summary_part.added', item_id=item.get('id'),
                                     output_index=index, summary_index=s, part={**part, 'text': ''}))
                events.append(_event(next(seq), 'response.reasoning_summary_text.delta', item_id=item.get('id'),
                                     output_index=index, summary_index=s, delta=part.get('text') or ''))
                events.append(_event(next(seq), 'response.reasoning_summary_text.done', item_id=item.get('id'),
                                     output_index=index, summary_index=s, text=part.get('text') or ''))
                events.append(_event(next(seq), 'response.reasoning_summary_part.done', item_id=item.get('id'),
                                     output_index=index, summary_index=s, part=part))
        else:
            events.append(_event(next(seq), 'response.output_item.added', output_index=index, item=item))
        events.append(_event(next(seq), 'response.output_item.done', output_index=index, item=item))
    terminal = {'completed': 'response.completed', 'incomplete': 'response.incomplete',
                'failed': 'response.failed'}.get(resp.get('status'), 'response.completed')
    events.append(_event(next(seq), terminal, response=resp))
    return events


def sse_full(resp):
    return b''.join(b'event: ' + e['type'].encode() + b'\ndata: ' + json.dumps(e).encode() + b'\n\n'
                    for e in sse_events(resp))


def sse_minimal(resp):
    """The v8 fake's stream (created, one reasoning delta, terminal) for byte-equivalence tests."""
    kind = {'completed': 'response.completed', 'incomplete': 'response.incomplete',
            'failed': 'response.failed'}[resp['status']]
    events = [{'type': 'response.created', 'response': {'id': resp.get('id'), 'model': MODEL, 'status': 'in_progress'}},
              {'type': 'response.reasoning_text.delta', 'delta': 'thinking'},
              {'type': kind, 'response': resp}]
    return b''.join(b'data: ' + json.dumps(e).encode() + b'\n\n' for e in events)


class Upstream:
    """Fake upstream on 127.0.0.1:<random>; `url` is the full /v1/responses URL."""

    def __init__(self, responder, *, sse=sse_full):
        if isinstance(responder, (list, tuple)):
            queue = list(responder)
            responder = lambda body, index: queue.pop(0) if queue else response([], status='failed')
        self.responder, self.sse = responder, sse
        self.bodies, self.raw, self.headers, self.served = [], [], [], []
        self.lock = threading.Lock()
        self.closing = threading.Event()
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def log_message(self, *_):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                with upstream.lock:
                    index = len(upstream.bodies)
                    upstream.raw.append(raw)
                    upstream.headers.append({k.lower(): v for k, v in self.headers.items()
                                             if k.lower() != 'authorization'})
                    upstream.bodies.append(json.loads(raw))
                reply = upstream.responder(upstream.bodies[-1], index)
                try:
                    if isinstance(reply, tuple) and reply and reply[0] == 'http':
                        _, code, body = reply
                        data = body if isinstance(body, bytes) else json.dumps(body).encode()
                        self.send_response(code)
                        self.send_header('Content-Type', 'application/json' if not isinstance(body, bytes) else 'text/plain')
                        self.send_header('Content-Length', str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    if reply == 'stall':
                        upstream.closing.wait(120)
                        return
                    if reply == 'drop':
                        created = {'type': 'response.created', 'response': {'id': 'resp_drop', 'model': MODEL,
                                                                            'status': 'in_progress'}}
                        self.wfile.write(b'data: ' + json.dumps(created).encode() + b'\n\n')
                        return
                    data = reply if isinstance(reply, bytes) else upstream.sse(reply)
                    upstream.served.append((index, data))
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1/responses'

    def close(self):
        self.closing.set()
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
