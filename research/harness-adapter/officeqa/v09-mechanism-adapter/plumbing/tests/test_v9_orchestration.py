"""S6 orchestration tests: arms, question order, barrier, notes, README/AGENTS composition, W0 budgets,
the campaign loop (seal -> release -> learn -> next adapter; resume), the audit on synthetic roots, cost.

Everything here is $0 and offline: synthetic corpora and answers, a fake broker/actor (or the real broker
driven by tests/codexsim.py against tests/fake_upstream.py), fake learners, an injected scorer. No real
reference answer, grader output or benchmark question is read. The one live-Codex test is opt-in
(`-m live_codex`): the pinned Codex binary against a local fake upstream, $0.
"""
from __future__ import annotations

import contextlib
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import types
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import acting  # noqa: E402
import arms  # noqa: E402
import barrier as B  # noqa: E402
import campaign as CP  # noqa: E402
import cost  # noqa: E402
import data  # noqa: E402
import experiment  # noqa: E402
import full_audit  # noqa: E402
import kit  # noqa: E402
import notes_render as NR  # noqa: E402
import report  # noqa: E402
from harness_core import contracts as C  # noqa: E402
from harness_core import rules as R  # noqa: E402

MODEL = 'accounts/fireworks/models/glm-5p3-flash'
KEY = 'dummy-orchestration-key-not-a-secret'
SCORER = lambda reference, predicted, tolerance=0.0: float(reference.strip() == (predicted or '').strip())  # noqa: E731

# ---------------------------------------------------------------------------------------------
# Synthetic data
# ---------------------------------------------------------------------------------------------

BULLETIN = '''TREASURY BULLETIN {issue}

Table FFO-1. - Summary of Fiscal Operations
(In millions of dollars)

| Fiscal year | Receipts | Outlays | Surplus or deficit |
|---|---|---|---|
| {y1} | 1,991,082 | 1,862,846 | 128,236 |
| {y2} | 1,853,136 | 2,010,894 | -157,758 |
'''
QUESTIONS = ['What were total federal receipts in fiscal year 2001 in millions of dollars?',
             'What were federal outlays in fiscal year 2002?',
             'By how much did the public debt change between 2001 and 2002?',
             'What was the surplus in fiscal year 2001?',
             'Report the receipts and outlays for fiscal year 2002 as a list.',
             'What share of outlays did receipts cover in 2002, as a percent?',
             'Which fiscal year had a deficit?']
TASK_IDS = ['UID0001', 'UIDT002', 'UIDT003', 'UID0004', 'UIDT005', 'UIDT006', 'UIDT007']   # 2 issue-47 ids


def code_copy(tmp_factory) -> Path:
    """A private copy of the code the freeze hashes (so concurrent edits elsewhere cannot race a test)."""
    target = tmp_factory.mktemp('code') / 'plumbing'
    ignore = shutil.ignore_patterns('__pycache__', '.pytest_cache', '*.pyc')
    target.mkdir()
    for path in ROOT.glob('*.py'):
        shutil.copyfile(path, target / path.name)
    for name in ('PROTOCOL.md', 'PREREG.md'):
        if (ROOT / name).is_file():
            shutil.copyfile(ROOT / name, target / name)
    for name in ('harness_core', 'packs', 'adapters'):
        shutil.copytree(ROOT / name, target / name, ignore=ignore)
    return target


@pytest.fixture(scope='session')
def code(tmp_path_factory):
    return code_copy(tmp_path_factory)


def make_spec(arm_name, *, repeat=1, barrier=None, root='root', pack='officeqa', seed='test-order-seed'):
    config = {'profile': 'v9', 'arm': arm_name, 'repeat': repeat, 'pack': pack, 'order_seed': seed}
    if barrier is not None:
        config['barrier'] = barrier
    return arms.parse_config(config, root_name=root)


