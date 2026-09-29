"""v10 orchestration tests (port of the v9 S6 suite): question order, barrier, notes, README/AGENTS composition
per arm, the neutral code root, manifest-driven kit wrappers, the controls-driven controller (NullGate when the
gate is off), parser modes and observation coverage in the episode summary, the ToolRunner seam, the campaign
loop (seal -> release -> learn -> next adapter; resume), the audit on synthetic roots, cost.

Everything here is $0 and offline: a synthetic corpus and answers (tests/synthetic_officeqa.py), a fake
broker/actor (or the real broker driven by tests/codexsim.py against tests/fake_upstream.py), fake learners, an
injected scorer. No real reference answer, grader output or benchmark question is read. The live-Codex tests
are opt-in (`-m live_codex` / V9_LIVE_CODEX=1): the pinned Codex binary against a local fake upstream, $0.

Gate-off sites (DESIGN §3), each asserted here on a `harness-static` episode: no `submit` wrapper, no
HARNESS_SUBMIT_URL, no submit README section, POST /v1/submit -> 404, no enforce rows; `harness-gate` (a
test-only custom arm with the gate on, over a gate-on copy of the harness W0) keeps the submit path covered.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import time
import types
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import acting  # noqa: E402
import arms  # noqa: E402
import barrier as B  # noqa: E402
import campaign as CP  # noqa: E402
import controller_runtime as CR  # noqa: E402
import cost  # noqa: E402
import data  # noqa: E402
import experiment  # noqa: E402
import full_audit  # noqa: E402
import kit  # noqa: E402
import notes_render as NR  # noqa: E402
import report  # noqa: E402
import synthetic_officeqa as S  # noqa: E402
from harness_core import contracts as C  # noqa: E402

MODEL = 'accounts/fireworks/models/glm-5p3-flash'
KEY = 'dummy-orchestration-key-not-a-secret'
SCORER = lambda reference, predicted, tolerance=0.0: float(reference.strip() == (predicted or '').strip())  # noqa: E731
QUESTIONS, TASK_IDS = S.QUESTIONS, S.TASK_IDS
GATE_ARM = replace(C.STANDARD_ARMS['harness-static'], name='harness-gate', gate=True)
ARMS = {**C.STANDARD_ARMS, 'harness-gate': GATE_ARM}

# ---------------------------------------------------------------------------------------------
# Synthetic data and adapters
# ---------------------------------------------------------------------------------------------


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
    if not (target / 'PREREG.md').is_file():        # PREREG-v10 is written after S4: a v10-headed placeholder
        (target / 'PREREG.md').write_text(TEST_PREREG)
    for name in ('harness_core', 'packs', 'adapters'):
        shutil.copytree(ROOT / name, target / name, ignore=ignore)
    return target


TEST_PREREG = ('# v10 pre-registration (test placeholder)\n\nWritten by the tests for a code copy: the real '
               'PREREG.md is written and hashed after S4 (DESIGN §7).\n')


@pytest.fixture(scope='session')
def code(tmp_path_factory):
    return code_copy(tmp_path_factory)


def with_revision(adapter: Path) -> Path:
    """Stamp adapter.json with the tree's revision (what write_revision does for a candidate)."""
    info = json.loads((adapter / 'adapter.json').read_text())
    info['revision'] = arms.revision(adapter)
    (adapter / 'adapter.json').write_text(json.dumps(info, indent=2, sort_keys=True) + '\n')
    return adapter


def gate_adapter(source: Path, dest: Path) -> Path:
    """A gate-on copy of the harness W0 (controls gate=true, one enforcement re-query), revision stamped."""
    adapter = arms.copy_adapter(source, dest)
    controls = json.loads((adapter / 'controls.json').read_text())
    controls.update(gate=True, enforce_requery_max=1)
    (adapter / 'controls.json').write_text(json.dumps(controls, indent=2, sort_keys=True) + '\n')
    return with_revision(adapter)


def make_spec(arm_name, *, repeat=1, barrier=None, root='root', pack='officeqa', seed='test-order-seed'):
    config = {'profile': 'v10', 'arm': arm_name, 'repeat': repeat, 'pack': pack, 'order_seed': seed}
    if barrier is not None:
        config['barrier'] = barrier
    return arms.parse_config(config, root_name=root)


def make_prepared(root: Path, spec, *, n=7):
    """root/prepared in the v10 layout with the synthetic two-issue corpus, tables index and answers."""
    prepared = root / 'prepared'
    corpus = S.make_corpus(prepared / 'agent/corpus')
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
    source = S.build_tables_index(corpus, root / 'tables-src' / 'tables.sqlite')
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
COVERAGE = {'mode': 'rerun', 'revision': 'w0', 'commands': 1, 'invocation': 1, 'header': 0, 'raw': 0, 'none': 0}


class FakeBroker:
    local = threading.local()          # the broker of the episode running on this thread

    def __init__(self, key, api, *, controller=None, actor_deadline=None, submit_enabled=None, **options):
        self.api, self.controller, self.options = Path(api), controller, options
        self.submit_enabled = (controller is not None) if submit_enabled is None else submit_enabled
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
            summary.update(submits=1 if self.submit_enabled else 0, codex_requests=1, controller_calls=self.requeries,
                           inserts_by_reason={}, submit_enabled=self.submit_enabled, gate=self.submit_enabled,
                           observation_coverage=dict(COVERAGE))
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
        if arm.gate and relay is not None and answer:
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


def fake_learner(record, *, note=True, fail=False, verify=True, admit_note=True):
    """A learner with learner_common's calling convention: validates the released evidence with the real
    builder, runs one verification episode, and (block 1) admits a note into output/adapter (revision stamped)."""
    def learn(*, key, source, evidence, output, block, stop, actor_runner, pack, grader, root, w0, exclude, runner,
              arm, tool_runner=None, verify_dir=None):
        import evidence as EV
        record.append({'block': block, 'source': str(source), 'evidence': str(evidence), 'exclude': list(exclude),
                       'w0': str(w0), 'runner': runner, 'tool_runner': tool_runner, 'arm': arm})
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
                                   Path(verify_dir) / 'probe' / '01')
            record[-1]['verify_answer'] = summary['answer']
        if block != 1 or not note:
            return {'status': 'retained', 'activated': False, 'candidate': str(source), 'block': block}
        candidate = output / 'adapter'
        arms.copy_adapter(Path(source), candidate)
        text = note if isinstance(note, str) else 'A percent means x100: percent means the ratio times 100.'
        NR.save_note(candidate, C.NoteSpec(id='percent_share', triggers=['percent'], text=text, admitted_block=1,
                                           origin='learned'))
        with_revision(candidate)
        if admit_note:      # the learning-ledger row of a note admitted by paired re-runs (text and triggers as served)
            with (Path(root) / 'learning-ledger.jsonl').open('a') as stream:
                stream.write(json.dumps({'t': block, 'kind': 'note', 'action': 'add', 'id': 'percent_share',
                                         'admitted': True, 'text': text, 'triggers': ['percent']}) + '\n')
        control = output / 'sessions/learner/control'
        control.mkdir(parents=True)
        (control / 'mechanical-admission.json').write_text(json.dumps({'admitted': True, 'admitted_notes': ['percent_share'],
                                                                       'admitted_revision': None, 'revision': None,
                                                                       'candidate_revision': CP.revision(candidate)}))
        result = {'status': 'admitted', 'activated': True, 'candidate': str(candidate), 'block': block,
                  'revision': CP.revision(candidate)}
        (output / 'result.json').write_text(json.dumps(result))
        return result
    return learn


def hooks(lay, arm, **kw):
    learner = kw.pop('learn', None)
    return CP.Hooks(episode=fake_episode(lay, arm, **kw), learn=learner, scorer=SCORER, verify_data=False,
                    deep_runtime=False, report=True)

# ---------------------------------------------------------------------------------------------
# arms.py / experiment.py
# ---------------------------------------------------------------------------------------------


def test_experiment_constants_without_a_campaign():
    # preflight.py runs this suite inside every campaign root, whose .task-paths.json names an arm
    import paths
    if 'arm' in paths._config:
        assert experiment.CAMPAIGN is not None and experiment.ARM == experiment.CAMPAIGN.arm
    else:
        assert experiment.CAMPAIGN is None and experiment.ARM is None
    assert experiment.PROFILE == 'v10' == arms.PROFILE == CP.PROFILE
    assert experiment.FULL is True and experiment.MWM is False and experiment.PROJECT_DOC_MAX_BYTES == 4194304
    assert experiment.ACTOR_SECONDS == 3600 and experiment.LEARN_EVERY == 5 and experiment.LEARN_LAST_BLOCK is False
    assert experiment.BROKER_OPTIONS == {'max_completion': None, 'max_response': None, 'request_timeout': None,
                                         'max_attempts': None}
    assert experiment.REQUERY_WHEN_CLEAN is False
    assert experiment.TEMPERATURE == (experiment.CAMPAIGN.temperature if experiment.CAMPAIGN else 0)
    assert {arms.architecture(a) for a in C.STANDARD_ARMS.values()} == {'none', 'prompt', 'harness'}
    assert CP.LEARNER_MODULES == {'text': 'prompt_learning', 'harness': 'harness_learning'}
    assert set(CP.LEARNER_MODULES) == {a.learner for a in C.STANDARD_ARMS.values() if a.learner}
    source = (ROOT / 'experiment.py').read_text() + (ROOT / 'campaign.py').read_text()
    assert "in PROFILE" not in source and "'cards' in" not in source and "'prompt_only' in" not in source
    with pytest.raises(ValueError, match='v9'):
        make_spec('harness-evolve', seed='s').arm if False else arms.parse_config(
            {'profile': 'v9', 'arm': 'harness-evolve', 'repeat': 1, 'order_seed': 's'}, root_name='r')


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
# notes_render.py and the text budget pin
# ---------------------------------------------------------------------------------------------


