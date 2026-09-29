"""Per-actor Responses relay: one fixed upstream, credentials only in controller RAM.

Without a controller (`controller=None`, the v8 behaviour: byte-identical upstream bodies, forwarded
bytes and records) this is an HTTP transport, not an agent loop: native Codex owns tools/history and
the relay changes only the predeclared model sampling/budget/storage settings.

v9 (DESIGN §2 L2-L4, understand/broker.md §3/§6): with a `contracts.Controller` the relay also
- shows the controller every Codex request (`on_request`) and every upstream response (`on_response`);
- splices broker-owned items into the upstream input: role:'user' notes anchored right after a Codex
  item (the tail of the current input, or the compaction summary), sticky = re-applied on every later
  request until the anchor item disappears (compaction; then dropped and recorded), never between a
  tool call and its output;
- on a final answer asks `on_final`; a 'revise' verdict re-queries upstream with
  input + echo(draft) + feedback as its own numbered call and forwards the revision's own provider
  wire unchanged (the draft's wire if the revision fails in transport or cannot finish
  REVISION_DEADLINE_MARGIN_S before the actor deadline), splicing draft + feedback into later
  requests so the model's history stays what it saw;
- serves POST /v1/submit (contracts.SUBMIT_PATH) for the actor's `submit` CLI (no response lock) only when
  `submit_enabled` (v10, DESIGN §3: campaign passes `submit_enabled=arm.gate`; default False, never keyed on the controller: a controller is
  present, the v9 behaviour); with the submit gate off the route answers 404 like any unknown path, and the
  controller's `summary_extra()` (duck hook; loop notes, hard blocks, observation coverage) is merged into
  api/summary.json.
Wave 3: the upstream `temperature` is a Broker option (default 0, the v8 value) recorded in every
transport.json; so is `top_p` (default None = the body gets no top_p and transport.json no top_p entry, the
v8 behaviour; a value is sent in every upstream body, controller re-queries included); on exit (the episode ended or its actor was stopped) the broker abandons the upstream
call in flight (shuts down its socket, before or after the response headers) and waits for its handler,
so a stalled provider stream cannot write records after the episode (S8 request 4).
Codex only ever receives real provider bytes. An identical Codex retry (same input items) gets the
same upstream input and no new controller decision. Controller exceptions fail open (the v8 behaviour
for that request) and are recorded. Records (controller mode only): api/NNN/controller.json,
api/controller.jsonl, transport.json origin/kind/parent/codex_request/codex_input_sha256/forwarded,
summary.json controller counters.
"""
from collections import Counter
import copy
from dataclasses import dataclass, field
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
from pathlib import Path
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import rate_control

MODEL = 'accounts/fireworks/models/glm-5p3-flash'
URL = 'https://api.fireworks.ai/inference/v1/responses'
SUBMIT_PATH = '/v1/submit'          # == harness_core.contracts.SUBMIT_PATH (asserted in tests)
SUBMIT_MAX_BYTES = 64_000
SUBMIT_FIELD_MAX = 8_000
MAX_REVISIONS_PER_REQUEST = 3       # hard backstop; the controller's own per-episode cap is lower
MAX_NOTE_BYTES = 16_384             # one inserted note / feedback text (== contracts.TEXT_BUDGET)
REVISION_DEADLINE_MARGIN_S = 15     # a re-query must end this long before the actor deadline, so the
                                    # draft (still in RAM) reaches Codex before the actor is killed
EXIT_WAIT_S = 10                    # on exit: how long to wait for the abandoned call's handler to record it


def _upstream_ok(url):
    """https to any host, or plain http only to 127.0.0.1 (tests) without userinfo tricks
    ('http://127.0.0.1:80@host/' has hostname 'host')."""
    try:
        parts = urllib.parse.urlsplit(str(url))
        port = parts.port
    except ValueError:
        return False
    if parts.username is not None or parts.password is not None or not parts.hostname:
        return False
    if parts.scheme == 'https':
        return True
    return parts.scheme == 'http' and parts.hostname == '127.0.0.1' and port is not None

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


def normalized(payload, remaining, max_response=65536, temperature=0, top_p=None):
    """The upstream body: the fixed model, store=False and the declared `temperature` (v8: always 0; wave 3:
    the root's `.task-paths.json` "temperature", passed as the Broker option), output caps per v8 rules.
    `top_p` (wave 3): None leaves the body as v8 did (Codex sends no top_p); a number overrides it."""
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
    result.update(model=MODEL, store=False, temperature=temperature)
    if top_p is not None:
        result['top_p'] = top_p
    if remaining is None and max_response is None:
        # v8: only output caps are dropped. v5-v7 also dropped `reasoning`, so the
        # configured effort never reached Fireworks and every run used the
        # provider default; the native Responses field is now forwarded unchanged.
        for field in ('max_output_tokens', 'max_tokens', 'max_completion_tokens', 'thinking'):
            result.pop(field, None)
    else:
        result['max_output_tokens'] = min(x for x in (max_response, remaining) if x is not None)
    return result


