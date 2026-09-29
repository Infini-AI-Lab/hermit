"""v9 rule engine, core predicates, core offers and rule/policy files (harness_core). Offline.

Events are built by hand with the contracts dataclasses so each predicate's contract is pinned
independently of the ledger. Offers that execute code use an unsandboxed test runner over trusted
test code (the sandbox itself is tested in test_v9_runner.py).
"""
import ast
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness_core import contracts as C  # noqa: E402
from harness_core import engine as E  # noqa: E402
from harness_core import offers as O  # noqa: E402
from harness_core import predicates as P  # noqa: E402
from harness_core import rules as R  # noqa: E402
from harness_core.observe import RawPack  # noqa: E402
from harness_core.tools import calc as calc_tool  # noqa: E402

RAW = RawPack()


class LocalRunner:
    """Trusted test code only; production offers use runner.SandboxRunner."""
    def __init__(self):
        self.codes = []

    def run(self, code, *, timeout_s=5.0):
        self.codes.append(code)
        rc, out, _ = calc_tool.execute(code, timeout=30)
        return rc == 0, out


def obs(ref, value=None, values=None, text=None, locator=None, **attrs):
    if values is not None:
        attrs[C.VALUES_ATTR] = values
    return C.Observe(kind='observe', ref=ref, call=1, t=0.0, source='x1', locator=locator or f'f.txt:{ref[1:]}',
                     text=text or str(value), value=value, attrs=attrs)


def comp(ref, values, code='print(1)', inputs=()):
    return C.Compute(kind='compute', ref=ref, call=2, t=0.0, source='x2', code=code, code_sha256=C.sha256_text(code),
                     output=' '.join(map(str, values)), values=list(values), inputs=list(inputs))


def cand(answer, ref='a9', alt='', keep='', via='submit'):
    return C.Candidate(kind='candidate', ref=ref, call=3, t=0.0, answer=answer, alt=alt, keep=keep, via=via)


def note(ref, text, reason='gate'):
    return C.Note(kind='note', ref=ref, call=3, t=0.0, reason=reason, text=text, sticky=False)


def ctx(events, candidate, spec=None, question='q', pack=RAW, runner=None):
    return C.RuleContext(question=question, spec=spec or C.QuestionSpec(), events=list(events) + [candidate],
                         candidate=candidate, pack=pack, runner=runner or LocalRunner())


def values(bindings, key):
    return [b.values[key] for b in bindings]

# ---------------------------------------------------------------------------------------------
# predicates
# ---------------------------------------------------------------------------------------------


def test_count_mismatch():
    two = C.QuestionSpec(n_values=2)
    assert values(P.count_mismatch(ctx([], cand('12.5'), two)), 'count') == [{'expected': 2, 'got': 1}]
    assert P.count_mismatch(ctx([], cand('[12.5, 13]'), two)) == []
    assert P.count_mismatch(ctx([], cand('12.5'))) == []                         # n_values unknown
    assert P.count_mismatch(ctx([], cand('a b'), C.QuestionSpec(n_values=2, value_type='text'))) == []
    assert values(P.count_mismatch(ctx([], cand('1, 2, 3')), n=2), 'count') == [{'expected': 2, 'got': 3}]


def test_not_from_compute():
    k = comp('k1', [12.3456])
    assert P.not_from_compute(ctx([k], cand('12.35'))) == []
    assert P.not_from_compute(ctx([k], cand('12.3'))) == []
    fired = P.not_from_compute(ctx([k], cand('12.4')))
    assert [b.values['value']['text'] for b in fired] == ['12.4']
    looked_up = [obs('c1', 99.0, text='total 99')]
    assert P.not_from_compute(ctx(looked_up, cand('99'))) == []
    assert len(P.not_from_compute(ctx(looked_up, cand('99')), allow_observed=False)) == 1
    assert P.not_from_compute(ctx([], cand('1979'), C.QuestionSpec(value_type='year'))) == []
    # only evidence *before* the candidate counts
    later = [cand('12.4', ref='a1'), comp('k2', [12.4])]
    c = ctx([], cand('12.4', ref='a1'))
    c = C.RuleContext(question='q', spec=C.QuestionSpec(), events=later, candidate=later[0], pack=RAW, runner=None)
    assert len(P.not_from_compute(c)) == 1


