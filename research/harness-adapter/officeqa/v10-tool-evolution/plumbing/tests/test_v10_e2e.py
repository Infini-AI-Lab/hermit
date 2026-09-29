"""S3: a $0 end-to-end rehearsal of the whole v10 pipeline on the toy pack (DESIGN §8 S3).

One repeat group of five roots (plain, prompt-evolve, harness-static, harness-evolve, harness-evolve-notes),
each a real `campaign.Campaign` over the toy pack (10 questions, 2 blocks of 5; one question declared
disputed), run concurrently behind one file barrier. Every episode (scored and verification) is the
production path: `campaign.prepare_episode` (kit; the harness arms' tools served from the adapter through the
neutral code root <root>/runs/code/<sha12>/tools; README = L0 + conventions + '# Tools' + the adapter's
USAGE.md; AGENTS.md with <= 3 notes) -> `campaign.execute_episode` -> the real broker (+ Ledger / NullGate /
MechController with controls.json on the harness arms, gate off: no submit route, no submit wrapper, gate-off
loop wording) -> the pinned real Codex binary under the Seatbelt profile. The broker's upstream is
tests/fake_upstream.py on 127.0.0.1 answering with the scripted weak actor (tests/e2e/weak_actor.py); the
learners are the real rounds (prompt_learning / harness_learning through learner_common) driven by scripted
learner models (tests/e2e/learners.py: one scoped note, and for the harness learner one tool patch verified
through screens, replay and paired re-runs), with verification through the campaign's actor runner. The toy
pack's grader stands in for reward.py. No network beyond 127.0.0.1, no API key (a dummy string), no paid call.

After the campaign: full_audit on every root, cost, report, and the rehearsal invariants (controller records
and gate-off counters, no controller fields on the plain / prompt arms, deterministic learner outcomes, the
served code root and its shas, arm-neutral model-visible text, no unreleased answer in any learner-visible
file). Further scenarios: a loop under gate false with the R6 hard block, a crash after a learning round with
a block-level resume, an unreliable upstream with an actor timeout, and a learned tool revision admitted,
served from its own code root, changing a later answer through the tool output alone, and audited.
Opt-in: `-m live_codex` or V9_LIVE_CODEX=1 (the fast regression tests at the end run by default).
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import arms  # noqa: E402
import campaign as CP  # noqa: E402
import controller_runtime as CR  # noqa: E402
import cost  # noqa: E402
import experiment  # noqa: E402
import fake_upstream as fu  # noqa: E402
import full_audit  # noqa: E402
import kit  # noqa: E402
import paths  # noqa: E402
import preflight  # noqa: E402
import rate_control  # noqa: E402
import report  # noqa: E402
import run as RUN  # noqa: E402
from e2e import learners as EL  # noqa: E402
from e2e import toy_campaign as TC  # noqa: E402
from e2e import weak_actor as WA  # noqa: E402
from harness_core import contracts as C  # noqa: E402
from harness_core.ledger import load_events  # noqa: E402
from packs.toy.items import ITEMS, REFERENCES  # noqa: E402

NO_CODEX = not paths.CODEX.is_file() or not Path('/usr/bin/sandbox-exec').is_file()
NO_SANDBOX = not Path('/usr/bin/sandbox-exec').is_file()


def live(test):
    """The rehearsal scenarios launch the pinned real Codex (opt-in group `live_codex`; each scenario < 60 s)."""
    test = pytest.mark.skipif(NO_CODEX, reason='pinned codex or sandbox-exec missing')(test)
    return pytest.mark.live_codex(test)


KEY = 'dummy-e2e-broker-key-not-a-secret-0000'
ARMS = ('plain', 'prompt-evolve', 'harness-static', 'harness-evolve', 'harness-evolve-notes')
ROOTS = {arm: f'v10-r1-{chr(ord("a") + i)}' for i, arm in enumerate(ARMS)}     # arm-neutral root names
ORDER_SEED = 'toy-e2e-73'       # block 1 = toy01 toy02 toy06 toy07 toy08; block 2 = toy04 toy05 toy03 toy09 toy10
WITHHELD = ('toy06',)           # the toy's stand-in for an issue-47 question: scored, never released
ACTOR_SECONDS = 300             # per-episode wall clock of the rehearsal (production: 3600)
QUESTION = {item.item_id: item.question for item in ITEMS}


class SimulatedCrash(RuntimeError):
    """Stands in for a controller crash (OOM, reboot) at a chosen point of a campaign."""


def make_group(base: Path, roots: dict[str, str], *, responder=WA.respond, seconds: int = ACTOR_SECONDS,
               order_seed: str = ORDER_SEED, harness_learner: dict | None = None) -> dict:
    """Code copy (toy W0s), one prepared root per arm, one barrier group, one fake upstream serving them all."""
    code = TC.code_copy(base / 'code')
    barrier = {'dir': str(base / 'barrier'), 'roots': list(roots.values()), 'timeout_s': 900, 'poll_s': 0.1}
    g = {'base': base, 'code': code, 'up': fu.Upstream(responder), 'roots': {}, 'specs': {}, 'states': {},
         'actor_seconds': seconds, 'harness_learner': dict(harness_learner or {}),
         'errors': {}, 'models': {}, 'rounds': {}, 'seconds': {}, 'episodes': {},
         'scheduler': rate_control.RateScheduler(max_in_flight=float('inf'), min_interval=0, backoffs=(0,))}
    for arm, name in roots.items():
        spec = arms.parse_config({'profile': arms.PROFILE, 'arm': arm, 'repeat': 1, 'pack': 'toy', 'order_seed': order_seed,
                                  'barrier': barrier}, root_name=name)
        g['specs'][arm] = spec
        g['roots'][arm] = TC.make_root(base, name, spec, withheld=WITHHELD)
    return g


class rehearsal_clock:
    """The per-episode wall clock of the rehearsal, for the campaign and the audit alike."""

    def __init__(self, seconds: int = ACTOR_SECONDS):
        self.seconds = seconds

    def __enter__(self):
        self.saved = (CP.ACTOR_SECONDS, experiment.ACTOR_SECONDS)
        CP.ACTOR_SECONDS = experiment.ACTOR_SECONDS = self.seconds

    def __exit__(self, *exc):
        CP.ACTOR_SECONDS, experiment.ACTOR_SECONDS = self.saved


def run_roots(g: dict, *, resume: bool = False, crash_after_learning: tuple[str, ...] = ()) -> None:
    """Run every root of the group concurrently (one thread each), through the production episode path."""
    up, scheduler, code = g['up'], g['scheduler'], g['code']
    g['states'], g['errors'] = {}, {}

    def go(arm):
        spec, root = g['specs'][arm], g['roots'][arm]
        lay = CP.Layout(root=root, code=code)
        timings = g['episodes'].setdefault(arm, [])

        def episode(*, task, adapter, base, ordinal, role, temperature=0, top_p=None):
            begun = time.monotonic()
            summary = CP.episode(KEY, lay, spec.arm, task, adapter, base, ordinal=ordinal, role=role,
                                 broker_extra={'upstream_url': up.url, 'scheduler': scheduler}, temperature=temperature,
                                 top_p=top_p)
            timings.append((role, round(time.monotonic() - begun, 2)))
            return summary
        learn = None
        if spec.arm.learner is not None:
            def learn(**offered):
                model = (EL.prompt_model() if spec.arm.learner == 'text'
                         else EL.harness_model(**g['harness_learner']))
                g['models'][arm] = model
                result = CP.call_learner(spec.arm, model=model, **offered)
                g['rounds'].setdefault(arm, []).append(result)
                if arm in crash_after_learning and len(g['rounds'][arm]) == 1:
                    raise SimulatedCrash(f'simulated crash after learning round {offered["block"]}')
                return result
        hooks = CP.Hooks(episode=episode, learn=learn, scorer=TC.toy_score, verify_data=False, deep_runtime=False,
                         report=True)
        begun = time.monotonic()
        try:
            g['states'][arm] = CP.Campaign(KEY, spec, lay, hooks=hooks).run(resume=resume)
        except BaseException as exc:          # recorded; the tests assert on it
            g['errors'][arm] = f'{type(exc).__name__}: {exc}'
        g['seconds'][arm] = round(time.monotonic() - begun, 1)

    with rehearsal_clock(g['actor_seconds']):
        begun = time.monotonic()
        threads = [threading.Thread(target=go, args=(arm,), name=f'root-{arm}') for arm in g['specs']]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1800)
        g['campaign_seconds'] = round(time.monotonic() - begun, 1)


def close_group(g: dict, *, close_upstream: bool = True) -> None:
    """Audit (with this group's upstream URL), cost and report of every completed root."""
    with rehearsal_clock(g['actor_seconds']):
        begun = time.monotonic()
        g['audits'] = {arm: full_audit.build(g['roots'][arm], code=g['code'], upstream=g['up'].url, write=True)
                       for arm in g['specs'] if arm in g['states']}
        g['audit_seconds'] = round(time.monotonic() - begun, 1)
    g['costs'] = {arm: cost.estimate(g['roots'][arm]) for arm in g['specs'] if arm in g['states']}
    for arm in g['specs']:
        if arm in g['states']:
            report.build(g['roots'][arm])
    if close_upstream:
        g['up'].close()


def run_group(base: Path) -> dict:
    g = make_group(base, ROOTS)
    try:
        run_roots(g)
    finally:
        close_group(g)
    return g


@pytest.fixture(scope='module')
def group(tmp_path_factory):
    base = tmp_path_factory.mktemp('g')
    result = run_group(base)
    timing = {'campaign_seconds': result['campaign_seconds'], 'audit_seconds': result['audit_seconds'],
              'root_seconds': result['seconds'],
              'episodes': {arm: {'scored': len([t for r, t in v if r == 'scored']),
                                 'verify': len([t for r, t in v if r == 'verify']),
                                 'mean_s': round(sum(t for _, t in v) / max(1, len(v)), 2),
                                 'max_s': max((t for _, t in v), default=0)}
                           for arm, v in result['episodes'].items()},
              'upstream_requests': len(result['up'].bodies)}
    (base / 'timings.json').write_text(json.dumps(timing, indent=1))
    print('\nS3 e2e timings: ' + json.dumps(timing))
    return result


def events_of(api: Path):
    return load_events(api / 'ledger.jsonl') if (api / 'ledger.jsonl').is_file() else []


def failed_checks(g, arm):
    return json.loads((g['roots'][arm] / 'preflight/final-audit.json').read_text())['failed']


@live
def test_every_root_completes_and_the_audit_passes(group):
    assert not group['errors'], group['errors']
    for arm in ARMS:
        state = group['states'][arm]
        assert state['status'] == 'completed', (arm, state.get('error'))
        assert group['audits'][arm]['passed'], (arm, failed_checks(group, arm)[:10])

# ---------------------------------------------------------------------------------------------
# Expected behaviour of the scripted weak actor (tests/e2e/weak_actor.py) per arm: with the gate off no arm
# corrects an answer at submit time; learned notes and the evolved tool touch no block-2 question's answer
# (their support questions are in block 1), so every arm answers the UNAIDED values.
# ---------------------------------------------------------------------------------------------

UNAIDED = {'toy01': '250.75', 'toy02': '11.53', 'toy06': '1.225', 'toy07': '36.35', 'toy08': '2.80',
           'toy04': '82.3', 'toy05': '65.7', 'toy03': '14.77', 'toy09': '242', 'toy10': '0.1340'}
EXPECTED = {arm: UNAIDED for arm in ARMS}
BLOCK1, BLOCK2 = ['toy01', 'toy02', 'toy06', 'toy07', 'toy08'], ['toy04', 'toy05', 'toy03', 'toy09', 'toy10']
CONTROLLER_ARMS = ('harness-static', 'harness-evolve', 'harness-evolve-notes')
TEXT_ARMS = ('plain', 'prompt-evolve')
LEARNER_ARMS = ('prompt-evolve', 'harness-evolve', 'harness-evolve-notes')
NOTE_ARMS = ('prompt-evolve', 'harness-evolve-notes')
VERIFY_RUNS = {'prompt-evolve': 8, 'harness-evolve-notes': 8, 'harness-evolve': 12}   # (support + controls) x K


def run_dir(group, arm, task):
    return group['roots'][arm] / 'runs/episodes' / f'{(BLOCK1 + BLOCK2).index(task) + 1:03}'


def api_dirs(root: Path):
    """Every episode api/ directory of a root (scored and verification)."""
    return sorted(p for p in root.rglob('api') if p.is_dir() and (p / 'summary.json').is_file())


def request_bodies(root: Path, *, scored_only: bool = False):
    for api in api_dirs(root):
        if scored_only and ('evolution' in api.parts or 'verify' in api.parts):
            continue
        for request in sorted(api.glob('[0-9][0-9][0-9]/request.json')):
            yield request, json.loads(request.read_text())


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)


