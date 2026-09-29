"""Offline tests of the v9 broker controller hook (DESIGN §2 L2-L4; understand/broker.md §7 items 1-21),
the /v1/submit route, MechController and the offline replay goldens over the v8 records.

No credentials, no network beyond 127.0.0.1: the upstream is tests/fake_upstream.py and Codex is
tests/codexsim.py. The ledger and gate are STUBS implementing contracts.LedgerAPI / GateAPI (the real
ones, harness_core/ledger.py and gate.py, are built by stream S4 and deliberately not imported here).
Slow tests are marked `slow` and opt-in (tests/conftest.py): `pytest -m slow tests/test_v9_broker.py` (or V9_SLOW=1).
"""
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
import filecmp
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import broker  # noqa: E402
import controller_runtime as CR  # noqa: E402
import fake_upstream as fu  # noqa: E402
import rate_control  # noqa: E402
from codexsim import CodexSim, SimError, SimIncomplete, parse_sse, reserialize  # noqa: E402
from harness_core import contracts as C  # noqa: E402

PROJECT = ROOT.parent
V8 = PROJECT / 'officeqa-pro133-v8-plumbing'
KEY = 'sk-test-SECRET-KEY-0123456789'
UNBOUNDED = {'max_completion': None, 'max_response': None, 'request_timeout': None, 'max_attempts': None}
V8_ROOTS = {'none': 'officeqa-pro133-none-v8-work/runs/baseline',
            'prompt': 'officeqa-pro133-prompt-v8-work/runs/adapted',
            'cards': 'officeqa-pro133-cards-v8-work/runs/adapted'}


def scheduler():
    return rate_control.RateScheduler(max_in_flight=float('inf'), min_interval=0, backoffs=(0,))


@contextmanager
def relay(tmp_path, responder, controller=None, **options):
    up = fu.Upstream(responder)
    opts = dict(UNBOUNDED)
    opts.update(options)
    try:
        with broker.Broker(KEY, tmp_path / 'api', scheduler=scheduler(), upstream_url=up.url,
                           controller=controller, **opts) as relay_:
            yield relay_, up
    finally:
        up.close()


def api(tmp_path, number, name):
    return json.loads((tmp_path / 'api' / f'{number:03}' / name).read_text())


def summary(tmp_path):
    return json.loads((tmp_path / 'api/summary.json').read_text())


def log_rows(tmp_path):
    path = tmp_path / 'api/controller.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def notes(items, reason=None):
    prefix = '[harness:' + (reason + ']' if reason else '')
    return [i for i, item in enumerate(items) if item.get('role') == 'user'
            and CR.message_text(item).startswith(prefix)]


def final_msg(text, **kw):
    return fu.response([fu.message(text)], **kw)


# ---------------------------------------------------------------------------------------------
# Stubs for S4's ledger and gate
# ---------------------------------------------------------------------------------------------

class StubLedger:
    """contracts.LedgerAPI stand-in: Command events from response function_calls (normalised with
    controller_runtime.command_of), output hashes from controller_runtime.normalize_output, candidates
    via add(); counts survive compaction."""

    def __init__(self, render_text='candidates: a1 "42" (k1)\ncomputes: k1 sha=abc -> 42'):
        self._events, self.counts, self.by_call, self.seen_out = [], Counter(), {}, set()
        self.render_text, self.render_calls, self.ingested = render_text, 0, []

    def ingest_request(self, call, codex_input, t):
        self.ingested.append((call, len(codex_input)))
        for item in codex_input:
            if item.get('type') == 'function_call_output' and item.get('call_id') not in self.seen_out:
                self.seen_out.add(item['call_id'])
                event = self.by_call.get(item['call_id'])
                if event is not None:
                    event.output_sha256 = C.sha256_text(CR.normalize_output(item.get('output')))

    def ingest_response(self, call, response, t):
        for item in response.get('output') or []:
            if item.get('type') == 'function_call' and item['call_id'] not in self.by_call:
                cmd = CR.command_of(item)
                self.counts[cmd] += 1
                event = C.Command(kind='command', ref=f'x{len(self.by_call) + 1}', call=call, t=t, cmd=cmd,
                                  call_id=item['call_id'], n=self.counts[cmd])
                self.by_call[item['call_id']] = event
                self._events.append(event)

    def add(self, event):
        if not event.ref:                     # like harness_core.ledger.Ledger.add: the ledger assigns refs
            prefix = {'command': 'x', 'observe': 'c', 'compute': 'k', 'candidate': 'a', 'note': 'n',
                      'compaction': 'z'}[event.kind]
            event = replace(event, ref=f'{prefix}{1 + sum(1 for e in self._events if e.kind == event.kind)}')
        self._events.append(event)
        return event

    @property
    def events(self):
        return list(self._events)

    def command_count(self, cmd):
        return self.counts[cmd]

    def candidates(self):
        return [e for e in self._events if e.kind == 'candidate']

    def render(self, max_bytes):
        self.render_calls += 1
        return self.render_text.encode()[:max_bytes].decode(errors='ignore')


class StubGate:
    """contracts.GateAPI stand-in: records a candidate per submit/check_final and returns a fixed result."""

    def __init__(self, final_findings=True, submit_status='accepted', message='recompute_final: 41 is not any calc output'):
        self.final_findings, self.submit_status, self.message = final_findings, submit_status, message
        self.final_calls, self.submits = [], []

    def _candidate(self, ledger, answer, via, t, call):
        ref = f'a{len(ledger.candidates()) + 1}'
        ledger.add(C.Candidate(kind='candidate', ref=ref, call=call, t=t, answer=answer, via=via))
        return ref

    def submit(self, payload, ledger, t, call):
        self.submits.append((dict(payload), call))
        ref = self._candidate(ledger, payload.get('answer', ''), 'submit', t, call)
        if self.submit_status == 'findings':
            finding = C.Finding('recompute_final', self.message, C.OfferResult('42', 'k1 re-run with c3'),
                                bindings={'internal': 'never shown'})
            return C.GateResult('findings', [finding], self.message + '\nAccept: submit --answer "42"', ref)
        return C.GateResult('accepted', [], f'accepted {ref}', ref)

    def check_final(self, final_text, ledger, t, call):
        self.final_calls.append((final_text, call))
        ref = self._candidate(ledger, final_text, 'final', t, call)
        if self.final_findings:
            return C.GateResult('findings', [C.Finding('recompute_final', self.message)], self.message, ref)
        return C.GateResult('accepted', [], 'accepted', ref)


def mech(policy=None, clock=time.monotonic, **gate_options):
    ledger, gate = StubLedger(), StubGate(**gate_options)
    return CR.MechController(ledger, gate, policy, clock), ledger, gate


class Scripted:
    """Generic controller with scripted decisions, for broker mechanics tests."""

    def __init__(self, inserts=None, verdict=None, fail=(), on_final_hook=None):
        self.inserts, self.verdict, self.fail, self.on_final_hook = inserts, verdict, set(fail), on_final_hook
        self.views, self.responses, self.finals, self.submits = [], [], [], []

    def on_request(self, view):
        self.views.append(view)
        if 'on_request' in self.fail:
            raise RuntimeError('boom on_request ' + KEY)
        return list(self.inserts(view)) if self.inserts else []

    def on_response(self, view, response, call):
        self.responses.append((call, response.get('id')))
        if 'on_response' in self.fail:
            raise RuntimeError('boom on_response')

    def on_final(self, view, response, round):
        self.finals.append((view.codex_request, round))
        if self.on_final_hook:
            self.on_final_hook()
        if 'on_final' in self.fail:
            raise RuntimeError('boom on_final')
        return self.verdict(view, response, round) if self.verdict else C.Verdict('forward')

    def on_submit(self, payload):
        self.submits.append(payload)
        return C.GateResult('accepted', [], 'ok', 'a1')