def make_prepared(root: Path, spec, *, n=7):
    """root/prepared in the v9 layout with a 2-document synthetic corpus and synthetic answers."""
    prepared = root / 'prepared'
    corpus = prepared / 'agent/corpus'
    corpus.mkdir(parents=True)
    for issue, years in (('2002_03', (2001, 2002)), ('2003_03', (2002, 2003))):
        (corpus / f'treasury_bulletin_{issue}.txt').write_text(BULLETIN.format(issue=issue, y1=years[0], y2=years[1]))
    index_meta = data._tools.build_index(corpus, prepared / 'agent/corpus.sqlite', expected_documents=2)
    (prepared / 'index-metadata.json').write_text(json.dumps(index_meta, indent=2) + '\n')
    ids, questions = TASK_IDS[:n], QUESTIONS[:n]
    if n > len(TASK_IDS):
        extra = n - len(TASK_IDS)
        ids = TASK_IDS + [f'UIDX{i:03}' for i in range(extra)]
        questions = QUESTIONS + [f'What was synthetic amount number {i} in fiscal year 2001?' for i in range(extra)]
    (prepared / 'agent/tasks.jsonl').write_text(''.join(json.dumps({'task_id': i, 'question': q}) + '\n'
                                                        for i, q in zip(ids, questions)))
    private = prepared / 'private'
    private.mkdir()
    (private / 'answers.jsonl').write_text(''.join(json.dumps({'task_id': i, 'answer': f'A-{i}'}) + '\n' for i in ids))
    rows = [{'filename': p.name, 'sha256': data.sha256(p), 'bytes': p.stat().st_size} for p in sorted(corpus.glob('*.txt'))]
    (private / 'corpus_manifest.json').write_text(json.dumps(rows))
    from packs.officeqa.tools import otables
    source = root / 'tables-src' / 'tables.sqlite'
    otables.build_index(corpus, source)
    tables = data.install_tables_index(prepared, source)
    base = {'selection': {'seed': data.SEED}, 'sample_count': n, 'task_ids': ids,
            'tasks_sha256': data.sha256(prepared / 'agent/tasks.jsonl'),
            'answers_sha256': data.sha256(private / 'answers.jsonl'),
            'corpus_manifest_sha256': data.sha256(private / 'corpus_manifest.json'), 'corpus_document_count': 2,
            'index_sha256': data.sha256(prepared / 'agent/corpus.sqlite'),
            'index_metadata_sha256': data.sha256(prepared / 'index-metadata.json'),
            'grader': {'path': 'reward.py', 'note': 'synthetic test; the scorer is injected'}, 'fixture_only': True}
    manifest = data.manifest_v9(base, spec, tables=tables, actor_seconds=experiment.ACTOR_SECONDS)
    (prepared / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def make_root(tmp_path, arm_name, code, *, name='root', barrier=None, n=7):
    root = tmp_path / name
    root.mkdir()
    spec = make_spec(arm_name, barrier=barrier, root=name)
    make_prepared(root, spec, n=n)
    return root, spec, CP.Layout(root=root, code=code)

# ---------------------------------------------------------------------------------------------
# Fake broker / actor (records in the real layouts; the controller arms get the real controller)
# ---------------------------------------------------------------------------------------------

UPSTREAM = full_audit.UPSTREAM
LIMITS = {'requests': None, 'completion_tokens': None, 'per_response': None, 'attempts_per_request': None,
          'request_timeout_seconds': None}
REASONING = {'effort': 'high', 'summary': 'auto'}
USAGE = {'input_tokens': 1000, 'input_tokens_details': {'cached_tokens': 200}, 'output_tokens': 50}


class FakeBroker:
    local = threading.local()          # the broker of the episode running on this thread

    def __init__(self, key, api, *, controller=None, actor_deadline=None, **options):
        self.api, self.controller, self.options = Path(api), controller, options
        self.endpoint = 'http://127.0.0.1:9/v1'
        self.requeries = 0

    def __enter__(self):
        self.api.mkdir(parents=True, exist_ok=True)
        FakeBroker.local.current = self
        return self

    def __exit__(self, *exc):
        calls = [('codex', None)] + [('controller', 1)] * self.requeries
        for number, (origin, parent) in enumerate(calls, start=1):
            call = self.api / f'{number:03}'
            call.mkdir()
            body = {'model': MODEL, 'reasoning': REASONING, 'input': [], 'temperature': self.options.get('temperature', 0)}
            if self.options.get('top_p') is not None:          # the real broker adds top_p only when declared
                body['top_p'] = self.options['top_p']
            (call / 'request.json').write_text(json.dumps(body))
            meta = {'upstream': UPSTREAM, 'native_request_settings': {'reasoning': REASONING},
                    'attempts': [{'dispatched': True, 'usage': USAGE}], 'usage': USAGE}
            if self.controller is not None:
                meta.update(origin=origin, kind='turn', parent=parent, codex_request=1,
                            codex_input_sha256='0' * 64, forwarded=origin == 'controller' or not self.requeries)
            (call / 'transport.json').write_text(json.dumps(meta))
        summary = {'limits': LIMITS, 'calls': len(calls), 'usage': USAGE, 'failed': False, 'call_failures': 0,
                   'wire_attempts': len(calls), 'transport_failed': False}
        if self.controller is not None:
            summary.update({k: 0 for k in CP.CONTROLLER_FIELDS})
            summary.update(submits=1, codex_requests=1, controller_calls=self.requeries, inserts_by_reason={})
        (self.api / 'summary.json').write_text(json.dumps(summary))
        FakeBroker.local.current = None
        return False


def fake_run_actor(answer_for):
    def run(workspace, records, prompt, corpus, index, *, timeout, endpoint, arm, env_extra, data, permissions_instructions,
            temperature=None, top_p=None):
        workspace, records = Path(workspace), Path(records)
        records.mkdir(parents=True)
        settings = acting.build_settings(workspace, records, corpus, index, None, endpoint=endpoint, arm=arm,
                                         env_extra=env_extra, data=data, permissions_instructions=permissions_instructions)
        marker = json.loads((workspace / acting.MARKER).read_text())
        question = prompt.split('Question:\n', 1)[1]
        answer = answer_for(question, workspace)
        relay = getattr(FakeBroker.local, 'current', None)
        if arm.controller and relay is not None and answer:
            relay.controller.on_submit({'answer': answer})
        (records / 'invocation.json').write_text(json.dumps({
            'arm': asdict(arm), 'timeout_seconds': timeout, 'settings': settings, 'temperature': temperature, 'top_p': top_p,
            'frozen_runtime': kit.verify_frozen(deep=False), 'permissions_instructions': permissions_instructions}))
        (workspace / 'final.md').write_text(answer + '\n')
        (records / 'final.md').write_text(answer + '\n')
        result = {'status': 'completed', 'timed_out': False, 'elapsed_seconds': 1.0, 'terminal_event': 'turn.completed',
                  'kit_after': {'intact': True, 'kit_sha256': kit.verify_kit(workspace, marker['kit'])},
                  'agents_md_bytes': marker['agents_md_bytes'], 'project_doc_max_bytes': marker['project_doc_max_bytes'],
                  'completed_item_counts': {'command_execution': 1, 'agent_message': 1}}
        (records / 'result.json').write_text(json.dumps(result))
        (records / 'events.jsonl').write_text(json.dumps({'type': 'item.completed', 'item': {
            'type': 'agent_message', 'text': answer}}) + '\n' + json.dumps({'type': 'turn.completed'}) + '\n')
        return result
    return run


def default_answer(question, workspace):
    """Right for every question except #3 and #6 (wrong), right on #6 once the learned note is served."""
    task = next(i for i, q in zip(TASK_IDS, QUESTIONS) if q == question)
    if task == 'UIDT003':
        return 'wrong'
    if task == 'UIDT006':
        return f'A-{task}' if 'percent means' in (Path(workspace) / 'AGENTS.md').read_text() else 'wrong'
    return f'A-{task}'


def fake_episode(lay, arm, answer_for=default_answer, calls=None):
    def run(*, task, adapter, base, ordinal, role, temperature=arms.DEFAULT_TEMPERATURE, top_p=arms.DEFAULT_TOP_P):
        if calls is not None:
            calls.append((role, task['task_id'], str(adapter) if adapter else None))
        prep = CP.prepare_episode(lay, arm, task, adapter, base, ordinal=ordinal, role=role)
        return CP.execute_episode(KEY, lay, arm, prep, broker_factory=FakeBroker, run_actor=fake_run_actor(answer_for),
                                  temperature=temperature, top_p=top_p)
    return run


def fake_learner(record, *, note=True, rule=False, fail=False, verify=True, admit_note=True):
    """A learner with S5's calling convention: validates the released evidence with S5's own builder, runs
    one verification episode, and (block 1) admits a note / a modified rule into output/adapter."""
    def learn(*, key, source, evidence, output, block, stop, actor_runner, pack, grader, root, w0, exclude, runner):
        import evidence as EV
        record.append({'block': block, 'source': str(source), 'evidence': str(evidence), 'exclude': list(exclude),
                       'w0': str(w0), 'runner': runner})
        ev = EV.build_evidence(root, block, grader=grader, exclude=exclude)
        record[-1]['released'] = sorted(e.task_id for e in ev.episodes.values())
        record[-1]['references'] = sorted(e.reference for e in ev.episodes.values())
        output = Path(output)
        output.mkdir(parents=True)
        if fail:
            return {'status': 'failed', 'activated': False, 'candidate': str(source), 'error': 'fake failure'}
        if verify:
            q = next(iter(ev.episodes.values()))
            summary = actor_runner(source, {'task_id': q.task_id, 'question': q.question, 'ordinal': q.ordinal},
                                   output / 'verify' / 'probe' / 'q-k1')
            record[-1]['verify_answer'] = summary['answer']
        if block != 1 or not (note or rule):
            return {'status': 'retained', 'activated': False, 'candidate': str(source), 'block': block}
        candidate = output / 'adapter'
        arms.copy_adapter(Path(source), candidate)
        admission = {'admitted': True, 'admitted_rules': [], 'rule_admission': {}, 'policy_changes': {}}
        if note:
            text = note if isinstance(note, str) else 'A percent means x100: percent means the ratio times 100.'
            NR.save_note(candidate, C.NoteSpec(id='percent_share', triggers=['percent'], text=text, admitted_block=1,
                                               origin='learned'))
            if admit_note:      # the S5 learning-ledger row of a note admitted by paired re-runs
                with (Path(root) / 'learning-ledger.jsonl').open('a') as stream:
                    stream.write(json.dumps({'t': block, 'kind': 'note', 'action': 'add', 'id': 'percent_share',
                                             'admitted': True}) + '\n')
        if rule:
            data_rule = json.loads((candidate / 'rules/answer_count.json').read_text())
            data_rule['priority'] = 11
            data_rule['origin'] = 'learned'
            (candidate / 'rules/answer_count.json').write_text(json.dumps(data_rule, indent=2, sort_keys=True))
            if rule != 'unadmitted':
                admission['admitted_rules'] = ['answer_count']
                admission['rule_admission'] = {'answer_count': {'counterfactual': [{'ordinal': 3, 'admit': True}],
                                                                'precision': {'n_right': 10, 'fired_right': 0}}}
        control = output / 'sessions/learner/control'
        control.mkdir(parents=True)
        (control / 'mechanical-admission.json').write_text(json.dumps(admission))
        result = {'status': 'admitted', 'activated': True, 'candidate': str(candidate), 'block': block,
                  'revision': CP.revision(candidate), 'architecture': arms.architecture(arms.parse_arm('text-evolve'))}
        (output / 'result.json').write_text(json.dumps(result))
        return result
    return learn


def hooks(lay, arm, **kw):
    learner = kw.pop('learn', None)
    return CP.Hooks(episode=fake_episode(lay, arm, **kw), learn=learner, scorer=SCORER, verify_data=False,
                    deep_runtime=False, report=True)

# ---------------------------------------------------------------------------------------------
# arms.py
# ---------------------------------------------------------------------------------------------


def test_parse_config_standard_custom_and_refusals(tmp_path):
    for name, arm in C.STANDARD_ARMS.items():
        assert make_spec(name).arm == arm
    toy = make_spec('mech-static', pack='toy')
    assert toy.arm == replace(C.STANDARD_ARMS['mech-static'], pack='toy') and toy.pack == 'toy'
    custom = {'name': 'mech-lite', 'pack': 'officeqa', 'l1_tools': True, 'controller': True, 'seed': 'rules',
              'learner': None, 'notes': False}
    spec = arms.parse_config({'arm': custom, 'repeat': 2, 'order_seed': 's'}, root_name='r2')
    assert spec.arm.name == 'mech-lite' and spec.repeat == 2 and arms.architecture(spec.arm) == 'mechanism'
    barrier = {'dir': str(tmp_path / 'b'), 'roots': ['r1', 'r2'], 'timeout_s': 60}
    assert arms.parse_config({'arm': 'plain', 'repeat': 1, 'order_seed': 's', 'barrier': barrier},
                             root_name='r1').barrier.roots == ('r1', 'r2')
    bad = [({'arm': 'plain', 'repeat': 1, 'order_seed': 's', 'typo': 1}, 'unknown'),
           ({'arm': 'plain', 'repeat': 1, 'order_seed': 's', 'profile': 'full_cards_unbounded'}, 'profile'),
           ({'arm': 'cards', 'repeat': 1, 'order_seed': 's'}, 'unknown arm'),
           ({'arm': 'mech-evolve-x', 'repeat': 1, 'order_seed': 's'}, 'unknown arm'),     # never inferred by substring
           ({'arm': {**custom, 'name': 'mech-static'}, 'repeat': 1, 'order_seed': 's'}, 'standard arm name'),
           ({'arm': {**custom, 'seed': 'text'}, 'repeat': 1, 'order_seed': 's'}, 'seed as rules'),
           ({'arm': {**custom, 'controller': False}, 'repeat': 1, 'order_seed': 's'}, 'seed=rules needs controller'),
           ({'arm': {k: v for k, v in custom.items() if k != 'notes'}, 'repeat': 1, 'order_seed': 's'}, 'exactly the fields'),
           ({'arm': {**custom, 'l1_tools': 1}, 'repeat': 1, 'order_seed': 's'}, 'true or false'),
           ({'arm': {**custom, 'seed': 'none', 'controller': False, 'l1_tools': False, 'notes': False, 'learner': 'text'},
             'repeat': 1, 'order_seed': 's'}, 'no adapter'),
           ({'arm': 'plain', 'repeat': 0, 'order_seed': 's'}, 'repeat'),
           ({'arm': 'plain', 'repeat': True, 'order_seed': 's'}, 'repeat'),
           ({'arm': 'plain', 'repeat': 1}, 'order_seed'),
           ({'arm': 'plain', 'repeat': 1, 'order_seed': 's', 'pack': 'nope'}, 'unknown pack'),
           ({'arm': {**custom, 'pack': 'toy'}, 'repeat': 1, 'order_seed': 's'}, 'differs from the root pack'),
           ({'arm': 'plain', 'repeat': 1, 'order_seed': 's', 'barrier': {**barrier, 'roots': ['x']}}, 'not listed'),
           ({'arm': 'plain', 'repeat': 1, 'order_seed': 's', 'barrier': {**barrier, 'dir': 'rel'}}, 'absolute'),
           ({'arm': 'plain', 'repeat': 1, 'order_seed': 's', 'barrier': {**barrier, 'timeout_s': 0}}, 'positive'),
           ({'repeat': 1, 'order_seed': 's'}, 'names its arm')]
    for config, message in bad:
        with pytest.raises((ValueError, TypeError), match=message):
            arms.parse_config(config, root_name='r1')


def test_experiment_constants_without_a_campaign():
    # preflight.py runs this suite inside every campaign root, whose .task-paths.json names an arm (S8 review)
    import paths
    if 'arm' in paths._config:
        assert experiment.CAMPAIGN is not None and experiment.ARM == experiment.CAMPAIGN.arm
    else:
        assert experiment.CAMPAIGN is None and experiment.ARM is None
    assert experiment.PROFILE == 'v9'
    assert experiment.FULL is True and experiment.MWM is False and experiment.PROJECT_DOC_MAX_BYTES == 4194304
    assert experiment.ACTOR_SECONDS == 3600 and experiment.LEARN_EVERY == 5 and experiment.LEARN_LAST_BLOCK is False
    assert experiment.BROKER_OPTIONS == {'max_completion': None, 'max_response': None, 'request_timeout': None,
                                         'max_attempts': None}
    # wave 3: the root's declared temperature (a pilot temperature-cell root declares its own), default 0
    assert experiment.REQUERY_WHEN_CLEAN is False
    assert experiment.TEMPERATURE == (experiment.CAMPAIGN.temperature if experiment.CAMPAIGN else 0)
    assert {arms.architecture(a) for a in C.STANDARD_ARMS.values()} == {'none', 'text', 'mechanism'}
    source = (ROOT / 'experiment.py').read_text() + (ROOT / 'full_audit.py').read_text()
    assert "in PROFILE" not in source and "'cards' in" not in source and "'prompt_only' in" not in source


def test_question_order_per_repeat_and_blocks():
    ids = [f'UID{i:04}' for i in range(1, 134)]
    first = arms.question_order(ids, 'seed', 1)
    assert sorted(first) == sorted(ids) and first != ids
    assert first == arms.question_order(list(reversed(ids)), 'seed', 1)          # a function of the ids only
    assert arms.question_order(ids, 'seed', 2) != first                         # a new order per repeat
    assert arms.question_order(ids, 'other', 1) != first
    # identical for every arm of a repeat: the order depends on (order_seed, repeat, ids) and nothing else
    orders = {name: arms.question_order(ids, make_spec(name).order_seed, 1) for name in C.STANDARD_ARMS}
    assert len({tuple(o) for o in orders.values()}) == 1
    blocks = arms.blocks_of(133, 5)
    assert len(blocks) == 27 and all(len(b) == 5 for b in blocks[:-1]) and list(blocks[-1]) == [131, 132, 133]
    with pytest.raises(ValueError):
        arms.question_order(['a', 'a'], 's', 1)

# ---------------------------------------------------------------------------------------------
# barrier.py
# ---------------------------------------------------------------------------------------------


def test_barrier_orders_blocks_across_roots(tmp_path):
    roots = ['a', 'b', 'c']
    starts, ends, errors = {}, {}, []

    def root(name, delay):
        group = B.FileBarrier(tmp_path / 'group', roots, name, timeout_s=20, poll_s=0.02)
        try:
            for block in range(1, 4):
                group.pass_block(block - 1)
                starts[(name, block)] = time.time()
                time.sleep(delay * block)
                ends[(name, block)] = time.time()
                group.arrive(block)
        except Exception as exc:          # pragma: no cover - reported below
            errors.append(exc)
    threads = [threading.Thread(target=root, args=(n, d)) for n, d in zip(roots, (0.01, 0.05, 0.02))]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert not errors and len(starts) == 9
    group = B.FileBarrier(tmp_path / 'group', roots, 'a', timeout_s=1)
    for block in range(1, 3):
        done = group.done(block)
        assert set(done) == set(roots)
        latest = max(v['end_unix'] for v in done.values())
        assert all(starts[(n, block + 1)] >= latest for n in roots)
    assert group.aborted() is None
    assert group.arrive(1)['end_unix'] == group.done(1)['a']['end_unix']       # idempotent arrival


def test_barrier_timeout_writes_abort_and_others_stop(tmp_path):
    fast = B.FileBarrier(tmp_path / 'g', ['a', 'b'], 'a', timeout_s=0.3, poll_s=0.05)
    other = B.FileBarrier(tmp_path / 'g', ['a', 'b'], 'b', timeout_s=5, poll_s=0.05)
    fast.arrive(1)
    begun = time.monotonic()
    with pytest.raises(B.BarrierTimeout, match="missing \\['b'\\]"):
        fast.wait(1)
    assert time.monotonic() - begun < 3
    info = fast.aborted()
    assert info['root'] == 'a' and 'timeout' in info['reason']
    with pytest.raises(B.BarrierAborted, match='aborted by a'):
        other.check()
    with pytest.raises(B.BarrierAborted):
        other.wait(1)
    assert other.abort('second reason')['reason'] == info['reason']           # the first reason is kept
    moved = B.clear_abort(tmp_path / 'g')
    assert moved.name.startswith('ABORT-') and moved.is_file() and fast.aborted() is None
    other.check()


def test_barrier_honours_stop_and_order_registration(tmp_path):
    stop = threading.Event()
    group = B.FileBarrier(tmp_path / 'g', ['a', 'b'], 'a', timeout_s=30, poll_s=5, stop=stop)
    threading.Timer(0.2, stop.set).start()
    begun = time.monotonic()
    with pytest.raises(B.BarrierStopped):
        group.wait(0)
    assert time.monotonic() - begun < 3
    first = group.register_order(1, 'abc', 7)
    assert first['first_root'] == 'a'
    B.FileBarrier(tmp_path / 'g', ['a', 'b'], 'b', timeout_s=1).register_order(1, 'abc', 7)
    with pytest.raises(ValueError, match='question order differs'):
        B.FileBarrier(tmp_path / 'g', ['a', 'b'], 'b', timeout_s=1).register_order(1, 'xyz', 7)
    with pytest.raises(ValueError):
        B.FileBarrier(tmp_path / 'g', ['a', 'b'], 'c', timeout_s=1)

# ---------------------------------------------------------------------------------------------
# notes_render.py, W0 and budgets
# ---------------------------------------------------------------------------------------------


def note(id, triggers, text='Some advice.', block=1):
    return C.NoteSpec(id=id, triggers=triggers, text=text, admitted_block=block, origin='learned')


def test_notes_choose_at_most_three_in_block_then_id_order(tmp_path):
    adapter = arms.copy_adapter(ROOT / 'adapters/text-w0', tmp_path / 'a')
    for n in (note('zeta', ['public debt'], block=1), note('alpha', ['public debt'], block=2),
              note('beta', ['*'], block=1), note('gamma', ['debt'], block=1), note('delta', ['outlays'], block=0),
              note('omega', ['debt of the public'], block=0)):
        NR.save_note(adapter, n)
    question = 'How much did the public debt grow in 2002?'
    chosen = NR.choose(NR.load_notes(adapter), question)
    assert [n.id for n in chosen] == ['beta', 'gamma', 'zeta']           # (admitted_block, id); 'omega' is not contiguous
    assert NR.choose_ids(adapter, question) == ['beta', 'gamma', 'zeta']
    assert NR.choose_ids(adapter, 'Outlays in 1990?') == ['delta', 'beta']
    text, ids, info = NR.compose_agents(CP.COMMON, adapter, question)
    assert ids == ['beta', 'gamma', 'zeta'] and text.startswith(CP.COMMON.rstrip())
    assert (adapter / 'SEED.md').read_text().strip() in text
    assert text.endswith(NR.NOTES_HEADER + '\n\n' + '- Some advice.\n' * 3)
    assert info['notes_shown'] == 3 and info['agents_md_bytes'] == len(text.encode())
    plain, none, _ = NR.compose_agents(CP.COMMON, None, question)
    assert plain == CP.COMMON.rstrip() + '\n' and none == []
    bad = [(note('Bad-Id', ['x']), 'id must'), (note('ok', ['deb(t|s)']), 'no regex'),
           (note('ok', ['x'], text='x' * (C.NOTE_MAX + 1)), 'exceeds'), (note('ok', ['x'], text='a\x00b'), 'control')]
    for spec, message in bad:
        assert any(message in e for e in NR.validate_note(spec)), spec
    (adapter / 'notes/other.json').write_text((adapter / 'notes/zeta.json').read_text())
    with pytest.raises(ValueError, match='holds note zeta'):
        NR.load_notes(adapter)


def test_committed_w0_equals_generated_and_fits_the_budget():
    text = arms.check_w0(C.STANDARD_ARMS['text-static'])
    mech = arms.check_w0(C.STANDARD_ARMS['mech-static'])
    assert text['model_visible']['within_budget'] and mech['model_visible']['within_budget']
    seed = (ROOT / 'adapters/text-w0/SEED.md').read_text()
    from packs.officeqa.pack import OfficeQAPack
    pack = OfficeQAPack()
    assert seed.startswith(arms.CORE_ADVICE.rstrip()) and pack.seed_text().strip() in seed
    for phrase in ('same output will not change', 'calc', 'exactly as many values', 'percent', 'weigh them'):
        assert phrase in seed
    mech_seed = (ROOT / 'adapters/mech-w0/SEED.md').read_text()
    assert 'submit --answer' in mech_seed and 'Accept' in mech_seed and 'Keep' in mech_seed and 'Offer' in mech_seed
    # PREREG addendum P2: the mech W0 adds the finishing text (and the rules) to the text W0's advice, and no
    # model-visible text suggests --alt any more
    assert mech_seed == seed + '\n' + arms.MECH_SEED and '--alt' not in mech_seed
    rules = arms.load_adapter_rules(ROOT / 'adapters/mech-w0')
    assert {r.id for r in rules} == {r.id for r in R.core_seed_rules()} | {r.id for r in pack.seed_rules()}
    policy = json.loads((ROOT / 'adapters/mech-w0/policy.json').read_text())
    assert policy == {**C.POLICY_DEFAULTS, 'disabled_rules': sorted(arms.W0_DISABLED_RULES['officeqa'])}
    assert set(policy['disabled_rules']) < {r.id for r in rules} and all(r.enabled for r in rules)
    assert not list((ROOT / 'adapters/text-w0/notes').glob('*.json')) and not (ROOT / 'adapters/text-w0/rules').exists()
    # seed parity (addendum P2): mech = the text seed + the finishing text + rule templates + controller templates,
    # both far inside the same 16 KB budget
    assert text['model_visible']['total'] == len(seed.encode()) > 0 and mech['model_visible']['rule_templates'] > 0
    assert mech['model_visible']['total'] == (text['model_visible']['total'] + 1 + len(arms.MECH_SEED.encode())
                                              + mech['model_visible']['rule_templates'] + mech['model_visible']['controller'])


def test_model_visible_equals_the_learners_text_budget(tmp_path):
    import learner_common as LC
    from packs.officeqa.pack import OfficeQAPack
    for name in ('text-static', 'mech-static'):
        arm = C.STANDARD_ARMS[name]
        adapter = arms.copy_adapter(arms.w0_dir(arm), tmp_path / name)
        NR.save_note(adapter, note('share', ['percent'], text='A note with   spaces.'))
        mine = arms.model_visible(adapter, arm)
        seed, _ = LC.seed_text(adapter, OfficeQAPack())
        rules = R.load_rules(adapter / 'rules') if (adapter / 'rules').is_dir() else []
        theirs = LC.text_budget(seed, LC.load_notes(adapter), rules,
                                LC.controller_template_bytes() if arm.controller else 0)
        assert mine['total'] == theirs['total'] and mine['within_budget'] == theirs['ok']


def test_check_adapter_refuses_foreign_or_oversized_adapters(tmp_path):
    text, mech = C.STANDARD_ARMS['text-evolve'], C.STANDARD_ARMS['mech-evolve']
    assert arms.check_adapter(ROOT / 'adapters/text-w0', text)['architecture'] == 'text'
    assert arms.check_adapter(ROOT / 'adapters/mech-w0', mech)['rules'] == 10
    with pytest.raises(ValueError, match='architecture'):
        arms.check_adapter(ROOT / 'adapters/mech-w0', text)
    a = arms.copy_adapter(ROOT / 'adapters/text-w0', tmp_path / 'a')
    (a / 'rules').mkdir()
    with pytest.raises(ValueError, match='no rules/'):
        arms.check_adapter(a, text)
    b = arms.copy_adapter(ROOT / 'adapters/mech-w0', tmp_path / 'b')
    (b / 'policy.json').write_text(json.dumps({**C.POLICY_DEFAULTS, 'loop_threshold': 1}))
    with pytest.raises(ValueError, match='loop_threshold'):
        arms.check_adapter(b, mech)
    c = arms.copy_adapter(ROOT / 'adapters/mech-w0', tmp_path / 'c')
    rule = json.loads((c / 'rules/scale.json').read_text())
    (c / 'rules/scale.json').write_text(json.dumps({**rule, 'when': [{'pred': 'no_such_predicate', 'args': {}}]}))
    with pytest.raises(ValueError, match='unknown predicate'):
        arms.check_adapter(c, mech)
    d = arms.copy_adapter(ROOT / 'adapters/text-w0', tmp_path / 'd')
    for i in range(30):
        NR.save_note(d, note(f'n{i:02}', ['x'], text='y' * C.NOTE_MAX))
    with pytest.raises(ValueError, match='exceeds 16384'):
        arms.check_adapter(d, text)

# ---------------------------------------------------------------------------------------------
# Episode composition per arm
# ---------------------------------------------------------------------------------------------


def test_readme_agents_and_kit_per_arm(tmp_path, code):
    root, _, lay = make_root(tmp_path, 'plain', code)
    task = {'task_id': 'UIDT006', 'question': QUESTIONS[5]}
    seen = {}
    for name, arm in C.STANDARD_ARMS.items():
        adapter = arms.w0_dir(arm, lay.adapters)
        prep = CP.prepare_episode(lay, arm, task, adapter, root / 'eps' / name, ordinal=6)
        ws = Path(prep['workspace'])
        readme, agents = (ws / 'README.md').read_text(), (ws / 'AGENTS.md').read_text()
        marker = json.loads((ws / acting.MARKER).read_text())
        tools = {p.name for p in (ws / '.harness/bin').iterdir()}
        seen[name] = (readme, agents)
        assert readme.startswith(kit.readme_l0(arm, lay.corpus, lay.index))
        assert 'officeqa.py' in readme and 'Task conventions (published with the OfficeQA benchmark)' in readme
        assert ('find_tables' in readme and 'calc -c' in readme) == arm.l1_tools
        assert ('submit --answer' in readme) == arm.controller
        assert {'find_tables', 'show_table', 'printings', 'calc'} <= tools if arm.l1_tools else \
            tools == {'python', 'python3', 'rg'}
        assert ('submit' in tools) == arm.controller
        assert agents.startswith(CP.COMMON.rstrip())
        if arm.seed == 'none':
            assert agents == CP.COMMON.rstrip() + '\n'
        else:
            assert (adapter / 'SEED.md').read_text().strip() in agents
        assert marker['extra']['notes'] == [] and marker['extra']['task_id'] == 'UIDT006'
        assert marker['arm'] == asdict(arm) and (ws / 'officeqa.py').is_file()
        assert json.loads((root / 'eps' / name / 'task.json').read_text())['question'] == QUESTIONS[5]
    assert seen['text-static'] == seen['text-evolve'] and seen['mech-static'] == seen['mech-evolve']
    assert seen['mech-static'][0] == seen['text-static'][0] + '\n' + CP.SUBMIT_README.rstrip() + '\n'
    # the L0 part and the conventions are byte-identical in every arm
    l0 = {v[0].split('# Table and calculation tools')[0].split('# Submitting')[0].rstrip() for v in seen.values()}
    assert len(l0) == 1


def test_episode_through_the_real_broker_and_controller(tmp_path, code):
    """execute_episode with the real broker (+ real Ledger/Gate/MechController on the synthetic tables index)
    driven by CodexSim through a fake upstream: records, summary and the audit's record checks."""
    import broker
    import fake_upstream as fu
    import rate_control
    from codexsim import CodexSim
    root, _, lay = make_root(tmp_path, 'mech-static', code)
    task = {'task_id': 'UIDT002', 'question': QUESTIONS[1]}
    results = {}
    for name in ('mech-static', 'plain'):
        arm = C.STANDARD_ARMS[name]
        up = fu.Upstream([fu.response([fu.call('echo 2,010,894')]),
                          fu.response([fu.call('submit --answer "2,010,894"')]),
                          fu.response([fu.message('2,010,894')])])
        prep = CP.prepare_episode(lay, arm, task, arms.w0_dir(arm, lay.adapters), root / name, ordinal=2)

        def run_actor(workspace, records, prompt, corpus, index, *, endpoint, arm, timeout, env_extra, data,
                      permissions_instructions, temperature=None, top_p=None):
            records = Path(records)
            records.mkdir(parents=True)
            settings = acting.build_settings(workspace, records, corpus, index, None, endpoint=endpoint, arm=arm,
                                             env_extra=env_extra, data=data, permissions_instructions=False)
            submit_url = endpoint.rsplit('/v1', 1)[0] + '/v1/submit'

            def tool(cmd):
                if cmd.startswith('submit') and arm.controller:
                    body = json.dumps({'answer': '2,010,894'}).encode()
                    request = urllib.request.Request(submit_url, data=body, headers={'Content-Type': 'application/json'})
                    return json.loads(urllib.request.urlopen(request, timeout=30).read())['message'] + '\n'
                return cmd.replace('echo ', '') + '\n'
            sim = CodexSim(endpoint, prompt, agents=(Path(workspace) / 'AGENTS.md').read_text(), run_tool=tool)
            final = ''
            for _ in range(5):
                response = sim.send()
                if sim.is_final(response):
                    final = sim.final_text(response)
                    break
            marker = json.loads((Path(workspace) / acting.MARKER).read_text())
            (Path(workspace) / 'final.md').write_text(final + '\n')
            (records / 'final.md').write_text(final + '\n')           # acting.collect_final copies it here
            (records / 'invocation.json').write_text(json.dumps({'arm': asdict(arm), 'timeout_seconds': timeout,
                                                                'settings': settings, 'permissions_instructions': False,
                                                                'frozen_runtime': kit.verify_frozen(deep=False)}))
            result = {'status': 'completed', 'timed_out': False,
                      'kit_after': {'intact': True, 'kit_sha256': kit.verify_kit(workspace, marker['kit'])},
                      'agents_md_bytes': marker['agents_md_bytes'], 'completed_item_counts': {'command_execution': 2}}
            (records / 'result.json').write_text(json.dumps(result))
            return result
        scheduler = rate_control.RateScheduler(max_in_flight=float('inf'), min_interval=0, backoffs=(0,))
        try:
            summary = CP.execute_episode(KEY, lay, arm, prep, run_actor=run_actor,
                                         broker_extra={'upstream_url': up.url, 'scheduler': scheduler})
        finally:
            up.close()
        results[name] = summary
        assert summary['answer'] == '2,010,894' and summary['status'] == 'completed' and summary['kit_intact']
        api = root / name / 'api'
        metas = [json.loads(p.read_text()) for p in sorted(api.glob('[0-9][0-9][0-9]/transport.json'))]
        audit = full_audit.Audit(root, code, upstream=up.url)
        audit.records(api, label=name, arm=arm, status='completed')
        assert all(c['passed'] for c in audit.checks), [c for c in audit.checks if not c['passed']]
        if arm.controller:
            assert summary['controller']['submits'] == 1 and summary['fallback_answer'] == '2,010,894'
            assert summary['fallback_differs'] is False
            assert all(m['origin'] == 'codex' for m in metas) and (api / 'ledger.jsonl').is_file()
            assert (api / 'checks/001.json').is_file()
        else:
            assert summary['controller'] is None and summary['fallback_answer'] is None
            assert not any(set(m) & set(full_audit.V9_RECORD_FIELDS) for m in metas)
            assert not (api / 'ledger.jsonl').exists() and not (api / 'controller.jsonl').exists()
    assert results['plain']['readme_bytes'] < results['mech-static']['readme_bytes']

# ---------------------------------------------------------------------------------------------
# The campaign loop
# ---------------------------------------------------------------------------------------------


def run_campaign(lay, spec, h, *, resume=False, stop=None):
    return CP.Campaign(KEY, spec, lay, hooks=h, stop=stop).run(resume=resume)


def test_text_evolve_campaign_seal_release_learn_serve_grade(tmp_path, code):
    group = {'dir': str(tmp_path / 'barrier'), 'roots': ['root', 'peer'], 'timeout_s': 30, 'poll_s': 0.05}
    root, spec, lay = make_root(tmp_path, 'text-evolve', code, barrier=group)
    peer = B.FileBarrier(tmp_path / 'barrier', ['root', 'peer'], 'peer', timeout_s=5)
    for block in range(0, 3):
        peer.arrive(block)
    record, calls = [], []
    h = hooks(lay, spec.arm, learn=fake_learner(record), calls=calls)
    state = run_campaign(lay, spec, h)
    order = arms.question_order(TASK_IDS, spec.order_seed, 1)
    assert state['status'] == 'completed' and [r['task_id'] for r in state['results']] == order
    assert [r['ordinal'] for r in state['results']] == list(range(1, 8))
    # one learning round (after block 1 of 2), skipped after the last block
    assert [(r['block'], r['status']) for r in state['learning']] == [(1, 'admitted'), (2, 'skipped_unused')]
    first = record[0]
    withheld_in_block = sorted(t for t in order[:5] if t in data.ISSUE_47)
    assert first['exclude'] == list(data.withheld_ids(root)) and first['w0'] == str(lay.adapters / 'text-w0')
    assert first['released'] == sorted(t for t in order[:5] if t not in data.ISSUE_47)       # S5 builder view
    assert first['references'] == sorted(f'A-{t}' for t in first['released'])
    seal = json.loads((root / 'private-learning-seals/01.json').read_text())
    assert sorted(e['id'] for e in seal['withheld_episodes']) == withheld_in_block
    assert seal['sealed_utc'] <= seal['reference_released_utc']
    released = [p for p in (root / 'evidence/01').rglob('reference_answer.json')]
    assert len(released) == 5 - len(withheld_in_block)
    assert not any(json.loads(p.read_text())['task_id'] in data.ISSUE_47 for p in released)
    manifest = json.loads((root / 'evidence/01/manifest.json').read_text())
    assert [w['id'] for w in manifest['withheld']] == [t for t in order[:5] if t in data.ISSUE_47]
    assert manifest['includes_grades'] is False and all('run_dir' in e for e in manifest['episodes'])
    # the verification episode ran through the scored path, under evolution/01/verify, and is not a scored result
    assert any(role == 'verify' for role, *_ in calls)
    assert (root / 'evolution/01/verify/probe/q-k1/summary.json').is_file()
    # the learned note is served (by trigger) from block 2 on, and its id recorded in the marker
    candidate = root / 'evolution/01/adapter'
    assert state['adapter']['path'] == str(candidate) and state['blocks']['2']['adapter']['path'] == str(candidate)
    for row in state['results']:
        marker = json.loads((root / 'runs/episodes' / f'{row["ordinal"]:03}' / 'workspace' / acting.MARKER).read_text())
        question = QUESTIONS[TASK_IDS.index(row['task_id'])]
        expected = ['percent_share'] if row['ordinal'] > 5 and 'percent' in question else []
        assert marker['extra']['notes'] == expected == row['notes_served']
    # final seal and grade (injected scorer), all and undisputed
    metrics = json.loads((root / 'private-evaluation/paired-metrics.json').read_text())
    by_task = {r['task_id']: r for r in metrics['results']}
    assert by_task['UIDT003']['text-evolve'] == 0.0 and metrics['denominator'] == 7
    assert metrics['undisputed']['n'] == 5 and metrics['arms']['text-evolve']['n'] == 7
    ordinal6 = order.index('UIDT006') + 1
    assert by_task['UIDT006']['text-evolve'] == (1.0 if ordinal6 > 5 else 0.0)
    # barrier: ready + both blocks recorded for this root
    assert all((tmp_path / f'barrier/block-{b:02}/root.done').is_file() for b in (0, 1, 2))
    # the audit passes on this root (fake upstream URL recorded by the fake broker is the Fireworks one)
    result = full_audit.build(root, code=code, write=False)
    failed = [c for c in result['checks'] if not c['passed']]
    assert result['passed'], failed
    names = {c['name'] for c in result['checks']}
    assert 'round 1 exact answer scope' in names and any('served notes re-derive' in n for n in names)
    assert (root / 'report.html').is_file() and 'A-UID' not in (root / 'report.html').read_text()


def test_two_roots_advance_block_by_block(tmp_path, code):
    group = {'dir': str(tmp_path / 'barrier'), 'roots': ['g1-a', 'g1-b'], 'timeout_s': 60, 'poll_s': 0.02}
    roots = {name: make_root(tmp_path, arm, code, name=name, barrier=group)
             for name, arm in (('g1-a', 'plain'), ('g1-b', 'mech-static'))}
    slow = {'g1-a': 0.0, 'g1-b': 0.15}
    errors, states = [], {}

    def go(name):
        root, spec, lay = roots[name]
        base = fake_episode(lay, spec.arm)

        def episode(**kw):
            time.sleep(slow[name])
            return base(**kw)
        h = CP.Hooks(episode=episode, scorer=SCORER, verify_data=False, deep_runtime=False, report=False)
        try:
            states[name] = run_campaign(lay, spec, h)
        except Exception as exc:          # pragma: no cover
            errors.append(exc)
    threads = [threading.Thread(target=go, args=(n,)) for n in roots]
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    assert not errors and all(s['status'] == 'completed' for s in states.values())
    group_barrier = B.FileBarrier(tmp_path / 'barrier', group['roots'], 'g1-a', timeout_s=1)
    end1 = max(v['end_unix'] for v in group_barrier.done(1).values())
    assert all(s['blocks']['2']['start_unix'] >= end1 for s in states.values())
    assert states['g1-a']['order_sha256'] == states['g1-b']['order_sha256']
    for name, (root, spec, lay) in roots.items():
        result = full_audit.build(root, code=code, write=False)
        assert result['passed'], [c for c in result['checks'] if not c['passed']]
        assert not (root / 'evidence').exists() and not (root / 'private-learning-seals').exists()
        if spec.arm.controller:
            assert all(r['fallback_answer'] == r['answer'] for r in states[name]['results'])


def test_crash_abort_then_block_level_resume(tmp_path, code):
    group = {'dir': str(tmp_path / 'barrier'), 'roots': ['root', 'peer'], 'timeout_s': 30, 'poll_s': 0.05}
    root, spec, lay = make_root(tmp_path, 'text-evolve', code, barrier=group)
    peer = B.FileBarrier(tmp_path / 'barrier', ['root', 'peer'], 'peer', timeout_s=5)
    for block in range(0, 3):
        peer.arrive(block)
    record = []
    h = hooks(lay, spec.arm, learn=fake_learner(record))
    real_episode = h.episode
    count = {'n': 0}

    def crashing(**kw):
        count['n'] += 1
        if kw['role'] == 'scored' and count['n'] == 7:          # 1st episode of block 2 (after 5 + 1 verify)
            Path(kw['base']).mkdir(parents=True)                # a half-written episode directory
            (Path(kw['base']) / 'task.json').write_text('{}')
            raise RuntimeError('simulated actor host crash')
        return real_episode(**kw)
    h.episode = crashing
    with pytest.raises(RuntimeError, match='simulated'):
        run_campaign(lay, spec, h)
    state = json.loads((root / 'state.json').read_text())
    assert state['status'] == 'stopped_on_error' and 'simulated' in state['error']
    abort = json.loads((tmp_path / 'barrier/ABORT').read_text())
    assert abort['root'] == 'root' and 'simulated' in abort['reason']
    with pytest.raises(B.BarrierAborted):                           # the peer stops at its next check
        peer.check()
    assert state['blocks']['1']['status'] == 'done' and state['blocks']['2']['status'] == 'running'
    partial = sorted(p.name for p in (root / 'runs/episodes').iterdir())
    assert len(partial) == 6                                          # block 1 + the crashed episode dir
    with pytest.raises(ValueError, match='already started'):
        run_campaign(lay, spec, hooks(lay, spec.arm))
    B.clear_abort(tmp_path / 'barrier')
    h.episode = real_episode
    state = run_campaign(lay, spec, h, resume=True)
    assert state['status'] == 'completed' and len(state['results']) == 7
    moved = state['attempts'][-1]['moved']
    assert moved['block'] == 2 and moved['kept_episodes'] is False and 'runs/episodes/006' in moved['moved']
    assert (root / moved['to'] / 'runs/episodes/006').is_dir()
    assert len(record) == 1                                           # the block-1 round was not repeated
    result = full_audit.build(root, code=code, write=False)
    assert result['passed'], [c for c in result['checks'] if not c['passed']]


def test_resume_after_a_failed_learning_round_keeps_the_episodes(tmp_path, code):
    root, spec, lay = make_root(tmp_path, 'text-evolve', code)
    (root / 'learner-state.json').write_text('{"claimed": {}}')
    record = []
    h = hooks(lay, spec.arm, learn=fake_learner(record, fail=True))
    with pytest.raises(CP.LearnerFailed, match='fake failure'):
        run_campaign(lay, spec, h)
    state = json.loads((root / 'state.json').read_text())
    assert state['status'] == 'stopped_on_error' and state['blocks']['1']['status'] == 'running'
    assert (root / 'private-learning-seals/01.json').is_file() and (root / 'evidence/01').is_dir()
    (root / 'learner-state.json').write_text('{"claimed": {"3": "advanced by the failed round"}}')
    h.learn = fake_learner(record)
    state = run_campaign(lay, spec, h, resume=True)
    moved = state['attempts'][-1]['moved']
    assert moved['kept_episodes'] is True and 'evidence/01' in moved['moved'] and 'evolution/01' in moved['moved']
    assert any(m.startswith('learner-state.json') for m in moved['moved'])
    assert json.loads((root / 'learner-state.json').read_text()) == {'claimed': {}}       # restored backup
    assert state['status'] == 'completed' and len(record) == 2
    assert full_audit.build(root, code=code, write=False)['passed']


def test_stop_event_saves_state_and_aborts_the_group(tmp_path, code):
    group = {'dir': str(tmp_path / 'barrier'), 'roots': ['root', 'peer'], 'timeout_s': 30, 'poll_s': 0.05}
    root, spec, lay = make_root(tmp_path, 'plain', code, barrier=group)
    B.FileBarrier(tmp_path / 'barrier', ['root', 'peer'], 'peer', timeout_s=5).arrive(0)
    stop = threading.Event()
    h = hooks(lay, spec.arm)
    inner = h.episode

    def episode(**kw):
        result = inner(**kw)
        if kw['ordinal'] == 2:
            stop.set()
        return result
    h.episode = episode
    with pytest.raises(CP.CampaignStopped):
        run_campaign(lay, spec, h, stop=stop)
    state = json.loads((root / 'state.json').read_text())
    assert state['status'] == 'stopped' and len(state['results']) == 2
    assert 'stopped' in json.loads((tmp_path / 'barrier/ABORT').read_text())['reason']


@pytest.mark.parametrize('arm_name,module', [('text-evolve', 'text_learning'), ('mech-evolve', 'mechanism_learning')])
def test_real_learner_round_through_the_campaign_loop(tmp_path, code, arm_name, module):
    """The real S5 learner (learner_common.run_round) with S5's scripted model ($0): the campaign's release
    passes S5's evidence validator, S5 re-runs actors through the campaign's verification runner, the
    admitted note comes back as a candidate adapter the campaign checks and serves, and the audit passes."""
    from fake_learner_model import ScriptedModel, calls, say, tool_call
    import importlib
    learner = importlib.import_module(module)
    seed = next(f'e2e-{i}' for i in range(500) if 'UIDT006' in arms.question_order(TASK_IDS, f'e2e-{i}', 1)[:5])
    root = tmp_path / 'root'
    root.mkdir()
    spec = arms.parse_config({'arm': arm_name, 'repeat': 1, 'order_seed': seed}, root_name='root')
    make_prepared(root, spec)
    lay = CP.Layout(root=root, code=code)
    order = arms.question_order(TASK_IDS, seed, 1)
    ordinal = order.index('UIDT006') + 1
    model = ScriptedModel([calls(tool_call('propose_note', id='percent_share', triggers=['percent'],
                                           text='A percent means the ratio times 100; percent means x100.',
                                           support=[ordinal], rationale='the answer missed the percent scale')),
                           say('Report: one note proposed for the percent question.')])
    rounds = []

    def learn(**offered):
        result = learner.run_learning(model=model, **offered)
        rounds.append(result)
        return result
    state = run_campaign(lay, spec, hooks(lay, spec.arm, learn=learn))
    assert rounds[0]['status'] == 'admitted', rounds[0].get('error')
    assert state['learning'][0]['activated'] is True and state['adapter']['path'] == str(root / 'evolution/01/adapter')
    runs = sorted(p.name for p in (root / 'evolution/01/verify/percent_share').iterdir() if p.is_dir() and p.name != 'adapter')
    assert f'q{ordinal:03}-k1' in runs and f'q{ordinal:03}-k2' in runs          # support x 2 through the scored path
    summary = json.loads((root / f'evolution/01/verify/percent_share/q{ordinal:03}-k1/summary.json').read_text())
    assert summary['role'] == 'verify' and summary['answer'] == 'A-UIDT006' and summary['notes_served'] == ['percent_share']
    request = json.loads((root / 'evolution/01/request.json').read_text())
    assert not set(request['episodes']) & {order.index(t) + 1 for t in data.ISSUE_47 if t in order}
    assert (root / 'evolution/01/sessions/learner/control/mechanical-admission.json').is_file()
    assert json.loads((root / 'learner-state.json').read_text())['claimed'] == {str(ordinal): 'percent_share'}
    result = full_audit.build(root, code=code, write=False)
    assert result['passed'], [c for c in result['checks'] if not c['passed']]
    verified = {c['name'] for c in result['checks'] if c['name'].startswith('verify evolution/01/verify/percent_share/q')}
    assert f'verify evolution/01/verify/percent_share/q{ordinal:03}-k1 kit intact after the run' in verified
    costs = cost.estimate(root)['groups']
    assert costs['verification_codex']['logical_calls'] >= 2 and costs['learners']['logical_calls'] == 2


def test_verification_runner_refuses_unreleased_questions_and_foreign_dirs(tmp_path, code):
    root, spec, lay = make_root(tmp_path, 'text-evolve', code)
    campaign = CP.Campaign(KEY, spec, lay, hooks=hooks(lay, spec.arm))
    campaign.load(resume=False)
    runner = campaign.verification_runner(1, lay.adapters / 'text-w0')
    future = campaign.order[5]
    with pytest.raises(ValueError, match='released questions'):
        runner(lay.adapters / 'text-w0', {'task_id': future, 'question': 'x'}, lay.evolution(1) / 'verify/a/q')
    released = campaign.order[0]
    with pytest.raises(ValueError, match='frozen question'):
        runner(lay.adapters / 'text-w0', {'task_id': released, 'question': 'another text'}, lay.evolution(1) / 'verify/a/q')
    question = campaign.tasks[released]['question']
    with pytest.raises(ValueError, match='live under'):
        runner(lay.adapters / 'text-w0', {'task_id': released, 'question': question}, root / 'elsewhere')
    with pytest.raises(ValueError, match='source or a candidate'):
        runner(lay.adapters / 'mech-w0', {'task_id': released, 'question': question}, lay.evolution(1) / 'verify/a/q')
    summary = runner(lay.adapters / 'text-w0', {'task_id': released, 'question': question}, lay.evolution(1) / 'verify/a/q')
    assert summary['role'] == 'verify' and summary['ordinal'] == 1
    with pytest.raises(ValueError, match='once'):
        runner(lay.adapters / 'text-w0', {'task_id': released, 'question': question}, lay.evolution(1) / 'verify/a/q')


def test_call_learner_matches_the_s5_signature(monkeypatch):
    seen = {}

    def run_learning(key, source, evidence, output, block, stop=None, *, actor_runner, **options):
        seen.update(key=key, source=source, evidence=evidence, output=output, block=block, stop=stop,
                    actor_runner=actor_runner, **options)
        return {'status': 'retained'}
    module = types.ModuleType('text_learning')
    module.run_learning = run_learning
    monkeypatch.setitem(sys.modules, 'text_learning', module)
    offered = dict(key='k', source='s', evidence='e', output='o', block=1, stop=None, actor_runner=len,
                   pack='p', grader='g', root='r', w0='w', exclude=())
    assert CP.call_learner(C.STANDARD_ARMS['text-evolve'], **offered) == {'status': 'retained'}
    assert seen == offered
    strict = types.ModuleType('mechanism_learning')
    strict.run_learning = lambda key, source, evidence, output, block, *, needed: None
    monkeypatch.setitem(sys.modules, 'mechanism_learning', strict)
    with pytest.raises(TypeError, match="needs \\['needed'\\]"):
        CP.call_learner(C.STANDARD_ARMS['mech-evolve'], **offered)
    import learner_common, text_learning, mechanism_learning  # noqa: F401  (the real S5 entry points)
    monkeypatch.undo()
    import inspect
    for name in ('text_learning', 'mechanism_learning'):
        params = inspect.signature(__import__(name).run_learning).parameters
        assert {'key', 'source', 'evidence', 'output', 'block', 'stop', 'actor_runner'} <= set(params)
    rr = inspect.signature(learner_common.run_round).parameters
    assert {'pack', 'grader', 'root', 'w0', 'exclude'} <= set(rr)

# ---------------------------------------------------------------------------------------------
# Audit failure detection on synthetic roots
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def completed_mech_evolve(tmp_path, code):
    root, spec, lay = make_root(tmp_path, 'mech-evolve', code)
    record = []
    state = run_campaign(lay, spec, hooks(lay, spec.arm, learn=fake_learner(record, note=True, rule=True)))
    assert state['status'] == 'completed'
    return root, spec, lay


def failed_names(root, code):
    result = full_audit.build(root, code=code, write=False)
    return {c['name'] for c in result['checks'] if not c['passed']}


def test_audit_passes_then_detects_tampering(completed_mech_evolve, code):
    root, spec, lay = completed_mech_evolve
    assert failed_names(root, code) == set()
    run1 = root / 'runs/episodes/001'
    # a tampered kit, an arm mismatch, a v9 field missing, a prediction changed, notes that do not re-derive
    result = json.loads((run1 / 'records/result.json').read_text())
    (run1 / 'records/result.json').write_text(json.dumps({**result, 'kit_after': {'intact': False}}))
    invocation = json.loads((run1 / 'records/invocation.json').read_text())
    (run1 / 'records/invocation.json').write_text(json.dumps({**invocation, 'arm': asdict(C.STANDARD_ARMS['mech-static'])}))
    meta = json.loads((run1 / 'api/001/transport.json').read_text())
    (run1 / 'api/001/transport.json').write_text(json.dumps({k: v for k, v in meta.items() if k != 'origin'}))
    run7 = root / 'runs/episodes/007'
    marker_path = run7 / 'workspace' / acting.MARKER
    os.chmod(marker_path, 0o644)
    marker = json.loads(marker_path.read_text())
    marker['extra']['notes'] = ['n1', 'n2', 'n3', 'n4']
    marker_path.write_text(json.dumps(marker))
    os.chmod(root / 'runs/episodes/002/prediction.json', 0o644)
    (root / 'runs/episodes/002/prediction.json').write_text('{"task_id": "x", "answer": "changed"}')
    failed = failed_names(root, code)
    assert 'mech-evolve/001 kit intact after the run' in failed
    assert 'mech-evolve/001 invocation.arm == root arm' in failed
    assert 'mech-evolve/001 api/001 origin/kind/codex_input_sha256' in failed
    assert 'mech-evolve/007 served notes recorded and at most 3' in failed
    assert 'mech-evolve/007 served notes re-derive from the adapter and question' in failed
    assert 'mech-evolve/002 prediction unchanged' in failed


def test_audit_requires_rule_admission_and_barrier_order(tmp_path, code):
    group = {'dir': str(tmp_path / 'barrier'), 'roots': ['root', 'late'], 'timeout_s': 30, 'poll_s': 0.05}
    root, spec, lay = make_root(tmp_path, 'mech-evolve', code, barrier=group)
    late = B.FileBarrier(tmp_path / 'barrier', ['root', 'late'], 'late', timeout_s=5)
    for block in range(0, 3):
        late.arrive(block)
    state = run_campaign(lay, spec, hooks(lay, spec.arm, learn=fake_learner([], note=False, rule='unadmitted')))
    assert state['status'] == 'completed'
    failed = failed_names(root, code)
    assert 'round 1 served rule answer_count (changed from W0) admitted with replay numbers' in failed
    # a peer whose block-1 end is after this root's block-2 start violates the barrier order
    done = json.loads(late.done_path(1).read_text())
    late.done_path(1).write_text(json.dumps({**done, 'end_unix': time.time() + 3600}))
    assert 'barrier block 02 started after every root finished block 01' in failed_names(root, code)


def test_audit_static_arm_must_not_learn(tmp_path, code):
    root, spec, lay = make_root(tmp_path, 'mech-static', code)
    run_campaign(lay, spec, hooks(lay, spec.arm))
    assert failed_names(root, code) == set()
    (root / 'evidence/01').mkdir(parents=True)
    (root / 'evidence/01/x.json').write_text('{}')
    assert 'static arm: no seals, evidence or evolution' in failed_names(root, code)

# ---------------------------------------------------------------------------------------------
# data.py pieces, freeze, cost, report
# ---------------------------------------------------------------------------------------------


def test_tables_index_is_bound_to_the_corpus(tmp_path):
    spec = make_spec('text-static')
    root = tmp_path / 'r'
    root.mkdir()
    make_prepared(root, spec)
    info = data.verify_tables_index(root)
    rows = json.loads((root / 'prepared/private/corpus_manifest.json').read_text())
    assert info['corpus_manifest_sha256'] == data.tables_corpus_hash(rows)
    assert data.tables_meta(root / 'prepared' / data.TABLES_RELATIVE)['corpus_manifest_sha256'] == data.tables_corpus_hash(rows)
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'private').mkdir()
    (other / 'private/corpus_manifest.json').write_text(json.dumps([{**rows[0], 'sha256': '0' * 64}, rows[1]]))
    with pytest.raises(ValueError, match='different corpus'):
        data.install_tables_index(other, root / 'prepared' / data.TABLES_RELATIVE)
    manifest = json.loads((root / 'prepared/manifest.json').read_text())
    assert manifest['learner_policy']['withheld_reference_ids'] == ['UID0001', 'UID0004']
    assert manifest['learner_policy']['includes_gold'] is False                 # a static arm releases nothing
    assert manifest['actor_read_allowlist'][-1] == 'agent/tables/'
    refs = data.release_references(root, ['UIDT002', 'UID0001'])
    assert refs == {'UIDT002': 'A-UIDT002', 'UID0001': None}