@live
def test_order_barrier_seal_release_and_grades(group):
    orders = {group['states'][arm]['order_sha256'] for arm in ARMS}
    assert len(orders) == 1                                             # one question order per repeat group
    barrier = Path(group['specs']['plain'].barrier.dir)
    for block in (0, 1, 2):
        assert sorted(p.stem for p in (barrier / f'block-{block:02}').glob('*.done')) == sorted(ROOTS.values())
    assert not (barrier / 'ABORT').exists()
    for arm in ARMS:
        root, state = group['roots'][arm], group['states'][arm]
        assert [r['task_id'] for r in sorted(state['results'], key=lambda r: r['ordinal'])] == BLOCK1 + BLOCK2
        assert {r['task_id']: r['answer'] for r in state['results']} == EXPECTED[arm], arm
        metrics = json.loads((root / 'private-evaluation/paired-metrics.json').read_text())
        right = sum(TC.toy_score(REFERENCES[t], a) for t, a in EXPECTED[arm].items())
        assert metrics['arms'][arm]['correct'] == right and metrics['denominator'] == 10
        assert metrics['undisputed']['n'] == 9                           # toy06 scored, excluded from the subset
        assert {r['task_id'] for r in metrics['results'] if r['issue_47']} == set(WITHHELD)
        if arm in LEARNER_ARMS:
            seal = json.loads((root / 'private-learning-seals/01.json').read_text())
            assert [e['id'] for e in seal['withheld_episodes']] == ['toy06']
            assert sorted(e['id'] for e in seal['episodes']) == sorted(set(BLOCK1) - {'toy06'})
            released = {json.loads(p.read_text())['task_id'] for p in (root / 'evidence').rglob('reference_answer.json')}
            assert released == set(BLOCK1) - {'toy06'}                    # block 2 and the disputed id: never
            assert not (root / 'private-learning-seals/02.json').exists() and not (root / 'evidence/02').exists()
            assert [(r['block'], r['status']) for r in state['learning']] == [(1, 'admitted'), (2, 'skipped_unused')]
        else:
            assert not any((root / d).exists() for d in ('private-learning-seals', 'evidence', 'evolution'))
            assert state['learning'] == []
    assert all(r['status'] == 'completed' for arm in ARMS for r in group['states'][arm]['results'])


@live
def test_controller_arms_loop_notes_gate_off_and_splices(group):
    """Gate off (DESIGN §3): the loop guard uses the gate-off wording (no `submit`, no candidates line), no
    submit, gate round, re-query or check file exists, and the post-compaction splice still runs."""
    for arm in CONTROLLER_ARMS:
        totals = {'loop_guard': 0, 'ledger': 0, 'enforce': 0}
        for task in BLOCK1 + BLOCK2:
            base = run_dir(group, arm, task)
            api = base / 'api'
            events = events_of(api)
            summary = json.loads((base / 'summary.json').read_text())
            counters = summary['controller']
            assert counters['controller_errors'] == 0, (arm, task)
            assert counters['gate'] is False and counters['submit_enabled'] is False, (arm, task)
            assert counters['submits'] == counters['gate_rounds'] == counters['enforce_requeries'] == 0, (arm, task)
            assert not (api / 'checks').exists() and not [e for e in events if isinstance(e, C.Candidate)]
            assert isinstance(counters['observation_coverage'], dict) and counters['observation_coverage']['commands'] >= 1
            notes = [e for e in events if isinstance(e, C.Note)]
            if task in ('toy02', 'toy09'):                               # the loop items
                loop_notes = [n for n in notes if n.reason == 'loop_guard']
                assert len(loop_notes) == 1 and counters['inserts_by_reason'].get('loop_guard') == 1, (arm, task)
                assert 'issued this exact command 3 times' in loop_notes[0].text, (arm, task)
                assert CR.LOOP_OPTIONS_NOGATE in loop_notes[0].text and 'submit' not in loop_notes[0].text.lower()
                assert 'Current candidates' not in loop_notes[0].text
                commands = [e for e in events if isinstance(e, C.Command) and e.cmd.startswith('lookup')]
                assert len(commands) == 3                                 # the note stopped the loop
                assert counters['loop_notes'] == 1 and counters['hard_blocks'] == 0
            if task == 'toy08':                                          # forced compaction -> frozen ledger splice
                assert [e.kind for e in events if e.kind == 'compaction'] == ['compaction']
                assert [n.reason for n in notes if n.reason == 'ledger'] == ['ledger']
                inserted = [i for p in sorted(api.glob('[0-9][0-9][0-9]/controller.json'))
                            for i in json.loads(p.read_text())['inserted'] if i['reason'] == 'ledger']
                assert inserted and counters['compactions'] == counters['resumes'] == 1
                resume = [json.loads(p.read_text()) for p in sorted(api.glob('[0-9][0-9][0-9]/transport.json'))]
                # gate off: the answer comes in the resume call itself (no submit call follows it, as with the gate)
                assert [m['kind'] for m in resume] == ['turn', 'turn', 'compaction', 'resume']
            assert not [n for n in notes if n.reason in ('enforce', 'gate')]
            metas = [json.loads(p.read_text()) for p in sorted(api.glob('[0-9][0-9][0-9]/transport.json'))]
            assert all(m['origin'] == 'codex' for m in metas)               # never a controller re-query
            for reason in ('loop_guard', 'ledger', 'enforce'):
                totals[reason] += counters['inserts_by_reason'].get(reason, 0)
            assert summary['fallback_answer'] is None and summary['fallback_differs'] is None
        assert totals == {'loop_guard': 2, 'ledger': 1, 'enforce': 0}, (arm, totals)


@live
def test_text_arms_carry_no_controller_records_and_harness_arms_serve_tools(group):
    for arm in TEXT_ARMS:
        root = group['roots'][arm]
        apis = api_dirs(root)
        assert len(apis) == 10 + VERIFY_RUNS.get(arm, 0)                  # scored + verification episodes
        for api in apis:
            assert not any((api / n).exists() for n in ('controller.jsonl', 'ledger.jsonl', 'checks'))
            summary = json.loads((api / 'summary.json').read_text())
            assert not set(summary) & set(full_audit.CONTROLLER_SUMMARY_FIELDS)
            for call in sorted(api.glob('[0-9][0-9][0-9]')):
                meta = json.loads((call / 'transport.json').read_text())
                assert not set(meta) & set(full_audit.CONTROLLER_RECORD_FIELDS) and not (call / 'controller.json').exists()
            episode = json.loads((api.parent / 'summary.json').read_text())
            assert episode['controller'] is None and episode['fallback_answer'] is None and episode['code_root'] is None
            kit_bin = api.parent / 'workspace/.kit/bin'
            assert not (kit_bin / 'submit').exists() and not (kit_bin / 'lookup').exists() and not (kit_bin / 'calc').exists()
            readme = (api.parent / 'workspace/README.md').read_text()
            assert '# Submitting your answer' not in readme and CP.TOOLS_HEADING not in readme
        for request, body in request_bodies(root):
            assert not any('[runtime:' in s for s in strings(body['input'])), request
    for arm in CONTROLLER_ARMS:
        root = group['roots'][arm]
        apis = api_dirs(root)
        assert len(apis) == 10 + VERIFY_RUNS.get(arm, 0)
        for api in apis:
            episode = json.loads((api.parent / 'summary.json').read_text())
            marker = json.loads((api.parent / 'workspace/.acting-prepared.json').read_text())
            code_root = Path(episode['code_root'])
            assert code_root == Path(marker['kit']['code_roots'][0]) and code_root.parent.parent == root / 'runs/code'
            assert code_root.parent.name == episode['code_sha12'] == episode['adapter_revision']
            kit_bin = api.parent / 'workspace/.kit/bin'
            assert (kit_bin / 'lookup').exists() and (kit_bin / 'calc').exists() and not (kit_bin / 'submit').exists()
            assert f'exec ' in (kit_bin / 'lookup').read_text() and str(code_root / 'lookup.py') in (kit_bin / 'lookup').read_text()
            readme = (api.parent / 'workspace/README.md').read_text()
            assert CP.TOOLS_HEADING + '\n' in readme and 'lookup <file>' in readme and '# Submitting your answer' not in readme
        texts = [s for _, body in request_bodies(root) for s in strings(body['input'])]
        assert any(s.startswith('[runtime:loop_guard]') for s in texts) and any(s.startswith('[runtime:ledger]') for s in texts)
        assert not any(s.startswith('[runtime:enforce]') or '[runtime:gate]' in s for s in texts)


