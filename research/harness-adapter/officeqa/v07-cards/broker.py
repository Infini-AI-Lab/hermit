"""Per-actor Responses relay: one fixed upstream, credentials only in controller RAM.

This is an HTTP transport, not an agent loop. Native Codex owns tools/history.
It changes only the predeclared model sampling/budget/storage settings.
"""
from collections import Counter
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

import rate_control

MODEL = 'accounts/fireworks/models/glm-5p3-flash'
URL = 'https://api.fireworks.ai/inference/v1/responses'

class BudgetExhausted(ValueError): pass
class NoTerminalEvent(ValueError): pass
# Exceptions leaving read_wire's SSE loop carry .generated so a drop after
# generation is never re-sent by the broker (Codex's own stream retry does that).
TRANSPORT_DROPS = (NoTerminalEvent, http.client.IncompleteRead, ConnectionError, urllib.error.URLError)
HEADERS, edge_block = rate_control.HEADERS, rate_control.edge_block


def token_count(value):
    """Return a known nonnegative integer, or None; never coerce unknown usage."""
    return value if type(value) is int and value >= 0 else None


def known_tokens(usage, field):
    """Only known counts enter the lower-bound ledger; incompleteness is explicit."""
    value = token_count(usage.get(field)) if isinstance(usage, dict) else None
    return value if value is not None else 0


def public(value):
    if isinstance(value, list):
        return [public(v) for v in value if not isinstance(v, dict) or v.get('type') != 'reasoning']
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if k not in
                ('reasoning_content', 'encrypted_content', 'api_key', 'Authorization')}
    return value


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def redact(value,key):
    if isinstance(value,str):return value.replace(key,'[REDACTED]') if key else value
    if isinstance(value,list):return [redact(v,key) for v in value]
    if isinstance(value,dict):return {k:redact(v,key) for k,v in value.items()}
    return value


def validate_tools(tools):
    for tool in tools:
        kind = tool.get('type')
        if kind == 'namespace':
            validate_tools(tool.get('tools', []))
        elif kind not in ('function', 'custom'):
            raise ValueError('Only locally executed function/custom tools are allowed')
        if any(k in tool for k in ('server_url', 'server_label', 'connector_id')):
            raise ValueError('Server-executed network tools are prohibited')


def normalized(payload, remaining, max_response=65536):
    if payload.get('model') != MODEL:
        raise ValueError('Only the exact GLM model is allowed; no fallback')
    validate_tools(payload.get('tools', []))
    if payload.get('previous_response_id'):
        raise ValueError('Fresh stateless per-task requests must contain their own history')
    def reject_remote(value):
        if isinstance(value, dict):
            if value.get('type') == 'input_image' and not str(value.get('image_url','')).startswith('data:image/'):
                raise ValueError('Only locally embedded images are admitted; remote image URLs prohibited')
            if value.get('type') == 'input_file':
                raise ValueError('This parsed-text experiment admits no provider-hosted or remote file inputs')
            for child in value.values(): reject_remote(child)
        elif isinstance(value, list):
            for child in value: reject_remote(child)
    reject_remote(payload.get('input'))
    result = dict(payload)
    result.update(model=MODEL, store=False, temperature=0)
    if remaining is None and max_response is None:
        for field in ('max_output_tokens', 'max_tokens', 'max_completion_tokens', 'reasoning', 'thinking'):
            result.pop(field, None)
    else:
        result['max_output_tokens'] = min(x for x in (max_response, remaining) if x is not None)
    # The native Responses reasoning field is preserved; both arms use high.
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Inference redirects are prohibited')


