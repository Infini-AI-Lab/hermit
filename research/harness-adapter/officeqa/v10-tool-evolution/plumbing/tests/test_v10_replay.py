"""v10 offline rule replay (harness_core/replay.py) end to end on the toy pack: the v9 test under its v10 name.
The rule engine, the gate and the counterfactual replay stay valid for a `gate: true` harness adapter (none
this round; DESIGN scope decision 7), so this suite pins them unchanged:
synthetic Codex items -> Ledger (api/ledger.jsonl) -> [Gate -> checks/] -> load_episode ->
fire statistics, Wilson precision gate, counterfactual admission, stored-check re-derivation.
Offline; recompute offers run in the real sandbox; the grader is injected (toy grader).
"""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness_core import contracts as C  # noqa: E402
from harness_core import replay as RP  # noqa: E402
from harness_core.gate import Gate  # noqa: E402
from harness_core.ledger import Ledger  # noqa: E402
from harness_core.rules import core_seed_rules  # noqa: E402
from harness_core.runner import SandboxRunner  # noqa: E402
from packs.toy import PACK, REFERENCES, grade  # noqa: E402
from packs.toy.pack import toy_seed_rules  # noqa: E402
from packs.toy.sim import QUESTIONS, run_item  # noqa: E402

RULES = {r.id: r for r in core_seed_rules() + toy_seed_rules()}
WRONG = {'toy01', 'toy03', 'toy05', 'toy07', 'toy09'}
pytestmark = pytest.mark.skipif(not Path('/usr/bin/sandbox-exec').exists(), reason='macOS sandbox-exec required')


@pytest.fixture(scope='module')
def runner():
    return SandboxRunner()


@pytest.fixture(scope='module')
def episodes(tmp_path_factory):
    """No-gate episodes: the first submission is final (what an arm without L4 would hand in)."""
    root = tmp_path_factory.mktemp('nogate')
    out = {}
    for item in sorted(QUESTIONS):
        api = root / item / 'api'
        sim = run_item(item, Ledger(PACK, api / 'ledger.jsonl'), None)
        out[item] = RP.load_episode(api, episode_id=item, question=QUESTIONS[item], final_answer=sim.final_text,
                                    label=int(grade(sim.final_text, REFERENCES[item])))
    return out


@pytest.fixture(scope='module')
def gated(tmp_path_factory, runner):
    root = tmp_path_factory.mktemp('gated')
    out = {}
    for item in sorted(QUESTIONS):
        api = root / item / 'api'
        sim = run_item(item, Ledger(PACK, api / 'ledger.jsonl'), Gate(PACK, list(RULES.values()), {}, runner,
                                                                     QUESTIONS[item], api_dir=api))
        out[item] = (api, RP.load_episode(api, episode_id=item, question=QUESTIONS[item], final_answer=sim.final_text,
                                          label=int(grade(sim.final_text, REFERENCES[item]))))
    return out


def test_labels_and_episode_loading(episodes, gated):
    assert {k for k, ep in episodes.items() if ep.label == 0} == WRONG
    assert all(ep.label == 1 for _, ep in gated.values())                   # every mistake repaired by the gate
    ep = episodes['toy01']
    assert ep.checks == [] and any(isinstance(e, C.Compute) for e in ep.events)
    _, gep = gated['toy01']
    assert len(gep.checks) == 2 and gep.checks[0]['result']['status'] == 'findings'
    ctx = RP.final_context(gep, PACK, None)
    assert ctx.candidate.ref == gep.events[[e.ref for e in gep.events].index(ctx.candidate.ref)].ref
    assert not ctx.candidate.ref.endswith('r')                              # the recorded submission, not synthetic
    ctx = RP.final_context(ep, PACK, None)
    assert ctx.candidate.via == 'final' and ctx.events[-1] is ctx.candidate


def test_fire_stats_on_no_gate_episodes(episodes, runner):
    stats = RP.fire_stats(list(RULES.values()), episodes.values(), PACK, runner)
    fired = {rid: (s['fired_wrong'], s['fired_right']) for rid, s in stats.items()}
    assert fired['later_printing'] == (2, 0) and fired['scale'] == (1, 0) and fired['answer_count'] == (1, 0)
    assert fired['period_match'] == (1, 0) and fired['recompute_final'] == (0, 1)
    assert fired['competing_candidates'] == (0, 0) and fired['grounded'] == (0, 0)
    lp = stats['later_printing']
    assert (lp['n_right'], lp['n_wrong'], lp['offers'], lp['precision']) == (5, 5, 2, 1.0)
    assert sorted(lp['fired_episodes']) == ['toy01', 'toy07']
    assert stats['recompute_final']['precision'] == 0.0 and stats['recompute_final']['right_fire_rate'] == 0.2
    assert stats['answer_count']['offers'] == 0
    blob = json.dumps(stats)
    assert not any(ref in blob for ref in ('252.00', '35.10', '0.1477', '238'))   # no answer values