def note(id, triggers, text='Some advice.', block=1):
    return C.NoteSpec(id=id, triggers=triggers, text=text, admitted_block=block, origin='learned')


def test_notes_choose_at_most_three_in_block_then_id_order(tmp_path):
    adapter = arms.copy_adapter(ROOT / 'adapters/prompt-w0', tmp_path / 'a')
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


def test_controller_templates_are_counted_per_gate_and_pinned_to_arms(tmp_path):
    """DESIGN §3: the gate-off templates (no `submit` wording) are what a standard harness arm counts in its
    model-visible budget; a gate-on adapter counts the v9 wording. arms.harness_fixed_text follows
    controller_runtime.fixed_templates, so the two can never drift."""
    off, on = CR.fixed_templates(False), CR.fixed_templates(True)
    assert set(off) == {'loop_template', 'loop_options', 'loop_escalate', 'hard_block'}
    assert set(on) == {'loop_template', 'loop_options', 'loop_escalate', 'enforce_template'}
    assert not any('submit' in t for t in off.values()) and all('submit' in on[k] for k in ('loop_options', 'loop_escalate'))
    for gate in (False, True):
        fixed = arms.harness_fixed_text(gate)
        assert sum(v for k, v in fixed.items() if k not in arms.RECORDED_ONLY) == CR.template_bytes(gate)
        assert ('hard_block_echo_cap' in fixed) is (not gate)               # recorded (gate off), never counted
    assert arms.harness_fixed_text(False)['hard_block_echo_cap'] == CR.HARD_BLOCK_ECHO_BYTES == 2000
    assert arms.harness_fixed_text(False)['hard_block'] == len(CR.HARD_BLOCK_TEMPLATE.encode('utf-8'))
    assert set(arms.CONTROLLER_COUNTED) == set(off) and set(arms.RECORDED_ONLY) >= set(arms.GATE_FRAME)
    harness = arms.copy_adapter(ROOT / 'adapters/harness-w0', tmp_path / 'h')
    assert arms.model_visible(harness, C.STANDARD_ARMS['harness-static'])['controller'] == CR.template_bytes(False)
    gated = gate_adapter(ROOT / 'adapters/harness-w0', tmp_path / 'g')
    assert arms.model_visible(gated, GATE_ARM)['controller'] == CR.template_bytes(True)
    assert arms.model_visible(ROOT / 'adapters/prompt-w0', C.STANDARD_ARMS['prompt-static'])['controller'] == 0


def test_model_visible_equals_the_learners_text_budget(tmp_path):
    """arms.model_visible (the W0 checks, PREREG numbers) and learner_common.text_budget (admission) are one
    formula: SEED.md + rendered notes + tools/USAGE.md (harness) + the controller's counted templates."""
    import learner_common as LC
    from packs.officeqa.pack import OfficeQAPack
    for name in ('prompt-static', 'harness-static'):
        arm = C.STANDARD_ARMS[name]
        adapter = arms.copy_adapter(arms.w0_dir(arm), tmp_path / name)
        NR.save_note(adapter, note('share', ['percent'], text='A note with   spaces.'))
        mine = arms.model_visible(adapter, arm)
        seed, _ = LC.seed_text(adapter, OfficeQAPack(), C.ARCHITECTURES[arm.seed])
        usage = (adapter / 'tools/USAGE.md').read_text() if arm.l1_tools else ''
        theirs = LC.text_budget(seed, LC.load_notes(adapter), [], LC.controller_template_bytes(arm.gate) if arm.controller else 0,
                                usage)
        assert mine['total'] == theirs['total'] and mine['within_budget'] == theirs['ok']
        assert mine['usage'] == theirs['usage'] and mine['controller'] == theirs['controller'] and mine['total'] > 0
    gated = gate_adapter(ROOT / 'adapters/harness-w0', tmp_path / 'g')
    assert arms.model_visible(gated, GATE_ARM)['controller'] == LC.controller_template_bytes(True) > LC.controller_template_bytes(False)

# ---------------------------------------------------------------------------------------------
# Episode composition per arm: README, AGENTS.md, the neutral code root, the kit
# ---------------------------------------------------------------------------------------------


def test_compose_readme_sections_follow_tools_and_gate():
    pack = arms.load_pack('officeqa')
    usage = (ROOT / 'adapters/harness-w0/tools/USAGE.md').read_text()
    plain, _ = CP.compose_readme(C.STANDARD_ARMS['plain'], pack, '/c', '/i')
    prompt, _ = CP.compose_readme(C.STANDARD_ARMS['prompt-evolve'], pack, '/c', '/i')
    harness, parts = CP.compose_readme(C.STANDARD_ARMS['harness-evolve'], pack, '/c', '/i', usage)
    gated, _ = CP.compose_readme(GATE_ARM, pack, '/c', '/i', usage)
    assert plain == prompt and CP.TOOLS_HEADING not in plain and 'submit --answer' not in plain
    assert harness == plain + '\n' + CP.TOOLS_HEADING + '\n\n' + usage.rstrip() + '\n'
    assert 'find_tables' in harness and 'calc -c' in harness and '# Submitting your answer' not in harness
    assert gated == harness + '\n' + CP.SUBMIT_README.rstrip() + '\n' and parts['tools'] == len((CP.TOOLS_HEADING + '\n\n' + usage.rstrip() + '\n').encode())
    with pytest.raises(ValueError, match='USAGE.md text is required'):
        CP.compose_readme(C.STANDARD_ARMS['harness-static'], pack, '/c', '/i')
    with pytest.raises(ValueError, match='serves no tools'):
        CP.compose_readme(C.STANDARD_ARMS['prompt-static'], pack, '/c', '/i', usage)
    assert not hasattr(CP, 'CALC_README') and not hasattr(CP, 'tool_wrappers')


def test_stage_code_root_is_neutral_read_only_and_idempotent(tmp_path, code):
    lay = CP.Layout(root=tmp_path / 'root', code=code)
    (tmp_path / 'root').mkdir()
    w0 = lay.adapters / 'harness-w0'
    tools, sha12 = CP.stage_code_root(lay, w0)
    assert sha12 == CP.revision(w0) == arms.revision(w0) and len(sha12) == 12
    assert tools == lay.code_roots / sha12 / 'tools' and tools.is_dir()
    assert not any(part in ('adapters', 'harness-w0', 'evolution') for part in tools.relative_to(lay.root).parts)
    assert CP.tools_snapshot(tools) == CP.tools_snapshot(w0 / 'tools')
    assert all(not os.access(p, os.W_OK) for p in tools.iterdir()) and not os.access(tools, os.W_OK)
    again, _ = CP.stage_code_root(lay, w0)                                   # idempotent: the same copy
    assert again == tools and len(list(lay.code_roots.iterdir())) == 1
    # a candidate revision gets its own hash-named copy; the W0 copy is untouched
    candidate = with_revision(arms.copy_adapter(w0, tmp_path / 'cand'))
    NR.save_note(candidate, note('nn', ['x']))
    with_revision(candidate)
    other, sha_other = CP.stage_code_root(lay, candidate)
    assert sha_other != sha12 and other.parent.parent == lay.code_roots and len(list(lay.code_roots.iterdir())) == 2
    # a copy that no longer equals the served revision's tools/ is refused (no silent re-copy)
    os.chmod(tools / 'USAGE.md', 0o644)
    (tools / 'USAGE.md').write_text('tampered\n')
    with pytest.raises(ValueError, match='differs from the served revision'):
        CP.stage_code_root(lay, w0)
    # the path is checked for arm-revealing fragments like the workspace path (relative to the root's parent)
    revealing = CP.Layout(root=tmp_path / 'prompt-root', code=code)
    (tmp_path / 'prompt-root').mkdir()
    with pytest.raises(ValueError, match='reveals the arm'):
        CP.stage_code_root(revealing, w0)
    with pytest.raises(ValueError, match='no tools/'):
        CP.stage_code_root(lay, lay.adapters / 'prompt-w0')


