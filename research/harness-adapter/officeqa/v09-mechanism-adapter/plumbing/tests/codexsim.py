"""CodexSim: drives multi-request Responses conversations against the broker the way Codex 0.155 does.

Behaviour copied from the real binary (tests/test_v9_codex_live.py re-checks each point) and from the
v8 records (understand/broker.md §3a, compaction.md §2):
- request body keys and order: model, instructions, input, tools, tool_choice 'auto',
  parallel_tool_calls true, reasoning, store false, stream true, include, prompt_cache_key,
  client_metadata; the first input items are [developer, AGENTS.md user, task user] with 'msg_' ids;
- provider output items come back re-serialised (`reserialize`): reasoning
  {type,id,summary,content,encrypted_content} with missing fields as null; message
  {type,id,role,content:[{type,text}]} (status, annotations, logprobs dropped); function_call
  {type,id,name,arguments,call_id}; parallel calls first, then their outputs in call order;
- a tool result is a function_call_output with a fresh 'fco_' id and the header
  'Chunk ID: <6 hex>\nWall time: <t> seconds\nProcess exited with code <c>\nOriginal token count: <n>\nOutput:\n';
- compaction: the whole history + the fixed compaction prompt (no id), tools=[]; the resume request is
  [developer, AGENTS.md, task, 'Another language model started…' + summary] with fresh ids;
- after an error event or HTTP error it re-sends the identical input; after response.incomplete it keeps
  the items it already received (output_item.done) and re-sends the turn with them appended.
"""
from __future__ import annotations

import json
import random
import urllib.error
import urllib.request
import uuid

MODEL = 'accounts/fireworks/models/glm-5p3-flash'
COMPACTION_PROMPT = ('You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another '
                     'LLM that will resume the task.\n\nInclude:\n- Current progress and key decisions made\n'
                     '- Important context, constraints, or user preferences\n- What remains to be done (clear next '
                     'steps)\n- Any critical data, examples, or references needed to continue\n\nBe concise, '
                     'structured, and focused on helping the next LLM seamlessly continue the work.\n')
RESUME_PREFIX = ('Another language model started to solve this problem and produced a summary of its thinking '
                 'process. You also have access to the state of the tools that were used by that language model. '
                 'Use this to build on the work that has already been done and avoid duplicating work. Here is the '
                 'summary produced by the other language model, use the information in this summary to assist '
                 'with your own analysis:\n')
TOOLS = [{'type': 'function', 'name': 'exec_command', 'description': 'Runs a command in a PTY.', 'strict': False,
          'parameters': {'type': 'object', 'properties': {'cmd': {'type': 'string'}}, 'required': ['cmd']}},
         {'type': 'function', 'name': 'write_stdin', 'description': 'Writes to a session.', 'strict': False,
          'parameters': {'type': 'object', 'properties': {'session_id': {'type': 'number'},
                                                         'chars': {'type': 'string'}}}}]


class SimError(Exception):
    """The broker returned an error event / HTTP error (Codex would retry the identical input)."""


class SimIncomplete(Exception):
    """response.incomplete: Codex keeps the received items and retries the turn."""



def reserialize(item):
    """Codex's re-serialisation of one provider output item for its next request."""
    kind = item.get('type')
    if kind == 'reasoning':
        value = {'type': 'reasoning'}
        if item.get('id') is not None:
            value['id'] = item['id']
        value.update(summary=item.get('summary') or [], content=item.get('content'),
                     encrypted_content=item.get('encrypted_content'))
        return value
    if kind == 'message':
        value = {'type': 'message'}
        if item.get('id') is not None:
            value['id'] = item['id']
        value['role'] = item.get('role', 'assistant')
        value['content'] = [{'type': p['type'], 'text': p['text']} for p in item.get('content') or []]
        return value
    if kind == 'function_call':
        value = {'type': 'function_call'}
        if item.get('id') is not None:
            value['id'] = item['id']
        value.update(name=item.get('name'), arguments=item.get('arguments'), call_id=item.get('call_id'))
        return value
    return {k: v for k, v in item.items() if k != 'status'}


def tool_output(text, *, exit_code=0, rng=random):
    """A Codex exec_command result as the model sees it (random Chunk ID, varying Wall time)."""
    return (f'Chunk ID: {rng.randrange(16 ** 6):06x}\nWall time: {rng.random():.4f} seconds\n'
            f'Process exited with code {exit_code}\nOriginal token count: {max(1, len(text) // 4)}\nOutput:\n{text}')


def parse_sse(wire: bytes):
    """-> (events, terminal response or None, error events, heartbeat count)."""
    events, errors, heartbeats, terminal = [], [], 0, None
    for block in wire.split(b'\n\n'):
        lines = block.split(b'\n')
        data = b'\n'.join(line[5:].strip() for line in lines if line.startswith(b'data:'))
        heartbeats += sum(1 for line in lines if line.startswith(b':'))
        if not data or data == b'[DONE]':
            continue
        event = json.loads(data)
        events.append(event)
        if event.get('type') == 'error':
            errors.append(event)
        if event.get('type') in ('response.completed', 'response.incomplete', 'response.failed'):
            terminal = event.get('response')
    return events, terminal, errors, heartbeats


