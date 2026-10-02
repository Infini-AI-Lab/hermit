"""Responses <-> chat/completions bridge for the actor relay (2026-10-01).

Fireworks' /responses endpoint closes a stream at ~600 s; /chat/completions does not (measured 10-01:
a 120k-token stream ran 1115 s, and the OfficeQA request cut six times at 602.7 s on /responses).
Codex speaks only the Responses API, so the relay translates: the Responses request codex sends
becomes a chat/completions request, and the chat stream is turned back into the Responses SSE
events codex (and read_wire) consume. Codex's requests carry only message / function_call /
function_call_output items and plain function tools; anything else is refused, not guessed.
"""
import json
import time
import uuid

CHAT_URL = 'https://api.fireworks.ai/inference/v1/chat/completions'
FLUSH = 16384   # chars per delta event: the relay buffers the whole reply anyway, and one event per token made a
                # 20-minute stream exceed the relay's 32 MB transport cap (2026-10-01)


def _text(content):
    if isinstance(content, str):
        return content
    return ''.join(c.get('text', '') for c in content or [] if c.get('type') in ('input_text', 'output_text', 'text'))


def request_to_chat(r):
    msgs = []
    if r.get('instructions'):
        msgs.append({'role': 'system', 'content': r['instructions']})
    items = r['input'] if isinstance(r['input'], list) else [{'type': 'message', 'role': 'user', 'content': r['input']}]
    calls = []   # consecutive function_call items form one assistant message
    def flush():
        if calls:
            msgs.append({'role': 'assistant', 'content': None, 'tool_calls': list(calls)}); calls.clear()
    for x in items:
        kind = x.get('type', 'message')
        if kind == 'function_call':
            calls.append({'id': x['call_id'], 'type': 'function', 'function': {'name': x['name'], 'arguments': x.get('arguments') or '{}'}})
            continue
        flush()
        if kind == 'function_call_output':
            out = x.get('output')
            msgs.append({'role': 'tool', 'tool_call_id': x['call_id'], 'content': out if isinstance(out, str) else json.dumps(out)})
        elif kind == 'message':
            msgs.append({'role': {'developer': 'system'}.get(x.get('role'), x.get('role')), 'content': _text(x.get('content'))})
        elif kind == 'reasoning':
            continue   # prior reasoning is not replayed (the Responses relay sends none either: store=false)
        else:
            raise ValueError(f'chat bridge: unsupported input item {kind!r}')
    flush()
    body = {'model': r['model'], 'messages': msgs, 'stream': True, 'stream_options': {'include_usage': True}}
    tools = []
    for t in r.get('tools') or []:
        if t.get('type') != 'function':
            raise ValueError(f'chat bridge: unsupported tool type {t.get("type")!r}')
        fn = {'name': t['name']}
        if t.get('description') is not None: fn['description'] = t['description']
        if t.get('parameters') is not None: fn['parameters'] = t['parameters']
        tools.append({'type': 'function', 'function': fn})
    if tools:
        body['tools'] = tools
        body['tool_choice'] = r.get('tool_choice', 'auto')
        if r.get('parallel_tool_calls') is not None:
            body['parallel_tool_calls'] = r['parallel_tool_calls']
    effort = (r.get('reasoning') or {}).get('effort')
    if effort:
        body['reasoning_effort'] = effort
    for k in ('temperature', 'top_p', 'user', 'prompt_cache_key'):
        if r.get(k) is not None:
            body[k] = r[k]
    if r.get('max_output_tokens') is not None:
        body['max_tokens'] = r['max_output_tokens']
    return body