def test_wilson_and_precision_gate(episodes, runner):
    lo, hi = RP.wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    lo, hi = RP.wilson(5, 10)
    assert (round(lo, 3), round(hi, 3)) == (0.237, 0.763)
    assert RP.wilson(0, 0) == (0.0, 1.0)
    stats = RP.fire_stats([RULES['later_printing'], RULES['recompute_final']], episodes.values(), PACK, runner)
    ok, reasons = RP.precision_check(stats['later_printing'])               # 5 right episodes < 10
    assert not ok and any('only 5 right episodes' in r for r in reasons)
    ok, reasons = RP.precision_check(stats['later_printing'], min_right=5)  # 0/5 still has upper bound 0.43
    assert not ok and any('fire rate bound' in r for r in reasons)
    assert RP.precision_check(stats['later_printing'], min_right=5, use_upper_bound=False) == (True, [])
    ok, reasons = RP.precision_check(stats['recompute_final'], min_right=5, use_upper_bound=False)
    assert not ok and any('wrong episodes' in r for r in reasons)


def test_counterfactual_admission(episodes, runner):
    def cf(rule, item):
        return RP.counterfactual(RULES[rule], episodes[item], REFERENCES[item], grade, pack=PACK, runner=runner)
    for rule, item in (('later_printing', 'toy01'), ('later_printing', 'toy07'), ('scale', 'toy03'),
                       ('period_match', 'toy09')):
        res = cf(rule, item)
        assert res['admit'] and res['fires'] and res['offer_right'] and res['silent_on_reference'], (rule, item, res)
        assert REFERENCES[item] not in json.dumps(res)                     # booleans only
    res = cf('answer_count', 'toy05')
    assert not res['admit'] and res['fires'] and res['reasons'] == ['rule fires but produces no offer']
    res = cf('recompute_final', 'toy10')
    assert not res['admit'] and 'episode is not labelled wrong' in res['reasons']
    res = cf('later_printing', 'toy03')
    assert not res['admit'] and res['reasons'] == ['rule does not fire on the episode']
    # a rule that fires whatever the answer is never admitted
    noisy = C.Rule(id='noisy', when=[{'pred': 'not_from_compute', 'args': {'allow_observed': False}}],
                   finding='{value.text}', offer={'op': 'use_alt', 'args': {}})
    res = RP.counterfactual(noisy, episodes['toy05'], REFERENCES['toy05'], grade, pack=PACK, runner=runner)
    assert not res['admit'] and not res['silent_on_reference']
    # a grader returning a score is thresholded, not truth-tested
    res = RP.counterfactual(RULES['scale'], episodes['toy03'], REFERENCES['toy03'], lambda a, r: 0.99,
                            pack=PACK, runner=runner)
    assert not res['offer_right'] and not res['admit']


def test_disabled_or_learned_rules_can_be_replayed(episodes, runner):
    off = C.Rule(**{**RULES['later_printing'].__dict__, 'enabled': False, 'id': 'later_printing_off'})
    out = RP.rule_outcome(off, RP.final_context(episodes['toy01'], PACK, runner))
    assert out['fired'] and out['offer'] is not None


def test_stored_checks_rederive_identically(gated, runner):
    n = 0
    for item, (api, ep) in gated.items():
        for check in ep.checks:
            res = RP.recheck(check, api / 'ledger.jsonl', PACK, runner)
            assert res['identical'] and res['ledger_sha_ok'], (item, check['n'], res)
            n += 1
    assert n >= 15
    # tampering with the ledger prefix is detected
    api, ep = gated['toy01']
    check = dict(ep.checks[0], ledger={'lines': ep.checks[0]['ledger']['lines'], 'sha256': '0' * 64})
    assert not RP.recheck(check, api / 'ledger.jsonl', PACK, runner)['identical']


def test_ledger_from_api_records_matches_live_ledger(tmp_path):
    """Recorded broker calls (request.json/response.json per call) rebuild the same commands/computes."""
    live = Ledger(PACK, None)
    calls = []

    class Recorder:
        def __init__(self, inner):
            self.inner = inner

        def ingest_request(self, call, items, t, tools=None):
            calls.append((call, 'request', {'input': list(items), 'tools': tools}))
            self.inner.ingest_request(call, items, t, tools=tools)

        def ingest_response(self, call, response, t):
            calls.append((call, 'response', response))
            self.inner.ingest_response(call, response, t)

        def __getattr__(self, name):
            return getattr(self.inner, name)
    run_item('toy07', Recorder(live), None)
    for call, kind, body in calls:
        d = tmp_path / f'{call:03d}'
        d.mkdir(exist_ok=True)
        (d / f'{kind}.json').write_text(json.dumps(body))
    rebuilt, info = RP.ledger_from_api_records(tmp_path, PACK)
    assert info['calls'] == max(c for c, _, _ in calls) and not info['errors']
    strip = lambda evs: [(e.kind, e.ref, getattr(e, 'cmd', ''), getattr(e, 'values', None)) for e in evs]  # noqa: E731
    assert strip(rebuilt.events) == strip(live.events)