def test_freeze_detects_changed_sources_and_settings(tmp_path_factory, tmp_path):
    code = code_copy(tmp_path_factory)
    root, spec, lay = make_root(tmp_path, 'mech-static', code)
    order = arms.question_order(TASK_IDS, spec.order_seed, 1)
    frozen = CP.freeze_record(lay, spec, order, deep=False)
    assert frozen['w0']['model_visible']['within_budget'] and frozen['settings']['temperature'] == 0
    CP.verify_freeze(lay, frozen, spec, order)
    with pytest.raises(ValueError, match='question order'):
        CP.verify_freeze(lay, frozen, spec, list(reversed(order)))
    rule = code / 'adapters/mech-w0/rules/scale.json'
    rule.write_text(rule.read_text().replace('"priority": 20', '"priority": 21'))
    with pytest.raises(ValueError, match='Frozen tree changed: adapters/'):
        CP.verify_freeze(lay, frozen, spec, order)
    rule.write_text(rule.read_text().replace('"priority": 21', '"priority": 20'))
    (code / 'PROTOCOL.md').write_text('changed')
    with pytest.raises(ValueError, match='PROTOCOL.md'):
        CP.verify_freeze(lay, frozen, spec, order)


def test_cost_groups_by_origin_learners_verification_and_aborted(tmp_path):
    good = {'dispatched': True, 'usage': {'input_tokens': 1_000_000, 'output_tokens': 100_000,
                                          'input_tokens_details': {'cached_tokens': 200_000}}}

    def call(path, **extra):
        path.mkdir(parents=True)
        (path / 'request.json').write_text('{}')
        (path / 'transport.json').write_text(json.dumps({'attempts': [good], **extra}))
    call(tmp_path / 'runs/episodes/001/api/001', origin='codex')
    call(tmp_path / 'runs/episodes/001/api/002', origin='controller', parent=1)
    call(tmp_path / 'runs/plain/001/api/001')                                    # controller=None record: codex
    call(tmp_path / 'evolution/01/verify/note/q001-k1/api/001', origin='codex')
    call(tmp_path / 'evolution/01/verify/note/q001-k1/api/002', origin='controller', parent=1)
    call(tmp_path / 'runs-aborted/X/runs/episodes/006/api/001', origin='codex')
    call(tmp_path / 'preflight/smoke-01/mech-evolve/api/001', origin='codex')
    control = tmp_path / 'evolution/01/sessions/learner/control/fireworks-calls'
    control.mkdir(parents=True)
    (control / '001.json').write_text(json.dumps({'meta': {'attempts': [good]}}))
    x = cost.estimate(tmp_path)
    g = x['groups']
    assert (g['actors_codex']['logical_calls'], g['actors_controller']['logical_calls']) == (2, 1)
    assert (g['verification_codex']['logical_calls'], g['verification_controller']['logical_calls']) == (1, 1)
    assert g['learners']['logical_calls'] == 1 and g['aborted_actors']['logical_calls'] == 1
    assert g['synthetic_preflight']['logical_calls'] == 1
    assert x['actor_calls_by_origin']['controller']['logical_calls'] == 2
    assert x['formal_total']['logical_calls'] == 7
    assert abs(x['formal_total']['estimate_usd'] - 7 * 0.176) < 1e-9
    assert cost.self_test()['passed']