@live
def ledger_rows(root: Path):
    path = root / 'learning-ledger.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []


def test_learners_admit_deterministically_and_serve_the_revision(group):
    import learner_common as LC

    def rows(arm):
        return ledger_rows(group['roots'][arm])
    for arm in NOTE_ARMS:
        note_rows = rows(arm)
        assert [(r['kind'], r['id'], r['admitted']) for r in note_rows] == [('note', 'revised_figures', True)], arm
        assert (note_rows[0]['support'], sorted(note_rows[0]['keep'])) == ([1, 4], [2, 5]), arm
    harness_rows = rows('harness-evolve')
    assert [(r['kind'], r['id'], r['admitted']) for r in harness_rows] == [
        ('note', 'revised_figures', True), ('revision', EL.REVISION_ID, True)]
    note_row, revision_row = harness_rows
    assert (note_row['support'], sorted(note_row['keep'])) == ([4], [2, 5])
    assert (revision_row['support'], sorted(revision_row['keep'])) == ([1], [2, 5])
    assert revision_row['tier'] == 'additive' and revision_row['files'] == [EL.PATCH_PATH]
    assert revision_row['expect'] == {'1': revision_row['expect']['1']} and len(revision_row['expect']['1']) == 1
    assert revision_row['replay']['status'] == 'ok' and revision_row['replay']['counts']['changed_output'] >= 1
    # the verdicts re-derive from the recorded verification runs (cards judge; no model call involved)
    for arm, row in (('prompt-evolve', rows('prompt-evolve')[0]), ('harness-evolve-notes', rows('harness-evolve-notes')[0]),
                     ('harness-evolve', note_row)):
        root = group['roots'][arm]
        verification = json.loads((root / 'evolution/01/verification.json').read_text())
        runs = verification['runs'][row['id']]
        originals = {int(p.parent.name): json.loads(p.read_text())['out_tokens']
                     for p in (root / 'evidence/01').glob('[0-9][0-9][0-9]/summary.json')}
        verdict = LC.judge(sorted(runs, key=lambda r: (r['role'], r['q'], r['k'])), originals)
        assert verdict['admitted'] and verdict['reason'] == row['reason']
        assert all(r['strict'] == 1 for r in runs) and len(runs) == 2 * (len(row['support']) + len(row['keep']))
    # the admitted revisions are served from block 2 on, and only there; the harness revision from its own code root
    for arm in LEARNER_ARMS:
        root, state = group['roots'][arm], group['states'][arm]
        candidate = root / 'evolution/01/adapter'
        assert state['blocks']['1']['adapter']['path'].endswith('adapters/' + arms.W0_NAMES[group['specs'][arm].arm.seed])
        assert state['blocks']['2']['adapter']['path'] == str(candidate)
        assert state['blocks']['2']['adapter_revision'] == arms.revision(candidate)
        served = {r['task_id']: r['notes_served'] for r in state['results']}
        assert served == {t: (['revised_figures'] if t == 'toy04' else []) for t in BLOCK1 + BLOCK2}
    root = group['roots']['harness-evolve']
    patched = root / 'evolution/01/adapter/tools/lookup.py'
    assert EL.HINT in patched.read_text() and EL.HINT not in (group['code'] / 'adapters/harness-w0/tools/lookup.py').read_text()
    admission = json.loads((root / 'evolution/01/sessions/learner/control/mechanical-admission.json').read_text())
    assert admission['admitted_revision'] == EL.REVISION_ID and admission['admitted_notes'] == ['revised_figures']
    assert admission['candidate_revision'] == arms.revision(root / 'evolution/01/adapter')
    assert admission['revision']['tier'] == 'additive' and admission['replay_seconds'] > 0
    for task in BLOCK2:
        episode = json.loads((run_dir(group, 'harness-evolve', task) / 'summary.json').read_text())
        code_root = Path(episode['code_root'])
        assert code_root.parent.name == arms.revision(root / 'evolution/01/adapter')
        assert (code_root / 'lookup.py').read_bytes() == patched.read_bytes()
    for task in BLOCK1:
        episode = json.loads((run_dir(group, 'harness-evolve', task) / 'summary.json').read_text())
        assert Path(episode['code_root']).parent.name == arms.revision(group['code'] / 'adapters/harness-w0')
    # the tool patch reached the model: the block-2 first-printing lookup (toy05) printed the hint line
    events = [json.loads(line) for line in (run_dir(group, 'harness-evolve', 'toy05') / 'records/events.jsonl').read_text().splitlines()
              if line.startswith('{')]
    outputs = [e['item']['aggregated_output'] for e in events if e.get('type') == 'item.completed'
               and e['item'].get('type') == 'command_execution' and 'widgets_2020_a' in e['item']['command']]
    assert outputs and all(EL.HINT in o for o in outputs)
    for arm in LEARNER_ARMS:
        result = group['rounds'][arm][0]
        assert result['status'] == 'admitted' and result['model'] == 'accounts/fireworks/models/glm-5p3-flash'
        assert result['reasoning_effort'] == 'high' and not result.get('error')


ARM_LABELS = ('prompt-static', 'prompt-evolve', 'harness-static', 'harness-evolve', 'harness-evolve-notes',
              'prompt-w0', 'harness-w0', 'prompt_learning', 'harness_learning', 'learner', 'adapters/')


@live
def test_model_visible_text_never_names_the_arm(group):
    fixed, bodies = set(), 0
    for arm in ARMS:
        for request, body in request_bodies(group['roots'][arm]):
            bodies += 1
            if body.get('tools'):
                fixed.add(json.dumps([body.get('instructions'), body.get('tools')], sort_keys=True))
            text = '\n'.join(strings(body['input']))
            found = [label for label in ARM_LABELS if label in text.lower()]
            found += ['plain'] if re.search(r'\bplain\b', text, re.I) else []
            assert not found, (request, found)
        for request, body in request_bodies(group['roots'][arm], scored_only=True):
            text = '\n'.join(strings(body['input'])).lower()
            assert not any(word in text for word in ('evolution/', 'revision')), request
        # verification bodies too (review fix): every path under the root that the model saw (the cwd of a
        # verification episode is runs/verify/BB/<token>/<NN>/workspace) names no arm, no proposal id and no
        # word of the ARM_REVEALING list; the bare fragments that are not English words never appear at all
        root = group['roots'][arm].resolve()
        ids = {row.get('id') for row in ledger_rows(group['roots'][arm]) if row.get('id')}
        seen_paths = 0
        for request, body in request_bodies(group['roots'][arm]):
            text = '\n'.join(strings(body['input']))
            for m in re.finditer(re.escape(str(root)) + r'[^\s"\'<>]*', text):
                rel = m.group(0)[len(str(root.parent)) + 1:]
                seen_paths += 1
                assert not arms.revealing_fragments(rel, group['specs'][arm].arm), (request, rel)
                assert not any(i in rel for i in ids) and 'evolution' not in rel and not re.search(r'q\d{3}', rel), (request, rel)
            assert not any(word in text.lower() for word in ('harness', 'evol', 'mwm', 'baseline', 'adapters/')), request
        assert seen_paths > 0
    assert len(fixed) == 1 and bodies == len(group['up'].bodies)   # Codex's own instructions/tools: arm-independent
    # no actor was granted the adapter, the evolution tree or anything else of a campaign root to read, except
    # the neutral code root under runs/code/ (harness arms: exactly the served revision's tool files)
    for arm in ARMS:
        root = group['roots'][arm].resolve()
        for invocation in root.rglob('records/invocation.json'):
            record = json.loads(invocation.read_text())
            granted = [*record['read_roots'], *record['data_roots']]
            assert record['adapter'] is None and not any(Path(p).resolve().is_relative_to(root) for p in granted)
            code = [*record['code_read']['files'], *record['code_read']['dirs']]
            assert bool(code) == (arm in CONTROLLER_ARMS), (arm, code)
            for p in code:
                assert Path(p).resolve().is_relative_to(root / 'runs/code'), p
            assert not any('/adapters' in p or 'items.py' in p or '/evolution/' in p for p in code + granted), code
    # the curious look-around of toy06 (weak_actor.PROBE) was denied in every arm
    for arm in ARMS:
        events = [json.loads(line) for line in (run_dir(group, arm, 'toy06') / 'records/events.jsonl').read_text().splitlines()
                  if line.startswith('{')]
        probe = [e['item'] for e in events if e.get('type') == 'item.completed'
                 and e['item'].get('type') == 'command_execution' and WA.PROBE in e['item']['command']]
        assert len(probe) == 1, arm
        output = probe[0]['aggregated_output']
        assert output.count('Operation not permitted') == 5, (arm, output)
        assert arm not in output and 'official_answer' not in output and '"answer"' not in output


@live
def test_seed_knowledge_reaches_the_model_as_decided(group):
    """Harness arms: AGENTS.md = COMMON + the harness W0 SEED; prompt-evolve: COMMON + the prompt W0 SEED; plain:
    COMMON only. The README of a harness arm carries the adapter's USAGE.md under '# Tools' and no submit
    section; both W0s satisfy the parity checklist and the text budget."""
    from packs.toy.pack import ToyPack
    pack = ToyPack()
    for arm in ARMS:
        first = json.loads((run_dir(group, arm, 'toy01') / 'api/001/request.json').read_text())
        agents = next(s for s in strings(first['input']) if s.startswith('# AGENTS.md instructions'))
        seed = arms.architecture(group['specs'][arm].arm)
        assert ('# Working advice' in agents) == (arm != 'plain'), arm
        assert (pack.seed_text_harness() in agents) == (seed == 'harness'), arm
        assert (pack.seed_text_prompt() in agents) == (seed == 'prompt'), arm
        assert '# Finishing a question' not in agents and '--alt' not in agents, arm
        assert '# OfficeQA task workspace' in agents          # COMMON (pack-specific wording; see the S8 report)
        readme = (run_dir(group, arm, 'toy01') / 'workspace/README.md').read_text()
        assert (pack.usage().rstrip() in readme) == (seed == 'harness') and '# Submitting your answer' not in readme
    checks = preflight.w0_checks('toy', group['code'] / 'adapters')
    assert checks['ok'] and set(checks['w0']) == {'text', 'harness'}
    assert checks['seed_parity']['items'] == [i['id'] for i in pack.SEED_KNOWLEDGE]
    visible = checks['model_visible']
    assert all(0 < v['total'] <= C.TEXT_BUDGET for v in visible.values())
    assert visible['harness']['usage'] > 0 == visible['text']['usage'] and visible['harness']['controller'] > 0
    print('\ntoy W0 model-visible bytes: ' + json.dumps({k: v['total'] for k, v in visible.items()}))