def test_adapter_tool_wrappers_come_from_the_manifest_and_point_at_the_code_root(tmp_path, code):
    lay = CP.Layout(root=tmp_path / 'root', code=code)
    (tmp_path / 'root').mkdir()
    w0 = lay.adapters / 'harness-w0'
    tools, _ = CP.stage_code_root(lay, w0)
    wrappers = CP.adapter_tool_wrappers(w0, tools)
    manifest = json.loads((w0 / 'tools/manifest.json').read_text())['tools']
    assert set(wrappers) == set(manifest) == {'find_tables', 'show_table', 'printings', 'calc'}
    for name, spec in wrappers.items():
        assert spec[0] == str(tools / manifest[name]['script']) and spec[1:] == manifest[name]['args']
    assert CP.adapter_tool_wrappers(w0)['calc'][0] == str(w0 / 'tools/calc.py')      # dry-run form
    stale = tmp_path / 'stale' / 'tools'
    shutil.copytree(tools, stale)
    os.chmod(stale, 0o755)
    os.chmod(stale / 'manifest.json', 0o644)
    (stale / 'manifest.json').write_text(json.dumps({'tools': {k: v for k, v in manifest.items() if k != 'printings'}}))
    with pytest.raises(ValueError, match='does not carry the adapter manifest'):
        CP.adapter_tool_wrappers(w0, stale)


def test_parser_mode_follows_the_tools_shas(tmp_path):
    arm = C.STANDARD_ARMS['harness-evolve']
    w0 = ROOT / 'adapters/harness-w0'
    assert CP.parser_mode(w0, arm) == 'rerun'
    usage_only = with_revision(arms.copy_adapter(w0, tmp_path / 'usage'))
    (usage_only / 'tools/USAGE.md').write_text((usage_only / 'tools/USAGE.md').read_text() + '  calc also prints its code\n')
    assert CP.parser_mode(with_revision(usage_only), arm) == 'rerun'          # USAGE.md is not a tool
    patched = arms.copy_adapter(w0, tmp_path / 'patched')
    (patched / 'tools/cli.py').write_text((patched / 'tools/cli.py').read_text() + '\n# evolved\n')
    assert CP.parser_mode(with_revision(patched), arm) == 'header'
    added = arms.copy_adapter(w0, tmp_path / 'added')
    (added / 'tools/extra.py').write_text('print(1)\n')
    assert CP.parser_mode(with_revision(added), arm) == 'header'


def test_build_controller_reads_controls_and_uses_the_null_gate_when_off(tmp_path, code):
    from harness_core.gate import Gate, NullGate
    root, spec, lay = make_root(tmp_path, 'harness-static', code)
    w0 = lay.adapters / 'harness-w0'
    controller, pack, ledger = CP.build_controller(lay, spec.arm, w0, QUESTIONS[1], root / 'api-off')
    try:
        assert isinstance(controller.gate, NullGate) and controller.gate_on is False and controller.pack is pack
        assert controller.controls == C.CONTROLS_DEFAULTS and controller.policy == C.POLICY_DEFAULTS
        assert ledger.parser_mode == 'rerun' and ledger.revision == CP.revision(w0)
        assert controller.on_final(C.View(1, 'turn', [], True, 1.0, None), {'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': '41'}]}]}, 0).action == 'forward'
        assert not (root / 'api-off/checks').exists()
    finally:
        ledger.close()
        pack.close()
    tuned = with_revision(arms.copy_adapter(w0, tmp_path / 'tuned'))
    controls = json.loads((tuned / 'controls.json').read_text())
    controls.update(loop_threshold=4, loop_notes_max=2, splice=False)
    (tuned / 'controls.json').write_text(json.dumps(controls))
    with_revision(tuned)
    (tuned / 'tools/cli.py').write_text((tuned / 'tools/cli.py').read_text() + '\n# evolved\n')
    with_revision(tuned)
    controller, pack, ledger = CP.build_controller(lay, spec.arm, tuned, QUESTIONS[1], root / 'api-tuned')
    try:
        assert (controller.controls['loop_threshold'], controller.controls['loop_notes_max'], controller.controls['splice']) == (4, 2, False)
        assert ledger.parser_mode == 'header' and ledger.revision == CP.revision(tuned)
    finally:
        ledger.close()
        pack.close()
    gated = gate_adapter(w0, tmp_path / 'gated')
    controller, pack, ledger = CP.build_controller(lay, GATE_ARM, gated, QUESTIONS[1], root / 'api-on')
    try:
        assert isinstance(controller.gate, Gate) and controller.gate_on and controller.controls['enforce_requery_max'] == 1
    finally:
        ledger.close()
        pack.close()
    with pytest.raises(ValueError, match='gate=True but the arm'):
        CP.build_controller(lay, spec.arm, gated, QUESTIONS[1], root / 'api-mismatch')
    with pytest.raises(ValueError, match='gate=False but the arm'):
        CP.build_controller(lay, GATE_ARM, w0, QUESTIONS[1], root / 'api-mismatch2')


def test_readme_agents_and_kit_per_arm(tmp_path, code):
    root, _, lay = make_root(tmp_path, 'plain', code)
    task = {'task_id': 'UIDT006', 'question': QUESTIONS[5]}
    gated = gate_adapter(lay.adapters / 'harness-w0', root / 'gated-w0')
    seen, markers = {}, {}
    for name, arm in ARMS.items():
        adapter = gated if arm is GATE_ARM else arms.w0_dir(arm, lay.adapters)
        prep = CP.prepare_episode(lay, arm, task, adapter, root / 'eps' / name, ordinal=6)
        ws = Path(prep['workspace'])
        readme, agents = (ws / 'README.md').read_text(), (ws / 'AGENTS.md').read_text()
        marker = json.loads((ws / acting.MARKER).read_text())
        tools = {p.name for p in (ws / '.kit/bin').iterdir()}
        seen[name], markers[name] = (readme, agents), marker
        assert readme.startswith(kit.readme_l0(arm, lay.corpus, lay.index))
        assert 'officeqa.py' in readme and 'Task conventions (published with the OfficeQA benchmark)' in readme
        assert (CP.TOOLS_HEADING + '\n' in readme and 'find_tables' in readme and 'calc -c' in readme) == arm.l1_tools
        assert ('# Table and calculation tools' not in readme)
        assert ('submit --answer' in readme) == arm.gate == ('submit' in tools)          # site: README + kit
        assert {'find_tables', 'show_table', 'printings', 'calc'} <= tools if arm.l1_tools else \
            tools == {'python', 'python3', 'rg'}
        assert agents.startswith(CP.COMMON.rstrip())
        if arm.seed == 'none':
            assert agents == CP.COMMON.rstrip() + '\n'
        else:
            assert (adapter / 'SEED.md').read_text().strip() in agents
        extra = marker['extra']
        assert extra['notes'] == [] and extra['task_id'] == 'UIDT006' and marker['arm'] == asdict(arm)
        assert (ws / 'officeqa.py').is_file() and (extra['adapter_revision'] == (CP.revision(adapter) if adapter else None))
        if arm.l1_tools:
            # the neutral code root: hash-named, beside the workspace, embedded in every wrapper
            code_root = Path(extra['code_root'])
            assert code_root == lay.code_roots / extra['code_sha12'] / 'tools' and extra['code_sha12'] == extra['adapter_revision']
            assert marker['kit']['code_roots'] == [str(code_root)]
            for tool, spec in marker['kit']['tools'].items():
                if tool != 'submit':
                    assert spec['script'].startswith(str(code_root) + '/'), tool
                    assert code_root.as_posix() in (ws / '.kit/bin' / tool).read_text()
            assert not any('adapters' in f or 'harness-w0' in f for f in marker['kit']['code']['files'])
            granted = set(map(os.path.basename, marker['kit']['code']['files']))
            assert granted == {'cli.py', 'otables.py', 'calc.py'} | ({'submit.py'} if arm.gate else set())  # submit.py: base harness
        else:
            assert extra['code_root'] is None and extra['code_sha12'] is None and marker['kit']['code_roots'] == []
        assert json.loads((root / 'eps' / name / 'task.json').read_text())['question'] == QUESTIONS[5]
    assert seen['prompt-static'] == seen['prompt-evolve']
    assert seen['harness-static'] == seen['harness-evolve'] == seen['harness-evolve-notes']
    assert seen['harness-gate'][0] == seen['harness-static'][0] + '\n' + CP.SUBMIT_README.rstrip() + '\n'
    assert seen['harness-gate'][1] == seen['harness-static'][1]
    assert seen['harness-static'][0] == seen['prompt-static'][0] + '\n' + CP.TOOLS_HEADING + '\n\n' + \
        (lay.adapters / 'harness-w0/tools/USAGE.md').read_text().rstrip() + '\n'
    # the three harness arms share one code root; the gate adapter (a different revision) has its own
    assert len({markers[n]['extra']['code_root'] for n in ('harness-static', 'harness-evolve', 'harness-evolve-notes')}) == 1
    assert markers['harness-gate']['extra']['code_root'] != markers['harness-static']['extra']['code_root']
    # the L0 part and the conventions are byte-identical in every arm
    l0 = {v[0].split(CP.TOOLS_HEADING + '\n')[0].rstrip() for v in seen.values()}
    assert len(l0) == 1
    # site: HARNESS_SUBMIT_URL iff gate, the tables index for every harness arm
    for name, arm in ARMS.items():
        env_extra, data_roots = CP.actor_data(arm, lay)
        settings = acting.build_settings(root / 'eps' / name / 'workspace', root / 'out' / name, lay.corpus, lay.index,
                                         None, endpoint='http://127.0.0.1:1/v1', arm=arm, env_extra=env_extra, data=data_roots)
        env = settings['shell_environment_policy.set']
        assert ('HARNESS_SUBMIT_URL' in env) == arm.gate and ('TABLES_INDEX' in env) == arm.l1_tools