# ---------------------------------------------------------------------------------------------
# 1. v8 equivalence with controller=None
# ---------------------------------------------------------------------------------------------

VOLATILE = {'seconds', 'queued_utc', 'dispatched_utc', 'finished_utc', 'wait_seconds', 'wire_seconds',
            'scheduler_wait_seconds', 'upstream'}          # 'upstream' = the fake's random port


def masked(value):
    if isinstance(value, dict):
        return {k: masked(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [masked(v) for v in value]
    return value


def load_v8_broker():
    spec = importlib.util.spec_from_file_location('broker_v8_reference', V8 / 'broker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def equivalence_bodies():
    sim = CodexSim('http://127.0.0.1:9/v1', seed=7)
    first = sim.body(list(sim.history))
    sim.history.extend([reserialize(fu.call('ls', call_id='cA', id='fc_A')),
                        {'type': 'function_call_output', 'id': 'fco_A', 'call_id': 'cA', 'output': 'Chunk ID: 1\nOutput:\nx'}])
    second = sim.body(list(sim.history))
    third = sim.body(list(sim.history) + [{'type': 'message', 'id': 'msg_x', 'role': 'user',
                                           'content': [{'type': 'input_text', 'text': 'go on'}]}])
    return [first, second, third]


def fixed_responder():
    good_call = fu.response([fu.reasoning('r', id='rs_1'), fu.call('cat a', call_id='c1', id='fc_1')], id='resp_1')
    defect = fu.response([], id='resp_2', use=fu.usage(0, 0))
    good_msg = fu.response([fu.message('FINAL 1', id='msg_1')], id='resp_3')
    script = {0: [good_call], 1: [defect, good_msg], 2: [('http', 400, {'error': {'message': 'bad'}})]}
    counters = Counter()

    def responder(body, index):
        turn = len([b for b in body['input'] if b.get('type') == 'function_call_output']) + (
            1 if body['input'][-1].get('id') == 'msg_x' else 0)
        replies = script[turn]
        reply = replies[min(counters[turn], len(replies) - 1)]
        counters[turn] += 1
        return reply
    return responder


def run_bodies(module, tmp, bodies, monkeypatch, options):
    up = fu.Upstream(fixed_responder())
    monkeypatch.setattr(module, 'URL', up.url)
    forwarded = []
    with module.Broker(KEY, tmp / 'api', scheduler=scheduler(), **options) as relay_:
        for body in bodies:
            request = urllib.request.Request(relay_.endpoint + '/responses', data=json.dumps(body).encode(),
                                             headers={'Content-Type': 'application/json'}, method='POST')
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    forwarded.append(response.read())
            except urllib.error.HTTPError as exc:
                forwarded.append(('http', exc.code))
    up.close()
    files = {str(p.relative_to(tmp / 'api')): masked(json.loads(p.read_text()))
             for p in sorted((tmp / 'api').rglob('*.json'))}
    return up.raw, forwarded, files


@pytest.mark.parametrize('options', [UNBOUNDED, {}], ids=['full_unbounded', 'bounded_default'])
def test_1_controller_none_is_byte_identical_to_v8(tmp_path, monkeypatch, options):
    assert filecmp.cmp(V8 / 'rate_control.py', ROOT / 'rate_control.py', shallow=False)
    v8 = load_v8_broker()
    bodies = equivalence_bodies()
    raw8, fwd8, files8 = run_bodies(v8, tmp_path / 'v8', bodies, monkeypatch, options)
    raw9, fwd9, files9 = run_bodies(broker, tmp_path / 'v9', bodies, monkeypatch, options)
    assert raw9 == raw8 and len(raw9) == 4                     # upstream bodies, byte for byte (incl. defect re-send)
    assert fwd9 == fwd8                                         # forwarded bytes (and the same HTTP error)
    assert files9 == files8                                     # every record, timing fields masked
    assert not any(name.endswith('controller.json') for name in files9)
    assert 'codex_requests' not in files9['summary.json'] and 'origin' not in files9['001/transport.json']


# ---------------------------------------------------------------------------------------------
# 2. history view and classification
# ---------------------------------------------------------------------------------------------

def test_2_history_view_and_classify(tmp_path):
    def responder(body, index):
        if not body.get('tools'):
            return final_msg('**Handoff Summary** did cat a')
        if index == 0:
            return fu.response([fu.call('cat a')])
        return final_msg('FINAL 42')
    controller = Scripted()
    with relay(tmp_path, responder, controller) as (relay_, up):
        sim = CodexSim(relay_.endpoint)
        sim.send(); sim.send(); sim.compact(); sim.send()
    assert [v.kind for v in controller.views] == ['turn', 'turn', 'compaction', 'resume']
    for view, sent in zip(controller.views, sim.sent):
        assert list(view.codex_input) == sent['input']            # exactly what Codex sent
        assert view.tools_offered == bool(sent['tools'])
    assert [api(tmp_path, n, 'transport.json')['kind'] for n in (1, 2, 3, 4)] == ['turn', 'turn', 'compaction', 'resume']
    assert summary(tmp_path)['compactions'] == 1 and summary(tmp_path)['codex_requests'] == 4


def test_classify_unit():
    developer = {'type': 'message', 'id': 'm0', 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'd'}]}
    prompt = {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': CR.COMPACTION_PROMPT + ' x'}]}
    summary_item = {'type': 'message', 'id': 'm9', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': CR.RESUME_PREFIX + ' ... summary'}]}
    call = fu.call('ls')
    out = {'type': 'function_call_output', 'call_id': call['call_id'], 'output': 'x'}
    assert CR.classify([developer, prompt], False) == 'compaction'
    assert CR.classify([developer, prompt], True) == 'turn'                   # tools offered: not a compaction
    assert CR.classify([developer, summary_item], True) == 'resume'
    assert CR.classify([developer, summary_item, call, out], True) == 'turn'  # the model has acted since
    assert CR.classify([developer, summary_item, call, out, prompt], False) == 'compaction'
    assert CR.classify([developer], True) == 'turn'


def test_normalize_output_and_command_of():
    a = 'Chunk ID: 0a1b2c\nWall time: 0.0012 seconds\nProcess exited with code 0\nOriginal token count: 3\nOutput:\nWall time: kept\n'
    b = 'Chunk ID: ffffff\nWall time: 9.9 seconds\nProcess exited with code 0\nOriginal token count: 3\nOutput:\nWall time: kept\n'
    assert CR.normalize_output(a) == CR.normalize_output(b)
    assert 'Wall time: kept' in CR.normalize_output(a) and 'Chunk ID' not in CR.normalize_output(a)
    assert CR.command_of(fu.call('cat   a.txt |\n head')) == 'cat a.txt | head'
    poll = fu.call(None, name='write_stdin', session_id=3, chars='')
    assert CR.command_of(poll).startswith('write_stdin ')
    assert CR.command_of({'type': 'message'}) is None
    shell = {'type': 'function_call', 'name': 'shell', 'call_id': 's', 'arguments': json.dumps({'command': 'ls  -l'})}
    assert CR.command_of(shell) == 'ls -l'                                   # == S4 ledger.command_text
    argv = {'type': 'function_call', 'name': 'shell', 'call_id': 's', 'arguments': json.dumps({'command': ['bash', '-lc', 'ls  -l']})}
    assert CR.command_of(argv) == 'ls -l'


# ---------------------------------------------------------------------------------------------
# 3-5. loop guard, placement, stickiness / cache prefix
# ---------------------------------------------------------------------------------------------

def loop_responder(commands):
    """Emit the scripted command batches one request at a time, then a final answer."""
    def responder(body, index):
        batch = commands[index] if index < len(commands) else None
        if batch is None:
            return final_msg('FINAL 42')
        batch = batch if isinstance(batch, list) else [batch]
        items = []
        for cmd in batch:
            if isinstance(cmd, dict):
                items.append(fu.call(None, name='write_stdin', **cmd))
            else:
                items.append(fu.call(cmd))
        return fu.response(items)
    return responder


def run_sim(relay_, turns, **sim_options):
    sim = CodexSim(relay_.endpoint, **sim_options)
    for _ in range(turns):
        response = sim.send()
        if sim.is_final(response):
            break
    return sim


def assert_prefix_consistent(bodies):
    for before, after in zip(bodies, bodies[1:]):
        if before.get('tools') and after.get('tools'):
            assert after['input'][:len(before['input'])] == before['input']


def test_3_loop_guard_fires_on_third_identical_command_once(tmp_path):
    controller, ledger, _ = mech(final_findings=False)
    with relay(tmp_path, loop_responder(['cat a.txt'] * 5), controller) as (relay_, up):
        sim = run_sim(relay_, 10, run_tool=lambda cmd: 'same output\n')
    counts = [len(notes(body['input'], 'loop_guard')) for body in up.bodies]
    assert counts == [0, 0, 0, 1, 1, 1]            # fires after the 3rd output, once, stays (sticky)
    outputs = [i['output'] for i in sim.history if i.get('type') == 'function_call_output']
    assert len(set(outputs)) == len(outputs)        # Chunk ID / Wall time differ every time ...
    text = CR.message_text(up.bodies[3]['input'][notes(up.bodies[3]['input'])[0]])
    assert 'identical each time' in text and 'cat a.txt' in text and '3 times' in text
    assert text.startswith('[harness:loop_guard] ')
    assert summary(tmp_path)['inserts_by_reason'] == {'loop_guard': 1}
    assert_prefix_consistent(up.bodies)             # 5: request N+1 extends request N as sent


def test_3_loop_guard_any_third_issue_not_only_streaks_and_not_polls(tmp_path):
    controller, _, _ = mech(final_findings=False)
    commands = ['cat a', 'cat b', 'cat a', 'cat b', 'cat a']
    with relay(tmp_path, loop_responder(commands), controller) as (relay_, up):
        run_sim(relay_, 10)
    counts = [len(notes(body['input'], 'loop_guard')) for body in up.bodies]
    assert counts == [0, 0, 0, 0, 0, 1]             # A,B,A,B,A: fires at the 3rd A (critic C2 any-3rd rule)

    controller, _, _ = mech(final_findings=False)
    polls = [{'session_id': 1, 'chars': ''}] * 5
    tick = iter(range(100))
    with relay(tmp_path / 'polls', loop_responder(polls), controller) as (relay_, up):
        run_sim(relay_, 10, run_tool=lambda cmd: f'progress {next(tick)}\n')
    assert all(not notes(body['input'], 'loop_guard') for body in up.bodies)
    # a 2nd repeat is not enough
    controller, _, _ = mech(final_findings=False)
    with relay(tmp_path / 'two', loop_responder(['cat a', 'cat a']), controller) as (relay_, up):
        run_sim(relay_, 10)
    assert all(not notes(body['input']) for body in up.bodies)


def test_4_placement_after_parallel_outputs(tmp_path):
    controller, _, _ = mech(final_findings=False)
    with relay(tmp_path, loop_responder([['cat a', 'cat b']] * 4), controller) as (relay_, up):
        run_sim(relay_, 10)
    body = up.bodies[3]['input']
    positions = notes(body, 'loop_guard')
    assert len(positions) == 2 and positions == [len(body) - 2, len(body) - 1]
    last_output = max(i for i, item in enumerate(body) if item.get('type') == 'function_call_output')
    assert min(positions) == last_output + 1
    for i in positions:
        assert body[i] == {'type': 'message', 'role': 'user',
                           'content': [{'type': 'input_text', 'text': body[i]['content'][0]['text']}]}
    open_calls = set()                               # never between a call and its output
    for i, item in enumerate(body):
        if item.get('type') == 'function_call':
            open_calls.add(item['call_id'])
        elif item.get('type') == 'function_call_output':
            open_calls.discard(item['call_id'])
        elif i in positions:
            assert not open_calls
    assert_prefix_consistent(up.bodies)


def test_materialize_defers_past_open_calls(tmp_path):
    relay_ = broker.Broker(KEY, tmp_path / 'api', controller=Scripted(), **UNBOUNDED)
    call1, call2 = fu.call('a', call_id='k1', id='fc1'), fu.call('b', call_id='k2', id='fc2')
    items = [{'type': 'message', 'id': 'u', 'role': 'user', 'content': []}, reserialize(call1), reserialize(call2),
             {'type': 'function_call_output', 'id': 'o1', 'call_id': 'k1', 'output': ''},
             {'type': 'function_call_output', 'id': 'o2', 'call_id': 'k2', 'output': ''}]
    relay_.splices = [broker.Splice(1, CR.anchor_key(items[1]), [CR.note_item('loop_guard', 'n')], 'loop_guard', True, 1)]
    exchange = broker.Exchange(1)
    sent, placed = relay_._materialize(items, exchange)
    assert [p[0] for p in placed] == [5] and sent[5]['role'] == 'user'   # deferred until both outputs arrived
    relay_.splices.append(broker.Splice(2, 'id:gone', [CR.note_item('loop_guard', 'x')], 'loop_guard', True, 1))
    sent, placed = relay_._materialize(items, exchange)
    assert [d['splice'] for d in exchange.dropped] == [2] and [s.id for s in relay_.splices] == [1]


# ---------------------------------------------------------------------------------------------
# 6. compaction: old splices dropped, frozen ledger splice after the summary
# ---------------------------------------------------------------------------------------------

def test_6_compaction_drops_splices_and_pins_ledger(tmp_path):
    controller, ledger, _ = mech(final_findings=False)

    def responder(body, index):
        if not body.get('tools'):
            return final_msg('**Handoff Summary** loop on cat a; candidate 42')
        resumed = any(CR.is_summary(item) for item in body['input'])
        if not resumed:
            return fu.response([fu.call('cat a')])
        calls = sum(1 for item in body['input'] if item.get('type') == 'function_call')
        return fu.response([fu.call(f'cat part{calls}')]) if calls < 2 else final_msg('FINAL 42')

    with relay(tmp_path, responder, controller) as (relay_, up):
        sim = CodexSim(relay_.endpoint)
        for _ in range(4):
            sim.send()
        sim.compact()
        while not sim.is_final(sim.send()):
            pass
    kinds = [api(tmp_path, n, 'transport.json')['kind'] for n in range(1, len(up.bodies) + 1)]
    assert kinds == ['turn'] * 4 + ['compaction', 'resume', 'turn', 'turn']
    compaction = up.bodies[4]['input']
    assert len(notes(compaction, 'loop_guard')) == 1 and not up.bodies[4]['tools']   # anchors still present
    resume = up.bodies[5]['input']
    assert not notes(resume, 'loop_guard')
    assert CR.is_summary(resume[3]) and notes(resume, 'ledger') == [4] and len(resume) == 5
    ledger_item = resume[4]
    for body in up.bodies[6:]:
        assert body['input'][4] == ledger_item and len(notes(body['input'], 'ledger')) == 1
    assert ledger.render_calls == 1                                            # frozen, never re-rendered
    dropped = api(tmp_path, 6, 'controller.json')['dropped_splices']
    assert [d['reason'] for d in dropped] == ['loop_guard']
    assert summary(tmp_path)['dropped_splices'] == 1 and summary(tmp_path)['resumes'] == 1
    assert_prefix_consistent(up.bodies[5:])


# ---------------------------------------------------------------------------------------------
# 7. Codex retry idempotence
# ---------------------------------------------------------------------------------------------

def test_7_codex_retry_gets_identical_upstream_input_without_duplicate_note(tmp_path):
    controller, ledger, _ = mech(final_findings=False)
    calls = Counter()
    inner = loop_responder(['cat a'] * 5)

    def responder(body, index):
        if notes(body['input'], 'loop_guard') and not calls['failed']:
            calls['failed'] += 1
            return ('http', 400, {'error': {'message': 'injected'}})
        return inner(body, index)

    with relay(tmp_path, responder, controller) as (relay_, up):
        sim = CodexSim(relay_.endpoint)
        for _ in range(3):
            sim.send()
        with pytest.raises(SimError):
            sim.send()
        sim.retry()
    assert up.bodies[3]['input'] == up.bodies[4]['input']
    assert len(notes(up.bodies[4]['input'], 'loop_guard')) == 1
    assert up.raw[3].split(b'"input": ')[1].split(b', "tools"')[0] == up.raw[4].split(b'"input": ')[1].split(b', "tools"')[0]
    assert summary(tmp_path)['retries_replayed'] == 1 and summary(tmp_path)['inserts_by_reason'] == {'loop_guard': 1}
    assert len(ledger.ingested) == 4                  # the retry is not shown to the controller again
    assert api(tmp_path, 5, 'controller.json')['replayed_retry'] is True


# ---------------------------------------------------------------------------------------------
# 8-17. enforcement (gate re-query)
# ---------------------------------------------------------------------------------------------

DRAFT = fu.response([fu.reasoning('draft plan', id='rs_d'), fu.message('The answer is 41.', id='msg_d')], id='resp_draft')


def test_8_gate_pass_forwards_the_single_upstream_wire(tmp_path):
    controller, _, gate = mech(final_findings=False)
    with relay(tmp_path, lambda body, index: DRAFT, controller) as (relay_, up):
        sim = CodexSim(relay_.endpoint)
        sim.send()
    assert len(up.bodies) == 1 and sim.wires[0] == up.served[0][1]
    assert gate.final_calls and api(tmp_path, 1, 'transport.json')['forwarded'] is True
    assert api(tmp_path, 1, 'controller.json')['verdict'] == {'action': 'forward', 'feedback': None}


def revise_responder(revision):
    def responder(body, index):
        return revision if notes(body['input'], 'enforce') else DRAFT
    return responder


def test_9_16_17_gate_revise_forwards_revision_records_and_accounting(tmp_path):
    controller, ledger, gate = mech()
    revision = fu.response([fu.message('42', id='msg_r')], id='resp_rev')
    with relay(tmp_path, revise_responder(revision), controller) as (relay_, up):
        sim = CodexSim(relay_.endpoint)
        response = sim.send()
    assert sim.final_text(response) == '42' and sim.wires[0] == up.served[1][1]     # 9: the revision's own wire
    feedback = CR.ENFORCE_TEMPLATE.format(message=gate.message)
    expected = up.bodies[0]['input'] + CR.echo(DRAFT['output']) + [CR.note_item('enforce', feedback)]
    assert up.bodies[1]['input'] == expected
    echoed = up.bodies[1]['input'][-3:-1]
    assert echoed[0] == {'type': 'reasoning', 'id': 'rs_d', 'summary': [{'type': 'summary_text', 'text': 'draft plan'}],
                         'content': None, 'encrypted_content': 'enc'}
    assert echoed[1] == {'type': 'message', 'id': 'msg_d', 'role': 'assistant',
                         'content': [{'type': 'output_text', 'text': 'The answer is 41.'}]}   # no status/annotations
    first, second = api(tmp_path, 1, 'transport.json'), api(tmp_path, 2, 'transport.json')
    assert (first['origin'], first['forwarded'], first['parent']) == ('codex', False, None)
    assert (second['origin'], second['forwarded'], second['parent']) == ('controller', True, 1)
    assert second['native_request_settings'] == first['native_request_settings'] and second['kind'] == 'turn'
    c1, c2 = api(tmp_path, 1, 'controller.json'), api(tmp_path, 2, 'controller.json')
    assert c1['verdict']['action'] == 'revise' and c2['verdict']['action'] == 'forward' and c2['round'] == 1
    s = summary(tmp_path)
    assert (s['enforce_requeries'], s['controller_calls'], s['discarded_responses']) == (1, 1, 1)
    # 16. accounting: every upstream attempt is summed; controller usage separately; audit invariants
    assert s['usage']['input_tokens'] == 100 * len(up.bodies) and s['controller_usage']['input_tokens'] == 100
    for number in (1, 2):
        record = api(tmp_path, number, 'transport.json')
        assert all(a['request_sha256'] == record['request_sha256'] for a in record['attempts'])
        assert record['usage']['output_tokens'] == sum(a['usage']['output_tokens'] for a in record['attempts'])
    # 17. reconstruction: removing the inserted items gives Codex's input, hashes match
    for number, body in ((1, up.bodies[0]), (2, up.bodies[1])):
        record = api(tmp_path, number, 'controller.json')
        full = [item for i, item in enumerate(body['input']) if i not in {r['index'] for r in record['inserted']}]
        assert full == sim.sent[0]['input'] and CR.canonical_sha(full) == record['codex_input_sha256']
        public_items = api(tmp_path, number, 'request.json')['input']
        kept = [item for i, item in enumerate(public_items)
                if i not in {r['public_index'] for r in record['inserted'] if r['public_index'] is not None}]
        assert CR.canonical_sha(kept) == record['codex_public_input_sha256']
    assert [r['public_index'] for r in c2['inserted']] == [None, 3, 4]              # reasoning is not in request.json
    # review fix: the echoed draft's reasoning stays private in controller.json too (only a skeleton)
    assert c2['inserted'][0]['item'] == {'type': 'reasoning', 'id': 'rs_d', 'keys': ['content', 'encrypted_content',
                                         'id', 'summary', 'type'], 'private': True}
    for path in (tmp_path / 'api').rglob('*'):
        if path.is_file():
            assert 'draft plan' not in path.read_text(), path                      # reasoning summary text
    # review fix: the enforce Note enters the ledger once the re-query was sent, with its call number
    enforce_notes = [e for e in ledger.events if e.kind == 'note' and e.reason == 'enforce']
    assert [(e.call, e.text) for e in enforce_notes] == [(2, feedback)]


def test_10_11_cap_per_episode_and_tool_call_revision(tmp_path):
    controller, _, gate = mech()
    revision = fu.response([fu.call('python calc.py', call_id='cR', id='fc_R')], id='resp_rev')

    def responder(body, index):
        if any(item.get('call_id') == 'cR' for item in body['input'] if item.get('type') == 'function_call_output'):
            return final_msg('still 41, never submitted')
        return revision if notes(body['input'], 'enforce') else DRAFT

    with relay(tmp_path, responder, controller) as (relay_, up):
        sim = CodexSim(relay_.endpoint)
        first = sim.send()
        assert first['id'] == 'resp_rev'                        # the tool-call revision is forwarded ...
        second = sim.send()                                     # ... executed, and the turn continues
    assert sim.final_text(second) == 'still 41, never submitted'
    assert len(up.bodies) == 3                                  # no 2nd re-query: cap 1 per episode
    revised_input = up.bodies[1]['input']
    tail = [reserialize(item) for item in revision['output']] + [sim.history[-2]]
    assert up.bodies[2]['input'] == revised_input + tail        # draft + feedback spliced back, strict extension
    assert len(gate.final_calls) == 1


def test_10_broker_backstop_caps_revisions_per_request(tmp_path):
    controller = Scripted(verdict=lambda view, response, round: C.Verdict('revise', f'try again {round}'))
    with relay(tmp_path, lambda body, index: final_msg(f'draft {index}'), controller) as (relay_, up):
        sim = CodexSim(relay_.endpoint)
        response = sim.send()
    assert len(up.bodies) == 1 + broker.MAX_REVISIONS_PER_REQUEST and sim.final_text(response) == 'draft 3'
    assert [api(tmp_path, n, 'transport.json')['forwarded'] for n in (1, 2, 3, 4)] == [False, False, False, True]
    assert any(row['event'] == 'rounds_exhausted' for row in log_rows(tmp_path))
    assert summary(tmp_path)['discarded_responses'] == 3


def test_12_defect_inside_revision_is_resent_with_identical_bytes(tmp_path):
    controller, _, _ = mech()
    bad = fu.response([fu.reasoning('runaway')], status='incomplete', reason='max_output_tokens',
                      use=fu.usage(100, 65536))
    good = fu.response([fu.message('42')], id='resp_good')
    seen = Counter()

    def responder(body, index):
        if notes(body['input'], 'enforce'):
            seen['rev'] += 1
            return bad if seen['rev'] == 1 else good
        return DRAFT

    with relay(tmp_path, responder, controller) as (relay_, up):
        sim = CodexSim(relay_.endpoint)
        response = sim.send()
    assert up.raw[1] == up.raw[2] and response['id'] == 'resp_good'
    events, _, _, _ = parse_sse(sim.wires[0])
    assert sum(e['type'] == 'response.completed' for e in events) == 1 and sim.wires[0] == up.served[2][1]
    assert [a['status'] for a in api(tmp_path, 2, 'transport.json')['attempts']] == ['transport_retry', 'succeeded']


def test_13_revision_transport_failure_forwards_the_draft(tmp_path):
    controller, ledger, _ = mech()

    def responder(body, index):
        return ('http', 400, {'error': {'message': 'revision refused'}}) if notes(body['input'], 'enforce') else DRAFT

    with relay(tmp_path, responder, controller) as (relay_, up):
        sim = CodexSim(relay_.endpoint)
        response = sim.send()
        assert not [s for s in relay_.splices if s.reason == 'enforce']       # no draft splice registered
    assert response['id'] == 'resp_draft' and sim.wires[0] == up.served[0][1]
    first, second = api(tmp_path, 1, 'transport.json'), api(tmp_path, 2, 'transport.json')
    assert first['forwarded'] is True and second['forwarded'] is False and 'HTTP 400' in second['error']
    assert api(tmp_path, 2, 'controller.json')['fallback'] == 'draft_forwarded'
    s = summary(tmp_path)
    assert s['enforce_fallbacks'] == 1 and s['call_failures'] == 1 and s['discarded_responses'] == 0
    # review fix: the model never saw the feedback, so no enforce Note is recorded as shown
    assert not [e for e in ledger.events if e.kind == 'note' and e.reason == 'enforce']


def test_14_revision_is_bounded_by_the_actor_deadline(tmp_path, monkeypatch):
    """Review fix: a stalled re-query must not hold the draft past the actor's kill time (v8 saw
    603-1460 s stalls). With 32 s left and a 29 s margin the revision gets ~3 s, then the draft goes out."""
    monkeypatch.setattr(broker, 'REVISION_DEADLINE_MARGIN_S', 29)
    controller, ledger, gate = mech({'enforce_min_remaining_s': 30})
    with relay(tmp_path, lambda body, index: 'stall' if notes(body['input'], 'enforce') else DRAFT, controller,
               actor_deadline=time.monotonic() + 32) as (relay_, up):
        begun = time.monotonic()
        response = CodexSim(relay_.endpoint).send()
        took = time.monotonic() - begun
        up.closing.set()
    assert response['id'] == 'resp_draft' and 2 <= took < 10 and len(gate.final_calls) == 1
    assert api(tmp_path, 2, 'controller.json')['fallback'] == 'draft_forwarded'
    assert api(tmp_path, 1, 'transport.json')['forwarded'] is True
    assert not [e for e in ledger.events if e.kind == 'note' and e.reason == 'enforce']


def test_14_revision_skipped_inside_the_deadline_margin(tmp_path, monkeypatch):
    monkeypatch.setattr(broker, 'REVISION_DEADLINE_MARGIN_S', 40)
    controller, _, gate = mech({'enforce_min_remaining_s': 30})
    with relay(tmp_path, revise_responder(final_msg('42')), controller,
               actor_deadline=time.monotonic() + 35) as (relay_, up):
        response = CodexSim(relay_.endpoint).send()
    assert response['id'] == 'resp_draft' and len(up.bodies) == 1 and len(gate.final_calls) == 1
    assert any(row['event'] == 'revision_skipped' for row in log_rows(tmp_path))


def test_upstream_url_guard():
    options = dict(UNBOUNDED)
    for url in ('http://127.0.0.1:80@evil.example/v1/responses', 'http://evil.example/v1', 'http://127.0.0.1/v1',
                'ftp://127.0.0.1:21/', 'https://user:pw@api.fireworks.ai/inference/v1/responses', 'http://127.0.0.1:x/'):
        with pytest.raises(ValueError):
            broker.Broker(KEY, '/nonexistent', upstream_url=url, **options)
    for url in ('https://api.fireworks.ai/inference/v1/responses', 'http://127.0.0.1:8123/v1/responses'):
        assert broker.Broker(KEY, '/nonexistent', upstream_url=url, **options).url == url


def test_oversize_insert_fails_open(tmp_path):
    controller = Scripted(inserts=lambda view: [C.Insert('loop_guard', 'x' * (broker.MAX_NOTE_BYTES + 1))])
    with relay(tmp_path, lambda body, index: final_msg('1'), controller) as (relay_, up):
        sim = CodexSim(relay_.endpoint)
        sim.send()
    assert up.bodies[0]['input'] == sim.sent[0]['input'] and summary(tmp_path)['controller_errors'] == 1


def test_policy_is_validated_against_ranges():
    for bad in ({'loop_threshold': 1}, {'enforce_requery_max': 5}, {'ledger_splice_bytes': 100_000},
                {'loop_threshold': 3.0}, {'loop_threshold': True}, {'no_such_knob': 1}, {'disabled_rules': 'x'}):
        with pytest.raises(ValueError):
            CR.MechController(StubLedger(), StubGate(), bad)
    assert CR.MechController(StubLedger(), StubGate(), {'loop_threshold': 6}).policy['loop_threshold'] == 6


def test_14_enforcement_skipped_near_the_deadline(tmp_path):
    now = [1000.0]
    clock = lambda: now[0]
    controller, _, gate = mech(clock=clock)
    with relay(tmp_path, revise_responder(final_msg('42')), controller, clock=clock, actor_deadline=1030.0) as (relay_, up):
        CodexSim(relay_.endpoint).send()
    assert len(up.bodies) == 1 and not gate.final_calls
    assert any(r.get('detail', {}).get('reason') == 'time' for r in log_rows(tmp_path))


def test_14_enforcement_skipped_when_final_equals_a_submission(tmp_path):
    controller, ledger, gate = mech()
    ledger.add(C.Candidate(kind='candidate', ref='a1', call=1, t=1.0, answer='41', via='submit'))
    draft = fu.response([fu.message('$41.0 million')])
    with relay(tmp_path, lambda body, index: draft, controller) as (relay_, up):
        CodexSim(relay_.endpoint).send()
    assert len(up.bodies) == 1 and not gate.final_calls


def test_15_incomplete_revision_retry_keeps_splice_and_counts_no_new_round(tmp_path):
    controller, _, gate = mech()
    partial = fu.response([fu.message('4', id='msg_p')], status='incomplete', reason='max_output_tokens', id='resp_part')
    seen = Counter()

    def responder(body, index):
        if notes(body['input'], 'enforce'):
            seen['rev'] += 1
            return partial if seen['rev'] == 1 else final_msg('42', id='resp_final')
        return DRAFT

    with relay(tmp_path, responder, controller) as (relay_, up):
        sim = CodexSim(relay_.endpoint)
        with pytest.raises(SimIncomplete):
            sim.send()
        response = sim.retry()                        # Codex keeps the partial item and re-sends the turn
    assert response['id'] == 'resp_final' and len(up.bodies) == 3
    assert up.bodies[2]['input'] == up.bodies[1]['input'] + [reserialize(partial['output'][0])]
    assert summary(tmp_path)['enforce_requeries'] == 1 and len(gate.final_calls) == 1


# ---------------------------------------------------------------------------------------------
# 18-21. heartbeats, stop, controller exceptions, redaction
# ---------------------------------------------------------------------------------------------

@pytest.mark.slow
def test_18_heartbeats_flow_through_gate_rounds(tmp_path):
    controller, _, _ = mech()

    def responder(body, index):
        if notes(body['input'], 'enforce'):
            time.sleep(16)                                 # longer than the 15 s heartbeat interval
            return final_msg('42')
        return DRAFT

    with relay(tmp_path, responder, controller) as (relay_, up):
        sim = CodexSim(relay_.endpoint)
        response = sim.send()
    assert sim.heartbeats >= 1 and response['output'][0]['content'][0]['text'] == '42'
    events, _, _, _ = parse_sse(sim.wires[0])
    assert sum(e['type'] in ('response.completed', 'response.incomplete') for e in events) == 1


def test_19_stop_during_gate_forwards_nothing_and_writes_summary(tmp_path):
    holder = {}
    controller = Scripted(verdict=lambda view, response, round: C.Verdict('revise', 'check again'),
                          on_final_hook=lambda: holder['relay'].stop.set())
    with relay(tmp_path, lambda body, index: DRAFT, controller) as (relay_, up):
        holder['relay'] = relay_
        sim = CodexSim(relay_.endpoint)
        with pytest.raises(SimError):
            sim.send()
        assert (tmp_path / 'api/summary.json').exists()
        assert sim.wires and b'event: error' in sim.wires[0] and b'response.completed' not in sim.wires[0]
    assert all(api(tmp_path, n, 'transport.json')['forwarded'] is False for n in (1, 2))


def test_20_controller_exceptions_fail_open(tmp_path):
    controller = Scripted(inserts=lambda view: [C.Insert('loop_guard', 'never')], fail={'on_request', 'on_response', 'on_final'})
    with relay(tmp_path, lambda body, index: DRAFT, controller) as (relay_, up):
        sim = CodexSim(relay_.endpoint)
        sim.send()
    assert up.bodies[0]['input'] == sim.sent[0]['input'] and sim.wires[0] == up.served[0][1]
    assert summary(tmp_path)['controller_errors'] == 3 and len(up.bodies) == 1
    assert sum(row['event'] == 'controller_error' for row in log_rows(tmp_path)) == 3


def test_21_key_never_in_records_notes_or_upstream(tmp_path):
    controller = Scripted(inserts=lambda view: [C.Insert('loop_guard', 'leak ' + KEY)] if view.codex_request == 1 else [],
                          verdict=lambda view, response, round: C.Verdict('revise', 'fb ' + KEY) if round == 0 else C.Verdict('forward'),
                          fail={'on_response'})
    with relay(tmp_path, lambda body, index: final_msg('41'), controller) as (relay_, up):
        CodexSim(relay_.endpoint).send()
        reply = post_submit(relay_, {'answer': '41'})
    assert KEY not in json.dumps(reply)
    for path in (tmp_path / 'api').rglob('*'):
        if path.is_file():
            assert KEY not in path.read_text(errors='replace'), path
    assert all(KEY.encode() not in raw for raw in up.raw)
    assert any('[REDACTED]' in CR.message_text(item) for item in up.bodies[-1]['input'])


# ---------------------------------------------------------------------------------------------
# /v1/submit
# ---------------------------------------------------------------------------------------------

def post_submit(relay_, payload, raw=None):
    data = raw if raw is not None else json.dumps(payload).encode()
    request = urllib.request.Request(relay_.submit_url, data=data, headers={'Content-Type': 'application/json'},
                                     method='POST')
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return {'http': response.status, **json.loads(response.read())}
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return {'http': exc.code, **json.loads(body)}
        except ValueError:
            return {'http': exc.code}


def test_submit_route_returns_gate_result_without_internals(tmp_path):
    assert broker.SUBMIT_PATH == C.SUBMIT_PATH
    controller, ledger, gate = mech(submit_status='findings')
    with relay(tmp_path, lambda body, index: DRAFT, controller) as (relay_, _):
        assert relay_.submit_url.endswith('/v1/submit') and relay_.submit_url.startswith('http://127.0.0.1:')
        reply = post_submit(relay_, {'answer': '41', 'alt': '42', 'reading': 'FY', 'keep': None})
    assert reply['http'] == 200 and reply['status'] == 'findings' and reply['candidate_ref'] == 'a1'
    assert reply['findings'] == [{'rule': 'recompute_final', 'text': gate.message,
                                  'offer': {'answer': '42', 'derivation': 'k1 re-run with c3'}}]
    assert 'bindings' not in json.dumps(reply) and 'never shown' not in json.dumps(reply)
    assert gate.submits == [({'answer': '41', 'alt': '42', 'reading': 'FY'}, 0)]
    assert [c.via for c in ledger.candidates()] == ['submit']
    s = summary(tmp_path)
    assert s['submits'] == 1 and s['gate_rounds'] == 1


@pytest.mark.parametrize('payload,raw,code', [
    (None, b'not json', 400), ([1], None, 400), ({'answer': '1', 'evil': 'x'}, None, 400),
    ({'answer': 5}, None, 400), ({'answer': ' '}, None, 400), ({'answer': 'x' * 9000}, None, 413),
    (None, b'{"answer": "' + b'1' * 70000 + b'"}', 413)])
def test_submit_route_rejects_bad_payloads(tmp_path, payload, raw, code):
    controller, _, gate = mech()
    with relay(tmp_path, lambda body, index: DRAFT, controller) as (relay_, _):
        reply = post_submit(relay_, payload, raw)
    assert reply['http'] == code and not gate.submits


def test_submit_invalid_payload_is_not_a_gate_round(tmp_path):
    class Rejecting(Scripted):
        def on_submit(self, payload):
            return C.GateResult('findings', [], 'rejected: answer has no value. Usage: submit --answer "<A>"')
    with relay(tmp_path, lambda body, index: DRAFT, Rejecting()) as (relay_, _):
        reply = post_submit(relay_, {'answer': 'n/a'})
    s = summary(tmp_path)
    assert reply['http'] == 200 and reply['findings'] == [] and (s['gate_rounds'], s['submit_invalid']) == (0, 1)


def test_submit_route_404_without_controller(tmp_path, monkeypatch):
    up = fu.Upstream(lambda body, index: DRAFT)
    with broker.Broker(KEY, tmp_path / 'api', scheduler=scheduler(), upstream_url=up.url, **UNBOUNDED) as relay_:
        assert post_submit(relay_, {'answer': '1'})['http'] == 404
    up.close()


def test_submit_route_takes_no_response_lock(tmp_path):
    controller, _, gate = mech()
    with relay(tmp_path, lambda body, index: 'stall', controller) as (relay_, up):
        thread = threading.Thread(target=lambda: _quiet(CodexSim(relay_.endpoint).send), daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not up.bodies and time.monotonic() < deadline:
            time.sleep(0.02)
        assert up.bodies and relay_.lock.locked()               # a /v1/responses call is in flight
        begun = time.monotonic()
        reply = post_submit(relay_, {'answer': '42'})
        assert reply['http'] == 200 and time.monotonic() - begun < 2
        relay_.stop.set()
        up.closing.set()
    thread.join(timeout=10)


def _quiet(fn):
    try:
        fn()
    except Exception:
        pass


# ---------------------------------------------------------------------------------------------
# MechController units
# ---------------------------------------------------------------------------------------------

def view(kind='turn', items=(), remaining=None, request=1):
    return C.View(codex_request=request, kind=kind, codex_input=list(items), tools_offered=kind != 'compaction',
                  elapsed_s=10.0, remaining_s=remaining)


def feed_commands(controller, cmd, n, output='same'):
    items = []
    for i in range(n):
        call = fu.call(cmd, call_id=f'{cmd}-{i}')
        controller.on_response(view(), fu.response([call]), i + 1)
        items += [reserialize(call), {'type': 'function_call_output', 'call_id': call['call_id'],
                                      'output': f'Chunk ID: {i:06x}\nWall time: 0.{i} seconds\nOutput:\n{output}'}]
    return items


def test_loop_note_wording_candidates_and_escalation():
    controller, ledger, _ = mech({'loop_notes_max': 1})
    ledger.add(C.Compute(kind='compute', ref='k1', call=1, t=1, values=[41.96]))
    ledger.add(C.Candidate(kind='candidate', ref='a1', call=1, t=1, answer='41.96', via='submit'))
    items = feed_commands(controller, 'cat a', 3)
    inserts = controller.on_request(view(items=items))
    assert len(inserts) == 1 and inserts[0].reason == 'loop_guard' and inserts[0].anchor == 'tail'
    text = inserts[0].text
    assert 'cat a' in text and 'identical each time' in text and 'a1 "41.96" (k1)' in text and 'Options:' in text
    assert controller.on_request(view(items=items)) == []                    # one note per distinct command
    ledger.add(C.Observe(kind='observe', ref='c1', call=2, t=2, text='new evidence'))
    ledger.add(C.Candidate(kind='candidate', ref='a2', call=3, t=3, answer='42.5', via='final'))
    items += feed_commands(controller, 'cat b', 3, output='changing')
    text = controller.on_request(view(items=items))[0].text
    assert 'Submit now: submit --answer "42.5"' in text                       # 2nd note > loop_notes_max=1, fresh a2
    assert [e.reason for e in ledger.events if e.kind == 'note'] == ['loop_guard', 'loop_guard']


def test_ledger_splice_once_per_summary_and_not_on_compaction():
    controller, ledger, _ = mech()
    summary_item = {'type': 'message', 'id': 's1', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': CR.RESUME_PREFIX + 'sum'}]}
    assert controller.on_request(view('compaction', [summary_item])) == []
    first = controller.on_request(view('resume', [summary_item]))
    assert [(i.reason, i.anchor, i.sticky) for i in first] == [('ledger', 'summary', True)]
    assert controller.on_request(view('resume', [summary_item])) == [] and ledger.render_calls == 1
    other = dict(summary_item, id='s2', content=[{'type': 'input_text', 'text': CR.RESUME_PREFIX + 'sum 2'}])
    assert len(controller.on_request(view('resume', [other]))) == 1
    ledger.render_text = ''
    third = dict(summary_item, content=[{'type': 'input_text', 'text': CR.RESUME_PREFIX + 'sum 3'}])
    assert controller.on_request(view('resume', [third])) == []               # nothing to splice


def test_enforcement_rules():
    draft = fu.response([fu.message('The total is $1,234.5 million')])
    controller, ledger, gate = mech()
    ledger.add(C.Candidate(kind='candidate', ref='a1', call=1, t=1, answer='1234.5', via='submit'))
    assert controller.on_final(view(), draft, 0).action == 'forward' and not gate.final_calls   # same numbers
    controller, ledger, gate = mech()
    ledger.add(C.Candidate(kind='candidate', ref='a1', call=1, t=1, answer='1234.5', via='final'))
    verdict = controller.on_final(view(), draft, 0)
    assert verdict.action == 'revise' and gate.message in verdict.feedback                 # only 'final', not submitted
    assert controller.on_final(view(), draft, 1).action == 'forward'                       # cap 1 per episode
    controller, _, gate = mech()
    assert controller.on_final(view(remaining=59), draft, 0).action == 'forward' and not gate.final_calls
    controller, _, gate = mech(final_findings=False)
    assert controller.on_final(view(), draft, 0).action == 'forward' and gate.final_calls  # checked clean
    eager = CR.MechController(StubLedger(), StubGate(final_findings=False), requery_when_clean=True)
    assert eager.on_final(view(), draft, 0).action == 'revise'                             # opt-in variant
    controller, _, gate = mech()
    assert controller.on_final(view('compaction'), draft, 0).action == 'forward'
    assert controller.on_final(view(), fu.response([fu.call('ls')]), 0).action == 'forward'
    assert CR.same_answer('41.0%', '41') and not CR.same_answer('41, 42', '41') and CR.same_answer('Yes.', 'yes')


def test_echo_matches_codex_reserialization():
    output = [{'type': 'reasoning', 'id': 'rs_1', 'summary': [], 'content': [{'type': 'reasoning_text', 'text': 'deep'}]},
              {'type': 'reasoning', 'id': 'rs_2', 'summary': [{'type': 'summary_text', 'text': 's'}]},
              {'type': 'message', 'id': 'msg_1', 'status': 'completed', 'role': 'assistant',
               'content': [{'type': 'output_text', 'text': 'hi', 'annotations': [{'type': 'url_citation'}], 'logprobs': []}]},
              {'type': 'function_call', 'id': 'fc_1', 'status': 'completed', 'name': 'exec_command',
               'arguments': '{"cmd":"echo a"}', 'call_id': 'c1', 'extra_key': 'z'}]
    # observed from codex 0.155.0-alpha.9.2 (tests/test_v9_codex_live.py re-checks this against the binary)
    observed = [{'type': 'reasoning', 'id': 'rs_1', 'summary': [], 'content': [{'type': 'reasoning_text', 'text': 'deep'}],
                 'encrypted_content': None},
                {'type': 'reasoning', 'id': 'rs_2', 'summary': [{'type': 'summary_text', 'text': 's'}], 'content': None,
                 'encrypted_content': None},
                {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'hi'}]},
                {'type': 'function_call', 'id': 'fc_1', 'name': 'exec_command', 'arguments': '{"cmd":"echo a"}', 'call_id': 'c1'}]
    assert CR.echo(output) == observed
    assert [json.dumps(item) for item in CR.echo(output)] == [json.dumps(item) for item in observed]   # key order too
    assert [reserialize(item) for item in output] == observed


def test_mech_controller_is_deterministic():
    def run():
        controller, ledger, _ = mech()
        items = feed_commands(controller, 'cat a', 4)
        return [i.text for i in controller.on_request(view(items=items))], [e.to_json() for e in ledger.events]
    assert run() == run()


# ---------------------------------------------------------------------------------------------
# 22. offline replay goldens over the v8 records (read-only)
# ---------------------------------------------------------------------------------------------

GOLDEN = {'none': (35, 22, 14), 'prompt': (38, 25, 9), 'cards': (46, 29, 13)}


@pytest.mark.slow
@pytest.mark.skipif(not all((PROJECT / r).is_dir() for r in V8_ROOTS.values()), reason='v8 roots not on disk')
def test_22_replay_goldens_over_v8_api_records():
    results = {arm: CR.replay_root(str(PROJECT / rel)) for arm, rel in V8_ROOTS.items()}
    for arm, (compactions, loops, streaks) in GOLDEN.items():
        got = results[arm]
        assert got['runs'] == 133
        assert got['compactions'] == compactions, arm           # classify: tools=[] + fixed prompt
        assert got['loop_runs'] == loops, arm                   # any identical command issued a 3rd time (critic C2)
        assert got['streak_runs'] == streaks, arm               # secondary golden: consecutive streak >= 3
        assert got['resumes'] <= got['compactions']
    assert sum(r['compactions'] for r in results.values()) == 119
    again = {arm: CR.replay_root(str(PROJECT / rel)) for arm, rel in V8_ROOTS.items()}
    assert again == results                                     # deterministic


def test_22_replay_fast_subset_matches_codex_request_kind():
    """Fast check on a few v8 runs: classify agrees with Codex's own client_metadata request_kind."""
    root = PROJECT / V8_ROOTS['none']
    if not root.is_dir():
        pytest.skip('v8 root not on disk')
    checked = 0
    for run in ('010', '072'):
        for request_path in sorted((root / run / 'api').glob('[0-9][0-9][0-9]/request.json')):
            request = json.loads(request_path.read_text())
            meta = json.loads((request.get('client_metadata') or {}).get('x-codex-turn-metadata') or '{}')
            kind = CR.classify(request['input'], bool(request.get('tools')))
            assert (kind == 'compaction') == (meta.get('request_kind') == 'compaction'), request_path
            checked += 1
    assert checked > 50
    assert CR.replay_run(str(root / '010' / 'api'))['compactions'] >= 1


# ---------------------------------------------------------------------------------------------
# wave-2 integration (S7): View.call, answer span, ledger-assigned note refs, Command.tool
# ---------------------------------------------------------------------------------------------

class CallRecorder(Scripted):
    """Records View.call per hook (contracts v9.1)."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = []

    def on_request(self, view):
        self.calls.append(('request', view.codex_request, view.call))
        return super().on_request(view)

    def on_response(self, view, response, call):
        self.calls.append(('response', view.codex_request, view.call, call))
        return super().on_response(view, response, call)

    def on_final(self, view, response, round):
        self.calls.append(('final', view.codex_request, view.call, round))
        return super().on_final(view, response, round)


def test_view_call_is_the_api_number_of_the_call_the_view_concerns(tmp_path):
    revised = {'done': False}

    def verdict(view, response, round):
        if round == 0 and not revised['done']:
            revised['done'] = True
            return C.Verdict('revise', 'check it')
        return C.Verdict('forward')
    controller = CallRecorder(verdict=verdict)

    def responder(body, index):
        return fu.response([fu.call('echo a', call_id='c1')]) if index == 0 else final_msg('42')
    with relay(tmp_path, responder, controller) as (relay_, up):
        sim = CodexSim(relay_.endpoint)
        sim.send()
        sim.send()
    # request 1 -> call 1; request 2 -> draft call 2, re-query call 3 (origin controller)
    assert controller.calls == [('request', 1, 1), ('response', 1, 1, 1), ('request', 2, 2), ('response', 2, 2, 2),
                                ('final', 2, 2, 0), ('response', 2, 3, 3), ('final', 2, 3, 1)]
    assert api(tmp_path, 3, 'transport.json')['origin'] == 'controller'


class SpanPack:
    """The only pack surface MechController uses: extract_final."""
    def extract_final(self, message):
        return message.strip().splitlines()[-1].replace('Answer:', '').strip()


def test_enforcement_checks_only_the_answer_span():
    """S4-review 1: prose numbers of the final message are not part of the checked candidate."""
    draft = fu.response([fu.message('In FY2004 the total was 12.3, up from 11.1.\nAnswer: 12.3')])
    controller, ledger, gate = mech()
    controller.pack = SpanPack()
    ledger.add(C.Candidate(kind='candidate', ref='', call=1, t=1, answer='12.3', via='submit'))
    assert controller.on_final(view(), draft, 0).action == 'forward' and not gate.final_calls   # span == submission
    controller, ledger, gate = mech()
    controller.pack = SpanPack()
    assert controller.on_final(view(), draft, 0).action == 'revise'
    assert gate.final_calls == [('12.3', 0)]                                    # the span, not the prose
    # without a pack: the last non-empty line; a gate's or ledger's pack is found by itself
    controller, _, gate = mech()
    assert controller.pack is None
    controller.on_final(view(), draft, 0)
    assert gate.final_calls[-1][0] == 'Answer: 12.3'
    gate.pack = SpanPack()
    assert CR.MechController(StubLedger(), gate).pack is gate.pack
    assert CR.last_answer_line('a\n\n  b 1.5  \n\n') == 'b 1.5' and CR.last_answer_line('') == ''


def test_notes_get_ledger_assigned_refs_and_calls_follow_view_call():
    controller, ledger, _ = mech()
    ledger.add(C.Note(kind='note', ref='', call=1, t=0, reason='gate', text='[harness:gate] recorded "1".'))
    items = feed_commands(controller, 'cat a', 3)
    controller.bind_call(99)                                     # stale adapter value ...
    inserts = controller.on_request(C.View(codex_request=4, kind='turn', codex_input=items, tools_offered=True,
                                           elapsed_s=1.0, remaining_s=None, call=7))
    notes_ = [e for e in ledger.events if e.kind == 'note']
    assert len(inserts) == 1 and [n.ref for n in notes_] == ['n1', 'n2']      # no collision with the gate's note
    assert notes_[-1].call == 7 and notes_[-1].reason == 'loop_guard'          # ... View.call wins


def test_loop_guard_skips_polls_by_command_tool():
    controller, ledger, _ = mech()
    for i in range(4):                                           # never seen by on_response: only Command.tool says poll
        ledger.add(C.Command(kind='command', ref=f'x{i + 1}', call=i, t=0, cmd='{"session_id": 3}',
                             call_id=f'p{i}', tool='write_stdin', n=i + 1))
        ledger.counts['{"session_id": 3}'] += 1
    assert controller.on_request(view()) == []
    ledger.add(C.Command(kind='command', ref='x9', call=9, t=0, cmd='cat b', call_id='q', tool='exec_command', n=1))
    ledger.counts['cat b'] += 3
    assert [i.reason for i in controller.on_request(view())] == ['loop_guard']