def unreleased_patterns():
    """Reference answers of every question never released to a learner (block 2 + the disputed one)."""
    hidden = BLOCK2 + list(WITHHELD)
    # a whole number token: not inside a hex digest or identifier, not a prefix of a longer decimal
    return {t: re.compile(r'(?<![0-9A-Za-z_.])' + re.escape(REFERENCES[t]) + r'(?![0-9A-Za-z_]|\.[0-9])') for t in hidden}


@live
def test_learner_visible_files_hold_no_unreleased_answer(group):
    patterns = unreleased_patterns()
    # positive control: the patterns find these values where they legitimately are (the sealed block-2 predictions
    # of an arm that answered them right, never shown to a learner)
    for task in ('toy04', 'toy06'):
        prediction = (run_dir(group, 'harness-evolve', task) / 'prediction.json').read_text()
        assert patterns[task].search(prediction), task
    for arm in LEARNER_ARMS:
        root = group['roots'][arm]
        visible = [p for p in (root / 'evidence').rglob('*') if p.is_file()]
        visible += [p for p in (root / 'evolution/01/sessions').rglob('*') if p.is_file()]
        visible += [root / 'evolution/01' / n for n in ('request.json', 'evidence-index.json', 'screen.json')]
        for path in visible:
            text = path.read_text()
            leaks = [t for t, pattern in patterns.items() if pattern.search(text)]
            assert not leaks, (path, leaks)
            if path.name != 'reference_answer.json':
                assert 'official_answer' not in text, path
        references = sorted(json.loads(p.read_text())['official_answer']
                            for p in (root / 'evidence').rglob('reference_answer.json'))
        assert references == sorted(REFERENCES[t] for t in BLOCK1 if t not in WITHHELD)
        # what the learner model itself was sent (every message of every call)
        model = group['models'][arm]
        sent = json.dumps([r['messages'] for r in model.requests], ensure_ascii=False)
        assert not [t for t, pattern in patterns.items() if pattern.search(sent)]
        assert 'reference: ' + REFERENCES['toy01'] in sent                 # released references are shown
        assert '1 further episodes are withheld' in sent


@live
def test_key_cost_report_and_timings(group):
    base = group['base']
    for path in base.rglob('*'):
        if path.is_file() and path.stat().st_size < 5_000_000 and path.suffix in ('.json', '.jsonl', '.md', '.html', '.log', '.txt'):
            assert KEY not in path.read_text(errors='replace'), path
    for arm in ARMS:
        groups = group['costs'][arm]['groups']
        scored = sum(r['requests'] for r in group['states'][arm]['results'])
        assert groups['actors_codex']['logical_calls'] == scored and groups['actors_controller']['logical_calls'] == 0
        assert (groups['verification_codex']['logical_calls'] > 0) == (arm in LEARNER_ARMS)
        assert group['costs'][arm]['verification']['logical_calls'] == groups['verification_codex']['logical_calls']
        assert groups['learners']['logical_calls'] == (len(group['models'][arm].requests) if arm in LEARNER_ARMS else 0)
        assert groups['aborted_actors']['logical_calls'] == groups['aborted_learners']['logical_calls'] == 0
        replay = group['costs'][arm]['replay_seconds']
        assert (replay['replays'] >= 1 and replay['jobs'] >= 1 and replay['wall_seconds'] > 0) == (arm == 'harness-evolve'), (arm, replay)
        assert not (group['roots'][arm] / 'report-error.json').exists()
        html = (group['roots'][arm] / 'report.html').read_text()
        assert KEY not in html and not any(QUESTION[t] in html for t in QUESTION)
        assert not any(REFERENCES[t] in html for t in ('toy01', 'toy03', 'toy05', 'toy10'))   # distinctive values
        assert 'Blocks (prequential' in html and ('gate off' in html) == (arm in CONTROLLER_ARMS)
        assert ('Admitted tool revisions' in html) == (arm == 'harness-evolve')
    timings = json.loads((base / 'timings.json').read_text())
    assert timings['campaign_seconds'] < 900 and timings['upstream_requests'] == len(group['up'].bodies)

# ---------------------------------------------------------------------------------------------
# A loop under gate false with the R6 hard block (DESIGN §3 gate-off loop templates)
# ---------------------------------------------------------------------------------------------

LOOP_ROOTS = {'harness-static': 'v10-u-a'}
STUBBORN = ['lookup widgets_2020_b.txt South', 'lookup widgets_2020_b.txt North', 'lookup widgets_2020_b.txt East']


class Stubborn:
    """The weak actor that ignores loop notes on toy02: three distinct lookups three times each (three loop
    notes = loop_notes_max), then the first one a fourth time (the hard block), then it goes on as usual."""

    def __call__(self, body, index):
        view = WA.parse(body)
        if body.get('tools') and view.item == 'toy02' and view.readme.get('tools'):
            plan = [STUBBORN[0]] * 3 + [STUBBORN[1]] * 3 + [STUBBORN[2]] * 3 + [STUBBORN[0]]
            done = [c for c in view.done if c.startswith('lookup')]
            if len(done) < len(plan):
                return fu.response([fu.call(plan[len(done)])], use=WA.usage())
        return WA.respond(body, index)


@live
def test_gate_off_loop_notes_then_hard_block(tmp_path_factory):
    g = make_group(tmp_path_factory.mktemp('u'), LOOP_ROOTS, responder=Stubborn())
    try:
        run_roots(g)
    finally:
        close_group(g)
    assert not g['errors'], g['errors']
    root, state = g['roots']['harness-static'], g['states']['harness-static']
    assert g['audits']['harness-static']['passed'], failed_checks(g, 'harness-static')[:8]
    base = run_dir(g, 'harness-static', 'toy02')
    counters = json.loads((base / 'summary.json').read_text())['controller']
    assert counters['loop_notes'] == 3 and counters['hard_blocks'] == 1 and counters['gate'] is False
    notes = [e for e in events_of(base / 'api') if isinstance(e, C.Note) and e.reason == 'loop_guard']
    assert len(notes) == 4
    for note in notes[:3]:
        assert 'issued this exact command 3 times' in note.text and CR.LOOP_OPTIONS_NOGATE in note.text
        assert 'submit' not in note.text.lower() and 'Current candidates' not in note.text
    assert notes[3].text.startswith(CR.HARD_BLOCK_PREFIX) and STUBBORN[0] in notes[3].text
    rows = [json.loads(line) for line in (base / 'api/controller.jsonl').read_text().splitlines()]
    decisions = [r['detail'] for r in rows if r.get('event') == 'decision']            # the broker wraps drained rows
    assert [d['event'] for d in decisions if d.get('event') in ('loop_note', 'hard_block')] == ['loop_note'] * 3 + ['hard_block']
    assert [d['n'] for d in decisions if d.get('event') == 'hard_block'] == [4] and all(d['gate'] is False for d in decisions if d.get('event') == 'loop_note')
    commands = [e for e in events_of(base / 'api') if isinstance(e, C.Command) and e.cmd.startswith('lookup')]
    assert len(commands) == 10 and {r['task_id']: r['answer'] for r in state['results']} == UNAIDED
    texts = [s for _, body in request_bodies(root) for s in strings(body['input'])]
    assert sum(s.startswith('[runtime:loop_guard] ' + CR.HARD_BLOCK_PREFIX) for s in texts) >= 1

# ---------------------------------------------------------------------------------------------
# Crash after a learning round, block-level resume
# ---------------------------------------------------------------------------------------------

RESUME_ROOTS = {'prompt-evolve': 'v10-s-a', 'harness-evolve': 'v10-s-b'}


@live
def test_crash_after_learning_aborts_the_group_and_resumes_block_level(tmp_path_factory):
    """A crash right after a learning round (its files written, the new adapter not yet adopted) stops that root
    and ABORTs the group; the peer stops too. After `clear_abort`, a block-level resume of both roots moves the
    unadopted round aside (runs-aborted/), restores the learner files from learner-backups/, re-runs the round
    (new verification actors through the real episode path) and completes; the audits pass."""
    import barrier as B
    g = make_group(tmp_path_factory.mktemp('h'), RESUME_ROOTS)
    barrier_dir = Path(g['specs']['harness-evolve'].barrier.dir)
    crashed, peer = g['roots']['harness-evolve'], g['roots']['prompt-evolve']
    try:
        run_roots(g, crash_after_learning=('harness-evolve',))
        assert g['errors']['harness-evolve'].startswith('SimulatedCrash'), g['errors']
        assert g['errors']['prompt-evolve'].startswith('BarrierAborted'), g['errors']
        state = json.loads((crashed / 'state.json').read_text())
        assert state['status'] == 'stopped_on_error' and state['active_learning_block'] == 1
        assert json.loads((peer / 'state.json').read_text())['status'] == 'aborted'
        abort = json.loads((barrier_dir / 'ABORT').read_text())
        assert 'v10-s-b' in abort['reason'] and 'simulated crash' in abort['reason']
        first = json.loads((crashed / 'evolution/01/result.json').read_text())
        assert first['status'] == 'admitted'                              # the round itself had finished
        rows_before = (crashed / 'learning-ledger.jsonl').read_text().splitlines()
        assert len(rows_before) == 2 and (crashed / 'learner-backups/01/backup.json').is_file()
        backup = json.loads((crashed / 'learner-backups/01/backup.json').read_text())
        assert backup == {'learner-state.json': None, 'learning-ledger.jsonl': None}    # nothing before round 1

        assert B.clear_abort(barrier_dir) is not None
        run_roots(g, resume=True)
        assert not g['errors'], g['errors']
    finally:
        close_group(g)
    for arm, root in (('harness-evolve', crashed), ('prompt-evolve', peer)):
        state = json.loads((root / 'state.json').read_text())
        assert state['status'] == 'completed' and len(state['attempts']) == 2
        assert {r['task_id']: r['answer'] for r in state['results']} == EXPECTED[arm]
        assert g['audits'][arm]['passed'], (arm, failed_checks(g, arm)[:8])
    state = json.loads((crashed / 'state.json').read_text())
    moved = state['attempts'][1]['moved']
    assert moved['block'] == 1 and moved['kept_episodes'] is True
    assert {'evidence/01', 'evolution/01', 'private-learning-seals/01.json'} <= set(moved['moved'])
    aside = crashed / moved['to']
    assert (aside / 'evolution/01/result.json').is_file() and (aside / 'learner-files/learning-ledger.jsonl').is_file()
    rows = [json.loads(line) for line in (crashed / 'learning-ledger.jsonl').read_text().splitlines()]
    assert [(r['kind'], r['id'], r['admitted']) for r in rows] == [('note', 'revised_figures', True),
                                                                  ('revision', EL.REVISION_ID, True)]
    # determinism across two real runs of the same round on the same sealed evidence: identical decisions
    first_rows = [json.loads(line) for line in (aside / 'learner-files/learning-ledger.jsonl').read_text().splitlines()]
    decision = lambda r: (r['kind'], r['id'], r['admitted'], r['reason'], r.get('support'), r.get('keep'), r.get('tier'))  # noqa: E731
    assert [decision(r) for r in first_rows] == [decision(r) for r in rows]
    assert len(g['rounds']['harness-evolve']) == 2 and state['adapter']['path'] == str(crashed / 'evolution/01/adapter')
    groups = g['costs']['harness-evolve']['groups']
    assert groups['aborted_learners']['logical_calls'] == groups['learners']['logical_calls'] > 0
    assert groups['aborted_actors']['logical_calls'] > 0 and groups['verification_codex']['logical_calls'] > 0
    # the re-run served the same revision content: both rounds staged the same patch, so the code roots agree
    assert arms.revision(crashed / 'evolution/01/adapter') == arms.revision(aside / 'evolution/01/adapter')