def test_episode_through_the_real_broker_and_controller(tmp_path, code):
    """execute_episode with the real broker (+ real Ledger/Gate-or-NullGate/MechController on the synthetic
    tables index) driven by CodexSim through a fake upstream: records, summary and the audit's record
    checks. harness-static (gate off): the tool pages are parsed by invocation (coverage), /v1/submit is 404,
    no checks, no fallback; harness-gate: the v9 submit path; plain: no controller records."""
    import fake_upstream as fu
    import rate_control
    from codexsim import CodexSim
    from harness_core.tools import calc as calc_tool
    from packs.officeqa.tools import cli, otables
    root, _, lay = make_root(tmp_path, 'harness-static', code)
    task = {'task_id': 'UIDT002', 'question': QUESTIONS[1]}
    fname, header_line = 'treasury_bulletin_2003_03.txt', S.table_header_line(lay.corpus, '2003_03')
    tables = otables.Tables(lay.corpus, lay.tables_index)
    page = cli.page('show_table', cli.parser('show_table').parse_args([fname, str(header_line)]), tables).text
    tables.close()
    code_text = 'print(2010894)'
    frame = calc_tool.frame(calc_tool.normalise(code_text), calc_tool.execute(calc_tool.normalise(code_text), timeout=30)[1])
    gated = gate_adapter(lay.adapters / 'harness-w0', root / 'gated-w0')
    results, submit_codes = {}, {}
    for name, arm in (('harness-static', ARMS['harness-static']), ('harness-gate', GATE_ARM), ('plain', ARMS['plain'])):
        script = [fu.response([fu.call(f'show_table {fname} {header_line}')]), fu.response([fu.call("calc -c 'print(2010894)'")])]
        if arm.gate:
            script.append(fu.response([fu.call('submit --answer "2,010,894"')]))
        script.append(fu.response([fu.message('2,010,894')]))
        up = fu.Upstream(script)
        adapter = gated if arm.gate else arms.w0_dir(arm, lay.adapters)
        prep = CP.prepare_episode(lay, arm, task, adapter, root / name, ordinal=2)

        def run_actor(workspace, records, prompt, corpus, index, *, endpoint, arm, timeout, env_extra, data,
                      permissions_instructions, temperature=None, top_p=None):
            records = Path(records)
            records.mkdir(parents=True)
            settings = acting.build_settings(workspace, records, corpus, index, None, endpoint=endpoint, arm=arm,
                                             env_extra=env_extra, data=data, permissions_instructions=False)
            submit_url = endpoint.rsplit('/v1', 1)[0] + '/v1/submit'

            def post(answer):
                body = json.dumps({'answer': answer}).encode()
                request = urllib.request.Request(submit_url, data=body, headers={'Content-Type': 'application/json'})
                try:
                    with urllib.request.urlopen(request, timeout=30) as response:
                        return response.status, json.loads(response.read())
                except urllib.error.HTTPError as exc:
                    return exc.code, None

            def tool(cmd):
                if cmd.startswith('submit'):
                    status, reply = post('2,010,894')
                    submit_codes[arm.name] = status
                    return reply['message'] + '\n'
                if cmd.startswith('show_table'):
                    return page + '\n'
                if cmd.startswith('calc'):
                    return frame + '\n'
                return cmd + '\n'
            sim = CodexSim(endpoint, prompt, agents=(Path(workspace) / 'AGENTS.md').read_text(), run_tool=tool)
            final = ''
            for _ in range(6):
                response = sim.send()
                if sim.is_final(response):
                    final = sim.final_text(response)
                    break
            if not arm.gate and arm.controller:                        # site: /v1/submit is 404 with the gate off
                submit_codes[arm.name] = post('2,010,894')[0]
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
            assert all(m['origin'] == 'codex' for m in metas) and (api / 'ledger.jsonl').is_file()
            transport = json.loads((api / 'summary.json').read_text())
            coverage = summary['observation_coverage']
            assert coverage == transport['observation_coverage'] and coverage['mode'] == 'rerun'
            assert coverage['invocation'] == 1 and coverage['commands'] == 1 and coverage['revision'] == prep['adapter_revision']
            assert summary['controller']['gate'] is arm.gate and summary['controller']['submit_enabled'] is arm.gate
            assert summary['controller']['loop_notes'] == 0 and summary['code_sha12'] == prep['code_sha12']
            from harness_core.ledger import load_events
            events = load_events(api / 'ledger.jsonl')
            assert any(e.kind == 'observe' and e.value == 2010894.0 and e.attrs['parse'] == 'invocation' for e in events)
            assert any(e.kind == 'compute' and e.values == [2010894.0] for e in events)
            if arm.gate:
                assert submit_codes[name] == 200 and summary['controller']['submits'] == 1
                assert summary['fallback_answer'] == '2,010,894' and summary['fallback_differs'] is False
                assert (api / 'checks/001.json').is_file()
            else:
                assert submit_codes[name] == 404 and summary['controller']['submits'] == 0        # site: 404
                assert summary['fallback_answer'] is None and summary['fallback_differs'] is None
                assert not (api / 'checks').exists() and not any(e.kind == 'candidate' for e in events)
                rows = [json.loads(line) for line in (api / 'controller.jsonl').read_text().splitlines()]
                assert not any(r.get('detail', {}).get('event') == 'enforce' for r in rows)        # site: no enforce rows
                assert any(r.get('detail', {}).get('reason') == 'gate_off' for r in rows)
                assert transport['enforce_requeries'] == 0 and transport['submit_enabled'] is False
        else:
            assert summary['controller'] is None and summary['fallback_answer'] is None
            assert summary['observation_coverage'] is None and summary['code_root'] is None
            assert not any(set(m) & set(full_audit.V9_RECORD_FIELDS) for m in metas)
            assert not (api / 'ledger.jsonl').exists() and not (api / 'controller.jsonl').exists()
    assert results['plain']['readme_bytes'] < results['harness-static']['readme_bytes'] < results['harness-gate']['readme_bytes']

# ---------------------------------------------------------------------------------------------
# The campaign loop
# ---------------------------------------------------------------------------------------------


def run_campaign(lay, spec, h, *, resume=False, stop=None):
    return CP.Campaign(KEY, spec, lay, hooks=h, stop=stop).run(resume=resume)