def test_report_renders_without_answers(completed_mech_evolve):
    root, spec, lay = completed_mech_evolve
    html = report.render(root)
    assert 'mech-evolve' in html and 'Controller' in html and 'Learning rounds' in html
    assert 'A-UID' not in html and 'UIDT00' not in html and 'prefers-color-scheme' in html

def test_smoke_fixture_prepares_every_arm_without_a_model_call(tmp_path):
    import run
    lay = run.fixture_root(tmp_path / 'smoke')
    assert data.tables_meta(lay.tables_index)['files'] == 1 and lay.index.is_file()
    for name, arm in C.STANDARD_ARMS.items():
        prep = CP.prepare_episode(lay, arm, {'task_id': 'SMOKE', 'question': 'Read note.txt.'},
                                  arms.w0_dir(arm), tmp_path / 'smoke' / name, ordinal=1, role='smoke')
        env_extra, data_roots = CP.actor_data(arm, lay)
        settings = acting.build_settings(prep['workspace'], tmp_path / 'out' / name, lay.corpus, lay.index, None,
                                         endpoint='http://127.0.0.1:1/v1', arm=arm, env_extra=env_extra, data=data_roots)
        assert settings['include_permissions_instructions'] is False
        assert ('TABLES_INDEX' in settings['shell_environment_policy.set']) == arm.l1_tools
        assert ('HARNESS_SUBMIT_URL' in settings['shell_environment_policy.set']) == arm.controller