# ---------------------------------------------------------------------------------------------
# Upstream faults and an actor timeout
# ---------------------------------------------------------------------------------------------

ADVERSITY_ROOTS = {'plain': 'v10-t-a', 'harness-static': 'v10-t-b'}
ADVERSITY_SECONDS = 20


class Adversity:
    """The weak actor behind an unreliable upstream: an HTTP 500 on the first request of toy01 and a dropped
    stream (response.created, then the connection closes) on toy08's second request, each once per distinct
    body (the broker's identical-body re-send then succeeds); toy05's lookup request never answers (a stalled
    provider), so the actor is killed at its wall clock."""

    def __init__(self):
        self.seen, self.lock, self.injected = set(), threading.Lock(), []

    def __call__(self, body, index):
        view = WA.parse(body)
        if body.get('tools') and view.item == 'toy05' and view.done == ['cat README.md']:
            with self.lock:
                self.injected.append(('stall', view.item))
            return 'stall'
        fault = ('http500' if view.item == 'toy01' and not view.done else
                 'drop' if view.item == 'toy08' and view.done == ['cat README.md'] and not view.summary else None)
        if fault and body.get('tools'):
            key = json.dumps(body['input'], sort_keys=True)
            with self.lock:
                first = key not in self.seen
                self.seen.add(key)
                if first:
                    self.injected.append((fault, view.item))
            if first:
                return ('http', 500, {'error': {'message': 'simulated upstream 500'}}) if fault == 'http500' else 'drop'
        return WA.respond(body, index)


@live
def test_upstream_errors_and_an_actor_timeout_keep_records_consistent(tmp_path_factory):
    adversity = Adversity()
    g = make_group(tmp_path_factory.mktemp('t'), ADVERSITY_ROOTS, responder=adversity, seconds=ADVERSITY_SECONDS)
    try:
        run_roots(g)
        # the broker abandons the stalled provider call when its episode ends, so the call is fully recorded
        # before the episode summary, the seal, the audit and cost -- while the fake provider still holds the
        # stream open
        stalled_calls = {arm: sorted(run_dir(g, arm, 'toy05').glob('api/[0-9][0-9][0-9]'))[-1]
                         for arm in ADVERSITY_ROOTS}
        assert all((c / 'request.json').is_file() and (c / 'transport.json').is_file()
                   for c in stalled_calls.values())
        snapshot = {arm: {p: p.read_bytes() for p in sorted(run_dir(g, arm, 'toy05').rglob('*'))
                          if p.is_file() and 'api' in p.relative_to(run_dir(g, arm, 'toy05')).parts}
                    for arm in ADVERSITY_ROOTS}
        close_group(g, close_upstream=False)
    finally:
        g['up'].close()
    # the provider closing its stream afterwards changes no record of the episode
    time.sleep(1.0)
    for arm, files in snapshot.items():
        after = {p: p.read_bytes() for p in sorted(run_dir(g, arm, 'toy05').rglob('*'))
                 if p.is_file() and 'api' in p.relative_to(run_dir(g, arm, 'toy05')).parts}
        assert after == files, arm
    assert not g['errors'], g['errors']
    assert sorted(adversity.injected) == sorted([('http500', 'toy01'), ('http500', 'toy01'), ('drop', 'toy08'),
                                                 ('drop', 'toy08'), ('stall', 'toy05'), ('stall', 'toy05')])
    for arm in ADVERSITY_ROOTS:
        root, state = g['roots'][arm], g['states'][arm]
        assert g['audits'][arm]['passed'], (arm, failed_checks(g, arm)[:8])
        answers = {r['task_id']: (r['status'], r['answer']) for r in state['results']}
        expected = {t: ('completed', a) for t, a in EXPECTED[arm].items()}
        expected['toy05'] = ('timeout', '')                             # no answer fallback in the official metric
        assert answers == expected, arm
        first = json.loads((root / 'runs/episodes/001/api/001/transport.json').read_text())
        assert [a.get('http_status') for a in first['attempts']] == [500, 200]
        toy08 = run_dir(g, arm, 'toy08') / 'api'
        attempts = [len(json.loads(p.read_text())['attempts']) for p in sorted(toy08.glob('[0-9][0-9][0-9]/transport.json'))]
        assert attempts.count(2) == 1 and set(attempts) == {1, 2}, attempts
        stalled = run_dir(g, arm, 'toy05')
        summary = json.loads((stalled / 'summary.json').read_text())
        assert summary['status'] == 'timeout' and summary['transport_failed'] is False
        assert json.loads((stalled / 'prediction.json').read_text())['answer'] == ''
        calls = sorted(stalled.glob('api/[0-9][0-9][0-9]'))
        last = json.loads((calls[-1] / 'transport.json').read_text())
        assert [(a['dispatched'], a['status']) for a in last['attempts']] == [(True, 'transport_retry'),
                                                                              (False, 'not_dispatched')]
        assert last['attempts'][0]['usage_unknown'] is True and 'stopped' in last['attempts'][1]['error']
        assert summary['usage_complete'] is False and summary['usage_unknown_calls'] == [len(calls)]
        api_summary = json.loads((stalled / 'api/summary.json').read_text())
        assert api_summary['abandoned_calls'] == [len(calls)] and api_summary['in_flight_calls'] == []
        metrics = json.loads((root / 'private-evaluation/paired-metrics.json').read_text())
        assert metrics['arms'][arm]['empty_answers'] == 1
        groups = g['costs'][arm]['groups']
        assert groups['actors_codex']['wire_attempts'] >= groups['actors_codex']['logical_calls'] + 2
        # one unknown-usage call per injected fault (500, dropped stream, stall): never priced as zero (cost.py)
        assert groups['actors_codex']['unknown_usage_logical_calls'] == 3 and groups['actors_codex']['estimate_usd'] is None
        assert groups['actors_codex']['known_cost_lower_bound_usd'] > 0
    ledger = events_of(run_dir(g, 'harness-static', 'toy05') / 'api')
    assert ledger and not [e for e in ledger if isinstance(e, C.Candidate)]   # gate off: never a candidate

# ---------------------------------------------------------------------------------------------
# A learned tool revision changes a later answer through the tool output alone
# ---------------------------------------------------------------------------------------------

TOOL_ROOTS = {'harness-evolve': 'v10-v-a'}
TOOL_SEED = 'toy-e2e-rule-0'    # block 1 = toy03 toy10 toy05 toy02 toy07; block 2 = toy04 toy09 toy08 toy06 toy01


@live
def test_learned_tool_revision_is_admitted_served_and_changes_a_later_answer(tmp_path_factory):
    """The harness learner's core path end to end without any note: a patch to the served tool proposed from
    real campaign evidence (the first-printing lookup of toy07) passes the staging screens, the recorded-command
    replay (tier additive, replay-chosen controls) and the paired re-runs, is written into the next revision,
    served from block 2 through its own neutral code root, and its hint line alone makes the weak actor read the
    revised printing on toy01 (right now; toy06, which pins the first printing, unchanged); the audit finds the
    admission record and the served shas."""
    g = make_group(tmp_path_factory.mktemp('v'), TOOL_ROOTS, order_seed=TOOL_SEED, harness_learner={'note': False})
    try:
        run_roots(g)
    finally:
        close_group(g)
    assert not g['errors'], g['errors']
    root, state = g['roots']['harness-evolve'], g['states']['harness-evolve']
    order = [r['task_id'] for r in sorted(state['results'], key=lambda r: r['ordinal'])]
    assert order == ['toy03', 'toy10', 'toy05', 'toy02', 'toy07', 'toy04', 'toy09', 'toy08', 'toy06', 'toy01']
    rows = [json.loads(line) for line in (root / 'learning-ledger.jsonl').read_text().splitlines()]
    assert [(r['kind'], r['id'], r['admitted']) for r in rows] == [('revision', EL.REVISION_ID, True)]
    row = rows[0]
    assert row['support'] == [5] and sorted(row['keep']) == [2, 4] and row['tier'] == 'additive'
    assert row['files'] == [EL.PATCH_PATH] and row['patches'] == {EL.PATCH_PATH: 1} and row['expect'] == {'5': row['expect']['5']}
    assert row['replay']['status'] == 'ok' and row['replay']['counts']['changed_output'] >= 1 and row['replay']['counts']['regressed'] == 0
    admission = json.loads((root / 'evolution/01/sessions/learner/control/mechanical-admission.json').read_text())
    assert admission['admitted_revision'] == EL.REVISION_ID and admission['admitted_notes'] == []
    assert admission['revision']['tier'] == 'additive' and admission['revision']['note'] is None
    assert admission['candidate_revision'] == arms.revision(root / 'evolution/01/adapter') == state['blocks']['2']['adapter_revision']
    assert state['blocks']['2']['adapter']['path'] == str(root / 'evolution/01/adapter')
    assert not list((root / 'evolution/01/adapter/notes').glob('*.json'))
    answers = {r['task_id']: r['answer'] for r in state['results']}
    assert answers == {**UNAIDED, 'toy01': '252.00'}
    # block 2: toy01 read the first printing, saw the hint, then read the revised printing
    episode = run_dir(g, 'harness-evolve', 'toy01') if False else root / 'runs/episodes/010'
    events = [json.loads(line) for line in (episode / 'records/events.jsonl').read_text().splitlines() if line.startswith('{')]
    commands = [e['item'] for e in events if e.get('type') == 'item.completed' and e['item'].get('type') == 'command_execution']
    lookups = [c['command'] for c in commands if 'lookup widgets_2020' in c['command']]
    assert any('widgets_2020_a.txt' in c for c in lookups) and any('widgets_2020_b.txt' in c for c in lookups)
    assert any(EL.HINT in c['aggregated_output'] for c in commands if 'widgets_2020_a.txt' in c['command'])
    summary = json.loads((episode / 'summary.json').read_text())
    assert Path(summary['code_root']).parent.name == admission['candidate_revision']
    assert summary['controller']['observation_coverage']['mode'] == 'header'   # evolved tool: no parser re-run
    block1 = json.loads((root / 'runs/episodes/001/summary.json').read_text())
    assert block1['controller']['observation_coverage']['mode'] == 'rerun'
    audit = json.loads((root / 'preflight/final-audit.json').read_text())
    assert audit['passed'], audit['failed'][:8]
    names = {c['name'] for c in audit['checks']}
    assert 'round 1 served tool revision admitted by paired re-runs (ledger row)' in names
    assert "round 1 served tool files are the admitted revision's staged files" in names
    assert 'round 1 admission record names the served candidate revision' in names
    assert 'round 1 served tools pass check_adapter' in names
    assert any(n.endswith('every granted code file sha equals the served revision tools/') for n in names)