class ResponsesFromChat:
    """Wraps the upstream chat SSE response; iterating yields Responses SSE lines (bytes)."""

    def __init__(self, upstream, request):
        self.upstream, self.request = upstream, request
        self.status = upstream.status
        self.headers = {'Content-Type': 'text/event-stream'}
        self.rid = 'resp_' + uuid.uuid4().hex
        self.created = int(time.time())
        self.model = request.get('model')
        self.seq = 0
        self.items = []          # output items in order: dicts with 'type' and live state
        self.open = None         # the reasoning/message item receiving text deltas
        self.calls = {}          # chat tool_call index -> item
        self.usage = None
        self.finish = None
        self.error = None

    def __getattr__(self, name):   # fp, close, … for wire_deadline and the caller
        return getattr(self.upstream, name)

    def _ev(self, kind, **fields):
        ev = {'type': kind, 'sequence_number': self.seq, **fields}; self.seq += 1
        return b'data: ' + json.dumps(ev, ensure_ascii=False).encode() + b'\n\n'

    def _resp(self, status, output=None, usage=None, incomplete=None, error=None):
        return {'id': self.rid, 'object': 'response', 'created_at': self.created, 'status': status, 'model': self.model,
                'output': output or [], 'previous_response_id': None, 'usage': usage, 'error': error,
                'incomplete_details': incomplete, 'parallel_tool_calls': self.request.get('parallel_tool_calls', True),
                'tool_choice': self.request.get('tool_choice', 'auto'), 'store': False,
                'max_output_tokens': self.request.get('max_output_tokens'), 'reasoning': self.request.get('reasoning'),
                'temperature': self.request.get('temperature')}

    def _start_text(self, kind):
        out = []
        if self.open is not None and self.open['type'] == kind:
            return out
        idx = len(self.items)
        if kind == 'reasoning':
            it = {'type': 'reasoning', 'id': 'rs_' + uuid.uuid4().hex, 'text': '', 'buf': '', 'index': idx}
            out.append(self._ev('response.output_item.added', output_index=idx, item={'type': 'reasoning', 'id': it['id'], 'status': 'in_progress', 'summary': []}))
            out.append(self._ev('response.reasoning_summary_part.added', item_id=it['id'], output_index=idx, summary_index=0, part={'type': 'summary_text', 'text': ''}))
        else:
            it = {'type': 'message', 'id': 'msg_' + uuid.uuid4().hex, 'text': '', 'buf': '', 'index': idx}
            out.append(self._ev('response.output_item.added', output_index=idx, item={'type': 'message', 'id': it['id'], 'status': 'in_progress', 'role': 'assistant', 'content': []}))
            out.append(self._ev('response.content_part.added', item_id=it['id'], output_index=idx, content_index=0, part={'type': 'output_text', 'text': '', 'annotations': []}))
        self.items.append(it); self.open = it
        return out

    def _flush(self, it, force=False):
        if not it['buf'] or (len(it['buf']) < FLUSH and not force):
            return []
        b, it['buf'] = it['buf'], ''
        if it['type'] == 'reasoning':
            return [self._ev('response.reasoning_summary_text.delta', item_id=it['id'], output_index=it['index'], summary_index=0, delta=b)]
        if it['type'] == 'message':
            return [self._ev('response.output_text.delta', item_id=it['id'], output_index=it['index'], content_index=0, delta=b)]
        return [self._ev('response.function_call_arguments.delta', item_id=it['id'], output_index=it['index'], delta=b)]

    def _final_item(self, it):
        if it['type'] == 'reasoning':
            return {'type': 'reasoning', 'id': it['id'], 'status': 'completed', 'summary': [{'type': 'summary_text', 'text': it['text']}]}
        if it['type'] == 'message':
            return {'type': 'message', 'id': it['id'], 'status': 'completed', 'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': it['text'], 'annotations': []}]}
        return {'type': 'function_call', 'id': it['id'], 'status': 'completed', 'call_id': it['call_id'], 'name': it['name'], 'arguments': it['args']}

    def _close_all(self):
        out = []
        for it in self.items:
            out += self._flush(it, force=True)
        for it in self.items:
            i = it['index']
            if it['type'] == 'reasoning':
                out.append(self._ev('response.reasoning_summary_text.done', item_id=it['id'], output_index=i, summary_index=0, text=it['text']))
                out.append(self._ev('response.reasoning_summary_part.done', item_id=it['id'], output_index=i, summary_index=0, part={'type': 'summary_text', 'text': it['text']}))
            elif it['type'] == 'message':
                out.append(self._ev('response.output_text.done', item_id=it['id'], output_index=i, content_index=0, text=it['text']))
                out.append(self._ev('response.content_part.done', item_id=it['id'], output_index=i, content_index=0, part={'type': 'output_text', 'text': it['text'], 'annotations': []}))
            else:
                out.append(self._ev('response.function_call_arguments.done', item_id=it['id'], output_index=i, arguments=it['args']))
            out.append(self._ev('response.output_item.done', output_index=i, item=self._final_item(it)))
        return out

    def _usage(self):
        u = self.usage or {}
        if not u:
            return None
        res = {'input_tokens': u.get('prompt_tokens'), 'output_tokens': u.get('completion_tokens'), 'total_tokens': u.get('total_tokens'),
               'input_tokens_details': {'cached_tokens': (u.get('prompt_tokens_details') or {}).get('cached_tokens', 0)}}
        rt = (u.get('completion_tokens_details') or {}).get('reasoning_tokens')
        if rt is not None:
            res['output_tokens_details'] = {'reasoning_tokens': rt}
        return res

    def _chunk(self, ev):
        out = []
        if ev.get('model'):
            self.model = ev['model']
        if ev.get('error'):
            self.error = ev['error']
            return out
        if ev.get('usage'):
            self.usage = ev['usage']
        for ch in ev.get('choices') or []:
            d = ch.get('delta') or {}
            if d.get('reasoning_content'):
                out += self._start_text('reasoning')
                self.open['text'] += d['reasoning_content']; self.open['buf'] += d['reasoning_content']
                out += self._flush(self.open)
            if d.get('content'):
                out += self._start_text('message')
                self.open['text'] += d['content']; self.open['buf'] += d['content']
                out += self._flush(self.open)
            for tc in d.get('tool_calls') or []:
                k = tc.get('index', 0)
                fn = tc.get('function') or {}
                it = self.calls.get(k)
                if it is None:
                    self.open = None
                    it = {'type': 'function_call', 'id': 'fc_' + uuid.uuid4().hex, 'call_id': tc.get('id') or ('call_' + uuid.uuid4().hex[:24]),
                          'name': fn.get('name') or '', 'args': '', 'buf': '', 'index': len(self.items)}
                    self.items.append(it); self.calls[k] = it
                    out.append(self._ev('response.output_item.added', output_index=it['index'],
                                        item={'type': 'function_call', 'id': it['id'], 'status': 'in_progress', 'call_id': it['call_id'], 'name': it['name'], 'arguments': ''}))
                elif fn.get('name') and not it['name']:
                    it['name'] = fn['name']
                if fn.get('arguments'):
                    it['args'] += fn['arguments']; it['buf'] += fn['arguments']
                    out += self._flush(it)
            if ch.get('finish_reason'):
                self.finish = ch['finish_reason']
        return out

    def __iter__(self):   # line by line, as an HTTP response iterates (read_wire flushes an event on the blank line)
        for chunk in self._events():
            for line in chunk.splitlines(keepends=True):
                yield line

    def _events(self):
        yield self._ev('response.created', response=self._resp('in_progress'))
        yield self._ev('response.in_progress', response=self._resp('in_progress'))
        done = False
        for raw in self.upstream:
            line = raw.strip()
            if not line.startswith(b'data:'):
                continue
            data = line[5:].strip()
            if data == b'[DONE]':
                done = True
                break
            for b in self._chunk(json.loads(data)):
                yield b
        if self.error is not None:
            yield self._ev('error', error=self.error)
            yield self._ev('response.failed', response=self._resp('failed', error=self.error))
            yield b'data: [DONE]\n\n'
            return
        if not done and self.finish is None:
            return   # the stream ended early: no terminal event, read_wire reports it as a cut stream
        for b in self._close_all():
            yield b
        output = [self._final_item(it) for it in self.items]
        if self.finish == 'length':
            yield self._ev('response.incomplete', response=self._resp('incomplete', output, self._usage(), {'reason': 'max_output_tokens'}))
        else:
            yield self._ev('response.completed', response=self._resp('completed', output, self._usage()))
        yield b'data: [DONE]\n\n'
