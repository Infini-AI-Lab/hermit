"""$0 tier-1 tests: the pinned real Codex binary against the v9 broker and a scripted fake model.

Opt-in (they launch a real binary; tests/conftest.py): `pytest -m live_codex tests/test_v9_codex_live.py`, or set
V9_LIVE_CODEX=1. Each run (S7-integrate: the v9 launch path end to end, S3 request 16):
- starts tests/fake_upstream.py (scripted responses, full SSE via `sse_full`) on 127.0.0.1 and
  `broker.Broker(upstream_url=<fake>)` with or without a controller: stub ledger/gate from test_v9_broker for
  the broker mechanics, or the real harness_core Ledger + Gate + OfficeQAPack + MechController;
- prepares the workspace with `acting.prepare_actor_workspace` (kit, AGENTS.md, marker) and runs ONE episode with
  `acting.run_actor`: the v9 argv and settings of `acting.build_settings` (multi-agent off, explicit
  model_auto_compact_token_limit=244800, include_permissions_instructions=false unless a test says otherwise),
  the v9 Seatbelt profile (reads confined to the kit/roots, writes to the workspace, TCP only to the broker port)
  and the kit environment (`submit` on controller arms, HARNESS_SUBMIT_URL derived from the endpoint).
Corpus/index are empty stand-ins (build_settings only requires them to exist) unless a test needs the real ones.
No credentials: the provider has requires_openai_auth=false and the broker key is a dummy string.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import acting  # noqa: E402
import broker  # noqa: E402
import controller_runtime as CR  # noqa: E402
import fake_upstream as fu  # noqa: E402
import paths  # noqa: E402
import rate_control  # noqa: E402
from harness_core import contracts as C  # noqa: E402
from test_v9_broker import StubGate, StubLedger, notes  # noqa: E402

CODEX = paths.CODEX
SANDBOX = Path('/usr/bin/sandbox-exec')
KEY = 'dummy-live-key-not-a-secret'
PROJECT = ROOT.parent
REAL_CORPUS = PROJECT / 'opus-officeqa-133/corpus'
REAL_INDEX = PROJECT / 'frameworks-v9/officeqa-tables-index/tables.sqlite'

pytestmark = [pytest.mark.live_codex,
              pytest.mark.skipif(not CODEX.is_file() or not SANDBOX.is_file(), reason='pinned codex or sandbox-exec missing')]
needs_officeqa = pytest.mark.skipif(not (REAL_CORPUS.is_dir() and REAL_INDEX.is_file()),
                                    reason='needs the OfficeQA corpus and the frameworks-v9 tables index')


class Run(dict):
    __getattr__ = dict.__getitem__


@contextmanager
def codex_run(tmp_path, responder, *, controller=None, arm=None, permissions=False, compact_limit=None,
              prompt='Answer the question.', **broker_options):
    """One real `codex exec` episode through acting.run_actor behind the broker. `compact_limit` overrides
    acting.V9_SETTINGS['model_auto_compact_token_limit'] for this episode only (to force a compaction)."""
    root = tmp_path.resolve()
    workspace, records = root / 'ws', root / 'records'
    corpus, index = root / 'corpus', root / 'index.sqlite'
    corpus.mkdir(parents=True)
    index.write_bytes(b'')
    arm = C.STANDARD_ARMS[arm or ('mech-static' if controller is not None else 'plain')]
    acting.prepare_actor_workspace(workspace, None, common_instructions='# Task workspace\nAnswer with one line.\n',
                                   arm=arm)
    up = fu.Upstream(responder)
    scheduler = rate_control.RateScheduler(max_in_flight=float('inf'), min_interval=0, backoffs=(0,))
    options = {'max_completion': None, 'max_response': None, 'request_timeout': None, 'max_attempts': None}
    options.update(broker_options)
    saved = dict(acting.V9_SETTINGS)
    if compact_limit is not None:
        acting.V9_SETTINGS['model_auto_compact_token_limit'] = compact_limit
    try:
        with broker.Broker(KEY, root / 'api', scheduler=scheduler, upstream_url=up.url, controller=controller,
                           **options) as relay:
            port = relay.server.server_port
            begun = time.monotonic()
            result = acting.run_actor(workspace, records, prompt, corpus, index, timeout=180, endpoint=relay.endpoint,
                                      arm=arm, permissions_instructions=permissions)
            seconds = time.monotonic() - begun
        acting.V9_SETTINGS.clear()
        acting.V9_SETTINGS.update(saved)
        events = [json.loads(line) for line in (records / 'events.jsonl').read_text().splitlines()
                  if line.strip().startswith('{')]
        final = workspace / 'final.md'
        yield Run(rc=result['exit_code'], status=result['status'], result=result, events=events, seconds=seconds,
                  stderr=(records / 'stderr.log').read_text(), up=up, root=root, workspace=workspace,
                  invocation=json.loads((records / 'invocation.json').read_text()),
                  final=final.read_text() if final.exists() else None, port=port,
                  upstream_port=int(up.url.split(':')[2].split('/')[0]))
    finally:
        acting.V9_SETTINGS.clear()
        acting.V9_SETTINGS.update(saved)
        up.close()


def agent_messages(run):
    return [e['item']['text'] for e in run.events if e.get('type') == 'item.completed'
            and e.get('item', {}).get('type') == 'agent_message']


def commands(run):
    return [e['item'] for e in run.events if e.get('type') == 'item.completed'
            and e.get('item', {}).get('type') == 'command_execution']


def record(run, number, name):
    return json.loads((run.root / 'api' / f'{number:03}' / name).read_text())


def mech(**gate_options):
    ledger, gate = StubLedger(), StubGate(**gate_options)
    return CR.MechController(ledger, gate), ledger, gate


# ---------------------------------------------------------------------------------------------


def test_tool_call_then_final_and_echo_fidelity(tmp_path):
    first = fu.response([fu.reasoning('plan', id='rs_1'),
                         {'type': 'reasoning', 'id': 'rs_2', 'summary': [], 'content': [{'type': 'reasoning_text', 'text': 'deep'}]},
                         fu.message('Let me look.', id='msg_1'),
                         dict(fu.call('sleep 0.5; echo hello', call_id='cA', id='fc_A'), extra_key='dropped'),
                         fu.call('printf x', call_id='cB', id='fc_B')])

    def responder(body, index):
        return first if index == 0 else fu.response([fu.message('FINAL 42')])

    with codex_run(tmp_path, responder) as run:
        pass
    assert run.rc == 0 and run.final.strip() == 'FINAL 42', run.stderr[-2000:]
    assert [c['aggregated_output'] for c in commands(run)] == ['x', 'hello\n']   # parallel: the slow call finishes last
    second = run.up.bodies[1]['input']
    n = len(run.up.bodies[0]['input'])
    assert second[:n] == run.up.bodies[0]['input']
    # Codex re-serialises the provider items exactly as controller_runtime.echo predicts (key order included)
    assert [json.dumps(item) for item in second[n:n + 5]] == [json.dumps(item) for item in CR.echo(first['output'])]
    outputs = second[n + 5:]
    # ... but its output comes first: outputs follow the calls' order, not completion order
    assert [o['type'] for o in outputs] == ['function_call_output'] * 2 and [o['call_id'] for o in outputs] == ['cA', 'cB']
    assert all(re.match(r'Chunk ID: [0-9a-f]{6}\nWall time: [0-9.]+ seconds\nProcess exited with code 0\n'
                        r'Original token count: \d+\nOutput:\n', o['output']) for o in outputs)
    assert all(o['id'].startswith('fco_') for o in outputs)


def test_gate_revise_makes_final_md_the_revision(tmp_path):
    controller, _, gate = mech()

    def responder(body, index):
        return fu.response([fu.message('FINAL 42')]) if notes(body['input'], 'enforce') else \
            fu.response([fu.message('The answer is 41.')])

    with codex_run(tmp_path, responder, controller=controller) as run:
        pass
    assert run.rc == 0 and run.final.strip() == 'FINAL 42', run.stderr[-2000:]
    assert agent_messages(run) == ['FINAL 42']                      # the draft never reached Codex
    assert len(run.up.bodies) == 2 and len(gate.final_calls) == 1
    assert record(run, 1, 'transport.json')['forwarded'] is False
    assert record(run, 2, 'transport.json')['origin'] == 'controller'


def test_tool_call_revision_is_executed_and_spliced(tmp_path):
    controller, _, _ = mech()

    def responder(body, index):
        if any(item.get('call_id') == 'cR' for item in body['input'] if item.get('type') == 'function_call_output'):
            return fu.response([fu.message('FINAL 42 (checked)')])
        if notes(body['input'], 'enforce'):
            return fu.response([fu.call('echo revised', call_id='cR', id='fc_R')])
        return fu.response([fu.message('The answer is 41.')])

    with codex_run(tmp_path, responder, controller=controller) as run:
        pass
    assert run.rc == 0 and run.final.strip() == 'FINAL 42 (checked)', run.stderr[-2000:]
    assert [c['aggregated_output'] for c in commands(run)] == ['revised\n']
    revision_input, next_input = run.up.bodies[1]['input'], run.up.bodies[2]['input']
    assert next_input[:len(revision_input)] == revision_input       # draft + feedback spliced back in
    tail = next_input[len(revision_input):]
    assert [t['type'] for t in tail] == ['function_call', 'function_call_output'] and tail[0]['call_id'] == 'cR'
    inserted = record(run, 3, 'controller.json')['inserted']
    assert [(r['reason'], r['reapplied']) for r in inserted] == [('enforce', True), ('enforce', True)]
    assert agent_messages(run) == ['FINAL 42 (checked)']


def test_loop_note_splice_accepted_and_cache_prefix_consistent(tmp_path):
    controller, _, _ = mech(final_findings=False)

    def responder(body, index):
        return fu.response([fu.call('printf same')]) if index < 4 else fu.response([fu.message('FINAL 7')])

    with codex_run(tmp_path, responder, controller=controller) as run:
        pass
    assert run.rc == 0 and run.final.strip() == 'FINAL 7', run.stderr[-2000:]
    counts = [len(notes(body['input'], 'loop_guard')) for body in run.up.bodies]
    assert counts == [0, 0, 0, 1, 1]
    for before, after in zip(run.up.bodies, run.up.bodies[1:]):
        assert after['input'][:len(before['input'])] == before['input']
        assert after['instructions'] == before['instructions'] and after['tools'] == before['tools']
    assert json.loads((run.root / 'api/summary.json').read_text())['inserts_by_reason'] == {'loop_guard': 1}


def test_injected_error_codex_resends_identical_input(tmp_path):
    controller, ledger, _ = mech(final_findings=False)
    state = {'failed': False}

    def responder(body, index):
        if index == 0:
            return fu.response([fu.call('echo x')])
        if not state['failed']:
            state['failed'] = True
            return ('http', 400, {'error': {'message': 'injected'}})
        return fu.response([fu.message('FINAL after retry')])

    with codex_run(tmp_path, responder, controller=controller) as run:
        pass
    assert run.rc == 0 and run.final.strip() == 'FINAL after retry', run.stderr[-2000:]
    failed, retried = run.up.bodies[1], run.up.bodies[2]
    assert failed['input'] == retried['input']                     # identical history
    assert {k for k in failed if failed[k] != retried[k]} == set()  # parsed bodies equal
    assert any(e.get('type') == 'error' and 'Reconnecting' in e.get('message', '') for e in run.events)
    summary = json.loads((run.root / 'api/summary.json').read_text())
    assert summary['retries_replayed'] == 1
    # finding: the raw bytes differ only in the key order of client_metadata (a Rust HashMap)
    raw_failed, raw_retried = run.up.raw[1], run.up.raw[2]
    input_part = lambda raw: raw.split(b'"input": ', 1)[1].split(b', "tools": ', 1)[0]
    assert input_part(raw_failed) == input_part(raw_retried)


def test_incomplete_response_codex_keeps_partial_items_and_retries(tmp_path):
    partial = fu.response([fu.message('partial', id='msg_p')], status='incomplete', reason='max_output_tokens')

    def responder(body, index):
        return partial if index == 0 else fu.response([fu.message('FINAL complete')])

    with codex_run(tmp_path, responder) as run:
        pass
    assert run.rc == 0 and run.final.strip() == 'FINAL complete'
    first, second = run.up.bodies[0]['input'], run.up.bodies[1]['input']
    assert second == first + CR.echo(partial['output'])            # not an identical re-send
    assert agent_messages(run) == ['partial', 'FINAL complete']


def test_permissions_instructions_toggle_changes_only_the_permissions_part(tmp_path):
    final = lambda body, index: fu.response([fu.message('FINAL 1')])
    with codex_run(tmp_path / 'kept', final, permissions=True) as kept:
        pass
    with codex_run(tmp_path / 'off', final) as off:                    # the v9 default: permissions text off
        pass
    assert kept.invocation['settings'].get('include_permissions_instructions') is None
    assert off.invocation['settings']['include_permissions_instructions'] is False
    a, b = kept.up.bodies[0], off.up.bodies[0]
    assert a['instructions'] == b['instructions'] and a['tools'] == b['tools']
    # multi-agent off (v9 decision): no multi_agent_v1 tool
    assert [t['name'] for t in a['tools']] == ['exec_command', 'write_stdin', 'request_user_input', 'view_image']
    dev_a, dev_b = a['input'][0]['content'], b['input'][0]['content']
    assert len(a['input']) == len(b['input']) == 3 and len(dev_a) == 2 and len(dev_b) == 1
    assert dev_a[0]['text'].replace(str(kept.root), 'ROOT') == dev_b[0]['text'].replace(str(off.root), 'ROOT')
    removed = dev_a[1]['text']
    assert removed.startswith('<permissions instructions>') and 'Network access is enabled' in removed
    for x, y in zip(a['input'][1:], b['input'][1:]):
        assert [p['text'].replace(str(kept.root), 'ROOT') for p in x['content']] == \
               [p['text'].replace(str(off.root), 'ROOT') for p in y['content']]


def test_tool_output_truncation_size(tmp_path):
    loop = "i=0; while [ $i -lt 6000 ]; do printf '%05d-abcdefghij\\n' $i; i=$((i+1)); done"

    def responder(body, index):
        return fu.response([fu.call(loop)]) if index == 0 else fu.response([fu.message('FINAL')])

    with codex_run(tmp_path, responder) as run:
        pass
    assert run.rc == 0
    output = run.up.bodies[1]['input'][-1]['output']
    produced = len(commands(run)[0]['aggregated_output'])
    marker = re.search(r'…(\d+) chars truncated…', output)
    assert produced == 102_000 and marker
    kept = produced - int(marker.group(1))
    assert kept == 10_000                                            # Codex keeps 10,000 chars (head + tail)
    assert 'Warning: truncated output (original token count: 25500)' in output
    assert 10_000 < len(output) < 10_400


def test_sandbox_allows_only_the_broker_port(tmp_path):
    holder = {}

    def responder(body, index):
        if index == 0:
            port = holder['port']
            return fu.response([fu.call(f'/usr/bin/curl -s -m 3 -o /dev/null http://127.0.0.1:{port}/ ; echo rc=$?')])
        return fu.response([fu.message('FINAL')])

    probe = fu.Upstream(lambda body, index: fu.response([]))       # a second local port that must be unreachable
    holder['port'] = probe.url.split(':')[2].split('/')[0]
    try:
        with codex_run(tmp_path, responder) as run:
            pass
    finally:
        probe.close()
    assert run.rc == 0
    output = commands(run)[0]['aggregated_output']
    assert 'rc=0' not in output and 'rc=' in output and not probe.bodies


def compaction_responder(big, *, first=1500, after='echo after', final='FINAL 42'):
    """turn (usage `first`) -> turn (usage `big`) -> Codex compacts iff its limit is crossed -> resume -> final."""
    def responder(body, index):
        if not body.get('tools'):
            return fu.response([fu.message('**Handoff Summary**\nran echo step0; candidate 42')], use=fu.usage(3000, 50))
        resumed = any(CR.is_summary(item) for item in body['input'])
        calls = sum(1 for item in body['input'] if item.get('type') == 'function_call')
        if not resumed and calls < 2:
            # the first request reports a small usage: below the limit, no compaction yet
            return fu.response([fu.call(f'echo step{calls} {calls + 7}.5')], use=fu.usage(first if index == 0 else big, 20))
        if not resumed:
            return fu.response([fu.message(final)], use=fu.usage(1000, 20))
        return fu.response([fu.call(after)], use=fu.usage(1000, 20)) if calls == 0 else \
            fu.response([fu.message(final)], use=fu.usage(1000, 20))
    return responder


def check_compacted_once(run):
    kinds = [record(run, n, 'transport.json')['kind'] for n in range(1, len(run.up.bodies) + 1)]
    assert kinds == ['turn', 'turn', 'compaction', 'resume', 'turn'], kinds
    compaction = run.up.bodies[2]
    assert compaction['tools'] == [] and CR.is_compaction_prompt(compaction['input'][-1])
    resume = run.up.bodies[3]['input']
    assert CR.is_summary(resume[3]) and notes(resume, 'ledger') == [4] and len(resume) == 5
    assert run.up.bodies[4]['input'][4] == resume[4]                 # the splice is frozen
    assert any('Heads up' in (e.get('item', {}).get('message') or '') for e in run.events)
    meta = [json.loads(b['client_metadata']['x-codex-turn-metadata']) for b in run.up.bodies]
    assert [m['request_kind'] for m in meta] == ['turn', 'turn', 'compaction', 'turn', 'turn']
    assert [m['window_number'] for m in meta] == [0, 0, 0, 1, 1]
    return resume


@pytest.mark.parametrize('mode', ['inflated_usage', 'low_limit'])
def test_forced_compaction_gets_one_frozen_ledger_splice(tmp_path, mode):
    """inflated_usage: the v9 settings unchanged (explicit limit 244,800), a reported 300,000 compacts;
    low_limit: the same settings with the limit lowered to 2,000 for this episode, a reported 5,000 compacts."""
    controller, ledger, _ = mech(final_findings=False)
    limit, big = (None, 300_000) if mode == 'inflated_usage' else (2000, 5_000)
    with codex_run(tmp_path, compaction_responder(big), controller=controller, compact_limit=limit) as run:
        pass
    assert run.rc == 0 and run.final.strip() == 'FINAL 42', run.stderr[-2000:]
    assert run.invocation['settings']['model_auto_compact_token_limit'] == (limit or 244800)
    check_compacted_once(run)
    assert ledger.render_calls == 1


@pytest.mark.parametrize('big, compacts', [(240_000, False), (250_000, True)])
def test_explicit_244800_limit_is_the_compaction_threshold(tmp_path, big, compacts):
    """The declared model_auto_compact_token_limit=244800 is accepted by the real Codex (strict config) and is
    the effective threshold: a reported usage just below it does not compact, just above it does."""
    with codex_run(tmp_path, compaction_responder(big)) as run:
        pass
    assert run.rc == 0 and run.final.strip() == 'FINAL 42', run.stderr[-2000:]
    assert run.invocation['settings']['model_auto_compact_token_limit'] == 244800
    assert '-c' in run.invocation['argv'] and 'model_auto_compact_token_limit=244800' in run.invocation['argv']
    meta = [json.loads(b['client_metadata']['x-codex-turn-metadata']) for b in run.up.bodies]
    assert ('compaction' in [m['request_kind'] for m in meta]) is compacts, meta
    assert len(run.up.bodies) == (5 if compacts else 3)


# ---------------------------------------------------------------------------------------------
# The real harness_core ledger + gate + OfficeQA pack behind the broker (S7 integration)
# ---------------------------------------------------------------------------------------------

def real_controller(root, question):
    from harness_core.gate import Gate
    from harness_core.ledger import Ledger
    from harness_core.rules import core_seed_rules
    from harness_core.runner import make_runner
    from packs.officeqa.pack import OfficeQAPack
    pack = OfficeQAPack(REAL_CORPUS, REAL_INDEX)
    api = Path(root).resolve() / 'api'
    ledger = Ledger(pack, api / 'ledger.jsonl')
    gate = Gate(pack, core_seed_rules() + pack.seed_rules(), None, make_runner(), question, api_dir=api)
    return CR.MechController(ledger, gate), ledger, gate


@needs_officeqa
def test_small_limit_compaction_splices_the_real_ledger(tmp_path):
    """The ledger splice path with the real Ledger: a lowered compaction limit forces one compaction; the
    resume request carries '[harness:ledger] ' + Ledger.render() right after the summary, frozen afterwards."""
    controller, ledger, _ = real_controller(tmp_path, 'What was the value in 1990?')
    with codex_run(tmp_path, compaction_responder(5_000, after='echo candidate 42'), controller=controller,
                   compact_limit=2000) as run:
        pass
    assert run.rc == 0 and run.final.strip() == 'FINAL 42', run.stderr[-2000:]
    resume = check_compacted_once(run)
    spliced = [e for e in ledger.events if e.kind == 'note' and e.reason == 'ledger']
    assert len(spliced) == 1 and CR.message_text(resume[4]) == '[harness:ledger] ' + spliced[0].text
    assert spliced[0].text.startswith('Ledger (recorded by the harness') and 'step0 7.5' in spliced[0].text
    compactions = [e for e in ledger.events if e.kind == 'compaction']
    assert [(z.request_call, z.summary_call) for z in compactions] == [(3, 4)]
    assert len(spliced[0].text.encode()) <= C.POLICY_DEFAULTS['ledger_splice_bytes']
    # the ledger file folds to the in-memory state (what offline replay reads)
    from harness_core.ledger import load_events
    assert [e.ref for e in load_events(tmp_path.resolve() / 'api/ledger.jsonl')] == [e.ref for e in ledger.events]
    commands_ = [e for e in ledger.events if e.kind == 'command']
    assert [c.tool for c in commands_] == ['exec_command'] * 3 and commands_[0].raw_cmd == 'echo step0 7.5'
    assert json.loads((tmp_path.resolve() / 'api/summary.json').read_text())['inserts_by_reason'] == {'ledger': 1}


@needs_officeqa
def test_submit_round_trip_through_the_kit_wrapper(tmp_path):
    """L4 end to end: the model runs the kit's `submit` (frozen python, v9 sandbox, HARNESS_SUBMIT_URL derived
    from the endpoint) -> broker /v1/submit -> MechController -> Gate over the real ledger -> the message is the
    command output; the final message equals the submission, so no enforcement re-query happens."""
    controller, ledger, gate = real_controller(tmp_path, 'What was the value in 1990?')

    def responder(body, index):
        return [fu.response([fu.call('echo value 42')]), fu.response([fu.call('submit --answer "42"')]),
                fu.response([fu.message('42')])][min(index, 2)]
    with codex_run(tmp_path, responder, controller=controller) as run:
        pass
    assert run.rc == 0 and run.final.strip() == '42', run.stderr[-2000:]
    submit_output = [i for i in run.up.bodies[2]['input'] if i.get('type') == 'function_call_output'][-1]['output']
    assert '[harness:gate] recorded "42". End your turn with exactly this answer' in submit_output
    assert 'Process exited with code 0' in submit_output
    cands = ledger.candidates()
    assert [(c.via, c.answer) for c in cands] == [('submit', '42')]
    assert [n.reason for n in ledger.events if n.kind == 'note'] == ['gate']
    assert not any('harness:gate' in e.text for e in ledger.events if e.kind == 'observe')   # never evidence
    summary = json.loads((tmp_path.resolve() / 'api/summary.json').read_text())
    assert summary['submits'] == 1 and summary.get('enforce_requeries', 0) == 0 and len(run.up.bodies) == 3
    assert (tmp_path.resolve() / 'api/checks/001.json').is_file() and gate.checks == 1
    assert run.invocation['settings']['shell_environment_policy.set'][C.SUBMIT_ENV].endswith(f':{run.port}/v1/submit')


@needs_officeqa
def test_real_gate_enforcement_checks_the_answer_span_and_records_one_note(tmp_path):
    """An unsubmitted final with prose: the controller checks only pack.extract_final's span, the real gate
    finds the count mismatch, the broker re-queries once, final.md is the revision; the ledger holds the
    final Candidate (the span) and exactly one 'enforce' Note (on the re-query's call; the gate adds none)."""
    question = 'What were the receipts in 1990 and 1991? Report both values.'
    controller, ledger, gate = real_controller(tmp_path, question)

    def responder(body, index):
        if notes(body['input'], 'enforce'):
            return fu.response([fu.message('[41, 43]')])
        return fu.response([fu.message('In 1990 the receipts were 41 and in 1991 they were 43.\nFinal answer: 41')])
    with codex_run(tmp_path, responder, controller=controller) as run:
        pass
    assert run.rc == 0 and run.final.strip() == '[41, 43]', run.stderr[-2000:]
    assert agent_messages(run) == ['[41, 43]']                              # the draft never reached Codex
    assert [(c.via, c.answer, c.call) for c in ledger.candidates()] == [('final', '41', 1)]
    enforce = [n for n in ledger.events if n.kind == 'note']
    assert [(n.reason, n.call) for n in enforce] == [('enforce', 2)] and enforce[0].ref == 'n1'
    assert 'The question asks for 2 value(s); your answer has 1.' in enforce[0].text
    feedback = CR.message_text(run.up.bodies[1]['input'][-1])
    assert feedback == '[harness:enforce] ' + enforce[0].text
    assert record(run, 2, 'transport.json')['origin'] == 'controller'
    check = json.loads((tmp_path.resolve() / 'api/checks/001.json').read_text())
    assert check['kind'] == 'final' and check['candidate']['answer'] == '41' and check['result']['status'] == 'findings'