def test_scaled_100_directions_and_forms():
    k = comp('k1', [0.123456])
    dec = C.QuestionSpec(percent_form='decimal')
    (b,) = P.scaled_100(ctx([k], cand('12.35'), dec))
    assert b.values['direction'] == 'x100' and b.values['compute']['ref'] == 'k1' and b.values['factor'] == 100
    assert P.scaled_100(ctx([k], cand('12.35'), C.QuestionSpec(percent_form='unspecified')))
    assert P.scaled_100(ctx([k], cand('12.35'), C.QuestionSpec(percent_form='percent'))) == []
    assert P.scaled_100(ctx([k], cand('0.1235'), dec)) == []                      # equals the output itself
    pct = comp('k1', [12.3456])
    (b,) = P.scaled_100(ctx([pct], cand('0.1235'), C.QuestionSpec(percent_form='percent')))
    assert b.values['direction'] == 'div100' and b.values['factor'] == 0.01
    # the model multiplied inside calc: both values printed; still a scale slip for a decimal question
    both = comp('k1', [0.123456, 12.3456])
    assert P.scaled_100(ctx([both], cand('12.35'), dec))


def test_ungrounded_inputs_scope_and_transitivity():
    k1 = comp('k1', [5.5], inputs=[{'literal': '3.25', 'value': 3.25, 'span': [0, 4], 'observe': None, 'match': None},
                                   {'literal': '2', 'value': 2, 'span': [5, 6], 'observe': None, 'match': None,
                                    'trivial': True},
                                   {'literal': '2.25', 'value': 2.25, 'span': [7, 11], 'observe': 'c1', 'match': 'exact'}])
    k2 = comp('k2', [11.0], inputs=[{'literal': '5.5', 'value': 5.5, 'span': [0, 3], 'observe': 'k1', 'match': 'exact'}])
    events = [obs('c1', 2.25), k1, k2]
    (b,) = P.ungrounded_inputs(ctx(events, cand('11.0')))                         # via k2 -> k1
    assert b.values == {'compute': {'ref': 'k1', 'sha': k1.code_sha256[:12]}, 'input': {'literal': '3.25', 'value': 3.25}}
    assert P.ungrounded_inputs(ctx(events, cand('42'))) == []                     # no compute produced it
    assert len(P.ungrounded_inputs(ctx(events, cand('42')), scope='all')) == 1


def test_competing_candidates_excludes_harness_driven_changes():
    assert values(P.competing_candidates(ctx([], cand('12.5', alt='13.1'))), 'alt') == [
        {'answer': '13.1', 'source': '--alt'}]
    assert P.competing_candidates(ctx([], cand('12.5', alt='12.50'))) == []       # same after normalisation
    own = [cand('13.1', ref='a1')]
    assert values(P.competing_candidates(ctx(own, cand('12.5'))), 'alt') == [{'answer': '13.1', 'source': 'a1'}]
    told = [cand('13.1', ref='a1'), note('n1', '[harness:gate] calc x uses ...\nKeep: submit ...')]
    assert P.competing_candidates(ctx(told, cand('12.5'))) == []
    accepted = [cand('13.1', ref='a1'), note('n1', P.ACCEPT_PREFIX + ' "13.1". End your turn ...')]
    assert P.competing_candidates(ctx(accepted, cand('12.5')))
    dup = [cand('13.1', ref='a1'), cand('13.10', ref='a2')]
    assert len(P.competing_candidates(ctx(dup, cand('12.5', alt='13.1')))) == 1

# ---------------------------------------------------------------------------------------------
# offers
# ---------------------------------------------------------------------------------------------