@pytest.mark.parametrize('arm_name', ['prompt-evolve', 'harness-evolve', 'harness-evolve-notes'])
def test_evolve_campaign_seal_release_learn_serve_grade(tmp_path, code, arm_name):
    group = {'dir': str(tmp_path / 'barrier'), 'roots': ['root', 'peer'], 'timeout_s': 30, 'poll_s': 0.05}
    root, spec, lay = make_root(tmp_path, arm_name, code, barrier=group)
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
    assert first['exclude'] == list(data.withheld_ids(root)) and first['w0'] == str(lay.adapters / arms.W0_NAMES[spec.arm.seed])
    assert first['released'] == sorted(t for t in order[:5] if t not in data.ISSUE_47)
    assert first['references'] == sorted(f'A-{t}' for t in first['released'])
    # the v10 offers: the arm and, for harness arms, the block's ToolRunner factory (tools_dir -> ToolRunner)
    assert first['arm'] == spec.arm
    if spec.arm.l1_tools:
        assert callable(first['tool_runner']) and first['tool_runner'].block == 1
        runner = first['tool_runner'](lay.adapters / 'harness-w0' / 'tools')
        assert hasattr(runner, 'run') and Path(runner.tmp_root) == (lay.evolution(1) / 'tmp').resolve()
        # the runner works from a neutral copy under runs/code/ (review fix), never from adapters/…
        replay_copy = lay.code_roots / CP.tools_sha12(lay.adapters / 'harness-w0' / 'tools') / 'tools'
        assert Path(runner.tools_dir) == replay_copy.resolve() and CP.tools_snapshot(replay_copy) == CP.tools_snapshot(lay.adapters / 'harness-w0' / 'tools')
    else:
        assert first['tool_runner'] is None
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
    verify = json.loads((root / 'runs/verify/01/probe/01/summary.json').read_text())
    assert verify['role'] == 'verify'
    # the learned note is served (by trigger) from block 2 on, and its id recorded in the marker
    candidate = root / 'evolution/01/adapter'
    assert state['adapter']['path'] == str(candidate) and state['blocks']['2']['adapter']['path'] == str(candidate)
    assert state['adapter']['revision'] == arms.revision(candidate) == json.loads((candidate / 'adapter.json').read_text())['revision']
    for row in state['results']:
        marker = json.loads((root / 'runs/episodes' / f'{row["ordinal"]:03}' / 'workspace' / acting.MARKER).read_text())
        question = QUESTIONS[TASK_IDS.index(row['task_id'])]
        expected = ['percent_share'] if row['ordinal'] > 5 and 'percent' in question else []
        assert marker['extra']['notes'] == expected == row['notes_served']
        if spec.arm.l1_tools:
            # block 2 serves the candidate's tools from its own neutral code root (same tools, new revision)
            served = marker['extra']['code_sha12']
            assert served == (arms.revision(candidate) if row['ordinal'] > 5 else arms.revision(lay.adapters / 'harness-w0'))
            assert row['code_root'] == str(lay.code_roots / served / 'tools') and row['observation_coverage'] == COVERAGE
            assert row['controller']['gate'] is False and row['fallback_answer'] is None
    if spec.arm.l1_tools:
        assert verify['code_sha12'] == arms.revision(lay.adapters / 'harness-w0')       # verified on the source
        served_roots = {arms.revision(candidate), arms.revision(lay.adapters / 'harness-w0')}
        assert served_roots <= {p.name for p in lay.code_roots.iterdir()} <= served_roots | {replay_copy.parent.name}
    # final seal and grade (injected scorer), all and undisputed
    metrics = json.loads((root / 'private-evaluation/paired-metrics.json').read_text())
    by_task = {r['task_id']: r for r in metrics['results']}
    assert by_task['UIDT003'][arm_name] == 0.0 and metrics['denominator'] == 7
    assert metrics['undisputed']['n'] == 5 and metrics['arms'][arm_name]['n'] == 7
    ordinal6 = order.index('UIDT006') + 1
    assert by_task['UIDT006'][arm_name] == (1.0 if ordinal6 > 5 else 0.0)
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
             for name, arm in (('g1-a', 'plain'), ('g1-b', 'harness-static'))}
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
            assert all(r['fallback_answer'] is None and r['observation_coverage'] == COVERAGE for r in states[name]['results'])


def test_crash_abort_then_block_level_resume(tmp_path, code):
    group = {'dir': str(tmp_path / 'barrier'), 'roots': ['root', 'peer'], 'timeout_s': 30, 'poll_s': 0.05}
    root, spec, lay = make_root(tmp_path, 'prompt-evolve', code, barrier=group)
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
    root, spec, lay = make_root(tmp_path, 'harness-evolve', code)
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


@pytest.mark.parametrize('arm_name,module', [('prompt-evolve', 'prompt_learning'), ('harness-evolve', 'harness_learning')])
def test_real_learner_round_through_the_campaign_loop(tmp_path, code, arm_name, module):
    """The real learner (learner_common.run_round, S2) with its scripted model ($0): the campaign's release
    passes the evidence validator, actors re-run through the campaign's verification runner, the admitted note
    comes back as a candidate adapter the campaign checks and serves, and the audit passes. Skipped until the
    S2 module exists."""
    learner = pytest.importorskip(module)
    from fake_learner_model import ScriptedModel, calls, say, tool_call
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
    # verification runs live under runs/verify/01/<token>/<NN> (review fix: opaque, neutral names); the mapping
    # token -> proposal and run -> question is in the round's verification.json
    verification = json.loads((root / 'evolution/01/verification.json').read_text())
    assert verification['tokens'] == {'percent_share': 'r01'} and verification['verify_dir'] == str(root / 'runs/verify/01')
    runs = sorted(p.name for p in (root / 'runs/verify/01/r01').iterdir() if p.is_dir())
    assert runs == ['01', '02', '03', '04', '05', '06']                     # support x 2 + 2 controls x 2
    support = [r for r in verification['runs']['percent_share'] if r['q'] == ordinal]
    assert len(support) == 2 and all(r['dir'].startswith(str(root / 'runs/verify/01/r01/')) for r in support)
    summary = json.loads((Path(support[0]['dir']) / 'summary.json').read_text())
    assert summary['role'] == 'verify' and summary['answer'] == 'A-UIDT006' and summary['notes_served'] == ['percent_share']
    assert not (root / 'evolution/01/verify/percent_share').exists() or not any(
        p.is_dir() and p.name != 'adapter' for p in (root / 'evolution/01/verify/percent_share').iterdir())
    assert not arms.revealing_fragments(str(Path(support[0]['dir']).relative_to(root.parent)), spec.arm)
    result = full_audit.build(root, code=code, write=False)
    assert result['passed'], [c for c in result['checks'] if not c['passed']]


def test_verification_runner_refuses_unreleased_questions_and_foreign_dirs(tmp_path, code):
    root, spec, lay = make_root(tmp_path, 'prompt-evolve', code)
    campaign = CP.Campaign(KEY, spec, lay, hooks=hooks(lay, spec.arm))
    campaign.load(resume=False)
    w0 = lay.adapters / 'prompt-w0'
    runner = campaign.verification_runner(1, w0)
    future = campaign.order[5]
    with pytest.raises(ValueError, match='released questions'):
        runner(w0, {'task_id': future, 'question': 'x'}, lay.evolution(1) / 'verify/a/q')
    released = campaign.order[0]
    with pytest.raises(ValueError, match='frozen question'):
        runner(w0, {'task_id': released, 'question': 'another text'}, lay.evolution(1) / 'verify/a/q')
    question = campaign.tasks[released]['question']
    with pytest.raises(ValueError, match='live under'):
        runner(w0, {'task_id': released, 'question': question}, root / 'elsewhere')
    with pytest.raises(ValueError, match='live under'):
        runner(w0, {'task_id': released, 'question': question}, lay.evolution(1) / 'verify/a/q')     # the old layout
    with pytest.raises(ValueError, match='reveals the arm'):
        runner(w0, {'task_id': released, 'question': question}, lay.verify_runs(1) / 'prompt_note/q001-k1')
    with pytest.raises(ValueError, match='source or a candidate'):
        runner(lay.adapters / 'harness-w0', {'task_id': released, 'question': question}, lay.verify_runs(1) / 'a/q')
    summary = runner(w0, {'task_id': released, 'question': question}, lay.verify_runs(1) / 'a/q')
    assert summary['role'] == 'verify' and summary['ordinal'] == 1
    with pytest.raises(ValueError, match='once'):
        runner(w0, {'task_id': released, 'question': question}, lay.verify_runs(1) / 'a/q')


def test_verification_runner_serves_a_candidates_own_tools(tmp_path, code):
    """A harness candidate is verified with its own tools/ (its neutral code root), through check_adapter and
    the scored episode path; a candidate outside the round is refused."""
    root, spec, lay = make_root(tmp_path, 'harness-evolve', code)
    campaign = CP.Campaign(KEY, spec, lay, hooks=hooks(lay, spec.arm))
    campaign.load(resume=False)
    w0 = lay.adapters / 'harness-w0'
    runner = campaign.verification_runner(1, w0)
    released = campaign.order[0]
    question = campaign.tasks[released]['question']
    candidate = arms.copy_adapter(w0, lay.evolution(1) / 'cand' / 'adapter')
    (candidate / 'tools/USAGE.md').write_text((candidate / 'tools/USAGE.md').read_text() + '  calc prints its code too\n')
    with_revision(candidate)
    summary = runner(candidate, {'task_id': released, 'question': question}, lay.verify_runs(1) / 'a/q')
    assert summary['role'] == 'verify' and summary['code_sha12'] == arms.revision(candidate) != arms.revision(w0)
    assert Path(summary['code_root']) == lay.code_roots / arms.revision(candidate) / 'tools'
    assert 'calc prints its code too' in (lay.verify_runs(1) / 'a/q/workspace/README.md').read_text()
    outside = with_revision(arms.copy_adapter(w0, tmp_path / 'outside'))
    with pytest.raises(ValueError, match='source or a candidate'):
        runner(outside, {'task_id': released, 'question': question}, lay.verify_runs(1) / 'a/q2')


