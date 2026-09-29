"""v9 L4 commit gate (harness_core/gate.py): message form, per-value rounds, keep, enforcement
helpers, check files and their offline re-derivation. Offline; offers run in the real sandbox.
"""
import json
from pathlib import Path
import re
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness_core import contracts as C  # noqa: E402
from harness_core.engine import RuleEngine  # noqa: E402
from harness_core.gate import Gate, shell_quote, validate_payload  # noqa: E402
from harness_core.ledger import Ledger  # noqa: E402
from harness_core.observe import RawPack  # noqa: E402
from harness_core.replay import recheck  # noqa: E402
from harness_core.rules import core_seed_rules  # noqa: E402
from harness_core.runner import SandboxRunner  # noqa: E402
from packs.toy import PACK, REFERENCES, grade  # noqa: E402
from packs.toy.pack import toy_seed_rules  # noqa: E402
from packs.toy.sim import QUESTIONS, run_item  # noqa: E402

RULES = core_seed_rules() + toy_seed_rules()
NAGS = re.compile(r'are you sure|double-check|double check|please verify|reconsider', re.I)


@pytest.fixture(scope='module')
def runner():
    return SandboxRunner()


def always(tag):
    return lambda ctx: [C.Binding({'tag': tag})]


def fixed_gate(policy=None, n=3):
    """A gate whose rules r1..rn always fire (no offers), for state-machine tests."""
    engine = RuleEngine(predicates={f'p{i}': always(f't{i}') for i in range(1, n + 1)})
    rules = [C.Rule(id=f'r{i}', when=[{'pred': f'p{i}', 'args': {}}], finding=f'finding {{tag}}', priority=i)
             for i in range(1, n + 1)]
    return Gate(RawPack(), rules, policy or {}, None, 'q', engine=engine), Ledger(RawPack(), None)


def test_message_form_one_finding_offer_accept_keep(runner, tmp_path):
    led = Ledger(PACK, tmp_path / 'api' / 'ledger.jsonl')
    gate = Gate(PACK, RULES, {}, runner, QUESTIONS['toy01'], api_dir=tmp_path / 'api')
    sim = run_item('toy01', led, gate)
    first, second = sim.results
    assert first.status == 'findings' and len(first.findings) == 1 and first.findings[0].rule == 'later_printing'
    lines = first.message.split('\n')
    assert len(lines) == 4 and lines[0].startswith('[harness:gate] calc ')
    assert lines[1].startswith('Offer: 252.00 (calc ') and 'widgets_2020_b.txt:5 in place of widgets_2020_a.txt:5' in lines[1]
    assert lines[2] == 'Accept: submit --answer "252.00"'
    assert lines[3] == 'Keep: submit --answer "250.75" --keep "<reason>"'
    assert len(lines[0].encode()) <= len('[harness:gate] ') + 200
    assert second.status == 'accepted' and second.message.startswith('[harness:gate] recorded "252.00"')
    assert grade(sim.final_text, REFERENCES['toy01'])
    # the shown offer's run is in the ledger as a calc output, so the accepted value is not re-flagged
    gate_computes = [e for e in led.events if isinstance(e, C.Compute) and e.source == 'gate:later_printing']
    assert len(gate_computes) == 1 and gate_computes[0].values == [252.0]
    cands = led.candidates()
    assert [c.answer for c in cands] == ['250.75', '252.00'] and all(c.via == 'submit' for c in cands)
    notes = [e for e in led.events if isinstance(e, C.Note)]
    assert [n.text for n in notes] == [first.message, second.message] and not any(n.sticky for n in notes)


def test_no_message_nags_and_all_toy_items_end_right(runner):
    for item in sorted(QUESTIONS):
        led = Ledger(PACK, None)
        sim = run_item(item, led, Gate(PACK, RULES, {}, runner, QUESTIONS[item]))
        assert grade(sim.final_text, REFERENCES[item]), item
        for res in sim.results:
            assert not NAGS.search(res.message), res.message
            assert len(res.message.split('\n')) <= 4


def test_one_finding_per_round_rounds_cap_and_value_key():
    gate, led = fixed_gate()
    seq = [gate.submit({'answer': a}, led, 0.0, 1) for a in ('5', '5', '5', '6', '5.0')]
    assert [(r.status, [f.rule for f in r.findings]) for r in seq] == [
        ('findings', ['r1']), ('findings', ['r2']), ('accepted', []), ('findings', ['r1']), ('accepted', [])]
    assert all(len(r.findings) <= 1 for r in seq)
    assert gate.summary[0]['fired'] == ['r1', 'r2', 'r3']


def test_keep_suppresses_the_shown_finding_for_that_value():
    gate, led = fixed_gate({'gate_rounds_max': 3})
    r1 = gate.submit({'answer': '5'}, led, 0.0, 1)
    r2 = gate.submit({'answer': '5', 'keep': 'the table says so'}, led, 0.0, 2)
    r3 = gate.submit({'answer': '5', 'keep': 'still'}, led, 0.0, 3)
    r4 = gate.submit({'answer': '5', 'keep': 'still'}, led, 0.0, 4)
    assert [r.findings[0].rule if r.findings else r.status for r in (r1, r2, r3, r4)] == ['r1', 'r2', 'r3', 'kept']
    assert '(kept)' in r4.message
    gate, led = fixed_gate({'gate_rounds_max': 1})
    gate.submit({'answer': '5'}, led, 0.0, 1)
    assert gate.submit({'answer': '5', 'keep': 'sure'}, led, 0.0, 2).status == 'kept'
    gate, led = fixed_gate({'gate_rounds_max': 0})
    assert gate.submit({'answer': '5'}, led, 0.0, 1).status == 'accepted'