def read_wire(response, stop, deadline=None, *, unbounded=False):
    """Buffer native bytes unchanged; retain evidence of even discarded generation."""
    content_type = response.headers.get('Content-Type', 'application/json')
    final, data_lines, wire, errors = None, [], bytearray(), []
    generated = False
    is_sse = 'text/event-stream' in content_type
    if is_sse:
        try:
            for line in response:
                if stop.is_set(): raise TimeoutError('Actor stopped')
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError('Wire wall-clock deadline exhausted')
                wire.extend(line)
                if not unbounded and len(wire) > 32_000_000: raise ValueError('Response transport size exceeded')
                if line.startswith(b'data:'):
                    data_lines.append(line[5:].strip())
                elif not line.strip() and data_lines:
                    data = b'\n'.join(data_lines); data_lines = []
                    if data == b'[DONE]': continue
                    event = json.loads(data)
                    kind = event.get('type', '')
                    if (kind.startswith(('response.output_', 'response.reasoning',
                                         'response.function_call_arguments', 'response.custom_tool_call_input'))
                            or event.get('delta') or event.get('item') or event.get('part')):
                        generated = True
                    if kind in ('response.completed', 'response.incomplete', 'response.failed'):
                        final = event.get('response', {})
                    if kind == 'error': errors.append(public(event))
            if final is None: raise NoTerminalEvent('No terminal Responses event')
        except Exception as exc:
            exc.generated = generated
            raise
    else:
        raw = response.read() if unbounded else response.read(32_000_001)
        if not unbounded and len(raw) > 32_000_000: raise ValueError('Response transport size exceeded')
        wire.extend(raw)
        final = json.loads(raw)
    return final, bytes(wire), content_type, is_sse, generated, errors