def test_unscale_and_use_alt_offers():
    k = comp('k1', [0.123456])
    c = ctx([k], cand('12.35%'), C.QuestionSpec(percent_form='decimal', decimals=(4,)))
    (b,) = P.scaled_100(c)
    res = O.unscale(c, b)
    assert res.answer == '0.1235' and 'printed 0.123456' in res.derivation
    c = ctx([k], cand('about 12.35 percent'), C.QuestionSpec(percent_form='decimal'))
    assert O.unscale(c, P.scaled_100(c)[0]).answer == 'about 0.1235 percent'   # d = 2 + 2 without spec
    alt = O.use_alt(ctx([], cand('1', alt='2')), C.Binding({'alt': {'answer': '2', 'source': '--alt'}}))
    assert alt.answer == '2' and alt.derivation == 'your --alt'
    assert O.use_alt(ctx([], cand('2')), C.Binding({'alt': {'answer': '2.0', 'source': 'a1'}})) is None


def test_recompute_substitutes_scaled_and_signed_literals():
    code = 'a = 1.2345\nb = -50.5\nprint(round(a * 1000 + b, 2))'
    k = comp('k1', [1184.0], code=code, inputs=[
        {'literal': '1.2345', 'value': 1.2345, 'span': [4, 10], 'observe': 'c1', 'match': 'scaled', 'scale': -3},
        {'literal': '50.5', 'value': 50.5, 'span': [16, 20], 'observe': 'c2', 'match': 'exact'}])
    events = [obs('c1', 1234.5, locator='t.txt:1'), obs('c2', -50.5, locator='t.txt:2'),
              obs('c3', 1300.0, locator='t.txt:9'), obs('c4', 20.0, locator='t.txt:10'), k]
    runner = LocalRunner()
    c = ctx(events, cand('1184.00'), C.QuestionSpec(decimals=(2,)), runner=runner)
    res = O.recompute(c, C.Binding({'substitutions': [{'from': 'c1', 'to': 'c3'}, {'from': 'c2', 'to': 'c4'}]}))
    # 1300 -> 1.3 (same 10^-3 scale); -50.5 -> +20 (sign flip against the source value)
    assert runner.codes[-1] == 'a = 1.3\nb = -(-20)\nprint(round(a * 1000 + b, 2))'
    assert res.answer == '1320.00' and res.compute_sha256 == C.sha256_text(runner.codes[-1])
    assert 't.txt:9 in place of t.txt:1' in res.derivation
    # rule args naming binding paths instead of a substitutions list
    res2 = O.recompute(c, C.Binding({'cell': {'ref': 'c1'}, 'later': {'ref': 'c3'}}), **{'from': 'cell', 'to': 'later'})
    assert res2.answer == '1249.50'
    # explicit value substitutions (pack index lookups)
    res3 = O.recompute(c, C.Binding({'substitutions': [{'from': 'c1', 'value': 1200.0, 'label': 'idx:7'}]}))
    assert res3.answer == '1149.50' and 'idx:7' in res3.derivation


def test_recompute_refuses_truncated_code_failures_and_no_change():
    k = comp('k1', [3.0], code='print(1.5 * 2)', inputs=[
        {'literal': '1.5', 'value': 1.5, 'span': [6, 9], 'observe': 'c1', 'match': 'exact'}])
    trunc = C.Compute(**{**k.__dict__, 'code_sha256': k.code_sha256[:12]})
    events = [obs('c1', 1.5), obs('c2', 2.5)]
    sub = C.Binding({'substitutions': [{'from': 'c1', 'to': 'c2'}]})
    assert O.recompute(ctx(events + [trunc], cand('3')), sub) is None
    assert O.recompute(ctx(events + [k], cand('3')), sub).answer == '5'
    same = C.Binding({'substitutions': [{'from': 'c1', 'value': 1.5}]})
    assert O.recompute(ctx(events + [k], cand('3')), same) is None
    boom = comp('k1', [3.0], code='print(1.5 * 2); raise SystemExit(3)', inputs=k.inputs)
    assert O.recompute(ctx(events + [boom], cand('3')), sub) is None

# ---------------------------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------------------------


def rule(id, preds, finding='f {x}', offer=None, priority=50, triggers=None, enabled=True):
    return C.Rule(id=id, when=[{'pred': p, 'args': a} for p, a in preds], finding=finding, offer=offer,
                  priority=priority, triggers=triggers or [], enabled=enabled)


