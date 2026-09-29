"""Fireworks transport. Credential stays in memory and is never logged."""
import json
import hashlib
import time
import urllib.error
import urllib.request

import rate_control

MODEL = 'accounts/fireworks/models/glm-5p3-flash'
URL = 'https://api.fireworks.ai/inference/v1/chat/completions'

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

def complete(key, messages, *, timeout=1200, **options):
    payload = dict(model=MODEL, messages=messages, temperature=0, top_k=40,
                   presence_penalty=0, frequency_penalty=0, max_tokens=32768, reasoning_effort=4096)
    # options are applied separately so a caller can set a smaller token limit.
    payload.update(options)
    if payload['model'] != MODEL:
        raise ValueError('Only the exact GLM model is allowed')
    request_body = json.dumps(payload).encode()
    request_sha = hashlib.sha256(request_body).hexdigest()
    start = time.monotonic()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def attempt(wire_timeout, number):
        deadline = time.monotonic() + wire_timeout
        request = urllib.request.Request(URL, data=request_body,
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
        try:
            with opener.open(request, timeout=wire_timeout) as response:
                with rate_control.wire_deadline(response, deadline):
                    raw = response.read(32_000_001)
                    if len(raw) > 32_000_000: raise ValueError('Response transport size exceeded')
                    result = json.loads(raw)
                usage = result.get('usage') or {}
                details = {'http_status':response.status, 'usage':usage,
                    'usage_known':rate_control.usage_is_reported(usage),
                    'response_model':result.get('model'), 'response_id':result.get('id')}
                if response.status != 200 or result.get('model') != MODEL or not result.get('choices'):
                    raise rate_control.AttemptFailure('Invalid or unexpected model response', details=details)
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
            raise rate_control.AttemptFailure('HTTP ' + str(exc.code), details=details, retryable=refusal,
                retry_after=rate_control.retry_after_seconds(exc.headers.get('Retry-After'))) from exc
    try:
        result, attempts = rate_control.run(attempt, timeout=timeout, request_sha256=request_sha)
        error = None
    except rate_control.TransportFailure as exc:
        result, attempts = None, exc.attempts
        error = str(exc).replace(key,'[REDACTED]')[:2000]
    usage = {field:sum(int((a.get('usage') or {}).get(field) or 0) for a in attempts)
             for field in ('prompt_tokens','completion_tokens','total_tokens')}
    usage['prompt_tokens_details'] = {'cached_tokens':sum(int(((a.get('usage') or {}).get('prompt_tokens_details') or {}).get('cached_tokens') or 0) for a in attempts)}
    meta = {'http_status':attempts[-1].get('http_status') if attempts else None,
            'seconds':round(time.monotonic()-start,3), 'usage':usage,
            'response_model':result.get('model') if result else None,
            'response_id':result.get('id') if result else None, 'attempts':attempts,
            'request_sha256':request_sha,
            'usage_complete':all(not a.get('usage_unknown',False) for a in attempts),
            'usage_is_lower_bound':any(a.get('usage_unknown',False) for a in attempts),
            'wire_attempts':sum(bool(a.get('dispatched')) for a in attempts)}
    if error is not None: meta['error'] = error
    return result, redact(meta,key)

def continuing_message(message):
    # Preserve provider reasoning only in the private in-memory conversation.
    return {k:v for k,v in message.items() if k in ('role','content','reasoning_content','tool_calls')}

def public_message(message):
    return {k:v for k,v in message.items() if k in ('role','content','tool_calls')}