def test_a_value_the_gate_offered_is_accepted_as_is():
    offer = C.OfferResult(answer='7', derivation='test')
    engine = RuleEngine(predicates={'p1': always('t1'), 'p2': always('t2')},
                        offers={'seven': lambda ctx, b: offer})
    rules = [C.Rule(id='r1', when=[{'pred': 'p1', 'args': {}}], finding='use {offer.answer}', offer={'op': 'seven'},
                    priority=1),
             C.Rule(id='r2', when=[{'pred': 'p2', 'args': {}}], finding='always', priority=2)]
    gate, led = Gate(RawPack(), rules, {}, None, 'q', engine=engine), Ledger(RawPack(), None)
    first = gate.submit({'answer': '5'}, led, 0.0, 1)
    assert first.findings[0].rule == 'r1' and 'Accept: submit --answer "7"' in first.message
    second = gate.submit({'answer': '7.0'}, led, 0.0, 2)
    assert second.status == 'accepted' and gate.summary[1]['fired'] == ['r2']     # evaluated, not shown


def test_disabled_rules_are_not_evaluated(runner):
    led = Ledger(PACK, None)
    gate = Gate(PACK, RULES, {'disabled_rules': ['later_printing']}, runner, QUESTIONS['toy01'])
    sim = run_item('toy01', led, gate)
    assert all('later_printing' not in s['fired'] for s in gate.summary)
    assert sim.final_text == '250.75'


def test_invalid_payloads_are_rejected_without_a_candidate():
    gate, led = fixed_gate()
    for payload in ({'answer': '1', 'extra': 'x'}, {'answer': 5}, {'answer': '  '}, {'answer': 'x' * 1001},
                    {'alt': 'only alt'}, ['not', 'a', 'dict']):
        res = gate.submit(payload, led, 0.0, 1)
        # contracts v9.1: an unusable payload is 'rejected' (not a gate round), with no candidate or check
        assert res.status == 'rejected' and not res.findings and res.message.startswith('[harness:gate] rejected')
    assert led.candidates() == [] and gate.checks == 0 and not [e for e in led.events if isinstance(e, C.Note)]
    assert validate_payload({'artifact_sha256': 'a' * 64}, 'artifact') is None
    assert validate_payload({'answer': 'x'}, 'artifact') == 'artifact answers need --artifact PATH'
    assert shell_quote('a "b" $c `d` \\e\nf') == 'a \\"b\\" \\$c \\`d\\` \\\\e f'


def test_check_final_and_enforcement_helper(runner):
    led = Ledger(PACK, None)
    gate = Gate(PACK, RULES, {}, runner, QUESTIONS['toy01'])
    assert gate.needs_enforcement('252.00', led)                        # nothing submitted yet
    run_item('toy01', led, gate)
    assert not gate.needs_enforcement('252.0', led) and not gate.needs_enforcement(' 252.00 ', led)
    assert gate.needs_enforcement('251', led)
    notes_before = [e for e in led.events if isinstance(e, C.Note)]
    res = gate.check_final('250.75', led, 9.0, 9)                     # the answer span, not the prose
    assert res.status == 'findings' and res.findings
    assert led.candidates()[-1].via == 'final' and res.candidate_ref == led.candidates()[-1].ref
    assert led.candidates()[-1].answer == '250.75'
    # S3-review R1: a final check shows the model nothing by itself -> no Note (the controller records the
    # feedback it actually sends); submit messages are printed by the CLI -> one 'gate' Note each
    assert [e for e in led.events if isinstance(e, C.Note)] == notes_before
    assert notes_before and all(n.reason == 'gate' for n in notes_before)
    assert len(notes_before) == sum(1 for c in led.candidates() if c.via == 'submit')


def test_check_files_record_identical_inputs_for_replay(runner, tmp_path):
    for item in ('toy01', 'toy03', 'toy05', 'toy09', 'toy10'):
        api = tmp_path / item / 'api'
        led = Ledger(PACK, api / 'ledger.jsonl')
        run_item(item, led, Gate(PACK, RULES, {}, runner, QUESTIONS[item], api_dir=api))
        checks = sorted((api / 'checks').glob('*.json'))
        assert checks and [p.name for p in checks][0] == '001.json'
        for path in checks:
            check = json.loads(path.read_text())
            assert check['ledger']['lines'] > 0 and len(check['ledger']['sha256']) == 64
            assert check['question'] == QUESTIONS[item] and check['rules'] and check['trace']
            out = recheck(check, api / 'ledger.jsonl', PACK, runner)
            assert out['identical'], (item, path.name, out)


def test_concurrent_submits_are_serialised(tmp_path):
    gate, led = fixed_gate()
    gate.checks_dir = tmp_path / 'checks'
    threads = [threading.Thread(target=gate.submit, args=({'answer': str(i)}, led, 0.0, i)) for i in range(12)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(p.name for p in (tmp_path / 'checks').glob('*.json')) == [f'{i:03d}.json' for i in range(1, 13)]
    refs = [c.ref for c in led.candidates()]
    assert len(refs) == 12 == len(set(refs))


def test_offer_line_is_one_line_whatever_the_alternative_contains():
    offer = C.OfferResult(answer='7\nand more', derivation='line one\nline two')
    engine = RuleEngine(predicates={'p1': always('t1')}, offers={'multi': lambda ctx, b: offer})
    rules = [C.Rule(id='r1', when=[{'pred': 'p1', 'args': {}}], finding='use it', offer={'op': 'multi'})]
    gate, led = Gate(RawPack(), rules, {}, None, 'q', engine=engine), Ledger(RawPack(), None)
    res = gate.submit({'answer': '5'}, led, 0.0, 1)
    lines = res.message.split('\n')
    assert len(lines) == 4 and lines[1] == 'Offer: 7 and more (line one line two)'