class CodexSim:
    def __init__(self, endpoint, task='Answer the question.', *, agents='# AGENTS.md\nAnswer.', tools=TOOLS,
                 instructions='You are Codex.', seed=0, run_tool=None):
        self.endpoint = endpoint.rstrip('/')
        self.instructions, self.tools = instructions, tools
        self.rng = random.Random(seed)
        self.session = str(uuid.UUID(int=self.rng.getrandbits(128)))
        self.task, self.agents = task, agents
        self.history = self.initial()
        self.run_tool = run_tool or (lambda cmd: f'ran {cmd}\n')
        self.sent = []            # every request body sent (parsed)
        self.responses = []       # terminal responses received
        self.wires = []           # raw forwarded bytes
        self.heartbeats = 0
        self.window = 0

    def _id(self, prefix):
        return prefix + str(uuid.UUID(int=self.rng.getrandbits(128)))

    def initial(self):
        _mid = lambda: self._id('msg_')
        return [{'type': 'message', 'id': _mid(), 'role': 'developer',
                 'content': [{'type': 'input_text', 'text': '<permissions instructions>sandbox</permissions instructions>'}]},
                {'type': 'message', 'id': _mid(), 'role': 'user',
                 'content': [{'type': 'input_text', 'text': self.agents},
                             {'type': 'input_text', 'text': '<environment_context>\n  <cwd>/ws</cwd>\n</environment_context>'}]},
                {'type': 'message', 'id': _mid(), 'role': 'user', 'content': [{'type': 'input_text', 'text': self.task}]}]

    def body(self, input_items, *, tools=True, kind='turn'):
        return {'model': MODEL, 'instructions': self.instructions, 'input': input_items,
                'tools': self.tools if tools else [], 'tool_choice': 'auto', 'parallel_tool_calls': True,
                'reasoning': {'effort': 'high', 'summary': 'auto'}, 'store': False, 'stream': True,
                'include': ['reasoning.encrypted_content'], 'prompt_cache_key': self.session,
                'client_metadata': {'session_id': self.session, 'x-codex-turn-metadata': json.dumps(
                    {'request_kind': kind, 'window_number': self.window})}}

    def post(self, body):
        data = json.dumps(body).encode()
        self.sent.append(json.loads(data))
        request = urllib.request.Request(self.endpoint + '/responses', data=data,
                                         headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                wire = response.read()
        except urllib.error.HTTPError as exc:
            raise SimError(f'HTTP {exc.code}') from exc
        self.wires.append(wire)
        events, terminal, errors, heartbeats = parse_sse(wire)
        self.heartbeats += heartbeats
        if errors or terminal is None:
            raise SimError('stream error: ' + json.dumps(errors)[:200])
        self.responses.append(terminal)
        return events, terminal

    def send(self):
        """One turn request with the current history; appends the output items and, for tool calls,
        their outputs. Returns the terminal response."""
        events, terminal = self.post(self.body(list(self.history)))
        items = [e['item'] for e in events if e.get('type') == 'response.output_item.done']
        if not items and terminal.get('output'):
            items = terminal['output']
        self.history.extend(reserialize(item) for item in items)
        if terminal.get('status') == 'incomplete':
            raise SimIncomplete(terminal.get('incomplete_details'))
        calls = [item for item in items if item.get('type') == 'function_call']
        for item in calls:
            try:
                args = json.loads(item.get('arguments') or '{}')
            except ValueError:
                args = {}
            text = self.run_tool(args.get('cmd') if item.get('name') == 'exec_command' else
                                 f"{item.get('name')} {json.dumps(args, sort_keys=True)}")
            self.history.append({'type': 'function_call_output', 'id': self._id('fco_'),
                                 'call_id': item.get('call_id'), 'output': tool_output(text, rng=self.rng)})
        return terminal

    def retry(self):
        """Codex after an error: the identical input again."""
        return self.send()

    def compact(self):
        """Compaction request (tools=[], history + fixed prompt), then restart the history from the
        summary with fresh ids. Returns the compaction response."""
        body = self.body(list(self.history) + [{'type': 'message', 'role': 'user',
                                                 'content': [{'type': 'input_text', 'text': COMPACTION_PROMPT}]}],
                         tools=False, kind='compaction')
        _, terminal = self.post(body)
        summary = ''.join(p.get('text', '') for item in terminal.get('output') or [] if item.get('type') == 'message'
                          for p in item.get('content') or [])
        self.window += 1
        self.history = self.initial() + [{'type': 'message', 'id': self._id('msg_'), 'role': 'user',
                                          'content': [{'type': 'input_text', 'text': RESUME_PREFIX + summary}]}]
        return terminal

    @staticmethod
    def final_text(response):
        texts = [''.join(p.get('text', '') for p in item.get('content') or [])
                 for item in response.get('output') or [] if item.get('type') == 'message']
        texts = [t for t in texts if t.strip()]
        return texts[-1] if texts else ''

    def is_final(self, response):
        return (response.get('status') == 'completed'
                and not any(item.get('type') == 'function_call' for item in response.get('output') or [])
                and bool(self.final_text(response)))
