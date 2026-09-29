"""S8: a $0 end-to-end rehearsal of the whole v9 pipeline on the toy pack.

One repeat group of five roots (plain, text-static, text-evolve, mech-static, mech-evolve), each a real
`campaign.Campaign` over the toy pack (10 questions, 2 blocks of 5; one question declared disputed), run
concurrently behind one file barrier. Every episode (scored and verification) is the production path:
`campaign.prepare_episode` (kit, README, AGENTS.md with <= 3 notes) -> `campaign.execute_episode` -> the real
broker (+ Ledger/Gate/SandboxRunner/MechController on the controller arms) -> the pinned real Codex binary under
the v9 Seatbelt profile. The broker's upstream is tests/fake_upstream.py on 127.0.0.1 answering with the scripted
weak actor (tests/e2e/weak_actor.py); the learners are the real S5 rounds (text_learning / mechanism_learning)
driven by scripted learner models (tests/e2e/learners.py), with paired re-run verification through the
campaign's actor runner. The toy pack's grader stands in for reward.py. No network beyond 127.0.0.1, no API key
(a dummy string), no paid call.

After the campaign: full_audit on every root, cost, report, and the rehearsal invariants (controller records,
no v9 fields on text arms, deterministic learner outcomes, arm-neutral model-visible text, no unreleased answer
in any learner-visible file). Further scenarios: a crash after a learning round with a block-level resume, an
unreliable upstream with an actor timeout, and a learned rule admitted, served, fired and audited.
Opt-in: `-m live_codex` or V9_LIVE_CODEX=1 (the fast regression tests at the end run by default).
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import arms  # noqa: E402
import campaign as CP  # noqa: E402
import cost  # noqa: E402
import experiment  # noqa: E402
import fake_upstream as fu  # noqa: E402
import full_audit  # noqa: E402
import paths  # noqa: E402
import rate_control  # noqa: E402
import report  # noqa: E402
from e2e import learners as EL  # noqa: E402
from e2e import toy_campaign as TC  # noqa: E402
from e2e import weak_actor as WA  # noqa: E402
from harness_core import contracts as C  # noqa: E402
from harness_core.ledger import load_events  # noqa: E402
from packs.toy.items import ITEMS, REFERENCES  # noqa: E402

NO_CODEX = not paths.CODEX.is_file() or not Path('/usr/bin/sandbox-exec').is_file()


def live(test):
    """The rehearsal scenarios launch the pinned real Codex (opt-in group `live_codex`; each scenario < 60 s)."""
    test = pytest.mark.skipif(NO_CODEX, reason='pinned codex or sandbox-exec missing')(test)
    return pytest.mark.live_codex(test)

KEY = 'dummy-e2e-broker-key-not-a-secret-0000'
ARMS = ('plain', 'text-static', 'text-evolve', 'mech-static', 'mech-evolve')
ROOTS = {arm: f'v9-r1-{chr(ord("a") + i)}' for i, arm in enumerate(ARMS)}      # arm-neutral root names
ORDER_SEED = 'toy-e2e-73'       # block 1 = toy01 toy02 toy06 toy07 toy08; block 2 = toy04 toy05 toy03 toy09 toy10
WITHHELD = ('toy06',)           # the toy's stand-in for an issue-47 question: scored, never released
ACTOR_SECONDS = 300             # per-episode wall clock of the rehearsal (production: 3600)
QUESTION = {item.item_id: item.question for item in ITEMS}


class SimulatedCrash(RuntimeError):
    """Stands in for a controller crash (OOM, reboot) at a chosen point of a campaign."""


def make_group(base: Path, roots: dict[str, str], *, responder=WA.respond, seconds: int = ACTOR_SECONDS,
               order_seed: str = ORDER_SEED, mechanism_learner: dict | None = None) -> dict:
    """Code copy (toy W0s), one prepared root per arm, one barrier group, one fake upstream serving them all."""
    code = TC.code_copy(base / 'code')
    barrier = {'dir': str(base / 'barrier'), 'roots': list(roots.values()), 'timeout_s': 900, 'poll_s': 0.1}
    g = {'base': base, 'code': code, 'up': fu.Upstream(responder), 'roots': {}, 'specs': {}, 'states': {},
         'actor_seconds': seconds, 'mechanism_learner': dict(mechanism_learner or {}),
         'errors': {}, 'models': {}, 'rounds': {}, 'seconds': {}, 'episodes': {},
         'scheduler': rate_control.RateScheduler(max_in_flight=float('inf'), min_interval=0, backoffs=(0,))}
    for arm, name in roots.items():
        spec = arms.parse_config({'profile': 'v9', 'arm': arm, 'repeat': 1, 'pack': 'toy', 'order_seed': order_seed,
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
                model = (EL.text_model() if spec.arm.learner == 'text'
                         else EL.mechanism_model(**g['mechanism_learner']))
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
    print('\nS8 e2e timings: ' + json.dumps(timing))
    return result


def events_of(api: Path):
    return load_events(api / 'ledger.jsonl') if (api / 'ledger.jsonl').is_file() else []


@live
def test_every_root_completes_and_the_audit_passes(group):
    assert not group['errors'], group['errors']
    for arm in ARMS:
        state = group['states'][arm]
        assert state['status'] == 'completed', (arm, state.get('error'))
        audit = group['audits'][arm]
        failed = json.loads((group['roots'][arm] / 'preflight/final-audit.json').read_text())['failed']
        assert audit['passed'], (arm, failed[:10])

# ---------------------------------------------------------------------------------------------
# Expected behaviour of the scripted weak actor (tests/e2e/weak_actor.py) per arm
# ---------------------------------------------------------------------------------------------

UNAIDED = {'toy01': '250.75', 'toy02': '11.53', 'toy06': '1.225', 'toy07': '36.35', 'toy08': '2.80',
           'toy04': '82.3', 'toy05': '65.7', 'toy03': '14.77', 'toy09': '242', 'toy10': '0.1340'}
GATED = {**UNAIDED, 'toy01': '252.00', 'toy05': '65.7, 71.9', 'toy03': '0.1477', 'toy09': '238'}   # toy07: kept
EXPECTED = {'plain': UNAIDED, 'text-static': UNAIDED, 'text-evolve': UNAIDED, 'mech-static': GATED, 'mech-evolve': GATED}
BLOCK1, BLOCK2 = ['toy01', 'toy02', 'toy06', 'toy07', 'toy08'], ['toy04', 'toy05', 'toy03', 'toy09', 'toy10']
CONTROLLER_ARMS = ('mech-static', 'mech-evolve')
TEXT_ARMS = ('plain', 'text-static', 'text-evolve')
LEARNER_ARMS = ('text-evolve', 'mech-evolve')


def run_dir(group, arm, task):
    return group['roots'][arm] / 'runs/episodes' / f'{(BLOCK1 + BLOCK2).index(task) + 1:03}'


def api_dirs(root: Path):
    """Every episode api/ directory of a root (scored and verification)."""
    return sorted(p for p in root.rglob('api') if p.is_dir() and (p / 'summary.json').is_file())


def request_bodies(root: Path):
    for api in api_dirs(root):
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
    assert group['states']['mech-static']['results'] and all(
        r['status'] == 'completed' for arm in ARMS for r in group['states'][arm]['results'])


@live
def test_controller_arms_record_loop_notes_submits_findings_and_splices(group):
    for arm in CONTROLLER_ARMS:
        totals = {'loop_guard': 0, 'ledger': 0, 'enforce': 0, 'submits': 0, 'gate_rounds': 0, 'submit_invalid': 0}
        for task in BLOCK1 + BLOCK2:
            base = run_dir(group, arm, task)
            api = base / 'api'
            events = events_of(api)
            summary = json.loads((base / 'summary.json').read_text())
            counters = summary['controller']
            assert counters['controller_errors'] == 0, (arm, task)
            notes = [e for e in events if isinstance(e, C.Note)]
            candidates = [e for e in events if isinstance(e, C.Candidate)]
            checks = sorted((api / 'checks').glob('*.json'))
            if task in ('toy02', 'toy09'):                               # the loop items
                loop_notes = [n for n in notes if n.reason == 'loop_guard']
                assert len(loop_notes) == 1 and counters['inserts_by_reason'].get('loop_guard') == 1, (arm, task)
                threshold = 4 if (arm == 'mech-evolve' and task in BLOCK2) else 3
                assert f'issued this exact command {threshold} times' in loop_notes[0].text, (arm, task)
                commands = [e for e in events if isinstance(e, C.Command) and e.cmd.startswith('lookup')]
                assert len(commands) == threshold                         # the note stopped the loop
            if task == 'toy08':                                          # forced compaction -> frozen ledger splice
                assert [e.kind for e in events if e.kind == 'compaction'] == ['compaction']
                assert [n.reason for n in notes if n.reason == 'ledger'] == ['ledger']
                inserted = [i for p in sorted(api.glob('[0-9][0-9][0-9]/controller.json'))
                            for i in json.loads(p.read_text())['inserted'] if i['reason'] == 'ledger']
                assert inserted and counters['compactions'] == counters['resumes'] == 1
                resume = [json.loads(p.read_text()) for p in sorted(api.glob('[0-9][0-9][0-9]/transport.json'))]
                assert [m['kind'] for m in resume] == ['turn', 'turn', 'compaction', 'resume', 'turn']
            if task == 'toy10':                                          # unsubmitted final with a finding
                assert counters['enforce_requeries'] == 1 and [n.reason for n in notes if n.reason == 'enforce'] == ['enforce']
                metas = [json.loads(p.read_text()) for p in sorted(api.glob('[0-9][0-9][0-9]/transport.json'))]
                assert [m['origin'] for m in metas].count('controller') == 1
                assert [c.via for c in candidates] == ['final']
            if task == 'toy04':                                          # unsubmitted final, clean check: no re-query
                assert counters['enforce_requeries'] == 0 and [c.via for c in candidates] == ['final']
            if task in ('toy01', 'toy03', 'toy09', 'toy05', 'toy07', 'toy02'):     # a finding, then accept / fix / keep
                rejected = 1 if task == 'toy07' else 0          # toy07: an over-long --keep reason, then a short one
                assert counters['gate_rounds'] == 1 and counters['submits'] == 2 + rejected, (arm, task, counters)
                assert counters['submit_invalid'] == rejected, (arm, task, counters)
                first = json.loads(checks[0].read_text())
                assert first['result']['status'] == 'findings' and len(checks) == 2       # no check file when rejected
                assert [c.via for c in candidates] == ['submit', 'submit']
                assert (candidates[-1].keep != '') == (task in ('toy02', 'toy07'))
                logged = [json.loads(line) for line in (api / 'controller.jsonl').read_text().splitlines()]
                assert [r['status'] for r in logged if r.get('event') == 'submit'].count('rejected') == rejected
            for key in ('submits', 'gate_rounds', 'submit_invalid'):
                totals[key] += counters[key]
            for reason in ('loop_guard', 'ledger', 'enforce'):
                totals[reason] += counters['inserts_by_reason'].get(reason, 0)
            assert summary['fallback_answer'] == (None if task in ('toy04', 'toy10') else EXPECTED[arm][task])
        assert totals == {'loop_guard': 2, 'ledger': 1, 'enforce': 1, 'submits': 15, 'gate_rounds': 6,
                          'submit_invalid': 1}, (arm, totals)


@live
def test_text_arms_carry_no_v9_record_fields(group):
    for arm in TEXT_ARMS:
        root = group['roots'][arm]
        apis = api_dirs(root)
        assert len(apis) == 10 + (8 if arm == 'text-evolve' else 0)          # scored + verification episodes
        for api in apis:
            assert not any((api / n).exists() for n in ('controller.jsonl', 'ledger.jsonl', 'checks'))
            summary = json.loads((api / 'summary.json').read_text())
            assert not set(summary) & set(full_audit.V9_SUMMARY_FIELDS)
            for call in sorted(api.glob('[0-9][0-9][0-9]')):
                meta = json.loads((call / 'transport.json').read_text())
                assert not set(meta) & set(full_audit.V9_RECORD_FIELDS) and not (call / 'controller.json').exists()
            episode = json.loads((api.parent / 'summary.json').read_text())
            assert episode['controller'] is None and episode['fallback_answer'] is None
            assert episode['fallback_differs'] is None
            kit_bin = api.parent / 'workspace/.harness/bin'
            assert not (kit_bin / 'submit').exists()
            assert (kit_bin / 'lookup').exists() == (arm != 'plain') == (kit_bin / 'calc').exists()
            readme = (api.parent / 'workspace/README.md').read_text()
            assert '# Submitting your answer' not in readme and ('lookup <file>' in readme) == (arm != 'plain')
        for request, body in request_bodies(root):
            assert not any('[harness:' in s for s in strings(body['input'])), request
    for arm in CONTROLLER_ARMS:                                            # and the controller arms do carry them
        texts = [s for _, body in request_bodies(group['roots'][arm]) for s in strings(body['input'])]
        assert any(s.startswith('[harness:loop_guard]') for s in texts) and any('[harness:gate]' in s for s in texts)
        assert any(s.startswith('[harness:ledger]') for s in texts) and any(s.startswith('[harness:enforce]') for s in texts)


@live
def test_learners_admit_or_reject_deterministically(group):
    import learner_common as LC
    text_root, mech_root = group['roots']['text-evolve'], group['roots']['mech-evolve']

    def rows(root):
        return [json.loads(line) for line in (root / 'learning-ledger.jsonl').read_text().splitlines()]
    text_rows, mech_rows = rows(text_root), rows(mech_root)
    assert [(r['kind'], r['id'], r['admitted']) for r in text_rows] == [('note', 'revised_figures', True)]
    assert (text_rows[0]['support'], sorted(text_rows[0]['keep'])) == ([1, 4], [2, 5])
    assert [(r['kind'], r['id'], r['admitted']) for r in mech_rows] == [
        ('rule', 'revised_cells', False), ('policy', 'loop_threshold', True), ('note', 'revised_figures', True)]
    assert 'precision: only 3 right episodes (< 10)' in mech_rows[0]['reason']
    assert (mech_rows[2]['support'], sorted(mech_rows[2]['keep'])) == ([4], [1, 5])
    # the verdicts re-derive from the recorded verification runs (cards judge; no model call involved)
    for root, row in ((text_root, text_rows[0]), (mech_root, mech_rows[2])):
        verification = json.loads((root / 'evolution/01/verification.json').read_text())
        runs = verification['runs'][row['id']]
        originals = {int(p.parent.name): json.loads(p.read_text())['out_tokens']
                     for p in (root / 'evidence/01').glob('[0-9][0-9][0-9]/summary.json')}
        verdict = LC.judge(sorted(runs, key=lambda r: (r['role'], r['q'], r['k'])), originals)
        assert verdict['admitted'] and verdict['reason'] == row['reason']
        assert all(r['strict'] == 1 for r in runs) and len(runs) == 2 * (len(row['support']) + len(row['keep']))
    # the admitted revisions are served from block 2 on, and only there
    for arm, root in (('text-evolve', text_root), ('mech-evolve', mech_root)):
        state = group['states'][arm]
        candidate = root / 'evolution/01/adapter'
        assert state['blocks']['1']['adapter']['path'].endswith('adapters/' + ('text-w0' if arm == 'text-evolve' else 'mech-w0'))
        assert state['blocks']['2']['adapter']['path'] == str(candidate)
        served = {r['task_id']: r['notes_served'] for r in state['results']}
        assert served == {t: (['revised_figures'] if t == 'toy04' else []) for t in BLOCK1 + BLOCK2}
    policy = json.loads((mech_root / 'evolution/01/adapter/policy.json').read_text())
    assert policy['loop_threshold'] == 4
    w0_rules = sorted(p.name for p in (group['code'] / 'adapters/mech-w0/rules').glob('*.json'))
    assert sorted(p.name for p in (mech_root / 'evolution/01/adapter/rules').glob('*.json')) == w0_rules
    # both learners were called through the same entry (campaign.call_learner) with the same learner model
    for arm in LEARNER_ARMS:
        result = group['rounds'][arm][0]
        assert result['status'] == 'admitted' and result['model'] == 'accounts/fireworks/models/glm-5p3-flash'
        assert result['reasoning_effort'] == 'high' and not result.get('error')


ARM_LABELS = ('text-static', 'text-evolve', 'mech-static', 'mech-evolve', 'text-w0', 'mech-w0', 'mechanism',
              'text_learning', 'mechanism_learning', 'learner')


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
    assert len(fixed) == 1 and bodies == len(group['up'].bodies)   # Codex's own instructions/tools: arm-independent
    # no actor was granted the adapter, the evolution tree or anything else of a campaign root to read (v9 passes
    # adapter=None to acting; AGENTS.md is composed into the workspace), in scored and verification episodes
    for arm in ARMS:
        root = group['roots'][arm].resolve()
        for invocation in root.rglob('records/invocation.json'):
            record = json.loads(invocation.read_text())
            granted = [*record['read_roots'], *record['data_roots'], *record['code_read']['files'], *record['code_read']['dirs']]
            assert record['adapter'] is None and not any(Path(p).resolve().is_relative_to(root) for p in granted)
            assert not any('/adapters' in p or 'items.py' in p for p in granted), granted
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
    """text arms: W0 = the full seed text (core advice + pack.seed_text()); mech arms: the minimal finishing text
    (the knowledge runs as rules); plain: COMMON only. Checked in the first request each actor sent."""
    from packs.toy.pack import ToyPack
    seed = ToyPack().seed_text()
    for arm in ARMS:
        first = json.loads((run_dir(group, arm, 'toy01') / 'api/001/request.json').read_text())
        agents = next(s for s in strings(first['input']) if s.startswith('# AGENTS.md instructions'))
        assert ('# Working advice' in agents and seed in agents) == (arm.startswith('text-')), arm
        assert ('# Finishing a question' in agents) == (arm.startswith('mech-')), arm
        assert '# OfficeQA task workspace' in agents          # COMMON (pack-specific wording; see the S8 report)
    visible = {arm: arms.model_visible(arms.w0_dir(group['specs'][arm].arm, group['code'] / 'adapters'),
                                       group['specs'][arm].arm)['total'] for arm in ('text-static', 'mech-static')}
    assert all(0 < v <= C.TEXT_BUDGET for v in visible.values())
    print('\ntoy W0 model-visible bytes: ' + json.dumps(visible))

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
    for task in ('toy04', 'toy03', 'toy09', 'toy10', 'toy06'):
        prediction = (run_dir(group, 'mech-evolve', task) / 'prediction.json').read_text()
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
        assert groups['actors_codex']['logical_calls'] + groups['actors_controller']['logical_calls'] == scored
        assert (groups['actors_controller']['logical_calls'] > 0) == (arm in CONTROLLER_ARMS)
        assert (groups['verification_codex']['logical_calls'] > 0) == (arm in LEARNER_ARMS)
        assert groups['learners']['logical_calls'] == (len(group['models'][arm].requests) if arm in LEARNER_ARMS else 0)
        assert groups['aborted_actors']['logical_calls'] == groups['aborted_learners']['logical_calls'] == 0
        assert not (group['roots'][arm] / 'report-error.json').exists()
        html = (group['roots'][arm] / 'report.html').read_text()
        assert KEY not in html and not any(QUESTION[t] in html for t in QUESTION)
        assert not any(REFERENCES[t] in html for t in ('toy01', 'toy03', 'toy05', 'toy10'))   # distinctive values
    timings = json.loads((base / 'timings.json').read_text())
    assert timings['campaign_seconds'] < 900 and timings['upstream_requests'] == len(group['up'].bodies)


RESUME_ROOTS = {'text-evolve': 'v9-s-a', 'mech-evolve': 'v9-s-b'}


@live
def test_crash_after_learning_aborts_the_group_and_resumes_block_level(tmp_path_factory):
    """A crash right after a learning round (its files written, the new adapter not yet adopted) stops that root
    and ABORTs the group; the peer stops too. After `clear_abort`, a block-level resume of both roots moves the
    unadopted round aside (runs-aborted/), restores the learner files from learner-backups/, re-runs the round
    (new verification actors through the real episode path) and completes; the audits pass."""
    import barrier as B
    g = make_group(tmp_path_factory.mktemp('h'), RESUME_ROOTS)
    barrier_dir = Path(g['specs']['mech-evolve'].barrier.dir)
    crashed, peer = g['roots']['mech-evolve'], g['roots']['text-evolve']
    try:
        run_roots(g, crash_after_learning=('mech-evolve',))
        assert g['errors']['mech-evolve'].startswith('SimulatedCrash'), g['errors']
        assert g['errors']['text-evolve'].startswith('BarrierAborted'), g['errors']
        state = json.loads((crashed / 'state.json').read_text())
        assert state['status'] == 'stopped_on_error' and state['active_learning_block'] == 1
        assert json.loads((peer / 'state.json').read_text())['status'] == 'aborted'
        abort = json.loads((barrier_dir / 'ABORT').read_text())
        assert 'v9-s-b' in abort['reason'] and 'simulated crash' in abort['reason']
        first = json.loads((crashed / 'evolution/01/result.json').read_text())
        assert first['status'] == 'admitted'                              # the round itself had finished
        rows_before = (crashed / 'learning-ledger.jsonl').read_text().splitlines()
        assert len(rows_before) == 3 and (crashed / 'learner-backups/01/backup.json').is_file()
        backup = json.loads((crashed / 'learner-backups/01/backup.json').read_text())
        assert backup == {'learner-state.json': None, 'learning-ledger.jsonl': None}    # nothing before round 1

        assert B.clear_abort(barrier_dir) is not None
        run_roots(g, resume=True)
        assert not g['errors'], g['errors']
    finally:
        close_group(g)
    for arm, root in (('mech-evolve', crashed), ('text-evolve', peer)):
        state = json.loads((root / 'state.json').read_text())
        assert state['status'] == 'completed' and len(state['attempts']) == 2
        assert {r['task_id']: r['answer'] for r in state['results']} == EXPECTED[arm]
        assert g['audits'][arm]['passed'], (arm, json.loads((root / 'preflight/final-audit.json').read_text())['failed'][:8])
    state = json.loads((crashed / 'state.json').read_text())
    moved = state['attempts'][1]['moved']
    assert moved['block'] == 1 and moved['kept_episodes'] is True
    assert {'evidence/01', 'evolution/01', 'private-learning-seals/01.json'} <= set(moved['moved'])
    aside = crashed / moved['to']
    assert (aside / 'evolution/01/result.json').is_file() and (aside / 'learner-files/learning-ledger.jsonl').is_file()
    rows = [json.loads(line) for line in (crashed / 'learning-ledger.jsonl').read_text().splitlines()]
    assert [(r['kind'], r['id'], r['admitted']) for r in rows] == [
        ('rule', 'revised_cells', False), ('policy', 'loop_threshold', True), ('note', 'revised_figures', True)]
    # determinism across two real runs of the same round on the same sealed evidence: identical decisions
    first_rows = [json.loads(line) for line in (aside / 'learner-files/learning-ledger.jsonl').read_text().splitlines()]
    decision = lambda r: (r['kind'], r['id'], r['admitted'], r['reason'], r.get('support'), r.get('keep'))  # noqa: E731
    assert [decision(r) for r in first_rows] == [decision(r) for r in rows]
    assert len(g['rounds']['mech-evolve']) == 2 and state['adapter']['path'] == str(crashed / 'evolution/01/adapter')
    groups = g['costs']['mech-evolve']['groups']
    assert groups['aborted_learners']['logical_calls'] == 4 and groups['aborted_actors']['logical_calls'] > 0
    assert groups['learners']['logical_calls'] == 4 and groups['verification_codex']['logical_calls'] > 0


ADVERSITY_ROOTS = {'plain': 'v9-t-a', 'mech-static': 'v9-t-b'}
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
        # wave 3 (S8 request 4): the broker abandons the stalled provider call when its episode ends, so the
        # call is fully recorded before the episode summary, the seal, the audit and cost -- while the fake
        # provider is still holding the stream open
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
        audit = json.loads((root / 'preflight/final-audit.json').read_text())
        assert audit['passed'], (arm, audit['failed'][:8])
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
        # the stalled call, abandoned when the episode ended: one dispatched attempt with unknown usage (never
        # priced as zero), and the retry refused because the broker had stopped with its episode
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
    ledger = events_of(run_dir(g, 'mech-static', 'toy05') / 'api')
    assert not [e for e in ledger if isinstance(e, C.Candidate)]         # the killed actor never submitted


RULE_ROOTS = {'mech-evolve': 'v9-u-a'}
RULE_SEED = 'toy-e2e-rule-0'    # block 1 = toy03 toy10 toy05 toy02 toy07; block 2 = toy04 toy09 toy08 toy06 toy01


@live
def test_learned_rule_is_admitted_served_fired_by_the_gate_and_audited(tmp_path_factory, monkeypatch):
    """The mechanism learner's core path end to end: a new declarative rule proposed from real campaign evidence
    (broker ledgers of real Codex episodes) passes the zero-cost admission (counterfactual on the wrong target +
    precision over the right episodes), is written into the next revision, served from block 2, fires first in the
    live gate on a later first-printing question (its own finding template and offer), and the audit finds its
    admission record and leak screen. Rehearsal-only: the precision minimum (10 right episodes, Wilson bound <=
    0.20; a rule needs >= 16 right released episodes with 0 fires) cannot be met with 4 right episodes, so this
    test lowers mechanism_learning.MIN_RIGHT to 3 and MAX_RIGHT_FIRE to 0.5 (bound of 0/4 = 0.49)."""
    import mechanism_learning as ML
    monkeypatch.setattr(ML, 'MIN_RIGHT', 3)
    monkeypatch.setattr(ML, 'MAX_RIGHT_FIRE', 0.5)
    g = make_group(tmp_path_factory.mktemp('u'), RULE_ROOTS, order_seed=RULE_SEED,
                   mechanism_learner={'note': False, 'edit': 'priority'})
    try:
        run_roots(g)
    finally:
        close_group(g)
    assert not g['errors'], g['errors']
    root, state = g['roots']['mech-evolve'], g['states']['mech-evolve']
    order = [r['task_id'] for r in sorted(state['results'], key=lambda r: r['ordinal'])]
    assert order == ['toy03', 'toy10', 'toy05', 'toy02', 'toy07', 'toy04', 'toy09', 'toy08', 'toy06', 'toy01']
    rows = [json.loads(line) for line in (root / 'learning-ledger.jsonl').read_text().splitlines()]
    assert [(r['kind'], r['id'], r['admitted']) for r in rows] == [('rule', 'revised_cells', True),
                                                                  ('rule', 'answer_count', True)]
    admission = json.loads((root / 'evolution/01/sessions/learner/control/mechanical-admission.json').read_text())
    assert admission['admitted_rules'] == ['revised_cells', 'answer_count'] and admission['policy_changes'] == {}
    assert admission['rule_admission']['answer_count'] == {'admitted': True, 'reasons': [], 'mode': 'priority'}
    record = admission['rule_admission']['revised_cells']
    assert [(c['ordinal'], c['admit']) for c in record['counterfactual']] == [(5, True)]      # toy07, kept wrong
    assert record['precision']['n_right'] == 4 and record['precision']['fired_right'] == 0
    served = json.loads((root / 'evolution/01/adapter/rules/revised_cells.json').read_text())
    assert served['origin'] == 'learned' and served['admitted_block'] == 1 and served['enabled'] is True
    assert state['blocks']['2']['adapter']['path'] == str(root / 'evolution/01/adapter')
    # block 2: the first-printing question meets the learned rule first (priority 20 < later_printing 30)
    api = root / 'runs/episodes' / f'{order.index("toy01") + 1:03}' / 'api'
    check = json.loads(sorted((api / 'checks').glob('*.json'))[0].read_text())
    shown = check['result']['findings'][0]
    assert shown['rule'] == 'revised_cells' and shown['text'].startswith('calc ') and 'the revised printing' in shown['text']
    assert shown['offer']['answer'] == '252.00'
    answers = {r['task_id']: r['answer'] for r in state['results']}
    assert answers == GATED
    audit = json.loads((root / 'preflight/final-audit.json').read_text())
    assert audit['passed'], audit['failed'][:8]
    names = {c['name'] for c in audit['checks']}
    assert 'round 1 served rule revised_cells (changed from W0) admitted with replay numbers' in names
    assert 'round 1 served rule revised_cells passes the leak screen' in names
    assert 'round 1 served rule answer_count (changed from W0) admitted with replay numbers' in names

# ---------------------------------------------------------------------------------------------
# Fast regression tests of the integration fixes (default tier; no Codex, no network)
# ---------------------------------------------------------------------------------------------


def test_toy_lookup_runs_as_a_kit_script():
    """The kit runs pack CLIs as plain scripts (`python -I -B <script>`); the toy CLI used a package-relative
    import only, so `lookup` failed under the kit (fix: sibling-import fallback, as packs/officeqa/tools/cli.py)."""
    import subprocess
    import kit
    from packs.toy import pack as toy_pack
    script = toy_pack.TOOLS_SCRIPT
    done = subprocess.run([sys.executable, '-I', '-B', str(script), 'widgets_2020_b.txt', 'North'],
                          capture_output=True, text=True, timeout=60, cwd='/')
    assert done.returncode == 0, done.stderr
    assert '⟦toy widgets_2020_b.txt:5|Q2 2020|North|131.5r⟧' in done.stdout
    # the kit grants exactly the script and its sibling import (never items.py, which holds the toy references)
    assert [p.name for p in kit._import_closure(script)] == ['corpus.py', 'tools.py']


def test_toy_pack_is_wired_like_any_pack(tmp_path):
    """campaign.tool_wrappers / actor_data / the tables index and arms.w0_dir were OfficeQA-only; a toy (or any
    non-OfficeQA) arm could not be prepared. The W0 of a code copy is built for its pack (`arms.py build --pack`)
    and check_adapter refuses a W0 built for another pack."""
    from packs.toy.pack import ToyPack, TOOLS_SCRIPT
    toy_text, toy_mech = (arms.parse_arm(name, pack='toy') for name in ('text-static', 'mech-static'))
    assert CP.tool_wrappers(toy_mech) == {'lookup': str(TOOLS_SCRIPT), 'calc': str(ROOT / 'harness_core/tools/calc.py')}
    assert CP.tool_wrappers(arms.parse_arm('plain', pack='toy')) == {}
    assert CP.actor_data(toy_mech, CP.Layout(root=tmp_path)) == ({}, ())
    assert CP.uses_tables_index('officeqa') and not CP.uses_tables_index('toy')
    officeqa_mech = arms.parse_arm('mech-static')
    env, roots = CP.actor_data(officeqa_mech, CP.Layout(root=tmp_path))
    assert set(env) == {'OFFICEQA_CORPUS', 'TABLES_INDEX'} and len(roots) == 1
    usage = ToyPack().usage()
    assert usage.startswith('lookup <file>') and all(name in usage for name in ('widgets_2020_a.txt', 'widgets_2021_a.txt'))
    adapters = tmp_path / 'adapters'
    for arm in (toy_text, toy_mech):
        directory = arms.w0_dir(arm, adapters)
        arms.build_w0(arm.seed, ToyPack(), directory)
        checked = arms.check_adapter(directory, arm)
        assert checked['model_visible']['within_budget']
        assert arms.check_w0(arm, adapters)['adapter'] == str(directory)
    with pytest.raises(ValueError, match="adapter pack 'officeqa' is not the arm pack 'toy'"):
        arms.check_adapter(ROOT / 'adapters/mech-w0', toy_mech)


def test_audit_accepts_every_declared_rule_admission_mode():
    """full_audit required counterfactual + precision for every served rule change, but the mechanism learner
    also admits priority-only changes (schema check) and disables (measured noise); a campaign whose learner used
    either failed its final audit (S5 review R2). Each mode is now checked against its own record."""
    import mechanism_learning as ML
    from full_audit import _admission_matches
    before = {'id': 'answer_count', 'when': [{'pred': 'count_mismatch', 'args': {}}], 'finding': 'x', 'offer': None,
              'priority': 10, 'enabled': True, 'triggers': [], 'scope': 'core', 'origin': 'seed', 'admitted_block': 0,
              'stats': {}}
    priority = {**before, 'priority': 12, 'stats': {'modified_block': 1}}
    assert _admission_matches({'mode': 'priority', 'admitted': True, 'reasons': []}, before, priority)
    assert not _admission_matches({'mode': 'priority', 'admitted': True}, before, {**priority, 'finding': 'y'})
    assert not _admission_matches({'mode': 'priority', 'admitted': True}, None, priority)
    disabled = {**before, 'enabled': False, 'stats': {'modified_block': 2}}
    noisy = {'n_right': ML.MIN_RIGHT, 'right_fire_rate': ML.MAX_RIGHT_FIRE + 0.05}
    assert _admission_matches({'mode': 'disable', 'precision': noisy}, before, disabled)
    assert not _admission_matches({'mode': 'disable', 'precision': {**noisy, 'right_fire_rate': ML.MAX_RIGHT_FIRE}},
                                  before, disabled)
    assert not _admission_matches({'mode': 'disable', 'precision': {**noisy, 'n_right': ML.MIN_RIGHT - 1}},
                                  before, disabled)
    assert not _admission_matches({'mode': 'disable', 'precision': noisy}, before, {**disabled, 'priority': 5})
    new_rule = {**before, 'id': 'revised_cells', 'origin': 'learned'}
    full = {'counterfactual': [{'ordinal': 5, 'admit': True}], 'precision': {'n_right': 16, 'fired_right': 0}}
    assert _admission_matches(full, None, new_rule) and _admission_matches({**full, 'mode': 'full'}, before, new_rule)
    assert not _admission_matches({'mode': 'priority', 'admitted': True, 'reasons': []}, before, {**new_rule, 'priority': 12})
    assert not _admission_matches({}, None, new_rule)
    # S8 review: a recorded but failed full verdict is not an admission
    assert not _admission_matches({**full, 'counterfactual': [{'ordinal': 5, 'admit': False}]}, None, new_rule)
    assert not _admission_matches({**full, 'admitted': False}, None, new_rule)
    assert not _admission_matches({**full, 'precision': {**full['precision'], 'ok': False}}, None, new_rule)
    assert _admission_matches({**full, 'admitted': True, 'precision': {**full['precision'], 'ok': True}}, None, new_rule)