def test_join_on_shared_keys_and_priority_order():
    preds = {'p1': lambda c: [C.Binding({'x': 1, 'y': 'a'}), C.Binding({'x': 2, 'y': 'b'})],
             'p2': lambda c: [C.Binding({'x': 2, 'z': 'z2'})],
             'p3': lambda c: [C.Binding({'x': 3})]}
    eng = E.RuleEngine(predicates=preds)
    rules = [rule('r_late', [('p1', {}), ('p2', {})], finding='{y}/{z}', priority=60),
             rule('r_none', [('p1', {}), ('p3', {})]),
             rule('r_first', [('p2', {})], finding='first {z}', priority=5)]
    findings, trace = eng.evaluate_trace(ctx([], cand('1')), rules)
    assert [f.rule for f in findings] == ['r_first', 'r_late'] and findings[1].text == 'b/z2'
    assert {t['rule']: t['status'] for t in trace} == {'r_first': 'fired', 'r_none': 'silent', 'r_late': 'fired'}


def test_template_rendering_is_safe_and_bounded():
    vals = {'v': {'text': '12.5', 'n': 0.1 + 0.2}, 'long': 'x' * 500, 'brace': '{v.text}', 'obj': {'a': 1}}
    assert E.render_template('a {v.text} b {v.n}', vals) == 'a 12.5 b 0.3'
    assert E.render_template('{v.__class__} {missing.key} {obj}', vals) == '? ? ?'
    assert E.render_template('{0} {v:>999} {v.text!r} {{v.text}}', vals) == '{0} {v:>999} {v.text!r} {12.5}'
    assert E.render_template('{brace}', vals) == '{v.text}'                      # values are not re-expanded
    out = E.render_template('{long} {long} {long}', vals)
    assert len(out.encode()) <= 200 and out.endswith('…')


def test_triggers_are_keywords_not_regex():
    assert E.trigger_match('What was the North region total?', ['north region'])
    assert not E.trigger_match('What was the Northern total?', ['north'])
    assert E.trigger_match('anything', ['*']) and E.trigger_match('anything', [])
    assert not E.trigger_match('a.b', ['.*'])
    eng = E.RuleEngine(predicates={'p': lambda c: [C.Binding({'x': 1})]})
    r = rule('r', [('p', {})], triggers=['south'])
    assert eng.evaluate(ctx([], cand('1'), question='north only'), [r]) == []
    assert eng.evaluate(ctx([], cand('1'), question='the South region'), [r])


def test_unknown_names_errors_and_failed_offers_are_isolated():
    def boom(c):
        raise RuntimeError('bad predicate')
    preds = {'ok': lambda c: [C.Binding({'x': 1})], 'boom': boom, 'bad_type': lambda c: [{'x': 1}]}
    offers = {'none': lambda c, b: None}
    eng = E.RuleEngine(predicates=preds, offers=offers)
    rules = [rule('unknown_pred', [('nope', {})]), rule('unknown_offer', [('ok', {})], offer={'op': 'nope'}),
             rule('raises', [('boom', {})]), rule('bad_return', [('bad_type', {})]),
             rule('needs_offer', [('ok', {})], finding='take {offer.answer}', offer={'op': 'none'}),
             rule('optional_offer', [('ok', {})], finding='x={x}', offer={'op': 'none'}),
             rule('disabled', [('ok', {})], enabled=False), rule('fine', [('ok', {})])]
    findings, trace = eng.evaluate_trace(ctx([], cand('1')), rules)
    status = {t['rule']: t['status'] for t in trace}
    assert status == {'unknown_pred': 'n/a', 'unknown_offer': 'n/a', 'raises': 'error', 'bad_return': 'error',
                      'needs_offer': 'offer_failed', 'optional_offer': 'fired_no_offer', 'disabled': 'disabled',
                      'fine': 'fired'}
    assert [f.rule for f in findings] == ['fine', 'optional_offer']            # same priority: by id