def test_call_learner_matches_the_learner_signature(monkeypatch):
    seen = {}

    def run_learning(key, source, evidence, output, block, stop=None, *, actor_runner, **options):
        seen.update(key=key, source=source, evidence=evidence, output=output, block=block, stop=stop,
                    actor_runner=actor_runner, **options)
        return {'status': 'retained'}
    module = types.ModuleType('prompt_learning')
    module.run_learning = run_learning
    monkeypatch.setitem(sys.modules, 'prompt_learning', module)
    offered = dict(key='k', source='s', evidence='e', output='o', block=1, stop=None, actor_runner=len,
                   pack='p', grader='g', root='r', w0='w', exclude=(), tool_runner=None, arm=C.STANDARD_ARMS['prompt-evolve'])
    assert CP.call_learner(C.STANDARD_ARMS['prompt-evolve'], **offered) == {'status': 'retained'}
    assert seen == offered
    assert CP.call_learner(C.STANDARD_ARMS['harness-evolve-notes'], **offered) == {'status': 'retained'}   # notes learner
    strict = types.ModuleType('harness_learning')
    strict.run_learning = lambda key, source, evidence, output, block, *, needed: None
    monkeypatch.setitem(sys.modules, 'harness_learning', strict)
    with pytest.raises(TypeError, match="needs \\['needed'\\]"):
        CP.call_learner(C.STANDARD_ARMS['harness-evolve'], **offered)
    with pytest.raises(KeyError):
        CP.call_learner(C.STANDARD_ARMS['plain'], **offered)


def test_tool_runner_roots_and_construction(tmp_path, code, monkeypatch):
    """DESIGN §5 ToolRunner: built in one place with the candidate tools dir, the corpus and the tables index;
    read roots may never touch evidence/, private-evaluation/, prepared/private/, runs/ or evolution/ (other
    than the candidate tools dir and tmp)."""
    from harness_core import runner as R
    root, spec, lay = make_root(tmp_path, 'harness-evolve', code)
    ok = CP.check_tool_runner_roots(root, [lay.corpus, lay.tables_index, lay.evolution(1) / 'tools'],
                                    allowed=[lay.evolution(1) / 'tools', lay.evolution(1) / 'tmp'])
    assert ok == [str(lay.corpus.resolve()), str(lay.tables_index.resolve()), str((lay.evolution(1) / 'tools').resolve())]
    for bad in (root / 'evidence', root / 'evidence/01', root / 'private-evaluation', root / 'prepared/private',
                root / 'prepared/private/answers.jsonl', root / 'runs', root / 'runs/episodes/001', root / 'evolution',
                root / 'evolution/01/verify', root / 'prepared', root, root.parent):
        with pytest.raises(ValueError, match='ToolRunner read root'):
            CP.check_tool_runner_roots(root, [lay.corpus, bad], allowed=[lay.evolution(1) / 'tools'])
    campaign = CP.Campaign(KEY, spec, lay, hooks=hooks(lay, spec.arm))
    built = {}

    class FakeToolRunner:
        def __init__(self, tools_dir, corpus=None, index=None, tmp_root=None, *, python=None, fsize=0):
            built.update(tools_dir=tools_dir, corpus=corpus, index=index, tmp_root=tmp_root, python=python)

        def run(self, argv, *, stdin=b''):
            return {'exit': 0, 'stdout': '', 'stderr_tail': '', 'seconds': 0.0}
    monkeypatch.setattr(R, 'ToolRunner', FakeToolRunner)
    make = campaign.tool_runner(2)
    assert callable(make) and make.block == 2 and make.tmp_root == (lay.evolution(2) / 'tmp').resolve()
    candidate = lay.evolution(2) / 'candidate' / 'tools'
    shutil.copytree(lay.adapters / 'harness-w0' / 'tools', candidate)
    runner = make(candidate)
    # the runner is built over a neutral hash-named copy of the candidate under runs/code/ (review fix)
    copy = lay.code_roots / CP.tools_sha12(candidate) / 'tools'
    assert isinstance(runner, FakeToolRunner) and built['tools_dir'] == copy.resolve() and CP.tools_snapshot(copy) == CP.tools_snapshot(candidate)
    assert built['corpus'] == lay.corpus.resolve() and built['index'] == lay.tables_index.resolve()
    assert built['tmp_root'] == make.tmp_root and built['tmp_root'].is_dir() and Path(built['python']) == Path(CP.ACTOR_PYTHON)
    for bad in (root / 'evidence', root / 'evidence/tools', root / 'prepared/private/tools', root / 'prepared',
                root / 'runs/episodes/001/workspace/tools', root / 'private-evaluation/tools'):
        with pytest.raises(ValueError, match='ToolRunner'):
            make(bad)
    # a source revision's tools/ (a learned adapter under evolution/, its neutral copy, or W0) is allowed
    for good in (lay.evolution(1) / 'adapter' / 'tools', lay.code_roots / 'abc' / 'tools', lay.adapters / 'harness-w0' / 'tools'):
        if not good.exists():
            shutil.copytree(lay.adapters / 'harness-w0' / 'tools', good)
        assert make(good) is not None and built['tools_dir'] == (lay.code_roots / CP.tools_sha12(good) / 'tools').resolve()
    # calibrate.py's source adapter (<scratch>/calibration/source/tools) is refused by a campaign's factory and
    # accepted only through extra_roots (the paid calibration of 2026-09-29 failed every replay on this refusal)
    source_tools = root / 'calibration' / 'source' / 'tools'
    shutil.copytree(lay.adapters / 'harness-w0' / 'tools', source_tools)
    with pytest.raises(ValueError, match='not a candidate, revision or W0'):
        make(source_tools)
    extra = campaign.tool_runner(2, extra_roots=(root / 'calibration' / 'source',))
    assert extra(source_tools) is not None and built['tools_dir'] == (lay.code_roots / CP.tools_sha12(source_tools) / 'tools').resolve()
    # the one constructor: preflight builds the same runner for the same inputs
    import preflight
    monkeypatch.undo()
    a = CP.make_tool_runner(lay, lay.adapters / 'harness-w0' / 'tools', lay.evolution(9) / 'tmp', pack='officeqa')
    b = preflight.tool_runner(lay.adapters / 'harness-w0' / 'tools', lay, pack='officeqa', tmp_root=lay.evolution(9) / 'tmp')
    assert (a.read_roots, a.env, a.tools_dir, a.tmp_root) == (b.read_roots, b.env, b.tools_dir, b.tmp_root)
    # the real ToolRunner over the W0 tools runs a manifest tool in the sandbox
    real = campaign.tool_runner(3)(lay.adapters / 'harness-w0' / 'tools')
    result = real.run(['cli.py', 'find_tables', 'fiscal operations'])
    assert result['exit'] == 0 and 'FFO-1' in result['stdout'], result


# ---------------------------------------------------------------------------------------------
# Audit failure detection on synthetic roots
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def completed_harness_evolve(tmp_path, code):
    root, spec, lay = make_root(tmp_path, 'harness-evolve', code)
    record = []
    state = run_campaign(lay, spec, hooks(lay, spec.arm, learn=fake_learner(record, note=True)))
    assert state['status'] == 'completed'
    return root, spec, lay


def failed_names(root, code):
    result = full_audit.build(root, code=code, write=False)
    return {c['name'] for c in result['checks'] if not c['passed']}


def test_audit_passes_then_detects_tampering(completed_harness_evolve, code):
    root, spec, lay = completed_harness_evolve
    assert failed_names(root, code) == set()
    run1 = root / 'runs/episodes/001'
    # a tampered kit, an arm mismatch, a v9 field missing, a prediction changed, notes that do not re-derive
    result = json.loads((run1 / 'records/result.json').read_text())
    (run1 / 'records/result.json').write_text(json.dumps({**result, 'kit_after': {'intact': False}}))
    invocation = json.loads((run1 / 'records/invocation.json').read_text())
    (run1 / 'records/invocation.json').write_text(json.dumps({**invocation, 'arm': asdict(C.STANDARD_ARMS['harness-static'])}))
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
    assert 'harness-evolve/001 kit intact after the run' in failed
    assert 'harness-evolve/001 invocation.arm == root arm' in failed
    assert 'harness-evolve/001 api/001 origin/kind/codex_input_sha256' in failed
    assert 'harness-evolve/007 served notes recorded and at most 3' in failed
    assert 'harness-evolve/007 served notes re-derive from the adapter and question' in failed
    assert 'harness-evolve/002 prediction unchanged' in failed


