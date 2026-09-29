"""Wave-3 pilot preparation tests ($0, offline; no real reference answer or grader output is read):

- subset runs: spec parsing and hashing, order = the subset in the repeat's seeded order, a text-evolve
  campaign on a subset (blocks of 5, learning, seal, grade, audit), refusals on a subset mismatch;
- temperature: the spec's value reaches every upstream body (real broker + fake upstream, including a
  controller re-query), transport.json, invocation.json and the freeze; the audit checks each; resume refuses
  a changed temperature;
- top_p (pilot configuration): absent = no top_p in any body or transport record (v8); declared = in every
  body (re-queries included), transport.json, invocation.json and the freeze; the audit checks each request;
  the campaign refuses a broker that would send another value; resume refuses a changed top_p;
- preflight on a synthetic 133-task root with a subset and a temperature + top_p cell;
- the broker abandons a stalled upstream call on exit (after and before the response headers) and writes no
  record after the episode;
- pilot/select_loop_prone.py: the label-free rule on a synthetic v8 tree whose answer/grade files are
  unreadable (an audit hook records every file it opens); the committed list re-derives from the v8 traces
  (kept for the record; the pilot uses pilot/first50.json);
- pilot/select_first50.py: the first 50 of the canonical seeded order, from task ids and ordinals only (same
  audit hook); the committed pilot/first50.json re-derives and the PREREG addendum pins it;
- pilot/metrics.py: every go/no-go number on synthetic sealed roots with a toy grader, no answer value in
  its output; the pre-registered confirmatory temperature rule on synthetic cells;
- run.py pilot-roots: refuses without the filled PREREG addendum, then creates the three roots.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import arms  # noqa: E402
import broker  # noqa: E402
import campaign as CP  # noqa: E402
import data  # noqa: E402
import full_audit  # noqa: E402
import fake_upstream as fu  # noqa: E402
from codexsim import CodexSim  # noqa: E402
from harness_core import contracts as C  # noqa: E402
from pilot import metrics as PM  # noqa: E402
from pilot import select_first50 as SF  # noqa: E402
from pilot import select_loop_prone as SL  # noqa: E402
import test_v9_broker as TB  # noqa: E402   (helpers only; module-qualified so pytest does not re-collect)
import test_v9_orchestration as TO  # noqa: E402

PY = sys.executable


@pytest.fixture(scope='session')
def code(tmp_path_factory):
    return TO.code_copy(tmp_path_factory)


def subset_config(ids, name='pilot-test-7'):
    return {'name': name, 'task_ids': list(ids), 'sha256': arms.subset_sha256(ids)}


def spec_with(arm='text-evolve', *, subset=None, temperature=None, root='root', barrier=None, top_p=None):
    config = {'profile': 'v9', 'arm': arm, 'repeat': 1, 'pack': 'officeqa', 'order_seed': 'test-order-seed'}
    if subset is not None:
        config['subset'] = subset
    if temperature is not None:
        config['temperature'] = temperature
    if top_p is not None:
        config['top_p'] = top_p
    if barrier is not None:
        config['barrier'] = barrier
    return arms.parse_config(config, root_name=root)


def all_ids(n):
    return TO.TASK_IDS + [f'UIDX{i:03}' for i in range(n - len(TO.TASK_IDS))]


def answer_by_id(n):
    ids, questions = all_ids(n), TO.QUESTIONS + [f'What was synthetic amount number {i} in fiscal year 2001?'
                                                for i in range(n - len(TO.TASK_IDS))]
    by_question = dict(zip(questions, ids))
    return lambda question, workspace: 'A-' + by_question[question] if by_question[question] != 'UIDX001' else 'wrong'

# ---------------------------------------------------------------------------------------------
# Spec: subset + temperature
# ---------------------------------------------------------------------------------------------


def test_subset_and_temperature_parse_hash_and_record():
    ids = ['UIDT005', 'UID0004', 'UIDT002']
    spec = spec_with(subset=subset_config(ids), temperature=0.6)
    assert spec.subset.task_ids == tuple(ids) and spec.subset.sha256 == arms.subset_sha256(ids)
    assert spec.temperature == 0.6 and spec.to_json()['temperature'] == 0.6
    assert spec.to_json()['subset'] == subset_config(ids)
    # a full run at the default temperature records exactly the pre-wave-3 spec keys
    full = spec_with()
    assert set(full.to_json()) == {'arm', 'repeat', 'pack', 'order_seed', 'root', 'barrier'}
    assert full.subset is None and full.temperature == 0 and spec_with(temperature=0).to_json() == full.to_json()
    assert arms.subset_sha256(ids) == SL.subset_sha256(ids) == hashlib.sha256(
        json.dumps(ids, separators=(',', ':')).encode()).hexdigest()
    bad = [({**subset_config(ids), 'sha256': arms.subset_sha256(ids[::-1])}, 'sha256'),
           ({**subset_config(ids), 'task_ids': ids + [ids[0]]}, 'distinct'),
           ({**subset_config(ids), 'name': 'Bad Name'}, 'subset.name'),
           ({**subset_config(ids), 'extra': 1}, 'exactly'),
           ({'name': 'x', 'task_ids': [], 'sha256': arms.subset_sha256([])}, 'non-empty')]
    for value, message in bad:
        with pytest.raises(ValueError, match=message):
            spec_with(subset=value)
    for value in (-0.1, 2.5, True, '0.7', None, float('nan')):          # an explicit null is refused too
        with pytest.raises(ValueError, match='temperature'):
            arms.parse_config({'arm': 'plain', 'repeat': 1, 'order_seed': 's', 'temperature': value}, root_name='r1')


def test_top_p_parses_records_only_when_declared():
    spec = spec_with(temperature=1.0, top_p=0.95)
    assert (spec.temperature, spec.top_p) == (1.0, 0.95)
    assert spec.to_json()['top_p'] == 0.95 and spec.to_json()['temperature'] == 1.0
    full = spec_with()
    assert full.top_p is None and arms.DEFAULT_TOP_P is None and 'top_p' not in full.to_json()
    assert spec_with(top_p=1).top_p == 1
    for value in (0, -0.1, 1.5, True, '0.95', None, float('nan')):     # absent is the only way to send none
        with pytest.raises(ValueError, match='top_p'):
            arms.parse_config({'arm': 'plain', 'repeat': 1, 'order_seed': 's', 'top_p': value}, root_name='r1')


def test_subset_order_is_the_full_order_without_the_other_questions():
    base = [f'UID{i:04}' for i in range(1, 134)]
    chosen = base[2::6][:20]
    spec = replace(spec_with(), subset=arms.parse_subset(subset_config(chosen, 'pilot-20')))
    order = arms.campaign_order(base, spec)
    full = arms.question_order(base, spec.order_seed, spec.repeat)
    assert order == [t for t in full if t in set(chosen)] and len(order) == 20
    assert [list(b) for b in arms.blocks_of(len(order), 5)] == [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10],
                                                                 [11, 12, 13, 14, 15], [16, 17, 18, 19, 20]]
    with pytest.raises(ValueError, match='not prepared'):
        arms.selected_ids(base[:10], spec.subset)

# ---------------------------------------------------------------------------------------------
# Subset campaign (fake broker/actor, real campaign/seal/grade/audit)
# ---------------------------------------------------------------------------------------------

SUBSET_IDS = ['UIDT002', 'UID0004', 'UIDT005', 'UIDX000', 'UIDX001', 'UIDX003', 'UIDX004']   # 1 issue-47 id


def make_subset_root(tmp_path, code, arm='text-evolve', *, n=12, ids=SUBSET_IDS, temperature=None, name='root'):
    root = tmp_path / name
    root.mkdir()
    spec = spec_with(arm, subset=subset_config(ids), temperature=temperature, root=name)
    TO.make_prepared(root, spec, n=n)
    return root, spec, CP.Layout(root=root, code=code)


def test_subset_campaign_learns_in_blocks_seals_grades_and_audits(tmp_path, code):
    root, spec, lay = make_subset_root(tmp_path, code)
    manifest = json.loads((root / 'prepared/manifest.json').read_text())
    assert manifest['subset'] == subset_config(SUBSET_IDS) and manifest['sample_count'] == 12
    assert manifest['grading_policy']['subsets'] == {'all': 7, 'undisputed': 6}
    assert data.selected_task_ids(manifest) == [t for t in manifest['task_ids'] if t in SUBSET_IDS]
    record, calls = [], []
    state = TO.run_campaign(lay, spec, TO.hooks(lay, spec.arm, learn=TO.fake_learner(record), calls=calls,
                                                answer_for=answer_by_id(12)))
    order = arms.campaign_order(manifest['task_ids'], spec)
    full = arms.question_order(manifest['task_ids'], spec.order_seed, spec.repeat)
    assert order == [t for t in full if t in SUBSET_IDS]
    assert state['status'] == 'completed' and [r['task_id'] for r in state['results']] == order
    assert state['question_count'] == 7 and state['subset'] == {'name': 'pilot-test-7', 'sha256': spec.subset.sha256,
                                                                 'count': 7}
    # blocks of 5 over the subset: one learning round (after block 1), released = block 1 minus issue-47
    assert [(r['block'], r['status']) for r in state['learning']] == [(1, 'admitted'), (2, 'skipped_unused')]
    assert record[0]['released'] == sorted(t for t in order[:5] if t not in data.ISSUE_47)
    assert {task for _, task, _ in calls} <= set(SUBSET_IDS)
    seal = json.loads((root / 'private-evaluation/predictions-seal.json').read_text())
    assert seal['total_predictions'] == 7 and seal['subset'] == {'name': 'pilot-test-7',
                                                                 'sha256': spec.subset.sha256, 'count': 7}
    metrics = json.loads((root / 'private-evaluation/paired-metrics.json').read_text())
    assert metrics['denominator'] == 7 and metrics['undisputed']['n'] == 6 and metrics['subset']['count'] == 7
    assert sorted(r['task_id'] for r in metrics['results']) == sorted(SUBSET_IDS)
    assert metrics['arms']['text-evolve']['correct'] == 6                # UIDX001 answered 'wrong'
    frozen = json.loads((root / 'freeze.json').read_text())
    assert frozen['campaign']['subset'] == subset_config(SUBSET_IDS) and frozen['order'] == order
    assert TO.failed_names(root, code) == set()
    assert '7 questions (subset pilot-test-7)' in (root / 'report.html').read_text()
    # the audit re-checks the subset against the prepared manifest
    path = root / 'prepared/manifest.json'
    changed = {**manifest, 'subset': subset_config(SUBSET_IDS[:-1] + ['UIDX005'])}
    path.write_text(json.dumps(changed, indent=2) + '\n')
    assert 'subset: hash of the declared ids, recorded in the prepared manifest, run exactly' in TO.failed_names(root, code)
    # review: a completed root without its grade outputs fails the coverage check (it used to be skipped)
    graded = root / 'private-evaluation/paired-metrics.json'
    graded.rename(root / 'paired-metrics.moved')
    assert "seal and grades cover exactly this root's questions" in TO.failed_names(root, code)


def test_subset_mismatch_and_bad_manifest_subset_are_refused(tmp_path, code):
    root, spec, lay = make_subset_root(tmp_path, code)
    other = replace(spec, subset=arms.parse_subset(subset_config(SUBSET_IDS[:5])))
    with pytest.raises(ValueError, match='subset differs'):
        CP.Campaign(TO.KEY, other, lay, hooks=TO.hooks(lay, other.arm)).load(resume=False)
    with pytest.raises(ValueError, match='subset differs'):
        data.check_subset(root, replace(spec, subset=None))
    assert data.check_subset(root, spec) == {'name': 'pilot-test-7', 'sha256': spec.subset.sha256, 'count': 7}
    manifest = json.loads((root / 'prepared/manifest.json').read_text())
    with pytest.raises(ValueError, match='sha256'):
        data.selected_task_ids({**manifest, 'subset': {**manifest['subset'], 'sha256': '0' * 64}})
    with pytest.raises(ValueError, match='not prepared'):
        data.selected_task_ids({**manifest, 'subset': subset_config(['UIDT002', 'NOPE'])})
    # a full root (no subset) seals and grades exactly as before: the full list
    full_root = tmp_path / 'full'
    full_root.mkdir()
    TO.make_prepared(full_root, spec_with('plain', root='full'), n=7)
    assert data.check_subset(full_root, spec_with('plain', root='full')) is None
    assert 'subset' not in json.loads((full_root / 'prepared/manifest.json').read_text())

# ---------------------------------------------------------------------------------------------
# Temperature
# ---------------------------------------------------------------------------------------------


def test_temperature_campaign_freezes_records_and_audits_it(tmp_path, code):
    root = tmp_path / 'root'
    root.mkdir()
    spec = spec_with('mech-static', temperature=0.6)
    TO.make_prepared(root, spec)
    lay = CP.Layout(root=root, code=code)
    state = TO.run_campaign(lay, spec, TO.hooks(lay, spec.arm))
    assert state['status'] == 'completed' and state['temperature'] == 0.6
    frozen = json.loads((root / 'freeze.json').read_text())
    assert frozen['campaign']['temperature'] == 0.6
    assert frozen['settings']['temperature'] == frozen['settings']['broker_temperature'] == 0.6
    for run in sorted((root / 'runs/episodes').iterdir()):
        assert json.loads((run / 'records/invocation.json').read_text())['temperature'] == 0.6
        assert json.loads((run / 'api/001/request.json').read_text())['temperature'] == 0.6
    assert TO.failed_names(root, code) == set()
    # a resume under another temperature is refused (spec and settings are frozen)
    with pytest.raises(ValueError, match='changed after the freeze'):
        CP.verify_freeze(lay, frozen, replace(spec, temperature=0), frozen['order'])
    invocation = root / 'runs/episodes/002/records/invocation.json'
    invocation.write_text(json.dumps({**json.loads(invocation.read_text()), 'temperature': 0}))
    request = root / 'runs/episodes/003/api/001/request.json'
    request.write_text(json.dumps({**json.loads(request.read_text()), 'temperature': 0}))
    failed = TO.failed_names(root, code)
    assert 'mech-static/002 invocation temperature as frozen' in failed
    assert 'mech-static/003 api/001 temperature as frozen' in failed


def test_real_broker_sends_the_declared_temperature_in_every_body(tmp_path):
    with TB.relay(tmp_path / 'a', lambda body, index: TB.DRAFT, temperature=0.6) as (relay_, up):
        CodexSim(relay_.endpoint).send()
    assert up.bodies[0]['temperature'] == 0.6
    assert TB.api(tmp_path / 'a', 1, 'transport.json')['changes']['temperature'] == 0.6
    # controller arm: the enforcement re-query (its own upstream call) carries it too
    controller, _, _ = TB.mech()
    revision = fu.response([fu.message('42', id='msg_r')], id='resp_rev')
    with TB.relay(tmp_path / 'b', TB.revise_responder(revision), controller, temperature=0.6) as (relay_, up):
        CodexSim(relay_.endpoint).send()
    assert len(up.bodies) == 2 and [b['temperature'] for b in up.bodies] == [0.6, 0.6]
    assert [TB.api(tmp_path / 'b', n, 'transport.json')['changes']['temperature'] for n in (1, 2)] == [0.6, 0.6]
    # default: v8's 0, byte-identical bodies otherwise
    with TB.relay(tmp_path / 'c', lambda body, index: TB.DRAFT) as (relay_, up):
        CodexSim(relay_.endpoint).send()
    assert up.bodies[0]['temperature'] == 0
    for value in (-1, 3, True, '0.5'):
        with pytest.raises(ValueError, match='temperature'):
            broker.Broker(TB.KEY, tmp_path / 'x', temperature=value, **TB.UNBOUNDED)

# ---------------------------------------------------------------------------------------------
# top_p (pilot cell c: temperature 1.0, top_p 0.95)
# ---------------------------------------------------------------------------------------------


def test_real_broker_sends_top_p_only_when_declared(tmp_path):
    with TB.relay(tmp_path / 'a', lambda body, index: TB.DRAFT, temperature=1.0, top_p=0.95) as (relay_, up):
        CodexSim(relay_.endpoint).send()
    assert (up.bodies[0]['temperature'], up.bodies[0]['top_p']) == (1.0, 0.95)
    assert json.loads((tmp_path / 'a/api/001/request.json').read_text())['top_p'] == 0.95
    changes = TB.api(tmp_path / 'a', 1, 'transport.json')['changes']
    assert (changes['temperature'], changes['top_p']) == (1.0, 0.95)
    # controller arm: the enforcement re-query (its own upstream call) carries it too
    controller, _, _ = TB.mech()
    revision = fu.response([fu.message('42', id='msg_r')], id='resp_rev')
    with TB.relay(tmp_path / 'b', TB.revise_responder(revision), controller, temperature=1.0, top_p=0.95) as (relay_, up):
        CodexSim(relay_.endpoint).send()
    assert len(up.bodies) == 2 and [(b['temperature'], b['top_p']) for b in up.bodies] == [(1.0, 0.95)] * 2
    assert [TB.api(tmp_path / 'b', n, 'transport.json')['changes']['top_p'] for n in (1, 2)] == [0.95, 0.95]
    assert TB.api(tmp_path / 'b', 2, 'transport.json')['origin'] == 'controller'
    # default: no top_p in the body nor in the record (v8 bytes; test_v9_broker test_1 compares with v8)
    with TB.relay(tmp_path / 'c', lambda body, index: TB.DRAFT) as (relay_, up):
        CodexSim(relay_.endpoint).send()
    assert 'top_p' not in up.bodies[0] and 'top_p' not in TB.api(tmp_path / 'c', 1, 'transport.json')['changes']
    assert broker.normalized({'model': broker.MODEL, 'input': []}, None, None) == {
        'model': broker.MODEL, 'input': [], 'store': False, 'temperature': 0}
    for value in (0, -0.5, 1.5, True, '0.9'):
        with pytest.raises(ValueError, match='top_p'):
            broker.Broker(TB.KEY, tmp_path / 'x', top_p=value, **TB.UNBOUNDED)


def test_top_p_campaign_freezes_records_audits_and_refuses_a_mismatch(tmp_path, code, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    spec = spec_with('mech-static', temperature=1.0, top_p=0.95)
    TO.make_prepared(root, spec)
    lay = CP.Layout(root=root, code=code)
    state = TO.run_campaign(lay, spec, TO.hooks(lay, spec.arm))
    assert state['status'] == 'completed' and (state['temperature'], state['top_p']) == (1.0, 0.95)
    frozen = json.loads((root / 'freeze.json').read_text())
    assert (frozen['campaign']['temperature'], frozen['campaign']['top_p']) == (1.0, 0.95)
    assert frozen['settings']['top_p'] == frozen['settings']['broker_top_p'] == 0.95
    runs = sorted((root / 'runs/episodes').iterdir())
    for run in runs:
        assert json.loads((run / 'records/invocation.json').read_text())['top_p'] == 0.95
        assert json.loads((run / 'api/001/request.json').read_text())['top_p'] == 0.95
    assert TO.failed_names(root, code) == set()
    # a resume under another (or no) top_p is refused: spec and settings are frozen
    for other in (None, 0.9):
        with pytest.raises(ValueError, match='changed after the freeze'):
            CP.verify_freeze(lay, frozen, replace(spec, top_p=other), frozen['order'])
    # the audit checks every invocation and every request body
    invocation = runs[1] / 'records/invocation.json'
    invocation.write_text(json.dumps({**json.loads(invocation.read_text()), 'top_p': None}))
    request = runs[2] / 'api/001/request.json'
    request.write_text(json.dumps({k: v for k, v in json.loads(request.read_text()).items() if k != 'top_p'}))
    failed = TO.failed_names(root, code)
    assert f'mech-static/{runs[1].name} invocation top_p as frozen' in failed
    assert f'mech-static/{runs[2].name} api/001 top_p as frozen' in failed
    # a call still in flight at the actor kill (request.json, no transport.json) is checked as well
    unfinished = runs[3] / 'api/001'
    (unfinished / 'transport.json').unlink()
    body = json.loads((unfinished / 'request.json').read_text())
    (unfinished / 'request.json').write_text(json.dumps({**body, 'top_p': 0.9, 'temperature': 0}))
    failed = TO.failed_names(root, code)
    assert {f'mech-static/{runs[3].name} api/001 top_p as frozen',
            f'mech-static/{runs[3].name} api/001 temperature as frozen'} <= failed
    # a root without top_p: frozen null, and a body that carries a top_p fails the audit
    plain_root = tmp_path / 'bare'
    plain_root.mkdir()
    plain = spec_with('plain', root='bare')
    TO.make_prepared(plain_root, plain)
    plain_lay = CP.Layout(root=plain_root, code=code)
    TO.run_campaign(plain_lay, plain, TO.hooks(plain_lay, plain.arm))
    frozen = json.loads((plain_root / 'freeze.json').read_text())
    assert 'top_p' not in frozen['campaign'] and frozen['settings']['top_p'] is frozen['settings']['broker_top_p'] is None
    assert all(json.loads((r / 'records/invocation.json').read_text())['top_p'] is None
               for r in (plain_root / 'runs/episodes').iterdir())
    assert TO.failed_names(plain_root, code) == set()
    request = plain_root / 'runs/episodes/001/api/001/request.json'
    request.write_text(json.dumps({**json.loads(request.read_text()), 'top_p': 0.95}))
    assert 'plain/001 api/001 top_p as frozen' in TO.failed_names(plain_root, code)
    # the campaign refuses to start when the broker would not send the declared top_p
    fresh = tmp_path / 'fresh'
    fresh.mkdir()
    TO.make_prepared(fresh, spec)
    fresh_lay = CP.Layout(root=fresh, code=code)
    real = broker.normalized
    monkeypatch.setattr(broker, 'normalized', lambda payload, remaining, max_response=65536, temperature=0, top_p=None:
                        real(payload, remaining, max_response, temperature))          # ignores its top_p option
    with pytest.raises(ValueError, match='top_p 0.95 but the broker sends None'):
        CP.Campaign(TO.KEY, spec, fresh_lay, hooks=TO.hooks(fresh_lay, spec.arm)).load(resume=False)
    assert not (fresh / 'freeze.json').exists()


def test_preflight_offline_on_a_subset_root_with_a_temperature_cell(tmp_path_factory, tmp_path):
    import paths
    copy = TO.code_copy(tmp_path_factory)
    ids = [f'UIDX{i:03}' for i in range(3, 126, 6)][:20]
    config = {'profile': 'v9', 'arm': 'text-static', 'repeat': 1, 'pack': 'officeqa', 'order_seed': 'pf-seed',
              'temperature': 1.0, 'top_p': 0.95, 'subset': subset_config(ids, 'pilot-first50'),
              'barrier': {'dir': str(tmp_path / 'barrier'), 'roots': [copy.name], 'timeout_s': 60},
              'codex': str(paths.CODEX), 'frozen_runtime': str(paths.FROZEN)}
    (copy / '.task-paths.json').write_text(json.dumps(config))
    spec = arms.parse_config(config, root_name=copy.name)
    manifest = TO.make_prepared(copy, spec, n=133)
    shutil.copyfile(ROOT / 'vendor/officeqa/reward.py', copy / 'prepared/private/reward.py')
    run = subprocess.run([PY, '-B', 'preflight.py', '--offline-only', '--no-suites'], cwd=copy,
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    result = json.loads((copy / 'preflight/offline-checks.json').read_text())
    checks, info = result['checks'], result['info']
    assert result['passed'] and checks['subset_hash_matches_manifest'] and checks['temperature_sent_equals_declared']
    assert checks['top_p_sent_equals_declared'] and (info['temperature'], info['top_p']) == (1.0, 0.95)
    assert checks['question_count_133'] and info['question_count'] == 20
    order = arms.campaign_order(manifest['task_ids'], spec)
    assert info['order_sha256'] == arms.order_sha256(order) and info['subset']['count'] == 20
    assert json.loads((tmp_path / 'barrier/order.json').read_text())['order_sha256'] == info['order_sha256']
    # a manifest prepared for another subset fails the data verification
    (copy / 'prepared/manifest.json').chmod(0o644)
    path = copy / 'prepared/manifest.json'
    path.write_text(json.dumps({**manifest, 'subset': subset_config(ids[:19], 'pilot-first50')}))
    run = subprocess.run([PY, '-B', 'preflight.py', '--offline-only', '--no-suites'], cwd=copy,
                         capture_output=True, text=True, timeout=300)
    assert run.returncode != 0 and 'subset differs' in run.stderr

# ---------------------------------------------------------------------------------------------
# Broker: abandon the upstream call in flight on exit (S8 request 4)
# ---------------------------------------------------------------------------------------------


def _api_files(api: Path) -> dict:
    return {p: p.read_bytes() for p in sorted(api.rglob('*')) if p.is_file()}


def _drive(relay_, errors):
    try:
        CodexSim(relay_.endpoint).send()
    except Exception as exc:                  # the actor is gone; Codex would see a dropped stream
        errors.append(type(exc).__name__)


@pytest.mark.parametrize('with_controller', [False, True])
def test_broker_exit_abandons_a_stalled_stream_and_records_it_before_returning(tmp_path, with_controller):
    controller = TB.mech()[0] if with_controller else None
    up = fu.Upstream(lambda body, index: 'stall')                      # headers, then nothing
    errors = []
    try:
        with broker.Broker(TB.KEY, tmp_path / 'api', scheduler=TB.scheduler(), upstream_url=up.url,
                           controller=controller, **TB.UNBOUNDED) as relay_:
            thread = threading.Thread(target=_drive, args=(relay_, errors), daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            while (relay_.active_response is None or not up.bodies) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert relay_.active_response is not None and relay_.lock.locked()
            begun = time.monotonic()
        took = time.monotonic() - begun                                # __exit__: abandon + wait for the handler
        assert took < 5 and not relay_.lock.locked()
        transport = json.loads((tmp_path / 'api/001/transport.json').read_text())
        assert [(a['dispatched'], a['status']) for a in transport['attempts']] == [(True, 'transport_retry'),
                                                                                   (False, 'not_dispatched')]
        assert transport['attempts'][0]['usage_unknown'] is True and 'stopped' in transport['attempts'][1]['error']
        summary = json.loads((tmp_path / 'api/summary.json').read_text())
        assert summary['abandoned_calls'] == [1] and summary['in_flight_calls'] == []
        assert summary['usage_unknown_calls'] == [1] and summary['usage_complete'] is False
        if with_controller:
            assert json.loads((tmp_path / 'api/001/controller.json').read_text())['forwarded'] is False
        before = _api_files(tmp_path / 'api')
    finally:
        up.close()                                                     # the provider finally closes its stream
    thread.join(timeout=10)
    time.sleep(0.5)
    assert _api_files(tmp_path / 'api') == before                     # nothing is written after the episode
    assert len(up.bodies) == 1                                         # never re-sent


def test_broker_exit_abandons_a_call_stalled_before_the_response_headers(tmp_path):
    """A provider that accepts the connection and reads the request but never answers (no headers)."""
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(4)
    held, release = [], threading.Event()

    def accept():
        try:
            conn, _ = server.accept()
        except OSError:
            return
        held.append(conn)
        conn.recv(65536)
        release.wait(30)
        conn.close()
    threading.Thread(target=accept, daemon=True).start()
    url = f'http://127.0.0.1:{server.getsockname()[1]}/v1/responses'
    errors = []
    try:
        with broker.Broker(TB.KEY, tmp_path / 'api', scheduler=TB.scheduler(), upstream_url=url,
                           **TB.UNBOUNDED) as relay_:
            thread = threading.Thread(target=_drive, args=(relay_, errors), daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            while (not held or relay_.active_connection is None) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert held and relay_.active_connection is not None and relay_.active_response is None
            begun = time.monotonic()
        assert time.monotonic() - begun < 5 and not relay_.lock.locked()
        transport = json.loads((tmp_path / 'api/001/transport.json').read_text())
        assert transport['attempts'][0]['dispatched'] is True and transport['attempts'][-1]['status'] == 'not_dispatched'
        assert json.loads((tmp_path / 'api/summary.json').read_text())['abandoned_calls'] == [1]
        before = _api_files(tmp_path / 'api')
    finally:
        release.set()
        server.close()
    thread.join(timeout=10)
    time.sleep(0.3)
    assert _api_files(tmp_path / 'api') == before


def test_normal_exit_writes_no_abandoned_field(tmp_path):
    with TB.relay(tmp_path, lambda body, index: TB.DRAFT) as (relay_, up):
        CodexSim(relay_.endpoint).send()
    summary = json.loads((tmp_path / 'api/summary.json').read_text())
    assert 'abandoned_calls' not in summary and summary['in_flight_calls'] == [] and not summary['failed']

# ---------------------------------------------------------------------------------------------
# pilot/select_loop_prone.py
# ---------------------------------------------------------------------------------------------

_OPENED: list[str] = []
_RECORD = threading.Event()


def _audit(event, args):
    if _RECORD.is_set() and event == 'open' and args and isinstance(args[0], (str, bytes, os.PathLike)):
        _OPENED.append(os.fsdecode(args[0]))


sys.addaudithook(_audit)


def make_v8_tree(project: Path, looped: dict[str, set[str]], n: int) -> None:
    """Three v8 arm run dirs; question k loops (a command issued 3x) in the arms listed in looped[id]; every
    run also has forbidden files (prediction/summary/final/grades/answers/index.json) made unreadable."""
    for arm, relative in SL.V8_RUNS.items():
        runs = project / relative
        for k in range(1, n + 1):
            task_id = f'UIDQ{k:03}'
            run = runs / f'{k:03}'
            (run / 'records').mkdir(parents=True)
            (run / 'task.json').write_text(json.dumps({'ordinal': k, 'task_id': task_id, 'question': f'Q{k}?'}))
            repeats = 3 if arm in looped.get(task_id, ()) else 2
            lines = [{'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'cat x'}}]
            lines += [{'type': 'item.completed', 'item': {'type': 'command_execution',
                                                          'command': f"/bin/zsh -lc  'rg  -n  total {k}'"}}] * repeats
            lines += [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'ANSWER-SHOULD-NOT-BE-READ'}}]
            (run / 'records/events.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in lines))
            for name in ('prediction.json', 'summary.json', 'records/final.md'):
                (run / name).write_text('{"answer": "SECRET"}')
                (run / name).chmod(0)
        root = runs.parents[1]
        for name in ('private-evaluation/adapted-grades.json', 'prepared/private/answers.jsonl', 'index.json'):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text('{"secret": true}')
            (root / name).chmod(0)


def test_select_loop_prone_rule_reads_only_commands_and_task_ids(tmp_path):
    looped = {f'UIDQ{k:03}': {'none', 'prompt', 'cards'} for k in (1, 2, 3)}
    looped.update({f'UIDQ{k:03}': {'none', 'cards'} for k in range(4, 16)})
    looped.update({f'UIDQ{k:03}': {'prompt'} for k in range(16, 25)})          # UIDQ025: never loops
    make_v8_tree(tmp_path, looped, 25)
    assert not os.access(tmp_path / SL.V8_RUNS['none'] / '001/prediction.json', os.R_OK) or os.geteuid() == 0
    _OPENED.clear()
    _RECORD.set()
    try:
        result = SL.select(SL.scan(tmp_path))
    finally:
        _RECORD.clear()
    opened = [p for p in _OPENED if p.startswith(str(tmp_path))]
    assert opened and all(p.endswith(('/task.json', '/records/events.jsonl')) for p in opened), opened
    one_arm = sorted((f'UIDQ{k:03}' for k in range(16, 25)),
                     key=lambda t: hashlib.sha256(f'v9-officeqa-pilot-loop-prone\0{t}'.encode()).hexdigest())
    expected = sorted([f'UIDQ{k:03}' for k in range(1, 16)] + one_arm[:5])
    assert result['task_ids'] == expected and result['sha256'] == arms.subset_sha256(expected)
    assert result['ordinals'] == [int(t[4:]) for t in expected] and result['eligible'] == 24
    assert result['arms_looped_histogram'] == {'1': 9, '2': 12, '3': 3}
    assert [r['arms_looped'] for r in result['ranking']] == [3] * 3 + [2] * 12 + [1] * 5
    assert 'ANSWER' not in json.dumps(result) and 'SECRET' not in json.dumps(result)
    # the whitespace-normalised command is what repeats (the rule's normaliser is the loop guard's)
    assert result['ranking'][0]['max_repeat'] == {'cards': 3, 'none': 3, 'prompt': 3}
    with pytest.raises(ValueError, match='only 19 eligible'):
        SL.select({t: e for t, e in SL.scan(tmp_path).items()
                   if t not in ('UIDQ016', 'UIDQ017', 'UIDQ018', 'UIDQ019', 'UIDQ020')})


def test_committed_loop_prone_draft_is_kept_and_re_derives_from_v8():
    """The earlier loop-prone draft list stays on disk for the record (the pilot uses pilot/first50.json)."""
    listing = json.loads((ROOT / 'pilot/loop_prone_20.json').read_text())
    ids = listing['task_ids']
    assert listing['name'] == SL.NAME and len(ids) == len(set(ids)) == 20 and ids == sorted(ids)
    assert listing['sha256'] == arms.subset_sha256(ids) and len(listing['ordinals']) == 20
    assert arms.parse_subset({k: listing[k] for k in ('name', 'task_ids', 'sha256')}).task_ids == tuple(ids)
    assert listing['rule']['threshold'] == 3 and listing['rule']['labels_used'] is False
    assert listing['script_sha256'] == hashlib.sha256((ROOT / 'pilot/select_loop_prone.py').read_bytes()).hexdigest()
    addendum = (ROOT / 'pilot/ADDENDUM.md').read_text()
    assert listing['sha256'] in addendum and f'pilot-list-sha256: {listing["sha256"]}' not in addendum
    project = ROOT.parent
    if not all((project / rel).is_dir() for rel in SL.V8_RUNS.values()):
        pytest.skip('v8 roots not next to this code copy')
    assert SL.select(SL.scan(project)) == listing

# ---------------------------------------------------------------------------------------------
# pilot/select_first50.py (the pilot's list: the first 50 of the canonical order)
# ---------------------------------------------------------------------------------------------


def make_v8_order_tree(project: Path, ids: list[str], historic: str = 'v7-cards') -> list[str]:
    """A v8 manifest (seeded order) + the three v8 arms' task.json + one historic 50-question root; every
    answer/prediction/final/summary/events/grades file is unreadable."""
    order = SF.seeded_order(ids)
    manifest = project / SF.MANIFEST
    (manifest.parent / 'private').mkdir(parents=True)
    manifest.write_text(json.dumps({'task_ids': order, 'selection': {'seed': SF.SEED}, 'sample_count': len(order),
                                    'answers_sha256': '0' * 64}))
    secrets_ = [manifest.parent / 'private/answers.jsonl']
    for relative, count in [*[(r, len(order)) for r in SF.V8_RUNS.values()], (SF.HISTORIC_50[historic], 50)]:
        runs = project / relative
        for k in range(1, count + 1):
            run = runs / f'{k:03}'
            (run / 'records').mkdir(parents=True)
            (run / 'task.json').write_text(json.dumps({'ordinal': k, 'task_id': order[k - 1], 'question': f'Q{k}?'}))
            secrets_ += [run / 'prediction.json', run / 'summary.json', run / 'records/final.md',
                         run / 'records/events.jsonl']
        secrets_.append(runs.parents[1] / 'private-evaluation/grades.json')
    for path in secrets_:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"answer": "SECRET"}')
        path.chmod(0)
    return order


def test_select_first50_reads_only_task_ids_and_takes_the_seeded_prefix(tmp_path):
    import officeqa_prepare
    ids = [f'UIDQ{k:03}' for k in range(1, 71)]
    order = make_v8_order_tree(tmp_path, ids)
    assert order != sorted(ids)
    _OPENED.clear()
    _RECORD.set()
    try:
        result = SF.select(tmp_path)
    finally:
        _RECORD.clear()
    opened = [p for p in _OPENED if p.startswith(str(tmp_path))]
    assert opened and all(p.endswith(('/task.json', '/prepared/manifest.json')) for p in opened), opened
    assert result['task_ids'] == sorted(order[:50]) and result['sha256'] == arms.subset_sha256(sorted(order[:50]))
    assert result['ordinals'] == [order.index(t) + 1 for t in result['task_ids']]
    assert result['historic_50_matches'] == ['v7-cards'] and result['questions_in_base_order'] == 70
    assert 'SECRET' not in json.dumps(result) and result['rule']['labels_used'] is False
    # the rule is officeqa_prepare.select_rows(count=50) under the v3-v8 seed (data.SEED)
    assert SF.SEED == data.SEED and SF.V8_RUNS == SL.V8_RUNS and SF.subset_sha256(ids) == arms.subset_sha256(ids)
    assert [r['uid'] for r in officeqa_prepare.select_rows([{'uid': i} for i in ids], 50, data.SEED)] == order[:50]
    # refusals: a historic 50-question root that ran other questions; v8 ordinals that disagree; a manifest
    # whose order is not the seeded order
    historic = tmp_path / SF.HISTORIC_50['v7-cards'] / '001/task.json'
    historic.write_text(json.dumps({'ordinal': 1, 'task_id': order[60], 'question': 'Q?'}))
    with pytest.raises(ValueError, match='historic'):
        SF.select(tmp_path)
    historic.write_text(json.dumps({'ordinal': 1, 'task_id': order[0], 'question': 'Q?'}))
    arm = tmp_path / SF.V8_RUNS['prompt']
    (arm / '001/task.json').write_text(json.dumps({'ordinal': 1, 'task_id': order[1], 'question': 'Q?'}))
    (arm / '002/task.json').write_text(json.dumps({'ordinal': 2, 'task_id': order[0], 'question': 'Q?'}))
    with pytest.raises(ValueError, match='ordinals disagree'):
        SF.select(tmp_path)
    manifest = tmp_path / SF.MANIFEST
    manifest.write_text(json.dumps({**json.loads(manifest.read_text()), 'task_ids': sorted(ids)}))
    with pytest.raises(ValueError, match='seeded order'):
        SF.select(tmp_path)


def test_committed_first50_list_is_pinned_by_the_addendum_and_re_derives_from_v8():
    import run
    listing = json.loads((ROOT / 'pilot/first50.json').read_text())
    ids = listing['task_ids']
    assert listing['name'] == SF.NAME == 'pilot-first50' and len(ids) == len(set(ids)) == 50 and ids == sorted(ids)
    assert sorted(listing['ordinals']) == list(range(1, 51)) and listing['sha256'] == arms.subset_sha256(ids)
    assert arms.parse_subset({k: listing[k] for k in ('name', 'task_ids', 'sha256')}).task_ids == tuple(ids)
    assert listing['rule']['labels_used'] is False and listing['rule']['seed'] == data.SEED
    assert listing['script_sha256'] == hashlib.sha256((ROOT / 'pilot/select_first50.py').read_bytes()).hexdigest()
    assert run.PILOT_LIST == ROOT / 'pilot/first50.json'
    # the PREREG addendum text pins the list, its script and the metrics script (a later edit must update it)
    addendum = (ROOT / 'pilot/ADDENDUM.md').read_text()
    for label, name in (('pilot-list-file-sha256', 'pilot/first50.json'),
                        ('pilot-selection-script-sha256', 'pilot/select_first50.py'),
                        ('pilot-metrics-script-sha256', 'pilot/metrics.py')):
        assert f'{label}: {hashlib.sha256((ROOT / name).read_bytes()).hexdigest()}\n' in addendum, label
    assert f'pilot-list-sha256: {listing["sha256"]}\n' in addendum and 'pilot-list-name: pilot-first50\n' in addendum
    assert '{' not in addendum and run.PLACEHOLDER not in addendum
    assert 'pilot-cell-temperature: 1.0\n' in addendum and 'pilot-cell-top-p: 0.95\n' in addendum
    # PREREG.md carries this addendum verbatim below its Addenda line
    prereg = (ROOT / 'PREREG.md').read_text()
    assert addendum in prereg and prereg.index(addendum) > prereg.index('Addenda (dated; appended after hashing):')
    assert run.pilot_addendum(ROOT / 'PREREG.md', listing['sha256'])['cell_top_p'] == 0.95
    project = ROOT.parent
    if not ((project / SF.MANIFEST).is_file() and all((project / rel).is_dir() for rel in SF.V8_RUNS.values())):
        pytest.skip('v8 roots not next to this code copy')
    assert SF.select(project) == listing
    counts = SF.overlap(project, ids)
    assert f'{counts["first50_in_eligible"]} of the 50 are in the loop-prone eligible set' in addendum

# ---------------------------------------------------------------------------------------------
# pilot/metrics.py
# ---------------------------------------------------------------------------------------------

REF = {'T1': '4217.31', 'T2': '1003.57', 'T3': '7.2519', 'T4': '5.5031', 'T5': '12.7593', 'T6': '3.5071',
       'S1': '64.0413', 'S2': '81.9027'}
CANDIDATES = ('10.0357', '6.5047', '99.1257', '3.7519')
TOY = lambda reference, predicted, tolerance=0.0: float((predicted or '').strip() == reference.strip())  # noqa: E731


def _ev(kind, **fields):
    types = {'command': C.Command, 'observe': C.Observe, 'compute': C.Compute, 'candidate': C.Candidate, 'note': C.Note}
    fields.setdefault('call', 1)
    fields.setdefault('t', 0.0)
    return types[kind](kind=kind, **fields).to_json()


def _check(n, kind, answer, status, shown=(), fired=(), message='ok'):
    return {'n': n, 'kind': kind, 'candidate': {'answer': answer, 'via': kind, 'ref': f'a{n}'},
            'result': {'status': status, 'findings': [{'rule': r, 'text': 'x'} for r in shown], 'message': message},
            'fired': [{'rule': r} for r in fired]}


def _cmd_lines(cmd, k):
    return [{'type': 'item.completed', 'item': {'type': 'command_execution', 'command': cmd}}] * k


def make_sealed_root(base: Path, name: str, arm: str, episodes: list[dict], temperature=0, top_p=None) -> Path:
    root = base / name
    order = [e['task'] for e in episodes]
    (root / 'prepared/private').mkdir(parents=True)
    answers = root / 'prepared/private/answers.jsonl'
    answers.write_text(''.join(json.dumps({'task_id': t, 'answer': REF[t]}) + '\n' for t in sorted(REF)))
    (root / 'prepared/manifest.json').write_text(json.dumps({'answers_sha256': data.sha256(answers)}))
    spec = C.STANDARD_ARMS[arm]
    (root / 'freeze.json').write_text(json.dumps({'arm': asdict(spec), 'order': order,
                                                  'settings': {'temperature': temperature, 'top_p': top_p},
                                                  'campaign': {'subset': {'sha256': 'f' * 64}}}))
    (root / 'state.json').write_text(json.dumps({'status': 'completed', 'results': [
        {'ordinal': i, 'task_id': t} for i, t in enumerate(order, start=1)]}))
    (root / 'private-evaluation').mkdir()
    (root / 'private-evaluation/predictions-seal.json').write_text('{}')
    grades = [{'task_id': e['task'], 'strict_score': float(e['final'] == REF[e['task']])} for e in episodes]
    (root / f'private-evaluation/{arm}-grades.json').write_text(json.dumps({'results': grades}))
    for i, e in enumerate(episodes, start=1):
        run = root / 'runs/episodes' / f'{i:03}'
        (run / 'records').mkdir(parents=True)
        (run / 'summary.json').write_text(json.dumps({'status': e.get('status', 'completed'), 'answer': e['final'],
                                                      'controller': e.get('controller')}))
        (run / 'records/events.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in e.get('events', [])))
        if spec.controller:
            (run / 'api/checks').mkdir(parents=True)
            (run / 'api/ledger.jsonl').write_text(''.join(line + '\n' for line in e.get('ledger', [])))
            for check in e.get('checks', []):
                (run / 'api/checks' / f'{check["n"]:03}.json').write_text(json.dumps(check))
    return root


MECH_EPISODES = [
    # 1: right, submitted and accepted, linked to a calc
    {'task': 'T1', 'final': REF['T1'], 'controller': {'submits': 1, 'enforce_requeries': 0},
     'ledger': [_ev('compute', ref='k1', values=[4217.31]), _ev('candidate', ref='a1', answer=REF['T1'])],
     'checks': [_check(1, 'submit', REF['T1'], 'accepted')]},
    # 2: wrong submit -> finding (scale) -> accepted offer -> right  (wrong->right)
    {'task': 'T2', 'final': REF['T2'], 'controller': {'submits': 2, 'enforce_requeries': 0},
     'ledger': [_ev('compute', ref='k1', values=[10.0357]), _ev('candidate', ref='a1', answer='10.0357'),
                _ev('note', ref='n1', reason='gate', text='[harness:gate] finding'),
                _ev('compute', ref='k2', values=[1003.57]), _ev('candidate', ref='a2', answer=REF['T2'])],
     'checks': [_check(1, 'submit', '10.0357', 'findings', ['scale'], ['scale']),
                _check(2, 'submit', REF['T2'], 'accepted')]},
    # 3: right submit -> finding (grounded) -> kept -> right (fired on a right candidate, unchanged); observed value
    {'task': 'T3', 'final': REF['T3'], 'controller': {'submits': 2, 'enforce_requeries': 0},
     'ledger': [_ev('observe', ref='c1', value=7.2519, text='7.2519'), _ev('candidate', ref='a1', answer=REF['T3'])],
     'checks': [_check(1, 'submit', REF['T3'], 'findings', ['grounded'], ['grounded']),
                _check(2, 'submit', REF['T3'], 'kept', (), ['grounded'])]},
    # 4: right submit -> finding (period_match) -> switched to a wrong value  (right->wrong)
    {'task': 'T4', 'final': '6.5047', 'controller': {'submits': 2, 'enforce_requeries': 0},
     'ledger': [_ev('compute', ref='k1', values=[6.5047]), _ev('candidate', ref='a1', answer=REF['T4'])],
     'checks': [_check(1, 'submit', REF['T4'], 'findings', ['period_match'], ['period_match']),
                _check(2, 'submit', '6.5047', 'accepted')]},
    # 5: never submitted; the enforcement re-query showed a finding (its Note is in the ledger); wrong, unlinked
    {'task': 'T5', 'final': '99.1257', 'controller': {'submits': 0, 'enforce_requeries': 1},
     'ledger': [_ev('candidate', ref='a1', answer='99.1257', via='final'),
                _ev('note', ref='n1', reason='enforce', text='Not submitted. MSG-FIVE')],
     'checks': [_check(1, 'final', '99.1257', 'findings', ['answer_count'], ['answer_count'], 'MSG-FIVE')]},
    # 6: a final check with a finding whose re-query was never sent (not shown); timeout, empty; a loop run whose
    #    noted command was issued again after the loop note
    {'task': 'T6', 'final': '', 'status': 'timeout', 'controller': {'submits': 0, 'enforce_requeries': 0},
     'events': _cmd_lines("/bin/zsh -lc 'cat a.txt'", 4),
     'ledger': [_ev('command', ref=f'x{i}', cmd='cat a.txt', tool='exec_command', n=i) for i in (1, 2, 3)]
     + [_ev('note', ref='n1', reason='loop_guard', text='loop'),
        _ev('command', ref='x4', cmd='cat a.txt', tool='exec_command', n=4)],
     'checks': [_check(1, 'final', '3.7519', 'findings', ['scale'], ['scale'], 'MSG-SIX')]},
]
TEXT_EPISODES = [
    {'task': 'S1', 'final': REF['S1'], 'events': _cmd_lines("/bin/zsh -lc 'rg total'", 3)},     # loop, recovered
    {'task': 'S2', 'final': '', 'status': 'timeout', 'events': _cmd_lines("/bin/zsh -lc 'ls'", 5)},
]


def test_pilot_metrics_on_synthetic_sealed_roots(tmp_path):
    mech = make_sealed_root(tmp_path, 'v9-p1-b', 'mech-static', MECH_EPISODES)
    text = make_sealed_root(tmp_path, 'v9-p1-c', 'text-static', TEXT_EPISODES, temperature=0.6)
    result = PM.compute([mech, text], grader=TOY)
    m, t = result['roots']
    assert (m['episodes'], m['right'], m['empty'], m['grade_file_agrees']) == (6, 3, 1, True)
    assert (t['temperature'], t['right'], t['loop_runs'], t['loop_runs_recovered'], t['loop_runs_right']) == (0.6, 1, 2, 1, 1)
    gate = result['mech_static']
    assert (gate['answered'], gate['linked'], gate['coverage']) == (5, 4, 0.8)
    assert (gate['right_episodes'], gate['wrong_episodes'], gate['fired_right'], gate['fired_wrong']) == (3, 3, 2, 2)
    assert (gate['wrong_to_right'], gate['right_to_wrong'], gate['unchanged'], gate['fired_pre_right']) == (1, 1, 2, 2)
    assert gate['right_to_wrong_of_fired_right'] == 0.5
    assert (gate['submit_rate'], gate['requery_rate']) == (4 / 6, 1 / 6)
    assert (gate['loop_notes'], gate['loop_note_episodes'], gate['repeated_after_note'], gate['noted_recovered']) == (1, 1, 1, 0)
    assert (gate['fire_rate_right_by_final'], gate['fire_rate_wrong_by_final']) == (2 / 3, 2 / 3)
    rules = {r['rule']: r for r in gate['per_rule']}
    assert (rules['scale']['engine_fired_wrong'], rules['scale']['engine_fired_right']) == (2, 0)
    assert rules['grounded']['engine_fired_right'] == 1 and rules['period_match']['first_shown_right'] == 1
    assert gate['per_rule'][0]['engine_precision'] == 0.0                 # lowest-precision rule listed first
    rows = {r['criterion'][:2]: r['verdict'] for r in result['decision']['rows']}
    assert rows == {'1 ': 'PASS', '2 ': 'FAIL', '3 ': 'FAIL', '4a': 'FAIL', '4b': 'FAIL', '5 ': 'REPORTED'}
    assert result['decision']['go'] is False
    text_out = PM.render(result) + json.dumps(result, default=str)
    for value in list(REF.values()) + list(CANDIDATES):
        assert value not in text_out, value                               # no answer value is ever printed
    assert 'DECISION: NO-GO' in PM.render(result)
    # the temperature rule needs the T=0 cell and the T=1.0 / top_p 0.95 cell (here: one text root at 0.6)
    assert result['temperature_rule']['computable'] is False and result['temperature_rule']['verdict'] is None
    assert 'not computable' in PM.render(result)


def _text_cell(loops: int, right: int) -> list[dict]:
    """Eight text-static episodes over the REF tasks: the first `loops` are loop runs (one command issued 3
    times; the others issue it twice), the first `right` answers are right."""
    episodes = []
    for i, task in enumerate(sorted(REF)):
        episodes.append({'task': task, 'final': REF[task] if i < right else 'no-match',
                         'events': _cmd_lines(f"/bin/zsh -lc  'cat {task}.txt'", 3 if i < loops else 2)})
    return episodes


@pytest.mark.parametrize('a, c, switch', [
    ((4, 6), (2, 4), True),      # exactly 50% fewer loop runs, exactly 2 fewer right: both conditions met
    ((5, 2), (2, 2), True),      # 60% fewer, same accuracy
    ((5, 2), (3, 8), False),     # 40% fewer loop runs: not met (even with better accuracy)
    ((4, 6), (0, 3), False),     # no loop run left, but 3 fewer right: not met
    ((0, 6), (0, 8), False),     # cell a has no loop run: nothing to reduce, T=0
])
def test_confirmatory_temperature_rule_on_synthetic_cells(tmp_path, a, c, switch):
    cell_a = make_sealed_root(tmp_path, 'v9-p1-a', 'text-static', _text_cell(*a), temperature=0)
    cell_b = make_sealed_root(tmp_path, 'v9-p1-b', 'mech-static', MECH_EPISODES)
    cell_c = make_sealed_root(tmp_path, 'v9-p1-c', 'text-static', _text_cell(*c), temperature=1.0, top_p=0.95)
    result = PM.compute([cell_a, cell_b, cell_c], grader=TOY)
    rule = result['temperature_rule']
    assert rule['computable'] and (rule['cell_a'], rule['cell_c'], rule['questions']) == ('v9-p1-a', 'v9-p1-c', 8)
    assert (rule['loop_runs_a'], rule['loop_runs_c'], rule['right_a'], rule['right_c']) == (a[0], c[0], a[1], c[1])
    assert rule['accuracy_difference_questions'] == c[1] - a[1]
    assert rule['loop_condition_met'] == (a[0] > 0 and 2 * c[0] <= a[0])
    assert rule['accuracy_condition_met'] == (c[1] - a[1] >= -2)
    assert rule['verdict'] == ('T=1.0, top_p 0.95' if switch else 'T=0')
    assert (rule['confirmatory_temperature'], rule['confirmatory_top_p']) == ((1.0, 0.95) if switch else (0, None))
    # the go/no-go decision is untouched by the rule (same mech-static episodes as above: NO-GO)
    assert result['decision']['go'] is False and PM.COVERAGE_MIN == 0.60 and PM.RIGHT_FIRE_MAX == 0.20
    text = PM.render(result)
    assert f'RULE VERDICT: confirmatory arms at {rule["verdict"]}' in text and 'DECISION: NO-GO' in text
    out = text + json.dumps(result, default=str)
    for value in list(REF.values()) + list(CANDIDATES):
        assert value not in out, value                                    # no answer value is ever printed


def test_temperature_rule_needs_both_cells_on_the_same_questions(tmp_path, capsys):
    cell_a = make_sealed_root(tmp_path, 'v9-p1-a', 'text-static', _text_cell(4, 6), temperature=0)
    other = make_sealed_root(tmp_path, 'v9-p1-c', 'text-static', _text_cell(1, 6), temperature=1.0)   # no top_p
    rule = PM.compute([cell_a, other], grader=TOY)['temperature_rule']
    assert rule['computable'] is False and 'found 0' in rule['reason']
    short = make_sealed_root(tmp_path, 'v9-p1-d', 'text-static', _text_cell(1, 6)[:6], temperature=1.0, top_p=0.95)
    with pytest.raises(ValueError, match='different numbers of questions'):
        PM.compute([cell_a, short], grader=TOY)
    # command line: no mech-static root -> exit 2, the rule verdict is still printed
    cell_c = make_sealed_root(tmp_path, 'v9-p1-e', 'text-static', _text_cell(2, 6), temperature=1.0, top_p=0.95)
    import unittest.mock
    with unittest.mock.patch.object(PM, 'official_grader', lambda root: TOY):
        assert PM.main([str(cell_a), str(cell_c)]) == 2
    assert 'RULE VERDICT: confirmatory arms at T=1.0, top_p 0.95' in capsys.readouterr().out


def test_pilot_metrics_command_line(tmp_path, monkeypatch, capsys):
    mech = make_sealed_root(tmp_path, 'v9-p1-b', 'mech-static', MECH_EPISODES)
    monkeypatch.setattr(PM, 'official_grader', lambda root: TOY)          # production: the root's pinned reward.py
    assert PM.main([str(mech), '--json', str(tmp_path / 'm.json')]) == 1  # NO-GO
    out = capsys.readouterr().out + (tmp_path / 'm.json').read_text()
    assert 'DECISION: NO-GO' in out and '1 ledger coverage' in out
    for value in list(REF.values()) + list(CANDIDATES):
        assert value not in out, value


def test_pilot_go_no_go_verdicts_and_refusals(tmp_path):
    good = {'coverage': 0.7, 'linked': 14, 'answered': 20, 'coverage_all_episodes': 0.7, 'episodes': 20,
            'fire_rate_right': 0.1, 'fire_rate_wrong': 0.5, 'fired_right': 1, 'right_episodes': 10,
            'fired_wrong': 5, 'wrong_episodes': 10, 'wrong_to_right': 3, 'right_to_wrong': 1, 'fired': 6,
            'fired_pre_right': 20, 'right_to_wrong_of_fired_right': 0.05, 'submit_rate': 0.4, 'requery_rate': 0.1}
    decision = PM.go_no_go(good)
    assert decision['go'] is True and decision['rows'][-2]['verdict'] == 'INVESTIGATE'
    assert PM.go_no_go({**good, 'wrong_to_right': 0, 'right_to_wrong': 0})['rows'][2]['verdict'] == 'NO DATA'
    assert PM.go_no_go({**good, 'fire_rate_right': 0.0, 'fire_rate_wrong': 0.0})['go'] is False
    assert PM.go_no_go({**good, 'fire_rate_right': 0.0})['rows'][1]['verdict'] == 'PASS'
    assert PM.go_no_go({**good, 'coverage': 0.59})['go'] is False
    assert PM.go_no_go({**good, 'fire_rate_right': 0.21, 'fire_rate_wrong': 0.9})['rows'][3]['verdict'] == 'FAIL'
    assert PM.go_no_go({**good, 'right_to_wrong_of_fired_right': 0.051})['go'] is False
    # only after the seal
    root = make_sealed_root(tmp_path, 'v9-p1-b', 'mech-static', MECH_EPISODES[:1])
    (root / 'private-evaluation/predictions-seal.json').unlink()
    with pytest.raises(ValueError, match='not sealed'):
        PM.compute([root], grader=TOY)
    root2 = make_sealed_root(tmp_path, 'v9-p1-x', 'text-static', TEXT_EPISODES)
    assert PM.compute([root2], grader=TOY)['decision'] is None
    answers = root2 / 'prepared/private/answers.jsonl'
    answers.write_text(answers.read_text() + '\n')
    with pytest.raises(ValueError, match='answer key changed'):
        PM.compute([root2], grader=TOY)

def test_pilot_metrics_review_refusals_and_answer_span(tmp_path):
    """Review fixes: coverage links the answer span (not prose numbers of the final message); a root whose
    official grades disagree with the controller-side grading, or that was never graded, is refused; roots
    with different subsets are refused."""
    # prose numbers: 2001 is printed by a tool, the answer 12.7593 is not -> not linked
    events = [C.Observe(kind='observe', ref='c1', call=1, t=0.0, value=2001.0, text='2001')]
    prose = 'In fiscal year 2001 the value was reported.\nFinal answer: 12.7593'
    assert PM.extract_final(prose) == '12.7593'
    assert PM.links_to_ledger(prose, events) and not PM.links_to_ledger(PM.extract_final(prose), events)
    episode = {'task': 'T5', 'final': prose, 'controller': {'submits': 0, 'enforce_requeries': 0},
               'ledger': [_ev('observe', ref='c1', value=2001.0, text='2001')]}
    root = make_sealed_root(tmp_path, 'v9-p1-b', 'mech-static', [episode])
    gate = PM.compute([root], grader=TOY)['mech_static']
    assert (gate['answered'], gate['linked']) == (1, 0)
    # the official grades file must agree with the controller-side grading of the sealed answers
    bad = make_sealed_root(tmp_path, 'v9-p1-y', 'mech-static', MECH_EPISODES)
    grades = bad / 'private-evaluation/mech-static-grades.json'
    value = json.loads(grades.read_text())
    value['results'][0]['strict_score'] = 0.0
    grades.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='differ'):
        PM.compute([bad], grader=TOY)
    grades.unlink()
    with pytest.raises(ValueError, match='graded'):
        PM.compute([bad], grader=TOY)
    # every root must run the same list
    text = make_sealed_root(tmp_path, 'v9-p1-z', 'text-static', TEXT_EPISODES)
    frozen = json.loads((text / 'freeze.json').read_text())
    frozen['campaign']['subset']['sha256'] = 'e' * 64
    (text / 'freeze.json').write_text(json.dumps(frozen))
    with pytest.raises(ValueError, match='different question subsets'):
        PM.compute([root, text], grader=TOY)

# ---------------------------------------------------------------------------------------------
# run.py pilot-roots
# ---------------------------------------------------------------------------------------------


def plumbing_copy(tmp_path_factory) -> Path:
    target = tmp_path_factory.mktemp('plumb') / 'officeqa-pro133-v9-plumbing'
    shutil.copytree(ROOT, target, ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache', '*.pyc',
                                                                'preflight', '.task-paths.json'))
    return target


def test_pilot_roots_need_the_filled_addendum_then_create_three_roots(tmp_path_factory, tmp_path):
    import run
    source = plumbing_copy(tmp_path_factory)
    parent = tmp_path / 'project'
    framework, cache = parent / 'fw', parent / 'cache'
    framework.mkdir(parents=True)
    cache.mkdir()
    options = {'framework': framework, 'data_cache': cache, 'source': source}
    # this copy's PREREG.md without any pilot addendum (the user may already have appended the filled one)
    prereg = source / 'PREREG.md'
    original = prereg.read_text().split('\n### Addendum P1: pilot', 1)[0].rstrip('\n') + '\n'
    prereg.write_text(original)
    with pytest.raises(ValueError, match='pilot addendum'):
        run.pilot_roots(parent, **options)
    addendum = (source / 'pilot/ADDENDUM.md').read_text()              # as committed, or already filled
    prereg.write_text(original + '\n' + addendum + '\npilot-budget-cap-usd: [USER: cap]\n')
    with pytest.raises(ValueError, match=r'\[USER:'):
        run.pilot_roots(parent, **options)
    filled = re.sub(r'\[USER:[^\]]*\]', '20', addendum)
    # the cell's top_p must be declared exactly once, as a number in (0, 1]
    for bad in ('', 'pilot-cell-top-p: 1.5\n', 'pilot-cell-top-p: high\n'):
        text = re.sub(r'^pilot-cell-top-p: .*\n', bad, filled, flags=re.M)
        prereg.write_text(original + '\n' + text)
        with pytest.raises(ValueError, match='top.p'):
            run.pilot_roots(parent, **options)
    prereg.write_text(original + '\n' + filled)
    listing = json.loads((source / 'pilot/first50.json').read_text())
    result = run.pilot_roots(parent, **options)
    assert result['paid_calls'] == 0 and result['subset'] == {'name': 'pilot-first50', 'sha256': listing['sha256'],
                                                              'count': 50}
    expected = {'v9-p1-a': ('text-static', 0, None), 'v9-p1-b': ('mech-static', 0, None),
                'v9-p1-c': ('text-static', 1.0, 0.95)}
    assert {n: (v['arm'], v['temperature'], v['top_p']) for n, v in result['created'].items()} == expected
    for name, (arm, temperature, top_p) in expected.items():
        root = parent / name
        config = json.loads((root / '.task-paths.json').read_text())
        spec = arms.parse_config(config, root_name=name)
        assert spec.arm.name == arm and spec.temperature == temperature and spec.repeat == 1
        assert spec.top_p == top_p and ('top_p' in config) == (top_p is not None)       # a, b: the key is absent
        assert spec.order_seed == 'v9-officeqa-pro133-order' and spec.subset.sha256 == listing['sha256']
        assert spec.barrier.roots == tuple(expected) and spec.barrier.timeout_s == 43200
        assert config['framework'] == str(framework) and config['data_cache'] == str(cache)
        assert not arms.revealing_fragments(name, spec.arm)
        assert CP.sha(root / 'PREREG.md') == CP.sha(prereg) and (root / 'tests/test_v9_pilot.py').is_file()
        assert not (root / 'prepared').exists() and not (root / 'preflight').exists()
    assert json.loads((parent / 'v9-p1-c/.task-paths.json').read_text())['temperature'] == 1.0     # kept as written
    probe = subprocess.run([PY, '-B', '-c', 'import json, experiment as e; print(json.dumps([e.ARM.name, e.TEMPERATURE, '
                            'e.TOP_P, e.CAMPAIGN.subset.sha256, len(e.CAMPAIGN.subset.task_ids)]))'],
                           cwd=parent / 'v9-p1-c', capture_output=True, text=True, timeout=60)
    assert json.loads(probe.stdout) == ['text-static', 1.0, 0.95, listing['sha256'], 50], probe.stderr
    with pytest.raises(ValueError, match='created once'):
        run.pilot_roots(parent, **options)
    with pytest.raises(ValueError, match='not inside a campaign root'):
        run.pilot_roots(tmp_path / 'elsewhere', source=parent / 'v9-p1-a', framework=framework, data_cache=cache)