def _visible(item, tools=True):
    """An output item Codex can act on: a tool call it offered tools for, or a message with text."""
    if not isinstance(item, dict): return False
    kind = item.get('type')
    if kind in ('function_call', 'custom_tool_call'): return tools
    return kind == 'message' and any(isinstance(part, dict) and str(part.get('text') or '').strip()
                                     for part in item.get('content') or [])


def _object_arguments(item):
    try: return isinstance(json.loads(item.get('arguments') or ''), dict)
    except (TypeError, ValueError): return False


def provider_defect(final, tools=True):
    """Name a provider answer that must not reach Codex, or return None.

    Seen in v6f/v7 (16,823 responses): a 'completed' response with zero usage
    and nothing to act on ends the Codex turn, and the harness then grades a
    leftover progress note (14 cases); a cut inside a function call (zero-usage
    'completed', or an 'incomplete' output-cap runaway) leaves arguments that
    are not a JSON object, and Fireworks then rejects every later request; a
    'completed' response with only reasoning leaves Codex nothing to do (5, all
    compaction requests, which offer no tools, so a tool call there is not
    usable either); and an 'incomplete' max_output_tokens runaway with no
    visible output (86) is retried by Codex but its ~65K reasoning tokens stay
    in the history. Each is re-sent by the broker instead, with identical
    bytes, so Codex only ever sees a usable response. `tools` says whether the
    request offered any."""
    status = final.get('status')
    output = [item for item in final.get('output') or [] if isinstance(item, dict)]
    usage = final.get('usage') if isinstance(final.get('usage'), dict) else {}
    if status not in ('completed', 'incomplete'):
        return None
    if any(item.get('type') == 'function_call' and not _object_arguments(item) for item in output):
        return 'invalid_tool_arguments'
    if status == 'incomplete':
        return None if any(_visible(item, tools) for item in output) else 'incomplete_without_output'
    if not rate_control.nonzero_usage({k: usage.get(k) for k in ('input_tokens', 'output_tokens')}):
        return 'completed_without_usage'
    if not any(_visible(item, tools) for item in output):
        return 'completed_without_output'
    return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Inference redirects are prohibited')


class _TrackedHTTP(urllib.request.HTTPHandler):
    """urllib's HTTPHandler, except that the broker sees the connection while it is open (Broker.abandon)."""
    def __init__(self, broker):
        super().__init__()
        self._broker = broker

    def http_open(self, req):
        return self.do_open(self._broker._connection(http.client.HTTPConnection), req)


class _TrackedHTTPS(urllib.request.HTTPSHandler):
    """urllib's HTTPSHandler (same default context), except that the broker sees the connection."""
    def __init__(self, broker):
        super().__init__()
        self._broker = broker

    def https_open(self, req):
        return self.do_open(self._broker._connection(http.client.HTTPSConnection), req, context=self._context)