def test_audit_ties_block_adapters_to_rounds_and_re_derives_code_grants(completed_harness_evolve, code):
    """review fixes: a state.json whose block-2 adapter is another valid tree fails, a served note whose
    ledger row carries other text fails, and a marker with an extra granted code file fails."""
    root, spec, lay = completed_harness_evolve
    assert failed_names(root, code) == set()
    state = json.loads((root / 'state.json').read_text())
    # another valid tree (the W0) in place of the admitted candidate, revision consistent with that tree
    w0 = lay.adapters / 'harness-w0'
    state['blocks']['2']['adapter'] = {'path': str(w0), 'revision': arms.revision(w0), 'since_block': 2}
    state['blocks']['2']['adapter_revision'] = arms.revision(w0)
    (root / 'state.json').write_text(json.dumps(state))
    failed = failed_names(root, code)
    assert 'block 2 adapter is the round 1 candidate (or the block 1 adapter)' in failed
    assert 'block 2 adapter revision equals the round record and arms.revision(tree)' in failed
    # the ledger row names the note but with other text: not the admission of what is served
    ledger = root / 'learning-ledger.jsonl'
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    rows[0]['text'] = 'something else'
    ledger.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    assert 'round 1 served note percent_share admitted by paired re-runs (same text and triggers)' in failed_names(root, code)
    # an over-grant: a file of the code root that no manifest script imports
    run6 = root / 'runs/episodes/006'
    marker_path = run6 / 'workspace' / acting.MARKER
    os.chmod(marker_path, 0o644)
    marker = json.loads(marker_path.read_text())
    code_root = Path(marker['kit']['code_roots'][0])
    extra = code_root / 'USAGE.md'
    marker['kit']['code']['files'][str(extra)] = CP.sha(extra)
    marker_path.write_text(json.dumps(marker))
    failed = failed_names(root, code)
    assert 'harness-evolve/006 granted code files are exactly the import closure of the manifest scripts' in failed
    marker['extra'].pop('code_sha12')
    marker_path.write_text(json.dumps(marker))
    assert 'harness-evolve/006 marker code_sha12 is the code root revision segment' in failed_names(root, code)


def test_audit_static_arm_must_not_learn(tmp_path, code):
    root, spec, lay = make_root(tmp_path, 'harness-static', code)
    run_campaign(lay, spec, hooks(lay, spec.arm))
    assert failed_names(root, code) == set()
    (root / 'evidence/01').mkdir(parents=True)
    (root / 'evidence/01/x.json').write_text('{}')
    assert 'static arm: no seals, evidence or evolution' in failed_names(root, code)

# ---------------------------------------------------------------------------------------------
# data.py pieces, freeze, cost, report
# ---------------------------------------------------------------------------------------------


def test_tables_index_is_bound_to_the_corpus(tmp_path):
    spec = make_spec('prompt-static')
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
    # the index version is the tools' (integration finding: a v9.1 index opens fine but carries no section tables)
    from packs.officeqa.tools import otables
    assert info['version'] == otables.INDEX_VERSION == 'otables-index-v10' and info['sections'] is not None
    assert data.tables_index_version(root / 'prepared' / data.TABLES_RELATIVE) == otables.INDEX_VERSION
    older = tmp_path / 'older.sqlite'
    shutil.copyfile(root / 'prepared' / data.TABLES_RELATIVE, older)
    older.chmod(0o644)
    with sqlite3.connect(older) as connection:
        connection.execute("UPDATE meta SET value = ? WHERE key = 'version'", (json.dumps('otables-index-v9.1'),))
    with pytest.raises(ValueError, match="version 'otables-index-v9.1'.*need 'otables-index-v10'"):
        data.tables_index_version(older)
    same_corpus = tmp_path / 'same'
    (same_corpus / 'private').mkdir(parents=True)
    (same_corpus / 'private/corpus_manifest.json').write_text(json.dumps(rows))
    with pytest.raises(ValueError, match='version'):
        data.install_tables_index(same_corpus, older)
    manifest = json.loads((root / 'prepared/manifest.json').read_text())
    assert manifest['learner_policy']['withheld_reference_ids'] == ['UID0001', 'UID0004']
    assert manifest['learner_policy']['includes_gold'] is False                 # a static arm releases nothing
    assert 'agent/tables/' not in manifest['actor_read_allowlist']       # a prompt arm serves no tables tools
    refs = data.release_references(root, ['UIDT002', 'UID0001'])
    assert refs == {'UIDT002': 'A-UIDT002', 'UID0001': None}


def test_freeze_detects_changed_sources_and_settings(tmp_path_factory, tmp_path):
    code = code_copy(tmp_path_factory)
    root, spec, lay = make_root(tmp_path, 'harness-static', code)
    order = arms.question_order(TASK_IDS, spec.order_seed, 1)
    frozen = CP.freeze_record(lay, spec, order, deep=False)
    assert frozen['w0']['model_visible']['within_budget'] and frozen['settings']['temperature'] == 0
    assert frozen['w0']['revision'] == arms.revision(lay.adapters / 'harness-w0') and frozen['profile'] == 'v10'
    assert frozen['settings']['controls_defaults'] == C.CONTROLS_DEFAULTS and frozen['architecture'] == 'harness'
    CP.verify_freeze(lay, frozen, spec, order)
    with pytest.raises(ValueError, match='question order'):
        CP.verify_freeze(lay, frozen, spec, list(reversed(order)))
    usage = code / 'adapters/harness-w0/tools/USAGE.md'
    original = usage.read_text()
    usage.write_text(original + '  calc x\n')
    with pytest.raises(ValueError, match='Frozen tree changed: adapters/'):
        CP.verify_freeze(lay, frozen, spec, order)
    usage.write_text(original)
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
    call(tmp_path / 'runs/verify/01/r01/01/api/001', origin='codex')
    call(tmp_path / 'runs/verify/01/r01/01/api/002', origin='controller', parent=1)
    call(tmp_path / 'runs-aborted/X/runs/episodes/006/api/001', origin='codex')
    call(tmp_path / 'preflight/smoke-01/harness-evolve/api/001', origin='codex')
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


def test_report_renders_without_answers(completed_harness_evolve):
    root, spec, lay = completed_harness_evolve
    html = report.render(root)
    assert 'harness-evolve' in html and 'Controller' in html and 'Learning rounds' in html
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
        assert 'HARNESS_SUBMIT_URL' not in settings['shell_environment_policy.set']       # every standard arm: gate off
        assert (prep['code_root'] is not None) == arm.l1_tools

# ---------------------------------------------------------------------------------------------
# Review fixes carried over: arm identity, withheld verification, learned text, temperature, readiness
# ---------------------------------------------------------------------------------------------


def test_root_names_and_run_paths_do_not_reveal_the_arm(tmp_path, code):
    for name in ('harness-evolve-r1', 'officeqa-pro133-cards-v8-work', 'plain-r2', 'promptevolve', 'r1-none', 'adapt-1'):
        with pytest.raises(ValueError, match='reveals the arm'):
            arms.parse_config({'arm': 'plain', 'repeat': 1, 'order_seed': 's'}, root_name=name)
    assert arms.parse_config({'arm': 'harness-evolve', 'repeat': 1, 'order_seed': 's'}, root_name='v10-r1-d').root == 'v10-r1-d'
    root, spec, lay = make_root(tmp_path, 'harness-static', code)
    assert lay.runs() == lay.runs('harness-static') == root / 'runs/episodes' and lay.code_roots == root / 'runs/code'
    run_campaign(lay, spec, hooks(lay, spec.arm))
    assert failed_names(root, code) == set()
    run1 = root / 'runs/episodes/001'
    marker_path = run1 / 'workspace' / acting.MARKER
    os.chmod(marker_path, 0o644)
    marker = json.loads(marker_path.read_text())
    marker_path.write_text(json.dumps({**marker, 'workspace': str(root / 'runs/harness-static/001/workspace')}))
    agents = run1 / 'workspace/AGENTS.md'
    os.chmod(agents, 0o644)
    agents.write_text(agents.read_text() + '\nYou are in the harness-static arm.\n')
    readme = root / 'runs/episodes/002/workspace/README.md'
    os.chmod(readme, 0o644)
    readme.write_text(readme.read_text().split(CP.TOOLS_HEADING + '\n')[0])
    request = root / 'runs/episodes/003/api/001/request.json'
    request.write_text(json.dumps({**json.loads(request.read_text()), 'temperature': 0.7}))
    failed = failed_names(root, code)
    assert 'harness-static/001 workspace path names no arm (model-visible)' in failed
    assert 'harness-static/001 AGENTS.md re-derives from COMMON + adapter + chosen notes' in failed
    assert any(n.startswith('harness-static/002 README sections match the arm') for n in failed)
    assert 'harness-static/003 api/001 temperature as frozen' in failed


def test_verification_runner_refuses_withheld_questions(tmp_path, code):
    root, spec, lay = make_root(tmp_path, 'prompt-evolve', code)
    campaign = CP.Campaign(KEY, spec, lay, hooks=hooks(lay, spec.arm))
    campaign.load(resume=False)
    runner = campaign.verification_runner(2, lay.adapters / 'prompt-w0')        # every question is in blocks 1-2
    for task_id in data.ISSUE_47[:2]:                                          # UID0001, UID0004 (synthetic)
        assert task_id in campaign.order
        with pytest.raises(ValueError, match='released questions'):
            runner(lay.adapters / 'prompt-w0', {'task_id': task_id, 'question': campaign.tasks[task_id]['question']},
                   lay.verify_runs(2) / 'a' / task_id)


def test_learners_get_the_gate_compute_runner(tmp_path, code):
    root, spec, lay = make_root(tmp_path, 'harness-evolve', code)
    record = []
    run_campaign(lay, spec, hooks(lay, spec.arm, learn=fake_learner(record, note=False)))
    runner = record[0]['runner']
    assert os.path.realpath(runner.python) == os.path.realpath(CP.ACTOR_PYTHON)
    assert os.path.realpath(lay.corpus) in runner.read_roots