# ---------------------------------------------------------------------------------------------
# Fast regression tests (default tier; no Codex, no network)
# ---------------------------------------------------------------------------------------------


def test_toy_lookup_runs_as_a_kit_script():
    """The kit runs pack CLIs as plain scripts (`python -I -B <script>`); the toy CLI used a package-relative
    import only, so `lookup` failed under the kit (fix: sibling-import fallback, as packs/officeqa/tools/cli.py)."""
    from packs.toy import pack as toy_pack
    script = toy_pack.TOOLS_SCRIPT
    done = subprocess.run([sys.executable, '-I', '-B', str(script), 'widgets_2020_b.txt', 'North'],
                          capture_output=True, text=True, timeout=60, cwd='/')
    assert done.returncode == 0, done.stderr
    assert '⟦toy widgets_2020_b.txt:5|Q2 2020|North|131.5r⟧' in done.stdout
    # the kit grants exactly the script and its sibling import (never items.py, which holds the toy references)
    assert [p.name for p in kit._import_closure(script)] == ['corpus.py', 'tools.py']


def toy_harness_arm():
    return arms.parse_arm('harness-static', pack='toy')


def build_toy_root(tmp_path: Path, name: str = 'v10-x-a'):
    """A toy root with its W0 adapters in a private code copy, and the served harness W0 staged to its neutral
    code root through the campaign (stage_code_root / adapter_tool_wrappers)."""
    code = TC.code_copy(tmp_path / 'code')
    spec = arms.parse_config({'profile': arms.PROFILE, 'arm': 'harness-static', 'repeat': 1, 'pack': 'toy',
                              'order_seed': 'x'}, root_name=name)
    root = TC.make_root(tmp_path, name, spec)
    lay = CP.Layout(root=root, code=code)
    adapter = arms.w0_dir(spec.arm, lay.adapters)
    code_root, sha12 = CP.stage_code_root(lay, adapter)
    return code, root, lay, spec, adapter, code_root, sha12


def test_toy_w0_tools_are_served_from_a_neutral_code_root(tmp_path):
    """The toy harness W0 (arms.py build --pack toy) serves lookup + calc from <root>/runs/code/<sha12>/tools:
    the kit wrappers embed that path, the staged copy equals the adapter's tools/, the wrapper runs, and the
    audit's code-root checks pass on the marker; a workspace prepared for the prompt arm has no tools."""
    code, root, lay, spec, adapter, code_root, sha12 = build_toy_root(tmp_path)
    assert code_root == root / 'runs/code' / sha12 / 'tools' and sha12 == arms.revision(adapter)
    assert not arms.revealing_fragments(str(code_root.relative_to(root.parent)), spec.arm)
    assert CP.tools_snapshot(code_root) == CP.tools_snapshot(adapter / 'tools')
    assert sorted(p.name for p in code_root.iterdir()) == ['USAGE.md', 'calc.py', 'corpus.py', 'lookup.py', 'manifest.json']
    wrappers = CP.adapter_tool_wrappers(adapter, code_root)
    assert wrappers == {'calc': [str(code_root / 'calc.py')], 'lookup': [str(code_root / 'lookup.py')]}
    task = {'task_id': 'toy01', 'question': QUESTION['toy01']}
    prep = CP.prepare_episode(lay, spec.arm, task, adapter, root / 'runs/episodes/001', ordinal=1)
    workspace = Path(prep['workspace'])
    marker = json.loads((workspace / '.acting-prepared.json').read_text())
    assert prep['code_root'] == str(code_root) and prep['code_sha12'] == sha12 == prep['adapter_revision']
    assert marker['kit']['code_roots'] == [str(code_root)] and 'submit' not in marker['kit']['tools']
    assert set(marker['kit']['code']['files']) == {str(code_root / n) for n in ('lookup.py', 'corpus.py', 'calc.py')}
    findings = full_audit.code_root_findings(marker, adapter, root, spec.arm)
    assert findings and all(passed for _, passed, _ in findings), [f for f in findings if not f[1]]
    readme = (workspace / 'README.md').read_text()
    submit = full_audit.submit_findings(marker, readme, {'controller': {'submits': 0}}, root / 'runs/episodes/001/api', spec.arm)
    assert all(passed for _, passed, _ in submit), submit
    assert CP.TOOLS_HEADING + '\n' in readme and 'lookup <file>' in readme and '# Submitting your answer' not in readme
    env = kit.tool_environment(workspace, spec.arm)
    done = subprocess.run([str(workspace / '.kit/bin/lookup'), 'widgets_2020_b.txt', 'North'], env=env,
                          capture_output=True, text=True, timeout=60, cwd=workspace)
    assert done.returncode == 0 and '⟦toy widgets_2020_b.txt:5|Q2 2020|North|131.5r⟧' in done.stdout, done.stderr
    # the same code root is reused (never re-staged) and a tampered copy is refused
    assert CP.stage_code_root(lay, adapter) == (code_root, sha12)
    prompt_spec = arms.parse_config({'profile': arms.PROFILE, 'arm': 'prompt-static', 'repeat': 1, 'pack': 'toy',
                                     'order_seed': 'x'}, root_name='v10-x-a')
    prep2 = CP.prepare_episode(lay, prompt_spec.arm, task, arms.w0_dir(prompt_spec.arm, lay.adapters),
                               root / 'runs/episodes/002', ordinal=2)
    marker2 = json.loads((Path(prep2['workspace']) / '.acting-prepared.json').read_text())
    assert prep2['code_root'] is None and marker2['kit']['code_roots'] == [] and marker2['kit']['tools'] == {}
    findings2 = full_audit.code_root_findings(marker2, arms.w0_dir(prompt_spec.arm, lay.adapters), root, prompt_spec.arm)
    assert [(n, p) for n, p, _ in findings2] == [('no code root, code files or tool wrappers without adapter tools', True)]


def test_replay_environment_matches_the_actor_kit(tmp_path):
    """review fix: a tool that branches on the environment (HOME), on __file__ (runs/code) or on the cwd name
    prints the same in the learners' replay (campaign.tool_runner over a neutral copy, actor-like env) and in
    the actor kit, so the replay screen cannot be bypassed by environment-conditional tool code."""
    code, root, lay, spec, adapter, code_root, sha12 = build_toy_root(tmp_path)
    candidate = arms.copy_adapter(adapter, root / 'evolution/01/candidate-adapter')
    lookup = candidate / 'tools/lookup.py'
    probe = ("import os as _os\nif 'HOME' in _os.environ and 'runs/code' in __file__ and _os.getcwd().endswith('workspace') "
             "and sorted(_os.environ)[:3] == ['HOME', 'LANG', 'MPLCONFIGDIR']:\n    print('extra')\nelse:\n    print('plain-run')\n")
    lookup.write_text(lookup.read_text().replace("TAG_OPEN = '⟦toy '\n", "TAG_OPEN = '⟦toy '\n" + probe, 1))
    info = json.loads((candidate / 'adapter.json').read_text())
    info['revision'] = arms.revision(candidate)
    (candidate / 'adapter.json').write_text(json.dumps(info, indent=2, sort_keys=True) + '\n')
    spec_evolve = arms.parse_config({'profile': arms.PROFILE, 'arm': 'harness-evolve', 'repeat': 1, 'pack': 'toy',
                                     'order_seed': 'x'}, root_name='v10-x-a')
    campaign_ = CP.Campaign('k', spec_evolve, lay)
    runner = campaign_.tool_runner(1)(candidate / 'tools')
    replay = runner.run(['lookup.py', 'widgets_2020_b.txt', 'North'])
    assert replay['exit'] == 0, replay
    task = {'task_id': 'toy01', 'question': QUESTION['toy01']}
    prep = CP.prepare_episode(lay, spec_evolve.arm, task, candidate, root / 'runs/episodes/001', ordinal=1)
    workspace = Path(prep['workspace'])
    env = kit.tool_environment(workspace, spec_evolve.arm)
    actor = subprocess.run([str(workspace / '.kit/bin/lookup'), 'widgets_2020_b.txt', 'North'], env=env,
                           capture_output=True, text=True, timeout=60, cwd=workspace)
    assert actor.returncode == 0, actor.stderr
    assert actor.stdout == replay['stdout'] and actor.stdout.startswith('extra\n'), (actor.stdout, replay['stdout'])
    assert Path(runner.tools_dir).is_relative_to(lay.code_roots) and 'evolution' not in str(runner.tools_dir)