def test_pack_predicates_are_found_but_cannot_shadow_core():
    class Pack(RawPack):
        def predicates(self):
            return {'count_mismatch': lambda c: [C.Binding({'count': {'expected': 9, 'got': 9}})],
                    'pack_only': lambda c: [C.Binding({'x': 'pack'})]}
    c = ctx([], cand('1, 2'), C.QuestionSpec(n_values=1), pack=Pack())
    found = E.RuleEngine().evaluate(c, [rule('core', [('count_mismatch', {})], finding='{count.expected}'),
                                        rule('pack', [('pack_only', {})], finding='{x}')])
    assert {f.rule: f.text for f in found} == {'core': '1', 'pack': 'pack'}


def test_core_seed_rules_end_to_end_on_a_small_context():
    k = comp('k1', [0.123456], code='print((101.0 - 88.0) / 88.0)')
    c = ctx([obs('c1', 101.0), obs('c2', 88.0), k], cand('12.35', alt='0.12'),
            C.QuestionSpec(n_values=1, percent_form='decimal', decimals=(4,)))
    findings = E.evaluate(c, R.core_seed_rules())
    assert [f.rule for f in findings] == ['scale', 'competing_candidates', 'recompute_final']
    assert findings[0].offer.answer == '0.1235'
    assert findings[0].text.startswith('Your answer 12.35 is 100 times the output 0.123456 of calc')

# ---------------------------------------------------------------------------------------------
# rules and policy files, package boundary
# ---------------------------------------------------------------------------------------------


def test_rule_validation():
    core = (P.REGISTRY, O.REGISTRY)
    for r in R.core_seed_rules():
        assert R.validate_rule(r, predicates=core[0], offers=core[1]) == [], r.id
    from packs.toy.pack import PACK, toy_seed_rules
    for r in toy_seed_rules():
        assert R.validate_rule(r, predicates={**core[0], **PACK.predicates()}, offers=core[1]) == []
    bad = C.Rule(id='Bad-Id', when=[{'pred': 'nope', 'args': {'f': object()}}], finding='x' * 201,
                 offer={'op': 'nope'}, priority=500, triggers=['a.*b'])
    errors = ' | '.join(R.validate_rule(bad, predicates=core[0], offers=core[1]))
    for piece in ('id must', 'unknown predicate', 'exceeds 200', 'unknown offer', 'priority', 'triggers'):
        assert piece in errors
    stray = C.Rule(id='stray', when=[{'pred': 'count_mismatch', 'args': {}}], finding='{count.got:>9}')
    assert 'only {dotted.name}' in ' '.join(R.validate_rule(stray, predicates=core[0], offers=core[1]))
    assert R.rendered_text_bytes(R.core_seed_rules()) < 1000


def test_rule_files_round_trip_and_policy(tmp_path):
    for r in R.core_seed_rules():
        R.save_rule(tmp_path, r)
    assert [r.id for r in R.load_rules(tmp_path)] == sorted(r.id for r in R.core_seed_rules())
    (tmp_path / 'other.json').write_text((tmp_path / 'scale.json').read_text())
    with pytest.raises(ValueError):
        R.load_rules(tmp_path)
    with pytest.raises(ValueError):
        R.rule_from_dict({'id': 'x', 'when': [], 'finding': 'f', 'code': 'import os'})
    assert R.effective_policy({'gate_rounds_max': 3})['gate_rounds_max'] == 3
    assert R.validate_policy({'gate_rounds_max': 9, 'secret': 1, 'disabled_rules': 'x'}) == [
        'gate_rounds_max must be an integer in [0, 3]', 'unknown policy key secret', 'disabled_rules must be a list of rule ids']
    with pytest.raises(ValueError):
        R.effective_policy({'loop_threshold': 1})