def test_preflight_offline_on_a_synthetic_133_task_root(tmp_path_factory, tmp_path):
    """preflight.py --offline-only --no-suites in a code copy with a synthetic prepared root: every offline check
    (arm, data + tables index, frozen runtime deep, barrier order, seed parity, W0, a real workspace for the
    first question and a strict Codex config read, PREREG) runs and passes. The pinned reward.py is copied
    for its hash check only (never run here)."""
    import subprocess
    code = code_copy(tmp_path_factory)
    import paths
    config = {'profile': 'v9', 'arm': 'mech-evolve', 'repeat': 1, 'pack': 'officeqa', 'order_seed': 'pf-seed',
              'barrier': {'dir': str(tmp_path / 'barrier'), 'roots': [code.name], 'timeout_s': 60},
              # a root outside the project directory names the private copies explicitly
              'codex': str(paths.CODEX), 'frozen_runtime': str(paths.FROZEN)}
    (code / '.task-paths.json').write_text(json.dumps(config))
    spec = arms.parse_config(config, root_name=code.name)
    manifest = make_prepared(code, spec, n=133)
    reward = code / 'prepared/private/reward.py'
    shutil.copyfile(ROOT / 'vendor/officeqa/reward.py', reward)
    run = subprocess.run([sys.executable, '-B', 'preflight.py', '--offline-only', '--no-suites'], cwd=code,
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    result = json.loads((code / 'preflight/offline-checks.json').read_text())
    checks, info = result['checks'], result['info']
    assert result['passed'] and all(checks.values())
    for name in ('arm_valid', 'data', 'tables_index_matches_corpus', 'question_count_133', 'frozen_runtime_deep',
                 'barrier_order_registered', 'seed_parity_within_budget', 'w0_valid', 'codex_config_valid',
                 'settings_v9', 'kit_built_for_arm', 'frameworks_are_private_copies', 'prereg_present'):
        assert checks[name] is True, name
    assert info['order_sha256'] == arms.order_sha256(arms.question_order(manifest['task_ids'], 'pf-seed', 1))
    assert json.loads((tmp_path / 'barrier/order.json').read_text())['order_sha256'] == info['order_sha256']
    assert info['prereg_pending_decisions'] and info['seed_parity']['mech-static']['within_budget']
    assert (code / 'preflight/agents-md-first-question.md').read_text().startswith(CP.COMMON.rstrip())
    assert 'submit --answer' in (code / 'preflight/readme-first-question.md').read_text()


def test_preflight_helpers_and_learner_constants():
    import learner_common as LC
    import preflight
    pending = preflight.prereg_pending()
    assert pending and all(line.startswith('- ') or line.startswith('`') or '[DECISION PENDING' in line for line in pending)
    assert any('temperature' in line for line in pending) and any('CPI' in line for line in pending)
    names = preflight.source_names()
    assert {'campaign.py', 'arms.py', 'PREREG.md', 'PROTOCOL.md', '.task-paths.json'} <= set(names)
    assert CP.Campaign.LEARNER_FILES == (LC.STATE_FILE, LC.LEDGER_FILE)
    assert set(LC.OFFICEQA_ISSUE47) == set(data.ISSUE_47) and len(data.ISSUE_47) == 18
    assert NR.render_note(note('x', ['y'], text=' a  b ')) == LC.render_note(note('x', ['y'], text=' a  b '))


# ---------------------------------------------------------------------------------------------
# Review fixes (S6-orchestration-review): arm identity, withheld verification, learned text, temperature,
# readiness trees, learner compute runner
# ---------------------------------------------------------------------------------------------


def test_root_names_and_run_paths_do_not_reveal_the_arm(tmp_path, code):
    for name in ('mech-evolve-r1', 'officeqa-pro133-cards-v8-work', 'plain-r2', 'textevolve', 'r1-none'):
        with pytest.raises(ValueError, match='reveals the arm'):
            arms.parse_config({'arm': 'plain', 'repeat': 1, 'order_seed': 's'}, root_name=name)
    assert arms.parse_config({'arm': 'mech-evolve', 'repeat': 1, 'order_seed': 's'}, root_name='v9-r1-a').root == 'v9-r1-a'
    root, spec, lay = make_root(tmp_path, 'mech-static', code)
    assert lay.runs() == lay.runs('mech-static') == root / 'runs/episodes'
    run_campaign(lay, spec, hooks(lay, spec.arm))
    assert failed_names(root, code) == set()
    run1 = root / 'runs/episodes/001'
    marker_path = run1 / 'workspace' / acting.MARKER
    os.chmod(marker_path, 0o644)
    marker = json.loads(marker_path.read_text())
    marker_path.write_text(json.dumps({**marker, 'workspace': str(root / 'runs/mech-static/001/workspace')}))
    agents = run1 / 'workspace/AGENTS.md'
    os.chmod(agents, 0o644)
    agents.write_text(agents.read_text() + '\nYou are in the mech-static arm.\n')
    readme = root / 'runs/episodes/002/workspace/README.md'
    os.chmod(readme, 0o644)
    readme.write_text(readme.read_text().split('# Submitting your answer')[0])
    request = root / 'runs/episodes/003/api/001/request.json'
    request.write_text(json.dumps({**json.loads(request.read_text()), 'temperature': 0.7}))
    failed = failed_names(root, code)
    assert 'mech-static/001 workspace path names no arm (model-visible)' in failed
    assert 'mech-static/001 AGENTS.md re-derives from COMMON + adapter + chosen notes' in failed
    assert 'mech-static/002 README sections match the arm (tools iff l1, submit iff controller)' in failed
    assert 'mech-static/003 api/001 temperature as frozen' in failed


def test_verification_runner_refuses_withheld_questions(tmp_path, code):
    root, spec, lay = make_root(tmp_path, 'text-evolve', code)
    campaign = CP.Campaign(KEY, spec, lay, hooks=hooks(lay, spec.arm))
    campaign.load(resume=False)
    runner = campaign.verification_runner(2, lay.adapters / 'text-w0')        # every question is in blocks 1-2
    for task_id in data.ISSUE_47[:2]:                                          # UID0001, UID0004 (synthetic)
        assert task_id in campaign.order
        with pytest.raises(ValueError, match='released questions'):
            runner(lay.adapters / 'text-w0', {'task_id': task_id, 'question': campaign.tasks[task_id]['question']},
                   lay.evolution(2) / 'verify/a' / task_id)


def test_learners_get_the_gate_compute_runner(tmp_path, code):
    root, spec, lay = make_root(tmp_path, 'mech-evolve', code)
    record = []
    run_campaign(lay, spec, hooks(lay, spec.arm, learn=fake_learner(record, note=False)))
    runner = record[0]['runner']
    assert os.path.realpath(runner.python) == os.path.realpath(CP.ACTOR_PYTHON)
    assert os.path.realpath(lay.corpus) in runner.read_roots


def test_audit_requires_note_admission_and_a_clean_leak_screen(tmp_path_factory, code):
    root, spec, lay = make_root(tmp_path_factory.mktemp('a'), 'text-evolve', code)
    run_campaign(lay, spec, hooks(lay, spec.arm, learn=fake_learner([], admit_note=False)))
    assert 'round 1 served note percent_share admitted by paired re-runs' in failed_names(root, code)
    leaky = 'A percent means x100. Known answers: ' + ', '.join(f'A-{t}' for t in TASK_IDS) + '.'
    root, spec, lay = make_root(tmp_path_factory.mktemp('b'), 'text-evolve', code)
    run_campaign(lay, spec, hooks(lay, spec.arm, learn=fake_learner([], note=leaky)))
    failed = failed_names(root, code)
    assert 'round 1 served note percent_share passes the leak screen' in failed
    assert 'round 1 served note percent_share admitted by paired re-runs' not in failed


def test_declared_temperature_must_be_the_one_the_broker_sends(tmp_path, code, monkeypatch):
    for value in (0, 0.7):
        assert CP.broker_temperature(value) == value == CP.settings_record(value)['broker_temperature']
    root, spec, lay = make_root(tmp_path, 'plain', code)
    spec = replace(spec, temperature=0.7)
    import broker
    v8_normalized = broker.normalized
    # a broker that ignores its temperature option (the v8 code sent a hard-coded 0) must stop the campaign
    monkeypatch.setattr(broker, 'normalized', lambda payload, remaining, max_response=65536, temperature=0:
                        v8_normalized(payload, remaining, max_response, 0))
    with pytest.raises(ValueError, match='broker sends'):
        CP.Campaign(KEY, spec, lay, hooks=hooks(lay, spec.arm)).load(resume=False)


def test_group_abort_stops_a_running_learner_round(tmp_path, code):
    group = {'dir': str(tmp_path / 'barrier'), 'roots': ['root', 'peer'], 'timeout_s': 30, 'poll_s': 0.05}
    root, spec, lay = make_root(tmp_path, 'text-evolve', code, barrier=group)
    peer = B.FileBarrier(tmp_path / 'barrier', ['root', 'peer'], 'peer', timeout_s=5)
    peer.arrive(0)
    seen = {}

    def learn(*, stop, output, source, **_):
        Path(output).mkdir(parents=True)
        peer.abort('peer crashed while this root was learning')
        seen['stopped'] = stop.wait(10)          # the watcher must set the learner's stop event
        return {'status': 'failed', 'activated': False, 'candidate': str(source), 'error': 'aborted by stop'}
    with pytest.raises(B.BarrierAborted, match='during learning round 1'):
        run_campaign(lay, spec, hooks(lay, spec.arm, learn=learn))
    assert seen['stopped'] is True
    state = json.loads((root / 'state.json').read_text())
    assert state['status'] == 'aborted' and 'peer crashed' in state['error']


def test_readiness_covers_the_code_trees(tmp_path_factory, monkeypatch):
    import preflight
    import run as runner
    copy = code_copy(tmp_path_factory)
    monkeypatch.setattr(CP, 'ROOT', copy)
    monkeypatch.setattr(preflight, 'ROOT', copy)
    readiness = {'passed': True, 'source_hashes': {n: CP.sha(copy / n) for n in ('campaign.py', 'arms.py')},
                 'source_trees': preflight.source_trees(), 'prereg_sha256': CP.sha(copy / 'PREREG.md')}
    assert set(readiness['source_trees']) == {'harness_core', 'packs', 'adapters'}
    CP.save(copy / 'preflight/readiness.json', readiness)
    assert runner.require_readiness()['passed']
    rule = copy / 'adapters/mech-w0/rules/scale.json'
    rule.write_text(rule.read_text().replace('"priority": 20', '"priority": 21'))
    with pytest.raises(ValueError, match='adapters/ changed after readiness'):
        runner.require_readiness()


# ---------------------------------------------------------------------------------------------
# $0 live tier: the composed workspace under the real pinned Codex (opt-in)
# ---------------------------------------------------------------------------------------------


@pytest.mark.live_codex
def test_live_codex_composed_mech_episode_submits_through_the_kit(tmp_path, code):
    import fake_upstream as fu
    import paths
    import rate_control
    if not paths.CODEX.is_file() or not Path('/usr/bin/sandbox-exec').is_file():
        pytest.skip('pinned codex or sandbox-exec missing')
    root, spec, lay = make_root(tmp_path, 'mech-static', code)
    arm = spec.arm
    task = {'task_id': 'UIDT002', 'question': QUESTIONS[1]}
    up = fu.Upstream([fu.response([fu.call('cat README.md | head -3; find_tables "fiscal operations" --limit 2')]),
                      fu.response([fu.call('submit --answer "2,010,894"')]),
                      fu.response([fu.message('2,010,894')])])
    scheduler = rate_control.RateScheduler(max_in_flight=float('inf'), min_interval=0, backoffs=(0,))
    try:
        prep = CP.prepare_episode(lay, arm, task, arms.w0_dir(arm, lay.adapters), root / 'live', ordinal=2)
        saved = CP.ACTOR_SECONDS
        CP.ACTOR_SECONDS = 180
        try:
            summary = CP.execute_episode(KEY, lay, arm, prep, broker_extra={'upstream_url': up.url, 'scheduler': scheduler})
        finally:
            CP.ACTOR_SECONDS = saved
    finally:
        up.close()
    assert summary['status'] == 'completed' and summary['answer'] == '2,010,894' and summary['kit_intact']
    assert summary['controller']['submits'] == 1 and summary['fallback_answer'] == '2,010,894'
    outputs = [item for body in up.bodies for item in body['input'] if item.get('type') == 'function_call_output']
    text = '\n'.join(str(o.get('output')) for o in outputs)
    assert '# Environment' in text and '[harness:gate]' in text          # README visible; submit reached the gate
    assert 'FFO-1' in text or 'TABLE' in text or 'Fiscal Operations' in text   # find_tables ran on the tables index
    assert 'Operation not permitted' not in text and 'command not found' not in text
    first = up.bodies[0]['input']
    agents = [p['text'] for item in first if item.get('role') == 'user' for p in item['content'] if 'OfficeQA task workspace' in p['text']]
    assert agents and '# Finishing a question' in agents[0]
    invocation = json.loads((root / 'live/records/invocation.json').read_text())
    assert invocation['settings']['include_permissions_instructions'] is False
    assert invocation['settings']['model_auto_compact_token_limit'] == 244800
    assert invocation['settings']['features.multi_agent'] is False


@pytest.mark.live_codex
def test_live_codex_smoke_path_at_zero_cost(tmp_path, monkeypatch):
    """run.smoke (the paid readiness round-trip) end to end with the real Codex binary against a local fake
    upstream: note.txt is read with the shell, controller arms submit it, the final answer is the marker."""
    import fake_upstream as fu
    import paths
    import rate_control
    import run
    if not paths.CODEX.is_file() or not Path('/usr/bin/sandbox-exec').is_file():
        pytest.skip('pinned codex or sandbox-exec missing')

    def responder(body, index):
        outputs = [str(i.get('output')) for i in body['input'] if i.get('type') == 'function_call_output']
        agents = json.dumps(body['input'][:3])
        if not outputs:
            return fu.response([fu.call('cat note.txt')])
        marker = next((w for w in outputs[0].split() if w.startswith('TRANSPORT_')), 'missing')
        if '# Finishing a question' in agents and not any('[harness:gate]' in o for o in outputs):
            return fu.response([fu.call(f'submit --answer "{marker}"')])
        return fu.response([fu.message(marker)])
    up = fu.Upstream(responder)
    scheduler = rate_control.RateScheduler(max_in_flight=float('inf'), min_interval=0, backoffs=(0,))
    saved = CP.ACTOR_SECONDS
    CP.ACTOR_SECONDS = 180
    try:
        monkeypatch.setattr(CP, 'CAMPAIGN', None)     # the first smoke checks the defaults, in any root
        result = run.smoke(KEY, 'smoke-t', ['plain', 'mech-static'], base=tmp_path,
                           broker_extra={'upstream_url': up.url, 'scheduler': scheduler})
        # a pilot cell-c root (T=1.0, top_p 0.95): its smoke sends and checks exactly that
        cell = arms.parse_config({'arm': 'text-static', 'repeat': 1, 'order_seed': 's', 'temperature': 1.0,
                                  'top_p': 0.95}, root_name='v9-p1-c')
        monkeypatch.setattr(CP, 'CAMPAIGN', cell)
        sent_before = len(up.bodies)
        cell_result = run.smoke(KEY, 'smoke-c', None, base=tmp_path,
                                broker_extra={'upstream_url': up.url, 'scheduler': scheduler})
    finally:
        CP.ACTOR_SECONDS = saved
        up.close()
    assert result['passed'], [(r['arm'], r['summary']['status'], r['summary']['answer'], r['reasoning_ok'],
                               r['controller_ok']) for r in result['results']]
    mech = next(r for r in result['results'] if r['arm'] == 'mech-static')
    assert mech['summary']['controller']['submits'] == 1
    assert all(r['sampling_ok'] for r in result['results']) and result['top_p'] is None     # T=0, no top_p sent
    assert cell_result['passed'] and cell_result['results'][0]['sampling_ok'] and cell_result['top_p'] == 0.95
    assert [(b['temperature'], b['top_p']) for b in up.bodies[sent_before:]] == [(1.0, 0.95)] * (len(up.bodies) - sent_before)
    assert json.loads((tmp_path / 'smoke-t/verification.json').read_text())['passed'] is True