class Broker:
    def __init__(self, key, output, *, max_calls=None, max_completion=65536,
                 max_response=65536, request_timeout=1200, scheduler=None,
                 max_attempts=rate_control.MAX_ATTEMPTS, local_sandbox=None):
        if max_calls is not None:
            raise ValueError('Actor inference requests are unbounded; max_calls must be None')
        self.key = key
        self.output = Path(output)
        self.max_calls, self.max_completion = max_calls, max_completion
        self.max_response, self.request_timeout = max_response, request_timeout
        self.scheduler = scheduler
        self.max_attempts = max_attempts
        self.local_sandbox = local_sandbox
        self.calls = 0
        self.call_failures = 0
        self.usage = Counter()
        self.failed = False
        self.failure_kind = None
        self.pending = set()
        self.unknown_usage = set()
        self.attempts = []
        self.active_response = None
        self.summary_lock = threading.Lock()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.server = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def __enter__(self):
        broker = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'
            def log_message(self, *_): pass
            def do_GET(self): self.send_error(405)
            def do_POST(self):
                if self.path == '/v1/local-mwm' and broker.local_sandbox is not None:
                    try:
                        length = int(self.headers.get('Content-Length', '0'))
                        if length <= 0: raise ValueError('Missing local tool body')
                        payload = json.loads(self.rfile.read(length))
                        from full_runtime import local_operation
                        reply = local_operation(broker.local_sandbox, payload)
                        data = json.dumps(reply).encode()
                        self.send_response(200)
                        self.send_header('Content-Type','application/json')
                        self.send_header('Content-Length',str(len(data)))
                        self.end_headers(); self.wfile.write(data)
                    except Exception:
                        self.send_error(400, 'Local MWM execution failed')
                    return
                if self.path != '/v1/responses':
                    self.send_error(404)
                    return
                if not broker.lock.acquire(blocking=broker.max_completion is None):
                    self.send_error(409, 'Concurrent requests within one actor prohibited')
                    return
                number, record = None, {}
                heartbeat_stop = threading.Event()
                heartbeat = None
                streaming_headers = False
                begun = time.monotonic()
                try:
                    if broker.stop.is_set():
                        raise ValueError('Task stopped')
                    remaining = None if broker.max_completion is None else broker.max_completion - broker.usage['output_tokens']
                    if remaining is not None and remaining <= 0: raise BudgetExhausted('Task completion budget exhausted')
                    length = int(self.headers.get('Content-Length', '0'))
                    if length <= 0 or (broker.max_completion is not None and length > 16_000_000): raise ValueError('Invalid request size')
                    incoming = json.loads(self.rfile.read(length))
                    outgoing = normalized(incoming, remaining, broker.max_response)
                    if broker.request_timeout is None and outgoing.get('stream'):
                        self.send_response(200)
                        self.send_header('Content-Type','text/event-stream')
                        self.end_headers()
                        streaming_headers = True
                        def keep_alive():
                            while not heartbeat_stop.wait(15):
                                try:
                                    self.wfile.write(b': controller waiting for Fireworks\n\n')
                                    self.wfile.flush()
                                except (OSError,ValueError): return
                        heartbeat=threading.Thread(target=keep_alive,daemon=True)
                        heartbeat.start()
                    broker.calls += 1
                    number = broker.calls
                    broker.pending.add(number)
                    directory = broker.output / f'{number:03}'
                    save(directory / 'request.json', public(outgoing))
                    record = {'number': number, 'upstream': URL, 'model': MODEL,
                              'native_request_settings': {k: incoming.get(k) for k in
                              ('model', 'reasoning', 'temperature', 'max_output_tokens', 'stream')},
                              'changes': {'temperature': 0, 'store': False,
                              'max_output_tokens': outgoing.get('max_output_tokens'),
                              'provider_default_reasoning': broker.max_completion is None}}
                    request_body = json.dumps(outgoing).encode()
                    request_sha = hashlib.sha256(request_body).hexdigest()
                    record['request_sha256'] = request_sha

                    def attempt(wire_timeout, attempt_number):
                        wire_end = None if wire_timeout is None else time.monotonic() + wire_timeout
                        request = urllib.request.Request(URL, data=request_body,
                            headers={'Authorization': 'Bearer ' + broker.key, **HEADERS}, method='POST')
                        attempt_dir = directory / 'attempts' / f'{attempt_number:02}'
                        if broker.stop.is_set():
                            raise rate_control.AttemptFailure('Actor stopped before wire dispatch',
                                details={'wire_dispatched':False, 'usage_known':True})
                        try:
                            with broker.opener.open(request, timeout=wire_timeout) as response:
                                broker.active_response = response
                                with rate_control.wire_deadline(response, wire_end):
                                    final, wire, content_type, sse, generated, errors = read_wire(response, broker.stop, wire_end,
                                        unbounded=broker.max_completion is None)
                                usage = final.get('usage') or {}
                                details = {'http_status':response.status, 'response_model':final.get('model'),
                                    'response_id':final.get('id'), 'response_status':final.get('status'),
                                    'usage':usage, 'usage_known':rate_control.usage_is_reported(usage),
                                    'observed_generation':generated, 'stream_errors':errors}
                                save(attempt_dir / 'response.json', public(final))
                                mismatch = final.get('model') not in (None, MODEL)
                                refusal = not mismatch and rate_control.retryable_refusal(
                                    response.status, final, sse=sse, observed_generation=generated, stream_errors=errors)
                                if refusal:
                                    raise rate_control.AttemptFailure('Explicit rate refusal before generation',
                                        details=details, retryable=True,
                                        retry_after=rate_control.retry_after_seconds(response.headers.get('Retry-After')))
                                if final.get('model') != MODEL:
                                    raise rate_control.AttemptFailure('Unexpected response model', details=details)
                                if response.status != 200 or final.get('status') not in ('completed', 'incomplete'):
                                    raise rate_control.AttemptFailure('Provider response failed', details=details)
                                output_tokens = token_count(usage.get('output_tokens')) if isinstance(usage, dict) else None
                                if output_tokens is None and broker.max_completion is not None:
                                    details.update(usage_known=False, failure_kind='output_usage_missing_or_invalid')
                                    raise rate_control.AttemptFailure(
                                        'Generated response has no reliable output-token count; aggregate budget cannot be verified',
                                        details=details)
                                if outgoing.get('max_output_tokens') is not None and output_tokens is not None and output_tokens > outgoing['max_output_tokens']:
                                    details['failure_kind'] = 'provider_output_budget_exceeded'
                                    raise rate_control.AttemptFailure(
                                        'Provider output-token count exceeded the requested remaining allowance', details=details)
                                return rate_control.Outcome((final, wire, content_type, response.status), details)
                        except urllib.error.HTTPError as exc:
                            try:
                                with rate_control.wire_deadline(exc, wire_end):
                                    raw = exc.read(3_000_001)
                            finally:exc.close()
                            try: body = json.loads(raw) if len(raw) <= 3_000_000 else {'unparsed_oversize_error':True}
                            except ValueError: body = None
                            usage = (body.get('usage') or {}) if isinstance(body, dict) else {}
                            details = {'http_status':exc.code, 'usage':usage,
                                'usage_known':rate_control.usage_is_reported(usage),
                                'provider_error':(json.dumps(public(body)) if body is not None else raw.decode(errors='replace')).replace(broker.key,'[REDACTED]')[:3000]}
                            save(attempt_dir / 'error.json', public(details))
                            mismatch = isinstance(body,dict) and body.get('model') not in (None,MODEL)
                            refusal = len(raw) <= 3_000_000 and not mismatch and rate_control.retryable_refusal(exc.code, body)
                            transport = not refusal and (500 <= exc.code < 600 or edge_block(exc.code, body))
                            if transport: details['failure_kind'] = 'transport_retry'
                            raise rate_control.AttemptFailure('HTTP ' + str(exc.code), details=details,
                                retryable=refusal or transport, kind='transport' if transport else 'rate',
                                retry_after=rate_control.retry_after_seconds(exc.headers.get('Retry-After'))) from exc
                        except TRANSPORT_DROPS as exc:
                            if wire_end is not None and time.monotonic() >= wire_end:
                                raise TimeoutError('Wire wall-clock deadline exhausted') from exc
                            # Nothing generated: the broker re-sends the identical bytes itself.
                            raise rate_control.AttemptFailure(type(exc).__name__ + ': ' + str(exc),
                                details={'wire_dispatched':True, 'usage_known':False, 'failure_kind':'transport_retry',
                                         'observed_generation':getattr(exc,'generated',False)},
                                retryable=not getattr(exc,'generated',False), kind='transport') from exc
                        finally:
                            broker.active_response = None

                    def log_attempt(attempt_record):
                        save(directory / 'attempts' / f'{attempt_record["attempt"]:02}' / 'transport.json', redact(attempt_record,broker.key))

                    try:
                        value, attempt_records = rate_control.run(attempt, timeout=broker.request_timeout,
                            request_sha256=request_sha, scheduler=broker.scheduler,
                            stop=broker.stop, on_attempt=log_attempt, max_attempts=broker.max_attempts)
                    except rate_control.TransportFailure as exc:
                        attempt_records = exc.attempts
                        broker.failure_kind = next((a['failure_kind'] for a in reversed(attempt_records)
                                                    if a.get('failure_kind')), broker.failure_kind)
                        raise
                    finally:
                        attempt_records = locals().get('attempt_records', [])
                        attempt_records = redact(attempt_records,broker.key)
                        record['attempts'] = attempt_records
                        broker.attempts.extend({'call':number, **entry} for entry in attempt_records)
                        for entry in attempt_records:
                            usage = entry.get('usage') or {}
                            for field in ('input_tokens', 'output_tokens', 'total_tokens'):
                                broker.usage[field] += known_tokens(usage, field)
                            details = usage.get('input_tokens_details') if isinstance(usage, dict) else None
                            broker.usage['cached_tokens'] += known_tokens(details, 'cached_tokens')
                        record['usage_complete'] = all(not e.get('usage_unknown',False) for e in attempt_records)
                        record['usage'] = {field:sum(known_tokens(e.get('usage'), field) for e in attempt_records)
                                           for field in ('input_tokens','output_tokens','total_tokens')}
                        if attempt_records: record['http_status'] = attempt_records[-1].get('http_status')
                    final, wire, content_type, status = value
                    save(directory / 'response.json', public(final))
                    record.update(response_model=final.get('model'), response_id=final.get('id'),
                                  response_status=final.get('status'))
                    if broker.stop.is_set(): raise TimeoutError('Actor stopped before response forwarding')
                    heartbeat_stop.set()
                    if heartbeat: heartbeat.join()
                    if not streaming_headers:
                        self.send_response(status)
                        self.send_header('Content-Type', content_type)
                        self.send_header('Content-Length', str(len(wire)))
                        self.end_headers()
                    self.wfile.write(wire)
                    self.wfile.flush()
                except Exception as exc:
                    # Only budget exhaustion latches the actor; any other failure is
                    # this call's alone, counted and recorded, and the next call proceeds.
                    if isinstance(exc, BudgetExhausted):
                        broker.failed = True; broker.failure_kind = 'budget_exhausted'
                    else:
                        broker.call_failures += 1
                        if broker.failure_kind is None: broker.failure_kind = 'transport_rejected'
                    error = str(exc)
                    if isinstance(exc, urllib.error.HTTPError):
                        record['http_status'] = exc.code
                        error += ': ' + exc.read().decode(errors='replace')[:3000]
                    record['error'] = error.replace(broker.key, '[REDACTED]')
                    heartbeat_stop.set()
                    if heartbeat: heartbeat.join()
                    try:
                        if streaming_headers:
                            self.wfile.write(b'event: error\ndata: {"type":"error","message":"Inference relay stopped; see controller audit"}\n\n')
                            self.wfile.flush()
                        else: self.send_error(400, 'Inference relay stopped; see controller audit')
                    except (BrokenPipeError, OSError): pass
                finally:
                    heartbeat_stop.set()
                    record['seconds'] = round(time.monotonic() - begun, 3)
                    if number: save(broker.output / f'{number:03}/transport.json', record)
                    else: save(broker.output / 'rejected.json', record)
                    if number:
                        if not record.get('usage_complete',False): broker.unknown_usage.add(number)
                        broker.pending.discard(number)
                    broker.active_response = None
                    if broker.stop.is_set(): broker.write_summary()
                    broker.lock.release()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f'http://127.0.0.1:{self.server.server_port}/v1'
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        # Never claim an unfinished upstream call cost zero. A late handler may
        # fill in its own record and refresh the summary; actor snapshots retain
        # the explicit lower-bound/unknown marker observed on return.
        obtained = self.lock.acquire(timeout=1)
        if obtained:
            self.lock.release()
        else:
            self.failed = True
            self.failure_kind = self.failure_kind or 'transport_in_flight'
        self.write_summary()

    def write_summary(self):
        with self.summary_lock:
            unknown = sorted(self.pending | self.unknown_usage)
            value = {'calls': self.calls, 'call_failures': self.call_failures, 'usage': dict(self.usage),
            'failed': self.failed or bool(self.pending), 'model': MODEL, 'upstream': URL,
            'failure_kind':self.failure_kind,
            'transport_failed':(self.failed or bool(self.pending)) and self.failure_kind != 'budget_exhausted',
            'in_flight_calls':sorted(self.pending), 'usage_unknown_calls':unknown,
            'usage_complete':not unknown, 'usage_is_lower_bound':bool(unknown),
            'wire_attempts':sum(bool(a.get('dispatched')) for a in self.attempts),
            'rate_refusals':sum(a.get('status')=='rate_refused' for a in self.attempts),
            'scheduler_wait_seconds':round(sum(a.get('wait_seconds',0) for a in self.attempts),6),
            'limits': {'requests': self.max_calls, 'completion_tokens': self.max_completion,
                       'per_response':self.max_response, 'attempts_per_request':self.max_attempts,
                       'request_timeout_seconds':self.request_timeout},
            'private_reasoning': 'Forwarded unchanged in RAM; omitted from public request/response records'}
            temporary = self.output / 'summary.tmp'
            save(temporary, value)
            temporary.replace(self.output / 'summary.json')