def _imports(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def test_harness_core_is_benchmark_neutral_and_stdlib_only():
    allowed = set(sys.stdlib_module_names) | {'harness_core', '__future__'}
    for path in sorted((ROOT / 'harness_core').rglob('*.py')):
        for name in _imports(path):
            top = name.split('.')[0]
            assert top != 'packs', f'{path.name} imports {name}'
            assert top in allowed, f'{path.name} imports non-stdlib {name}'


def test_numbers_stated_in_the_question_ground_calc_literals():
    """Review fix: a constant typed from the question (3.5%, a year) is not 'a value no tool showed'."""
    inputs = [{'literal': lit, 'value': v, 'span': [0, 1], 'observe': None, 'match': None}
              for lit, v in (('1955', 1955.0), ('0.035', 0.035), ('1.035', 1.035), ('77.25', 77.25))]
    k1 = comp('k1', [5.5], inputs=inputs)
    q = 'By what factor would the 1955 total grow at 3.5% per year?'
    got = P.ungrounded_inputs(ctx([k1], cand('5.5'), question=q))
    assert [b.values['input']['literal'] for b in got] == ['77.25']


def test_malformed_priority_sorts_last_instead_of_crashing():
    engine = E.RuleEngine(predicates={'yes': lambda c: [C.Binding({'x': 1})]})
    bad = C.Rule(id='bad', when=[{'pred': 'yes', 'args': {}}], finding='bad {x}', priority='high')
    good = C.Rule(id='good', when=[{'pred': 'yes', 'args': {}}], finding='good {x}', priority=90)
    found = engine.evaluate(ctx([], cand('1')), [bad, good])
    assert [f.rule for f in found] == ['good', 'bad']


def test_save_rule_replaces_a_planted_symlink_instead_of_writing_through(tmp_path):
    target = tmp_path / 'framework_file.txt'
    target.write_text('keep me')
    rules_dir = tmp_path / 'rules'
    rules_dir.mkdir()
    (rules_dir / 'scale.json').symlink_to(target)
    rule = next(r for r in R.core_seed_rules() if r.id == 'scale')
    R.save_rule(rules_dir, rule)
    assert target.read_text() == 'keep me' and not (rules_dir / 'scale.json').is_symlink()
    assert [r.id for r in R.load_rules(rules_dir)] == ['scale']
    with pytest.raises(ValueError):
        R.save_rule(rules_dir, C.Rule(id='../escape', when=[], finding='f'))


def test_count_mismatch_honours_allowed_words():
    """S2 request 3 (contracts v9.1 QuestionSpec.allowed_words): quoted words may fill value slots."""
    spec = C.QuestionSpec(n_values=2, allowed_words=('surplus', 'deficit'))
    assert P.count_mismatch(ctx([], cand('[0.35, surplus]'), spec)) == []
    assert P.count_mismatch(ctx([], cand('0.35 (Deficit)'), spec)) == []
    assert P.count_mismatch(ctx([], cand('[0.35, 0.36]'), spec)) == []           # numbers alone still count
    assert values(P.count_mismatch(ctx([], cand('0.35'), spec)), 'count') == [{'expected': 2, 'got': 1}]
    assert values(P.count_mismatch(ctx([], cand('0.35 surplus deficit'), spec)), 'count') == [{'expected': 2, 'got': 3}]
    # a word the answer merely contains does not break a count the numbers already satisfy
    one = C.QuestionSpec(n_values=1, allowed_words=('total',))
    assert P.count_mismatch(ctx([], cand('Total: 12.5'), one)) == []
    assert P.allowed_word_hits('surpluses, surplus-ish; SURPLUS', ('surplus',)) == 1
    assert P.count_mismatch(ctx([], cand('surplus'), C.QuestionSpec(n_values=1))) \
        and not P.count_mismatch(ctx([], cand('surplus'), C.QuestionSpec(n_values=1, allowed_words=('surplus',))))


def test_enforcement_notes_wrapping_an_accept_are_not_findings():
    """The controller records the enforce feedback it sends; it wraps the gate message, so an 'accepted'
    notice (requery_when_clean) must not count as a finding after the final candidate."""
    wrapped_accept = note('n2', 'Your final answer was not submitted with `submit`, so it was checked now:\n'
                                f'{P.ACCEPT_PREFIX} "13.1". End your turn with exactly this answer.', reason='enforce')
    wrapped_finding = note('n2', 'Your final answer was not submitted with `submit`, so it was checked now:\n'
                                 '[harness:gate] 13.1 is not any calc output', reason='enforce')
    final = cand('13.1', ref='a1', via='final')
    assert values(P.competing_candidates(ctx([final, wrapped_accept], cand('12.5'))), 'alt') == [
        {'answer': '13.1', 'source': 'a1'}]
    assert P.competing_candidates(ctx([final, wrapped_finding], cand('12.5'))) == []
