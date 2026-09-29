"""Fireworks transport. Credential stays in memory and is never logged."""
import http.client
import json
import hashlib
import time
import urllib.error
import urllib.request

import rate_control

MODEL = 'accounts/fireworks/models/glm-5p3-flash'
URL = 'https://api.fireworks.ai/inference/v1/chat/completions'
# Connection drops before any body was parsed; re-sent with identical bytes. Never a TimeoutError.
TRANSPORT_DROPS = (http.client.IncompleteRead, ConnectionError, urllib.error.URLError)
HEADERS, edge_block = rate_control.HEADERS, rate_control.edge_block

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Inference redirects are prohibited')

def public_error(value):
    if isinstance(value,list):
        return [public_error(v) for v in value if not isinstance(v,dict) or v.get('type')!='reasoning']
    if isinstance(value,dict):
        return {k:public_error(v) for k,v in value.items() if k not in
                ('reasoning_content','encrypted_content','api_key','Authorization')}
    return value

def redact(value,key):
    if isinstance(value,str):return value.replace(key,'[REDACTED]') if key else value
    if isinstance(value,list):return [redact(v,key) for v in value]
    if isinstance(value,dict):return {k:redact(v,key) for k,v in value.items()}
    return value

def token_count(value):
    return isinstance(value,int) and not isinstance(value,bool) and value >= 0

def complete(key, messages, *, timeout=1200, unbounded=False, **options):
    payload = dict(model=MODEL, messages=messages, temperature=0, top_k=40,
                   presence_penalty=0, frequency_penalty=0, max_tokens=65536, reasoning_effort=4096)
    # options are applied separately so a caller can set a smaller token limit.
    payload.update(options)
    if unbounded:
        for field in ('max_tokens', 'max_completion_tokens', 'reasoning_effort', 'thinking'):
            payload.pop(field, None)
    if payload['model'] != MODEL:
        raise ValueError('Only the exact GLM model is allowed')
    if not unbounded and (not token_count(payload.get('max_tokens')) or not 0 < payload['max_tokens'] <= 65536):
        raise ValueError('max_tokens must be a positive integer within the remaining 65536-token budget')
    request_body = json.dumps(payload).encode()
    request_sha = hashlib.sha256(request_body).hexdigest()
    start = time.monotonic()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def attempt(wire_timeout, number):
        deadline = None if wire_timeout is None else time.monotonic() + wire_timeout
        request = urllib.request.Request(URL, data=request_body,
            headers={'Authorization': 'Bearer ' + key, **HEADERS}, method='POST')
        try:
            with opener.open(request, timeout=wire_timeout) as response:
                with rate_control.wire_deadline(response, deadline):
                    raw = response.read() if unbounded else response.read(32_000_001)
                    if not unbounded and len(raw) > 32_000_000: raise ValueError('Response transport size exceeded')
                    result = json.loads(raw)
                usage = result.get('usage') or {}
                details = {'http_status':response.status, 'usage':usage,
                    'usage_known':rate_control.usage_is_reported(usage) and token_count(usage.get('completion_tokens')),
                    'completion_usage_valid':token_count(usage.get('completion_tokens')),
                    'response_model':result.get('model'), 'response_id':result.get('id')}
                if response.status != 200 or result.get('model') != MODEL or not result.get('choices'):
                    raise rate_control.AttemptFailure('Invalid or unexpected model response', details=details)
                if not unbounded and not details['completion_usage_valid']:
                    raise rate_control.AttemptFailure('Successful generation lacks valid completion-token usage; stop to preserve aggregate budget',details=details)
                if not unbounded and usage['completion_tokens'] > payload['max_tokens']:
                    raise rate_control.AttemptFailure('Provider completion usage exceeded the requested remaining budget',details=details)
                return rate_control.Outcome(result, details)
        except urllib.error.HTTPError as exc:
            try:
                with rate_control.wire_deadline(exc, deadline):
                    raw = exc.read(3_000_001)
            finally:exc.close()
            try: body = json.loads(raw) if len(raw) <= 3_000_000 else {'unparsed_oversize_error':True}
            except ValueError: body = None
            usage = (body.get('usage') or {}) if isinstance(body,dict) else {}
            mismatch = isinstance(body,dict) and body.get('model') not in (None,MODEL)
            details = {'http_status':exc.code, 'usage':usage,
                'usage_known':rate_control.usage_is_reported(usage),
                'provider_error':(json.dumps(public_error(body)) if body is not None else raw.decode(errors='replace')).replace(key,'[REDACTED]')[:3000]}
            refusal = len(raw) <= 3_000_000 and not mismatch and rate_control.retryable_refusal(exc.code,body)
            transport = not refusal and (500 <= exc.code < 600 or edge_block(exc.code, body))
            if transport: details['failure_kind'] = 'transport_retry'
            raise rate_control.AttemptFailure('HTTP ' + str(exc.code), details=details, retryable=refusal or transport,
                kind='transport' if transport else 'rate',
                retry_after=rate_control.retry_after_seconds(exc.headers.get('Retry-After'))) from exc
        except TRANSPORT_DROPS as exc:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('Wire wall-clock deadline exhausted') from exc
            raise rate_control.AttemptFailure(type(exc).__name__ + ': ' + str(exc),
                details={'wire_dispatched':True,'usage_known':False,'failure_kind':'transport_retry'},
                retryable=True, kind='transport') from exc
    try:
        result, attempts = rate_control.run(attempt, timeout=timeout, request_sha256=request_sha,
            **({'max_attempts':None} if unbounded else {}))
        error = None
    except rate_control.TransportFailure as exc:
        result, attempts = None, exc.attempts
        error = str(exc).replace(key,'[REDACTED]')[:2000]
    usage = {field:sum(value if token_count(value) else 0
                      for a in attempts for value in [(a.get('usage') or {}).get(field)])
             for field in ('prompt_tokens','completion_tokens','total_tokens')}
    usage['prompt_tokens_details'] = {'cached_tokens':sum(value if token_count(value) else 0
        for a in attempts for value in [((a.get('usage') or {}).get('prompt_tokens_details') or {}).get('cached_tokens')])}
    meta = {'http_status':attempts[-1].get('http_status') if attempts else None,
            'seconds':round(time.monotonic()-start,3), 'usage':usage,
            'response_model':result.get('model') if result else None,
            'response_id':result.get('id') if result else None, 'attempts':attempts,
            'request_sha256':request_sha,
            'usage_complete':all(not a.get('usage_unknown',False) for a in attempts),
            'usage_is_lower_bound':any(a.get('usage_unknown',False) for a in attempts),
            'wire_attempts':sum(bool(a.get('dispatched')) for a in attempts)}
    meta['completion_usage_valid'] = bool(result) and token_count((result.get('usage') or {}).get('completion_tokens'))
    if error is not None: meta['error'] = error
    return result, redact(meta,key)

def continuing_message(message):
    # Preserve provider reasoning only in the private in-memory conversation.
    return {k:v for k,v in message.items() if k in ('role','content','reasoning_content','tool_calls')}

def public_message(message):
    return {k:v for k,v in message.items() if k in ('role','content','tool_calls')}