def test_audit_code_root_and_submit_findings_detect_violations(tmp_path):
    """The audit refuses a code root that names the adapter, a script outside it, a changed granted file, a
    submit wrapper or README section on a gate-off arm, and gate counters that are not zero."""
    code, root, lay, spec, adapter, code_root, sha12 = build_toy_root(tmp_path)
    task = {'task_id': 'toy01', 'question': QUESTION['toy01']}
    prep = CP.prepare_episode(lay, spec.arm, task, adapter, root / 'runs/episodes/001', ordinal=1)
    marker = json.loads((Path(prep['workspace']) / '.acting-prepared.json').read_text())

    def failed(m, adapter_dir=adapter, arm=spec.arm):
        return {n for n, p, _ in full_audit.code_root_findings(m, adapter_dir, root, arm) if not p}
    assert failed(marker) == set()
    revealing = json.loads(json.dumps(marker))
    bad_root = str(adapter / 'tools')                       # served straight from adapters/harness-w0
    revealing['kit']['code_roots'] = [bad_root]
    assert {'code root under <root>/runs/code/<revision>/tools', 'code root path names no arm (model-visible)',
            'every kit tool script under the code root'} <= failed(revealing)
    tampered = json.loads(json.dumps(marker))
    path = next(p for p in tampered['kit']['code']['files'] if p.endswith('lookup.py'))
    tampered['kit']['code']['files'][path] = '0' * 64
    assert failed(tampered) == {'every granted code file sha equals the served revision tools/'}
    elsewhere = json.loads(json.dumps(marker))
    elsewhere['kit']['code']['files'] = {str(code / 'packs/toy/tools.py'): kit._sha_file(code / 'packs/toy/tools.py')}
    assert 'every granted code file lives in the code root' in failed(elsewhere)
    renamed = json.loads(json.dumps(marker))
    renamed['kit']['code_roots'] = [str(root / 'runs/code/000000000000/tools')]
    renamed['kit']['tools'] = {k: {**v, 'script': v['script'].replace(sha12, '000000000000')} for k, v in renamed['kit']['tools'].items()}
    renamed['kit']['code']['files'] = {k.replace(sha12, '000000000000'): v for k, v in renamed['kit']['code']['files'].items()}
    renamed['extra'] = {**renamed['extra'], 'code_sha12': '000000000000', 'code_root': renamed['kit']['code_roots'][0]}
    # a code root that does not exist also fails the grant re-derivation (review fix: grants are recomputed)
    assert failed(renamed) == {'code root revision segment is arms.revision(adapter)',
                               'granted code files are exactly the import closure of the manifest scripts'}
    # submit iff gate
    readme = (Path(prep['workspace']) / 'README.md').read_text()
    api = root / 'runs/episodes/001/api'
    ok = full_audit.submit_findings(marker, readme, {'controller': {'submits': 0, 'gate': False, 'submit_enabled': False}}, api, spec.arm)
    assert all(p for _, p, _ in ok)
    with_submit = json.loads(json.dumps(marker))
    with_submit['kit']['submit'] = {'script': 'x'}
    with_submit['kit']['files']['bin/submit'] = {'type': 'file'}
    names = {n for n, p, _ in full_audit.submit_findings(with_submit, readme + CP.SUBMIT_README,
                                                         {'controller': {'submits': 2, 'gate': False}}, api, spec.arm) if not p}
    assert names == {'kit submit wrapper iff gate', 'README sections match the arm (tools iff l1_tools, submit iff gate, conventions)',
                     'gate off: no submits, gate rounds, re-queries, check files or submit route'}


def test_preflight_tool_runner_smoke_and_w0_checks_on_the_toy_pack(tmp_path):
    """Every toy W0 manifest tool but the pinned calc runs once through the sandboxed ToolRunner (lookup --help);
    both toy W0s pass check_w0 with the parity numbers; the idempotence check runs the learner's own staged-file
    screen (calc.py skipped: pinned) plus any registered extra screen, and fails on a W0 the screen would refuse."""
    if NO_SANDBOX:
        pytest.skip('sandbox-exec missing')
    code, root, lay, spec, adapter, code_root, sha12 = build_toy_root(tmp_path)
    smoke = preflight.tool_runner_smoke('toy', lay, adapters=lay.adapters, tmp_root=tmp_path / 'tool-tmp')
    assert smoke['passed'] and smoke['runner'] == 'ToolRunner', smoke
    assert set(smoke['tools']) == {'lookup'} and all(t['passed'] and t['exit_code'] == 0 for t in smoke['tools'].values())
    assert set(smoke['skipped']) == {'calc'} and 'pinned' in smoke['skipped']['calc']
    assert str(lay.corpus.resolve()) in smoke['read_roots'] and not any('prepared/private' in r for r in smoke['read_roots'])
    checks = preflight.w0_checks('toy', lay.adapters)
    assert checks['ok'] and checks['seed_parity']['bytes']['harness'] > 0 and checks['model_visible']['harness']['usage'] > 0
    # the learner's own staged-file screen (source = candidate = W0) always runs; calc.py is skipped (pinned)
    result = preflight.w0_idempotence('toy', lay.adapters)
    assert result['status'] == 'passed' and result['screens'] == ['screen_staged'] and result['findings'] == {}, result
    assert set(result['files']) == {'tools/USAGE.md', 'tools/corpus.py', 'tools/lookup.py', 'tools/manifest.json'}
    assert set(result['skipped']) == {'tools/calc.py'} and 'pinned' in result['skipped']['tools/calc.py']
    seen = []
    preflight.W0_SCREENS.append(('record', lambda rel, text: seen.append(rel) or []))
    try:
        result = preflight.w0_idempotence('toy', lay.adapters)
        assert result['status'] == 'passed' and sorted(seen) == sorted(result['files'])
        assert result['screens'] == ['screen_staged', 'record']
        preflight.W0_SCREENS.append(('strict', lambda rel, text: ['finding'] if rel.endswith('lookup.py') else []))
        result = preflight.w0_idempotence('toy', lay.adapters)
        assert result['status'] == 'failed' and result['findings'] == {'strict:tools/lookup.py': ['finding']}
    finally:
        preflight.W0_SCREENS.clear()
    # a W0 whose tool would be refused by the learner screen (a banned builtin call) fails the check
    broken = arms.copy_adapter(adapter, tmp_path / 'broken-adapters' / adapter.name)
    lookup = broken / 'tools/lookup.py'
    lookup.write_text(lookup.read_text() + '\nx = eval("1")\n')
    result = preflight.w0_idempotence('toy', tmp_path / 'broken-adapters')
    assert result['status'] == 'failed' and list(result['findings']) == ['screen_staged:tools/lookup.py']
    assert 'eval()' in result['findings']['screen_staged:tools/lookup.py'][0]
    # a read root inside a private tree (here a corpus placed under evidence/) is refused before any runner is
    # built; the tools dir and tmp root are the only exempt roots (a candidate lives under evolution/BB/)
    import types
    private_corpus = root / 'evidence/01/corpus'
    private_corpus.mkdir(parents=True)
    fake = types.SimpleNamespace(root=root, corpus=private_corpus, tables_index=lay.tables_index, tables_dir=lay.tables_dir)
    with pytest.raises(ValueError, match='private tree'):
        preflight.tool_runner(adapter / 'tools', fake, pack='toy', tmp_root=tmp_path / 't2')


RECORDED = [   # (command as Codex records it, the manifest-tool invocations preflight must rebuild from it)
    ("/bin/zsh -lc 'lookup widgets_2020_b.txt North'", [('lookup', ['widgets_2020_b.txt', 'North'])]),
    ('/bin/zsh -lc "lookup widgets_2020_a.txt North South | head -n 3; echo done"', [('lookup', ['widgets_2020_a.txt', 'North', 'South'])]),
    ("/bin/zsh -lc 'lookup widgets_2021_a.txt --table W-2 2>&1 && lookup widgets_2021_a.txt West > out.txt'",
     [('lookup', ['widgets_2021_a.txt', '--table', 'W-2']), ('lookup', ['widgets_2021_a.txt', 'West'])]),
    ("/bin/zsh -lc 'for r in North South; do lookup widgets_2020_b.txt $r; done'", []),                # unexpanded variable
    ("/bin/zsh -lc 'calc -c \"print(1+1)\"'", []),                                                    # calc: pinned, skipped
    ("/bin/zsh -lc 'python3 lookup.py widgets_2020_b.txt East'", [('lookup', ['widgets_2020_b.txt', 'East'])]),
    ("/bin/zsh -lc 'cat README.md'", []),
    ("/bin/zsh -lc 'lookup nosuchfile.txt'", [('lookup', ['nosuchfile.txt'])]),
    ("/bin/zsh -lc 'lookup widgets_2020_b.txt North'", [('lookup', ['widgets_2020_b.txt', 'North'])]),   # duplicate: one job
]


def test_preflight_replay_rebuilds_recorded_commands_and_sees_no_change(tmp_path):
    """The optional replay: commands rebuilt from a root's records/events.jsonl (zsh -lc wrappers, pipelines,
    redirections, loops and calc handled), each run twice through the ToolRunner over the unchanged W0 tools:
    0 changed outputs; the pass rule needs `min_jobs` rebuilt invocations."""
    if NO_SANDBOX:
        pytest.skip('sandbox-exec missing')
    code, root, lay, spec, adapter, code_root, sha12 = build_toy_root(tmp_path)
    import learner_common as LC
    manifest = arms.load_manifest(adapter / 'tools')
    # one definition of "which manifest tools did this command run": the learners' replay and preflight share it
    for raw, expected in RECORDED:
        found = LC.tool_invocations(raw, manifest)
        assert [f for f in found if f[0] != 'calc'] == expected, raw
    assert not hasattr(preflight, 'tool_invocation') and not hasattr(preflight, 'segments')
    episode = root / 'runs/episodes/001/records'
    episode.mkdir(parents=True)
    lines = [json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution', 'command': raw, 'exit_code': 0}})
             for raw, _ in RECORDED]
    (episode / 'events.jsonl').write_text('not json\n' + '\n'.join(lines) + '\n')
    commands = preflight.recorded_commands(root)
    assert len(commands) == len(RECORDED) and {c['source'] for c in commands} == {'events'}
    jobs = preflight.replay_jobs(commands, manifest)
    expected_jobs = []
    for _, expected in RECORDED:
        expected_jobs += [x for x in expected if x not in expected_jobs]          # de-duplicated on (tool, argv)
    assert [(j['tool'], j['argv']) for j in jobs] == expected_jobs and len(jobs) == 6
    assert preflight.replay_jobs(commands, manifest, limit=2) == jobs[-2:]
    runner = preflight.tool_runner(adapter / 'tools', lay, pack='toy', tmp_root=tmp_path / 'replay-tmp')
    result = preflight.replay(jobs, runner, manifest)
    assert result['jobs'] == 6 and result['changed'] == 0 and result['timed_out'] == 0 and result['ok'] == 6
    assert result['per_tool'] == {'lookup': {'jobs': 6, 'ok': 6, 'changed': 0, 'timed_out': 0}}
    # a candidate whose tool prints an extra line is seen as a change on every job that printed cells
    candidate = tmp_path / 'candidate' / 'tools'
    candidate.mkdir(parents=True)
    for p in (adapter / 'tools').iterdir():
        (candidate / p.name).write_bytes(p.read_bytes())
    (candidate / 'lookup.py').write_text((candidate / 'lookup.py').read_text().replace(EL.PATCH_OLD, EL.PATCH_NEW))
    changed = preflight.replay(jobs, runner, manifest, candidate=preflight.tool_runner(candidate, lay, pack='toy', tmp_root=tmp_path / 'c-tmp'))
    assert changed['changed'] == 1 and changed['ok'] == 6                  # the one widgets_2020_a lookup
    record = preflight.replay_v9a(root, lay=lay, pack='toy', adapters=lay.adapters, min_jobs=6, out=tmp_path / 'replay.json',
                                  tmp_root=tmp_path / 'v-tmp')
    assert record['passed'] and record['rebuilt'] == 6 and record['changed'] == 0 and record['sources'] == ['events']
    assert json.loads((tmp_path / 'replay.json').read_text())['rebuilt'] == 6
    assert not preflight.replay_v9a(root, lay=lay, pack='toy', adapters=lay.adapters, min_jobs=7, tmp_root=tmp_path / 'w-tmp')['passed']
    # a ledger (controller arm) takes precedence over events.jsonl: command events with raw_cmd, folded by ref
    api = root / 'runs/episodes/001/api'
    api.mkdir()
    (api / 'ledger.jsonl').write_text(json.dumps({'kind': 'command', 'ref': 'x1', 'raw_cmd': RECORDED[0][0]}) + '\n'
                                      + json.dumps({'kind': 'command', 'ref': 'x1', 'raw_cmd': RECORDED[0][0], 'exit_code': 0}) + '\n')
    commands = preflight.recorded_commands(root)
    assert [(c['source'], c['ref'], c['exit_code']) for c in commands] == [('ledger', 'x1', 0)]