def test_audit_requires_note_admission_and_a_clean_leak_screen(tmp_path_factory, code):
    root, spec, lay = make_root(tmp_path_factory.mktemp('a'), 'prompt-evolve', code)
    run_campaign(lay, spec, hooks(lay, spec.arm, learn=fake_learner([], admit_note=False)))
    assert 'round 1 served note percent_share admitted by paired re-runs (same text and triggers)' in failed_names(root, code)
    leaky = 'A percent means x100. Known answers: ' + ', '.join(f'A-{t}' for t in TASK_IDS) + '.'
    root, spec, lay = make_root(tmp_path_factory.mktemp('b'), 'prompt-evolve', code)
    run_campaign(lay, spec, hooks(lay, spec.arm, learn=fake_learner([], note=leaky)))
    failed = failed_names(root, code)
    assert 'round 1 served note percent_share passes the leak screen' in failed
    assert 'round 1 served note percent_share admitted by paired re-runs (same text and triggers)' not in failed


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
    root, spec, lay = make_root(tmp_path, 'prompt-evolve', code, barrier=group)
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
    usage = copy / 'adapters/harness-w0/tools/USAGE.md'
    usage.write_text(usage.read_text() + '  calc x\n')
    with pytest.raises(ValueError, match='adapters/ changed after readiness'):
        runner.require_readiness()

# ---------------------------------------------------------------------------------------------
# $0 live tier: the composed workspace under the real pinned Codex (opt-in)
# ---------------------------------------------------------------------------------------------


def _live_episode(lay, arm, adapter, base, up):
    import rate_control
    task = {'task_id': 'UIDT002', 'question': QUESTIONS[1]}
    scheduler = rate_control.RateScheduler(max_in_flight=float('inf'), min_interval=0, backoffs=(0,))
    prep = CP.prepare_episode(lay, arm, task, adapter, base, ordinal=2)
    saved = CP.ACTOR_SECONDS
    CP.ACTOR_SECONDS = 180
    try:
        return CP.execute_episode(KEY, lay, arm, prep, broker_extra={'upstream_url': up.url, 'scheduler': scheduler})
    finally:
        CP.ACTOR_SECONDS = saved


@pytest.mark.live_codex
def test_live_codex_composed_harness_episode_gate_off_runs_the_served_tools(tmp_path, code):
    """The pinned Codex in the composed harness-static workspace: find_tables runs from the neutral code root
    over the tables index, no `submit` exists (command not found is never seen because the model never calls
    it), /v1/submit is not offered, and the summary carries the observation coverage."""
    import fake_upstream as fu
    import paths
    if not paths.CODEX.is_file() or not Path('/usr/bin/sandbox-exec').is_file():
        pytest.skip('pinned codex or sandbox-exec missing')
    root, spec, lay = make_root(tmp_path, 'harness-static', code)
    arm = spec.arm
    fname, header_line = 'treasury_bulletin_2003_03.txt', S.table_header_line(lay.corpus, '2003_03')
    up = fu.Upstream([fu.response([fu.call(f'cat README.md | head -3; find_tables "fiscal operations" --limit 2; '
                                           f'show_table {fname} {header_line}; ls .kit/bin')]),
                      fu.response([fu.message('2,010,894')])])
    try:
        summary = _live_episode(lay, arm, arms.w0_dir(arm, lay.adapters), root / 'live', up)
    finally:
        up.close()
    assert summary['status'] == 'completed' and summary['answer'] == '2,010,894' and summary['kit_intact']
    assert summary['controller']['submits'] == 0 and summary['fallback_answer'] is None
    assert summary['controller']['submit_enabled'] is False and summary['observation_coverage']['commands'] >= 1
    outputs = [item for body in up.bodies for item in body['input'] if item.get('type') == 'function_call_output']
    text = '\n'.join(str(o.get('output')) for o in outputs)
    assert '# Environment' in text and 'TABLE' in text and 'submit' not in text.split('ls .kit/bin')[-1]
    assert 'Operation not permitted' not in text and 'command not found' not in text
    invocation = json.loads((root / 'live/records/invocation.json').read_text())
    assert 'HARNESS_SUBMIT_URL' not in invocation['settings']['shell_environment_policy.set']
    assert all(f.startswith(str(lay.code_roots)) for f in invocation['code_read']['files'])


@pytest.mark.live_codex
def test_live_codex_composed_gate_on_episode_submits_through_the_kit(tmp_path, code):
    import fake_upstream as fu
    import paths
    if not paths.CODEX.is_file() or not Path('/usr/bin/sandbox-exec').is_file():
        pytest.skip('pinned codex or sandbox-exec missing')
    root, _, lay = make_root(tmp_path, 'harness-static', code)
    gated = gate_adapter(lay.adapters / 'harness-w0', root / 'gated-w0')
    up = fu.Upstream([fu.response([fu.call('cat README.md | head -3; find_tables "fiscal operations" --limit 2')]),
                      fu.response([fu.call('submit --answer "2,010,894"')]),
                      fu.response([fu.message('2,010,894')])])
    try:
        summary = _live_episode(lay, GATE_ARM, gated, root / 'live', up)
    finally:
        up.close()
    assert summary['status'] == 'completed' and summary['answer'] == '2,010,894' and summary['kit_intact']
    assert summary['controller']['submits'] == 1 and summary['fallback_answer'] == '2,010,894'
    outputs = [item for body in up.bodies for item in body['input'] if item.get('type') == 'function_call_output']
    text = '\n'.join(str(o.get('output')) for o in outputs)
    assert '# Environment' in text and '[runtime:gate]' in text          # README visible; submit reached the gate
    assert 'FFO-1' in text or 'TABLE' in text or 'Fiscal Operations' in text
    assert 'Operation not permitted' not in text and 'command not found' not in text
    invocation = json.loads((root / 'live/records/invocation.json').read_text())
    assert invocation['settings']['include_permissions_instructions'] is False
    assert invocation['settings']['model_auto_compact_token_limit'] == 244800
    assert invocation['settings']['features.multi_agent'] is False


@pytest.mark.live_codex
def test_live_codex_smoke_path_at_zero_cost(tmp_path, monkeypatch):
    """run.smoke (the paid readiness round-trip) end to end with the real Codex binary against a local fake
    upstream: note.txt is read with the shell, the final answer is the marker; no standard arm submits."""
    import fake_upstream as fu
    import paths
    import rate_control
    import run
    if not paths.CODEX.is_file() or not Path('/usr/bin/sandbox-exec').is_file():
        pytest.skip('pinned codex or sandbox-exec missing')

    def responder(body, index):
        outputs = [str(i.get('output')) for i in body['input'] if i.get('type') == 'function_call_output']
        if not outputs:
            return fu.response([fu.call('cat note.txt')])
        marker = next((w for w in outputs[0].split() if w.startswith('TRANSPORT_')), 'missing')
        return fu.response([fu.message(marker)])
    up = fu.Upstream(responder)
    scheduler = rate_control.RateScheduler(max_in_flight=float('inf'), min_interval=0, backoffs=(0,))
    saved = CP.ACTOR_SECONDS
    CP.ACTOR_SECONDS = 180
    try:
        monkeypatch.setattr(CP, 'CAMPAIGN', None)     # the first smoke checks the defaults, in any root
        result = run.smoke(KEY, 'smoke-t', ['plain', 'harness-static'], base=tmp_path,
                           broker_extra={'upstream_url': up.url, 'scheduler': scheduler})
        cell = arms.parse_config({'arm': 'prompt-static', 'repeat': 1, 'order_seed': 's', 'temperature': 1.0,
                                  'top_p': 0.95}, root_name='v10-p1-c')
        monkeypatch.setattr(CP, 'CAMPAIGN', cell)
        sent_before = len(up.bodies)
        cell_result = run.smoke(KEY, 'smoke-c', None, base=tmp_path,
                                broker_extra={'upstream_url': up.url, 'scheduler': scheduler})
    finally:
        CP.ACTOR_SECONDS = saved
        up.close()
    assert result['passed'], [(r['arm'], r['summary']['status'], r['summary']['answer'], r['reasoning_ok'],
                               r['controller_ok']) for r in result['results']]
    harness = next(r for r in result['results'] if r['arm'] == 'harness-static')
    assert harness['summary']['controller']['submits'] == 0 and harness['summary']['fallback_answer'] is None
    assert all(r['sampling_ok'] for r in result['results']) and result['top_p'] is None     # T=0, no top_p sent
    assert cell_result['passed'] and cell_result['results'][0]['sampling_ok'] and cell_result['top_p'] == 0.95
    assert [(b['temperature'], b['top_p']) for b in up.bodies[sent_before:]] == [(1.0, 0.95)] * (len(up.bodies) - sent_before)
    assert json.loads((tmp_path / 'smoke-t/verification.json').read_text())['passed'] is True