def _shutdown(sock):
    """Wake a thread blocked reading `sock` (the read returns EOF or raises); never raises itself."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
        return True
    except (AttributeError, OSError, ValueError):
        return False


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


@dataclass
class Splice:
    """Broker-owned items placed right after the Codex item `anchor` (controller_runtime.anchor_key),
    re-applied on every later request (sticky) until that item disappears, then dropped and recorded."""
    id: int
    anchor: str
    items: list
    reason: str
    sticky: bool
    created_request: int
    created_call: int | None = None


@dataclass
class Exchange:
    """State of one Codex /v1/responses request in controller mode."""
    codex_request: int
    kind: str = 'turn'
    tools_offered: bool = False
    codex_input: list = field(default_factory=list)
    view: object = None
    codex_body_sha256: str = ''
    codex_input_sha256: str = ''
    codex_public_input_sha256: str = ''
    replayed_retry: bool = False
    placed: list = field(default_factory=list)          # [(index in sent input, Splice, item)]
    new_splices: set = field(default_factory=set)
    dropped: list = field(default_factory=list)
    errors: list = field(default_factory=list)


class _SubmitError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class Broker:
    def __init__(self, key, output, *, max_calls=None, max_completion=65536,
                 max_response=65536, request_timeout=1200, scheduler=None,
                 max_attempts=rate_control.MAX_ATTEMPTS, local_sandbox=None, retry_defects=True,
                 controller=None, actor_deadline=None, upstream_url=None, clock=time.monotonic, temperature=0,
                 top_p=None, submit_enabled=None):
        """v9 keywords: `controller` (harness_core.contracts.Controller; None = the v8 relay),
        `actor_deadline` (the actor's kill time on `clock`, time.monotonic by default; feeds
        View.remaining_s), `upstream_url` (Responses endpoint; None = the module URL, Fireworks; only
        https or a 127.0.0.1 test endpoint), `clock` (injectable for deadline tests), `temperature`
        (wave 3: sent in every upstream body; default 0 = v8), `top_p` (wave 3: None = not sent, as v8;
        a number in (0, 1] is sent in every upstream body). v10: `submit_enabled` (the arm's gate) serves
        /v1/submit; it defaults to False (review fix: never keyed on the controller - the campaign is the only
        caller that turns it on, with arm.gate) and cannot be true without a controller."""
        if max_calls is not None:
            raise ValueError('Actor inference requests are unbounded; max_calls must be None')
        if submit_enabled is None:
            submit_enabled = False
        if not isinstance(submit_enabled, bool):
            raise ValueError('submit_enabled must be true or false (the arm gate)')
        if submit_enabled and controller is None:
            raise ValueError('/v1/submit needs a controller (submit_enabled=True without one)')
        self.submit_enabled = submit_enabled
        if upstream_url is not None and not _upstream_ok(upstream_url):
            raise ValueError('Upstream must be https or a local http://127.0.0.1:<port> test endpoint')
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2:
            raise ValueError('temperature must be a number in [0, 2]')
        self.temperature = temperature
        if top_p is not None and (isinstance(top_p, bool) or not isinstance(top_p, (int, float)) or not 0 < top_p <= 1):
            raise ValueError('top_p must be None (not sent) or a number in (0, 1]')
        self.top_p = top_p
        self.key = key
        self.output = Path(output)
        self.max_calls, self.max_completion = max_calls, max_completion
        self.max_response, self.request_timeout = max_response, request_timeout
        self.scheduler = scheduler
        self.max_attempts = max_attempts
        self.local_sandbox = local_sandbox
        self.retry_defects = retry_defects
        self.calls = 0
        self.call_failures = 0
        self.usage = Counter()
        self.failed = False
        self.failure_kind = None
        self.pending = set()
        self.unknown_usage = set()
        self.attempts = []
        self.active_response = None
        self.active_connection = None
        self.abandoned_calls = []
        self.summary_lock = threading.Lock()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.server = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                  _TrackedHTTP(self), _TrackedHTTPS(self))
        # v9 controller state (inert when controller is None)
        self.controller = controller
        self.actor_deadline = actor_deadline
        self.upstream_url = upstream_url
        self.clock = clock
        self.started = clock()
        self.controller_lock = threading.Lock()      # serialises controller hooks (responses vs submit)
        self.log_lock = threading.Lock()
        self.splices = []
        self.splice_ids = 0
        self.seen_inputs = {}
        self.codex_requests = 0
        self.stats = Counter()
        self.inserts_by_reason = Counter()
        self.controller_usage = Counter()

    @property
    def url(self):
        return self.upstream_url or URL

    @property
    def submit_url(self):
        """What the actor's `submit` CLI POSTs to (contracts.SUBMIT_ENV value for controller arms)."""
        return self.endpoint.rsplit('/v1', 1)[0] + SUBMIT_PATH

    # ------------------------------------------------------------------ one upstream call (v8 path)
    def _connection(self, cls):
        """Connection factory for the tracked handlers: the open upstream connection stays visible to
        `abandon` until its response has been read (a stall before the response headers)."""
        def factory(host, **kwargs):
            connection = cls(host, **kwargs)
            self.active_connection = connection
            return connection
        return factory

    def abandon(self):
        """Stop and abandon the upstream call in flight (S8 request 4): with request_timeout=None a stalled
        provider would otherwise keep its handler blocked until the provider closes the stream, and that
        call's records would be written after the episode (possibly after a seal or an audit). Sets stop
        (so nothing is re-sent) and shuts down the socket of the open connection (before the headers) and
        of the response being read (a stalled stream). The handler then records the call as unfinished
        (usage unknown, never priced as zero). Returns the call numbers that were in flight."""
        self.stop.set()
        in_flight = sorted(self.pending.copy())
        woke = False
        connection, response = self.active_connection, self.active_response
        if connection is not None:
            woke = _shutdown(getattr(connection, 'sock', None)) or woke
        if response is not None:
            if _shutdown(getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)):
                woke = True             # the reader sees EOF and closes the response itself
            else:
                try:                    # fallback (no socket reachable): close it under the reader
                    response.close()
                    woke = True
                except Exception:
                    pass
        if in_flight and woke:
            self.abandoned_calls = sorted(set(self.abandoned_calls) | set(in_flight))
        return in_flight

    def _allocate(self):
        self.calls += 1
        number = self.calls
        self.pending.add(number)
        return number

    def _call(self, number, outgoing, incoming, record, timeout=None):
        """Send `outgoing` as api/NNN exactly as v8 did: save request.json, fill `record` in place (so a
        failure still leaves it for transport.json), run identical-body attempts through rate_control
        with defect re-sends, sum usage from every attempt, save response.json. Returns
        (final, wire, content_type, http_status); raises like v8. `timeout` (controller re-queries only)
        replaces request_timeout as the logical call's total deadline in seconds."""
        broker = self
        timeout = broker.request_timeout if timeout is None else timeout
        directory = broker.output / f'{number:03}'
        save(directory / 'request.json', public(outgoing))
        record.update({'number': number, 'upstream': broker.url, 'model': MODEL,
                  'native_request_settings': {k: incoming.get(k) for k in
                  ('model', 'reasoning', 'temperature', 'max_output_tokens', 'stream')},
                  'changes': {'temperature': broker.temperature,
                  **({'top_p': broker.top_p} if broker.top_p is not None else {}), 'store': False,
                  'max_output_tokens': outgoing.get('max_output_tokens'),
                  'reasoning_forwarded': outgoing.get('reasoning'),
                  'provider_default_reasoning': outgoing.get('reasoning') is None,
                  'provider_defects_resent': broker.retry_defects}})
        request_body = json.dumps(outgoing).encode()
        request_sha = hashlib.sha256(request_body).hexdigest()
        record['request_sha256'] = request_sha

        def attempt(wire_timeout, attempt_number):
            wire_end = None if wire_timeout is None else time.monotonic() + wire_timeout
            request = urllib.request.Request(broker.url, data=request_body,
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
                    defect = provider_defect(final, tools=bool(outgoing.get('tools'))) if broker.retry_defects else None
                    if defect:
                        # Nothing was forwarded yet: the whole wire is buffered. A
                        # zero-usage answer still generated, so its cost is unknown.
                        details.update(failure_kind='provider_defect_retry', provider_defect=defect)
                        if not rate_control.nonzero_usage({k: usage.get(k) for k in ('input_tokens', 'output_tokens')}):
                            details['usage_known'] = False
                        raise rate_control.AttemptFailure('Provider defect re-sent: ' + defect,
                            details=details, retryable=True, kind='transport')
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
                broker.active_connection = None

        def log_attempt(attempt_record):
            save(directory / 'attempts' / f'{attempt_record["attempt"]:02}' / 'transport.json', redact(attempt_record,broker.key))

        try:
            value, attempt_records = rate_control.run(attempt, timeout=timeout,
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
        return final, wire, content_type, status

    # ------------------------------------------------------------------ controller machinery
    def _count(self, name, n=1):
        with self.log_lock:
            self.stats[name] += n

    def _log(self, **row):
        """Append one decision row to api/controller.jsonl (redacted)."""
        row = {'t': round(self.clock() - self.started, 3), **row}
        line = json.dumps(redact(row, self.key), ensure_ascii=False, default=str) + '\n'
        with self.log_lock:
            self.output.mkdir(parents=True, exist_ok=True)
            with (self.output / 'controller.jsonl').open('a') as handle:
                handle.write(line)

    def _error(self, exchange, where, exc):
        self._count('controller_errors')
        text = f'{where}: {type(exc).__name__}: {exc}'
        text = (text.replace(self.key, '[REDACTED]') if self.key else text)[:2000]
        if exchange is not None:
            exchange.errors.append(text)
        self._log(codex_request=getattr(exchange, 'codex_request', None), event='controller_error', error=text)

    def _hook(self, name, exchange, *args):
        """One controller hook under the controller lock; an exception fails open (returns None)."""
        try:
            with self.controller_lock:
                result = getattr(self.controller, name)(*args)
                drain = getattr(self.controller, 'drain_log', None)
                rows = drain() if drain is not None else []
            for row in rows or []:
                self._log(codex_request=getattr(exchange, 'codex_request', None), call=self.calls,
                          event='decision', hook=name, detail=row)
            return result
        except Exception as exc:
            self._error(exchange, name, exc)
            return None

    def _bind(self, exchange, number):
        """Adapter (see S3-broker-requests.md): tell the controller the api/NNN number of the upstream
        call about to be sent, since contracts.View carries only the Codex request ordinal."""
        bind = getattr(self.controller, 'bind_call', None)
        if bind is None:
            return
        try:
            with self.controller_lock:
                bind(number)
        except Exception as exc:
            self._error(exchange, 'bind_call', exc)

    def _view(self, exchange, call):
        """contracts.View at this moment. codex_input is a deep copy made once per Codex request, so no
        controller mutation can reach the upstream body. `call` (contracts v9.1 View.call) is the api/NNN
        number the view concerns: the call about to be sent (on_request), or the call whose response is
        shown (on_response, on_final)."""
        from harness_core import contracts
        now = self.clock()
        remaining = None if self.actor_deadline is None else round(self.actor_deadline - now, 3)
        items = exchange.view.codex_input if exchange.view is not None else copy.deepcopy(exchange.codex_input)
        return contracts.View(codex_request=exchange.codex_request, kind=exchange.kind, codex_input=items,
                              tools_offered=exchange.tools_offered,
                              elapsed_s=round(now - self.started, 3), remaining_s=remaining, call=int(call or 0))

    def _materialize(self, codex_input, exchange):
        """Return (sent_input, placed): every live splice right after its anchor item, in creation
        order, deferred past any tool call whose output has not appeared yet; splices whose anchor is
        gone are dropped (recorded in exchange.dropped); one-shot splices are used once."""
        from controller_runtime import anchor_key, CALL_TYPES, OUTPUT_TYPES
        keys = [anchor_key(item) for item in codex_input]
        first = {}
        for index, key in enumerate(keys):
            first.setdefault(key, index)
        live = []
        for splice in self.splices:
            if splice.anchor in first:
                live.append(splice)
            else:
                exchange.dropped.append({'splice': splice.id, 'reason': splice.reason, 'anchor_id': splice.anchor,
                                         'created_request': splice.created_request, 'items': len(splice.items)})
                self._count('dropped_splices')
        self.splices = [s for s in live if s.sticky]
        by_anchor = {}
        for splice in live:
            by_anchor.setdefault(first[splice.anchor], []).append(splice)
        sent, placed, deferred, open_calls = [], [], [], set()
        for index, item in enumerate(codex_input):
            kind = item.get('type') if isinstance(item, dict) else None
            if kind in CALL_TYPES:
                open_calls.add(item.get('call_id'))
            elif kind in OUTPUT_TYPES:
                open_calls.discard(item.get('call_id'))
            sent.append(item)
            deferred.extend(by_anchor.get(index, []))
            if deferred and not open_calls:
                for splice in deferred:
                    for part in splice.items:
                        placed.append((len(sent), splice, part))
                        sent.append(part)
                deferred = []
        for splice in deferred:                  # outputs never arrived: place at the end
            for part in splice.items:
                placed.append((len(sent), splice, part))
                sent.append(part)
        return sent, placed

    def _register(self, exchange, insert, codex_input, number):
        from controller_runtime import anchor_key, note_item, summary_index
        if insert.reason not in ('loop_guard', 'ledger', 'enforce') or not str(insert.text or '').strip():
            raise ValueError(f'invalid insert reason/text: {insert.reason!r}')
        if len(str(insert.text).encode('utf-8')) > MAX_NOTE_BYTES:
            raise ValueError(f'insert text over {MAX_NOTE_BYTES} bytes ({insert.reason})')
        index = len(codex_input) - 1
        if insert.anchor == 'summary':
            found = summary_index(codex_input)
            if found is None:
                exchange.errors.append('summary anchor not found; placed at tail')
            else:
                index = found
        self.splice_ids += 1
        text = str(insert.text).replace(self.key, '[REDACTED]') if self.key else str(insert.text)
        splice = Splice(self.splice_ids, anchor_key(codex_input[index]), [note_item(insert.reason, text)],
                        insert.reason, bool(insert.sticky), exchange.codex_request, number)
        self.splices.append(splice)
        with self.log_lock:
            self.inserts_by_reason[insert.reason] += 1
        return splice

    def _prepare(self, raw, incoming, outgoing, number):
        """Controller mode, before the first upstream call of a Codex request: classify it, show it to
        the controller (not for an identical Codex retry, which only gets its splices re-applied),
        register the inserts and materialize every live splice into outgoing['input']. Never raises: on
        any internal error the request goes upstream as Codex sent it (v8)."""
        from controller_runtime import canonical_sha, classify
        self.codex_requests += 1
        exchange = Exchange(self.codex_requests, tools_offered=bool(incoming.get('tools')))
        try:
            codex_input = incoming.get('input')
            if not isinstance(codex_input, list) or not codex_input:
                exchange.errors.append('Codex input is not a non-empty list; relayed unchanged')
                return exchange
            exchange.codex_input = codex_input
            exchange.kind = classify(codex_input, exchange.tools_offered)
            exchange.codex_body_sha256 = hashlib.sha256(raw).hexdigest()
            exchange.codex_input_sha256 = canonical_sha(codex_input)
            exchange.codex_public_input_sha256 = canonical_sha(public(codex_input))
            self._count(exchange.kind + 's')
            exchange.view = self._view(exchange, number)
            if exchange.codex_input_sha256 in self.seen_inputs:
                exchange.replayed_retry = True
                self._count('retries_replayed')
            else:
                self.seen_inputs[exchange.codex_input_sha256] = exchange.codex_request
                self._bind(exchange, number)
                for insert in self._hook('on_request', exchange, exchange.view) or []:
                    try:
                        exchange.new_splices.add(self._register(exchange, insert, codex_input, number).id)
                    except Exception as exc:
                        self._error(exchange, 'insert', exc)
            sent, placed = self._materialize(codex_input, exchange)
            outgoing['input'], exchange.placed = sent, placed
        except Exception as exc:
            self._error(exchange, 'prepare', exc)
            outgoing['input'], exchange.placed = incoming.get('input'), []
        self._log(codex_request=exchange.codex_request, call=number, event='request', kind=exchange.kind,
                  items=len(exchange.codex_input), codex_input_sha256=exchange.codex_input_sha256,
                  replayed_retry=exchange.replayed_retry, splices=sorted({p[1].id for p in exchange.placed}),
                  new=sorted(exchange.new_splices), dropped=[d['splice'] for d in exchange.dropped])
        return exchange

    def _after(self, exchange, entries, incoming, outgoing, result):
        """Controller mode, after the first upstream call: feed the response to the controller, then
        the enforcement loop: on_final -> 'revise' -> re-query with input + echo(draft) + feedback as
        its own call (origin 'controller', parent = the draft's call), forward the revision's own wire;
        the draft is forwarded if the revision fails in transport. Returns the result to forward."""
        from controller_runtime import anchor_key, echo, is_final_answer, note_item
        self._hook('on_response', exchange, exchange.view, result[0], entries[-1]['number'])
        rounds = 0
        while (exchange.kind != 'compaction' and exchange.codex_input and rounds < MAX_REVISIONS_PER_REQUEST
               and is_final_answer(result[0], exchange.tools_offered) and not self.stop.is_set()):
            verdict = self._hook('on_final', exchange, self._view(exchange, entries[-1]['number']), result[0], rounds)
            entries[-1]['verdict'] = verdict
            self._log(codex_request=exchange.codex_request, call=entries[-1]['number'], event='verdict',
                      round=rounds, action=getattr(verdict, 'action', None))
            if verdict is None or verdict.action != 'revise' or not str(verdict.feedback or '').strip():
                break
            try:
                limit = None                      # the revision's total deadline (seconds)
                if self.actor_deadline is not None:
                    left = self.actor_deadline - self.clock() - REVISION_DEADLINE_MARGIN_S
                    if left <= 0:
                        exchange.errors.append('enforcement skipped: too close to the actor deadline')
                        self._log(codex_request=exchange.codex_request, call=entries[-1]['number'],
                                  event='revision_skipped', reason='deadline')
                        break
                    limit = left if self.request_timeout is None else min(self.request_timeout, left)
                revised = dict(outgoing)
                if self.max_completion is not None:
                    remaining = self.max_completion - self.usage['output_tokens']
                    if remaining <= 0:
                        exchange.errors.append('enforcement skipped: completion budget exhausted')
                        break
                    revised['max_output_tokens'] = min(x for x in (self.max_response, remaining) if x is not None)
                if len(str(verdict.feedback).encode('utf-8')) > MAX_NOTE_BYTES:
                    raise ValueError(f'verdict feedback over {MAX_NOTE_BYTES} bytes')
                feedback = str(verdict.feedback).replace(self.key, '[REDACTED]') if self.key else str(verdict.feedback)
                extra = echo(result[0].get('output') or []) + [note_item('enforce', feedback)]
                revised['input'] = list(outgoing['input']) + extra
                self.splice_ids += 1
                splice = Splice(self.splice_ids, anchor_key(exchange.codex_input[-1]), extra, 'enforce', True,
                                exchange.codex_request)
            except Exception as exc:
                self._error(exchange, 'requery', exc)
                break
            parent = entries[-1]['number']
            number = self._allocate()
            splice.created_call = number
            entry = {'number': number, 'record': {}, 'begun': time.monotonic(), 'ended': None,
                     'origin': 'controller', 'parent': parent, 'round': rounds + 1, 'forwarded': False,
                     'sent': revised['input'], 'verdict': None,
                     'placed': list(entries[-1]['placed']) + [(len(outgoing['input']) + i, splice, item)
                                                              for i, item in enumerate(extra)],
                     'new': set(entries[-1]['new']) | {splice.id}}
            entries[-1]['ended'] = entry['begun']
            entries.append(entry)
            self._count('enforce_requeries')
            self._bind(exchange, number)
            try:
                second = self._call(number, revised, incoming, entry['record'], timeout=limit)
            except Exception as exc:
                # The revision failed in transport: forward the draft (still in RAM) instead.
                entry['ended'] = time.monotonic()
                entry['record']['error'] = str(exc).replace(self.key, '[REDACTED]') if self.key else str(exc)
                entry['fallback'] = 'draft_forwarded'
                self.call_failures += 1
                if self.failure_kind is None: self.failure_kind = 'transport_rejected'
                entries.append(entries.pop(-2))          # the draft is the forwarded (last) entry again
                self._count('enforce_fallbacks')
                self._log(codex_request=exchange.codex_request, call=number, event='revision_failed',
                          parent=parent, error=entry['record']['error'][:500])
                break
            self.splices.append(splice)
            with self.log_lock:
                self.inserts_by_reason['enforce'] += 1
            self._count('discarded_responses')
            self._log(codex_request=exchange.codex_request, call=number, event='revision', parent=parent,
                      round=rounds + 1, splice=splice.id)
            self._hook('on_response', exchange, self._view(exchange, number), second[0], number)
            result, outgoing = second, revised
            rounds += 1
        if rounds >= MAX_REVISIONS_PER_REQUEST and is_final_answer(result[0], exchange.tools_offered):
            self._log(codex_request=exchange.codex_request, call=entries[-1]['number'], event='rounds_exhausted')
        return result

    def _controller_record(self, exchange, entry):
        """api/NNN/controller.json. `inserted` lists every non-Codex item of this call's upstream input:
        `index` in the full input, `public_index` in request.json (None for a reasoning item, which
        public() drops); the item itself where this call created it, its hash always. Deleting them
        reproduces Codex's input (hash codex_input_sha256 / codex_public_input_sha256)."""
        from controller_runtime import canonical_sha
        sent, position, skipped = entry['sent'], {}, 0
        for index, item in enumerate(sent):
            if isinstance(item, dict) and item.get('type') == 'reasoning':
                skipped += 1
                position[index] = None
            else:
                position[index] = index - skipped
        def shown(item):
            # A reasoning item (an echoed draft's) is private like everywhere else in the public records
            # (public() drops it from request/response.json): only its type, id and key set are kept.
            if isinstance(item, dict) and item.get('type') == 'reasoning':
                return {'type': 'reasoning', 'id': item.get('id'), 'keys': sorted(item), 'private': True}
            return public(item)
        inserted = [{'index': index, 'public_index': position.get(index), 'reason': splice.reason,
                     'sticky': splice.sticky, 'splice': splice.id, 'anchor_id': splice.anchor,
                     'reapplied': splice.id not in entry['new'], 'item_sha256': canonical_sha(item),
                     # the full item only where it is new; re-applied copies are identical by hash
                     'item': shown(item) if splice.id in entry['new'] else None}
                    for index, splice, item in entry['placed']]
        verdict = entry.get('verdict')
        first = entry['origin'] == 'codex'
        return redact({'number': entry['number'], 'codex_request': exchange.codex_request,
                       'origin': entry['origin'], 'parent': entry['parent'], 'round': entry['round'],
                       'kind': exchange.kind, 'codex_body_sha256': exchange.codex_body_sha256,
                       'codex_input_sha256': exchange.codex_input_sha256,
                       'codex_public_input_sha256': exchange.codex_public_input_sha256,
                       'codex_input_items': len(exchange.codex_input), 'sent_input_items': len(sent),
                       'sent_input_sha256': canonical_sha(sent), 'replayed_retry': exchange.replayed_retry,
                       'inserted': inserted, 'dropped_splices': exchange.dropped if first else [],
                       'verdict': None if verdict is None else {'action': verdict.action, 'feedback': verdict.feedback},
                       'forwarded': entry['forwarded'], 'fallback': entry.get('fallback'),
                       'errors': list(exchange.errors) if first else []}, self.key)

    def _finish(self, exchange, entries):
        """Write transport.json (+ v9 fields) and controller.json for every upstream call of the request."""
        now = time.monotonic()
        for entry in entries:
            record, number = entry['record'], entry['number']
            record.update(origin=entry['origin'], kind=exchange.kind, parent=entry['parent'],
                          codex_request=exchange.codex_request, codex_input_sha256=exchange.codex_input_sha256,
                          forwarded=entry['forwarded'])
            record['seconds'] = round((entry['ended'] or now) - entry['begun'], 3)
            save(self.output / f'{number:03}/transport.json', record)
            try:
                save(self.output / f'{number:03}/controller.json', self._controller_record(exchange, entry))
            except Exception as exc:
                self._error(exchange, 'controller_record', exc)
            if not record.get('usage_complete', False): self.unknown_usage.add(number)
            self.pending.discard(number)
            if entry['origin'] == 'controller':
                self._count('controller_calls')
                with self.log_lock:
                    for key, value in (record.get('usage') or {}).items():
                        self.controller_usage[key] += value
        self._log(codex_request=exchange.codex_request, event='forward',
                  forwarded=next((e['number'] for e in entries if e['forwarded']), None),
                  calls=[e['number'] for e in entries])

    # ------------------------------------------------------------------ submit route
    def _submit(self, handler):
        """POST /v1/submit: validate {answer, alt, reading, keep, artifact_sha256} (strings, size-capped),
        run controller.on_submit, reply with the GateResult as JSON (never records, bindings or keys)."""
        from controller_runtime import gate_result_json
        from harness_core import contracts
        status, reply, payload = 200, None, None
        try:
            if self.stop.is_set():
                raise _SubmitError(503, 'Task stopped')
            length = int(handler.headers.get('Content-Length', '0'))
            if length <= 0:
                raise _SubmitError(400, 'Missing submit body')
            if length > SUBMIT_MAX_BYTES:
                if length <= 1_000_000:
                    handler.rfile.read(length)       # drain so the client sees the 413, not a reset
                raise _SubmitError(413, 'Submit body too large')
            try:
                payload = json.loads(handler.rfile.read(length))
            except ValueError:
                raise _SubmitError(400, 'Submit body is not JSON') from None
            if not isinstance(payload, dict):
                raise _SubmitError(400, 'Submit body must be a JSON object')
            unknown = set(payload) - set(contracts.SUBMIT_FIELDS)
            if unknown:
                raise _SubmitError(400, 'Unknown submit fields: ' + ', '.join(sorted(map(str, unknown)))[:200])
            for name, value in payload.items():
                if value is not None and not isinstance(value, str):
                    raise _SubmitError(400, f'Submit field {name} must be a string')
                if value is not None and len(value) > SUBMIT_FIELD_MAX:
                    raise _SubmitError(413, f'Submit field {name} too long')
            payload = {k: v for k, v in payload.items() if v is not None}
            if not str(payload.get('answer') or payload.get('artifact_sha256') or '').strip():
                raise _SubmitError(400, 'Submit needs --answer (or an artifact)')
            result = self._hook('on_submit', None, payload)
            if result is None:
                raise _SubmitError(500, 'Submit check failed; see controller audit')
            reply = gate_result_json(result)
            self._count('submits')
            if result.status == 'findings' and result.findings:
                self._count('gate_rounds')
            elif result.status in ('findings', 'rejected'):        # contracts v9.1: an unusable payload is
                self._count('submit_invalid')                      # 'rejected' (wave 1: 'findings', no finding)
        except _SubmitError as exc:
            status, reply = exc.code, {'status': 'error', 'message': exc.message, 'findings': []}
            self._count('submit_rejected')
        except Exception:
            status, reply = 500, {'status': 'error', 'message': 'Submit check failed; see controller audit',
                                  'findings': []}
            self._count('submit_rejected')
        reply = redact(reply, self.key)
        self._log(event='submit', http_status=status, status=reply.get('status'),
                  rules=[f.get('rule') for f in reply.get('findings') or []],
                  payload=payload if status == 200 else None)
        data = json.dumps(reply, ensure_ascii=False).encode()
        try:
            handler.send_response(status)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
        except (BrokenPipeError, OSError):
            pass

    # ------------------------------------------------------------------ server
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
                if self.path == SUBMIT_PATH and broker.controller is not None and broker.submit_enabled:
                    broker._submit(self)          # no response lock: runs while Codex executes tools
                    return
                if self.path != '/v1/responses':
                    self.send_error(404)
                    return
                if not broker.lock.acquire(blocking=broker.max_completion is None):
                    self.send_error(409, 'Concurrent requests within one actor prohibited')
                    return
                number, record = None, {}
                exchange, entries = None, []      # controller mode: every upstream call of this request
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
                    raw = self.rfile.read(length)
                    incoming = json.loads(raw)
                    outgoing = normalized(incoming, remaining, broker.max_response, broker.temperature, broker.top_p)
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
                    number = broker._allocate()
                    if broker.controller is not None:
                        entry = {'number': number, 'record': record, 'begun': begun, 'ended': None,
                                 'origin': 'codex', 'parent': None, 'round': 0, 'forwarded': False,
                                 'sent': [], 'placed': [], 'new': set(), 'verdict': None}
                        entries.append(entry)
                        exchange = broker._prepare(raw, incoming, outgoing, number)
                        entry.update(sent=outgoing['input'] if isinstance(outgoing.get('input'), list) else [],
                                     placed=exchange.placed, new=exchange.new_splices)
                    value = broker._call(number, outgoing, incoming, record)
                    if exchange is not None:
                        value = broker._after(exchange, entries, incoming, outgoing, value)
                    final, wire, content_type, status = value
                    if broker.stop.is_set(): raise TimeoutError('Actor stopped before response forwarding')
                    heartbeat_stop.set()
                    if heartbeat: heartbeat.join()
                    if not streaming_headers:
                        self.send_response(status)
                        self.send_header('Content-Type', content_type)
                        self.send_header('Content-Length', str(len(wire)))
                        self.end_headers()
                    if entries: entries[-1]['forwarded'] = True
                    self.wfile.write(wire)
                    self.wfile.flush()
                except Exception as exc:
                    # Only budget exhaustion latches the actor; any other failure is
                    # this call's alone, counted and recorded, and the next call proceeds.
                    if entries:
                        entries[-1]['forwarded'] = False
                        record = entries[-1]['record']
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
                    if exchange is None:
                        record['seconds'] = round(time.monotonic() - begun, 3)
                        if number: save(broker.output / f'{number:03}/transport.json', record)
                        else: save(broker.output / 'rejected.json', record)
                        if number:
                            if not record.get('usage_complete',False): broker.unknown_usage.add(number)
                            broker.pending.discard(number)
                    else:
                        broker._finish(exchange, entries)
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
        # Wave 3 (S8 request 4): wake a handler blocked on a stalled provider, so it records its call now
        # (usage unknown) instead of whenever the provider closes the stream.
        self.abandon()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        # Never claim an unfinished upstream call cost zero. A handler that is still busy after
        # EXIT_WAIT_S (not blocked on the provider any more) may fill in its own record later; actor
        # snapshots retain the explicit lower-bound/unknown marker observed on return.
        obtained = self.lock.acquire(timeout=EXIT_WAIT_S)
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
            'failed': self.failed or bool(self.pending), 'model': MODEL, 'upstream': self.url,
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
            if self.abandoned_calls:
                # upstream calls cut when the broker exited (stalled provider; recorded, never re-sent)
                value['abandoned_calls'] = list(self.abandoned_calls)
            if self.controller is not None:
                with self.log_lock:
                    stats = Counter(self.stats)
                    value.update({
                        'codex_requests': self.codex_requests, 'controller_calls': stats['controller_calls'],
                        'discarded_responses': stats['discarded_responses'],
                        'inserts_by_reason': dict(self.inserts_by_reason), 'gate_rounds': stats['gate_rounds'],
                        'enforce_requeries': stats['enforce_requeries'], 'enforce_fallbacks': stats['enforce_fallbacks'],
                        'submits': stats['submits'], 'submit_rejected': stats['submit_rejected'],
                        'submit_invalid': stats['submit_invalid'],
                        'controller_errors': stats['controller_errors'], 'compactions': stats['compactions'],
                        'resumes': stats['resumes'], 'dropped_splices': stats['dropped_splices'],
                        'retries_replayed': stats['retries_replayed'],
                        'controller_usage': dict(self.controller_usage), 'submit_enabled': self.submit_enabled})
                extra = getattr(self.controller, 'summary_extra', None)
                if callable(extra):
                    try:
                        with self.controller_lock:
                            value.update(redact(dict(extra()), self.key))
                    except Exception as exc:            # a counter must never lose the summary
                        value['summary_extra_error'] = f'{type(exc).__name__}: {exc}'[:300].replace(self.key, '[REDACTED]')
            temporary = self.output / 'summary.tmp'
            save(temporary, value)
            temporary.replace(self.output / 'summary.json')