def test_report_and_cost_render_v10_records_without_answers(tmp_path):
    """The report shows the prequential block table, the gate-off controller line with the observation coverage
    average, the learning rounds and the admitted revisions (from mechanical-admission.json), and never a
    question or an answer; cost groups replay time separately."""
    root = tmp_path
    coverage = {'mode': 'rerun', 'revision': 'w0', 'commands': 4, 'invocation': 3, 'header': 0, 'raw': 1, 'none': 0}
    results = [{'ordinal': i, 'task_id': f'toy{i:02}', 'status': 'completed', 'answer': REFERENCES[f'toy{i:02}'],
                'controller': {'inserts_by_reason': {'loop_guard': 1 if i == 2 else 0}, 'hard_blocks': 0, 'compactions': 0,
                               'controller_errors': 0, 'observation_coverage': coverage}, 'observation_coverage': coverage,
                'requests': 3} for i in range(1, 8)]
    state = {'status': 'running', 'arm': asdict(arms.parse_arm('harness-evolve', pack='toy')), 'question_count': 10, 'repeat': 1,
             'blocks': {'1': {'status': 'done', 'ordinals': [1, 2, 3, 4, 5], 'adapter_revision': 'w0'},
                        '2': {'status': 'running', 'ordinals': [6, 7, 8, 9, 10], 'adapter_revision': 'abc123def456'}},
             'results': results, 'learning': [{'block': 1, 'status': 'admitted', 'activated': True, 'admitted': [EL.REVISION_ID],
                                               'check': {'notes': 0}, 'retired': []}], 'warnings': []}
    (root / 'state.json').write_text(json.dumps(state))
    (root / 'private-evaluation').mkdir()
    (root / 'private-evaluation/paired-metrics.json').write_text(json.dumps({
        'denominator': 7, 'results': [{'task_id': f'toy{i:02}', 'strict': 1 if i != 3 else 0} for i in range(1, 8)]}))
    control = root / 'evolution/01/sessions/learner/control'
    control.mkdir(parents=True)
    (control / 'mechanical-admission.json').write_text(json.dumps({
        'admitted_revision': EL.REVISION_ID, 'candidate_revision': 'abc123def456', 'replay_seconds': 3.5, 'try_tool_calls': 2,
        'screens': [{'kind': 'revision', 'id': EL.REVISION_ID, 'ok': True, 'replay_status': 'ok',
                     'replay_counts': {'jobs': 9, 'changed_output': 2}, 'replay_seconds': 3.5}],
        'revision': {'id': EL.REVISION_ID, 'kind': 'revision', 'files': [EL.PATCH_PATH], 'patches': {EL.PATCH_PATH: 1},
                     'tier': 'additive', 'admitted': True, 'note': None,
                     'replay': {'status': 'ok', 'counts': {'jobs': 9, 'changed_output': 2}, 'wall_seconds': 3.5}}}))
    html = report.render(root)
    assert 'Blocks (prequential' in html and '<td>4</td>' in html and '4/5' in html and 'gate off' in html
    assert 'invocation 75%' in html and 'raw 25%' in html and 'parser modes' in html and 'rerun' in html
    assert 'Admitted tool revisions' in html and EL.REVISION_ID in html and 'additive' in html and '9 / 2' in html
    assert not any(QUESTION[t] in html for t in QUESTION) and not any(REFERENCES[t] in html for t in ('toy01', 'toy03', 'toy10'))
    blocks = report.block_table(state, json.loads((root / 'private-evaluation/paired-metrics.json').read_text()))
    assert [(b['block'], b['completed'], b['right'], b['cumulative']) for b in blocks] == [(1, 5, 4, (4, 5)), (2, 2, 2, (6, 7))]
    replay = cost.estimate(root)['replay_seconds']
    assert (replay['replays'], replay['jobs'], replay['changed_outputs'], replay['wall_seconds'], replay['try_tool_calls']) == (1, 9, 2, 3.5, 2)
    assert cost.self_test()['passed']


def test_run_roots_creates_the_five_neutral_study_roots(tmp_path):
    """run.py roots ($0): five copies of the plumbing named <prefix>-a..e with the DESIGN §7 arms, the first-50
    subset, temperature 0 and no top_p, one barrier; every .task-paths.json parses; a second call is refused."""
    parent = tmp_path / 'parent'
    framework, data_cache = parent / 'frameworks-v8/next-x', parent / 'baseline-work/data-cache/officeqa-official'
    framework.mkdir(parents=True)
    data_cache.mkdir(parents=True)
    with pytest.raises(ValueError, match='directory missing'):
        RUN.study_roots(parent, framework=framework, data_cache=parent / 'nope', source=ROOT)
    # the tables index every root installs: recorded in .task-paths.json, must exist and carry the tools' version
    with pytest.raises(ValueError, match='tables_index file missing'):
        RUN.study_roots(parent, framework=framework, data_cache=data_cache, source=ROOT)
    corpus = parent / 'corpus'
    corpus.mkdir()
    (corpus / 'treasury_bulletin_2000_01.txt').write_text('Table T-1. - Synthetic\n\n| Fiscal year | Amount |\n|---|---|\n| 1999 | 1 |\n')
    from packs.officeqa.tools import otables
    tables_index = parent / RUN.STUDY_TABLES_INDEX
    tables_index.parent.mkdir(parents=True)
    otables.build_index(corpus, tables_index)
    older = parent / 'older.sqlite'
    shutil.copyfile(tables_index, older)
    older.chmod(0o644)
    with sqlite3.connect(older) as connection:
        connection.execute("UPDATE meta SET value = ? WHERE key = 'version'", (json.dumps('otables-index-v9.1'),))
    with pytest.raises(ValueError, match='version'):
        RUN.study_roots(parent, framework=framework, data_cache=data_cache, tables_index=older, source=ROOT)
    created = RUN.study_roots(parent, framework=framework, data_cache=data_cache, source=ROOT)
    names = [f'v10-r1-{x}' for x in 'abcde']
    assert list(created['created']) == names and created['paid_calls'] == 0
    assert created['tables_index'] == str(tables_index) and created['tables_index_version'] == otables.INDEX_VERSION
    assert [v['arm'] for v in created['created'].values()] == [arm for _, arm in RUN.STUDY_ARMS]
    subset = RUN.load_subset(ROOT / RUN.SUBSET_FILE)
    assert created['subset'] == {'name': subset.name, 'sha256': subset.sha256, 'count': len(subset.task_ids)} and len(subset.task_ids) == 50
    assert subset.sha256.startswith('c95f9613') and created['order_seed'] == RUN.STUDY_ORDER_SEED
    for name in names:
        config = json.loads((parent / name / '.task-paths.json').read_text())
        spec = arms.parse_config(config, root_name=name)
        assert spec.subset.sha256 == subset.sha256 and spec.temperature == 0 and spec.top_p is None
        assert spec.barrier.roots == tuple(names) and spec.barrier.dir == parent / 'v10-r1-barrier'
        assert config['tables_index'] == str(tables_index)
        assert config['profile'] == 'v10' and (parent / name / 'adapters/harness-w0/tools/manifest.json').is_file()
        assert not (parent / name / 'prepared').exists() and not (parent / name / 'preflight').exists()
        assert not arms.revealing_fragments(name, spec.arm)
        # the pre-registration: one regular PREREG.md per root (the plumbing's PREREG.md is a symlink to PREREG-v10.md,
        # which is not copied), hashed identically by run.py, preflight and the freeze
        if (ROOT / 'PREREG.md').is_file():
            prereg = parent / name / 'PREREG.md'
            assert prereg.is_file() and not prereg.is_symlink() and CP.sha(prereg) == created["prereg_sha256"] == CP.sha(ROOT / "PREREG.md")
            assert not (parent / name / 'PREREG-v10.md').exists()
            assert prereg.read_text(encoding='utf-8').lstrip().startswith('# v10')
    with pytest.raises(ValueError, match='created once'):
        RUN.study_roots(parent, framework=framework, data_cache=data_cache, source=ROOT)
    other = RUN.study_configs(parent, prefix='v10-r2', subset=None, temperature=1.0, top_p=0.95, repeat=2,
                              framework=framework, data_cache=data_cache, tables_index=tables_index,
                              arm_names=['plain', 'harness-static'])
    assert list(other) == ['v10-r2-a', 'v10-r2-b'] and all('subset' not in c and c['top_p'] == 0.95 and c['repeat'] == 2
                                                         for c in other.values())
    with pytest.raises(ValueError, match='retired|unknown arm'):
        RUN.study_configs(parent, framework=framework, data_cache=data_cache, tables_index=tables_index,
                          arm_names=['mech-static'])


def test_smoke_question_and_counters_key_on_the_gate():
    """run.smoke asks for `submit` and requires a submission only when the arm's gate is on (DESIGN §3); every
    standard arm has the gate off this round."""
    assert not any(arm.gate for arm in C.STANDARD_ARMS.values())
    source = (ROOT / 'run.py').read_text()
    assert 'if arm.gate:' in source and 'submit --answer' in source and "if arm.controller:\n            question" not in source
    assert 'counters.get(\'submit_enabled\') is not True' in source
