"""Aggregate per-question/*.json and reverse/*.json into counts (value-free)."""
import json
from collections import Counter, defaultdict
from pathlib import Path
OUT = Path(__file__).resolve().parent
A = {int(p.stem): json.loads(p.read_text()) for p in sorted((OUT / 'per-question').glob('*.json'))}
R = {int(p.stem): json.loads(p.read_text()) for p in sorted((OUT / 'reverse').glob('*.json'))}
print('type A files', len(A), '| type R files', len(R))
stage, stage_arm, seen, doubt, adapter, levels, gen, lvl_first = Counter(), defaultdict(Counter), Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
text_fixable = Counter()
failures = []
for o, q in A.items():
    for arm, a in q.get('arms', {}).items():
        if a.get('correct'): continue
        fd = a.get('first_divergence') or {}
        s = fd.get('stage', '?'); stage[s] += 1; stage_arm[arm][s] += 1
        seen[(a.get('saw_correct_evidence') or {}).get('value', '?')] += 1
        doubt[(a.get('glm_expressed_doubt') or {}).get('value', '?')] += 1
        ac = a.get('adapter_context') or {}
        if arm != 'none':
            adapter[(arm, 'relevant' if ac.get('relevant_item') not in (None, 'none', 'n/a', '') else 'no item',
                     'followed=' + str(ac.get('followed')), 'caused=' + str(ac.get('caused_or_contributed_to_error')))] += 1
        rem = a.get('remedies') or []
        for i, r in enumerate(rem):
            levels[r.get('level')] += 1; gen[r.get('generality')] += 1
            if i == 0: lvl_first[r.get('level')] += 1
        failures.append((o, arm, s))
    text_fixable[q.get('text_vs_mechanism', '')[:0]] += 0
print('failures analysed', len(failures))
print('first-divergence stage:', dict(stage.most_common()))
for arm in ('none', 'prompt', 'cards'): print('  ', arm, dict(stage_arm[arm].most_common()))
print('saw correct evidence:', dict(seen)); print('expressed doubt:', dict(doubt))
print('remedy levels (all):', dict(levels.most_common())); print('remedy level (first-listed):', dict(lvl_first.most_common()))
print('remedy generality:', dict(gen.most_common()))
print('adapter context on wrong adapted runs:')
for k, v in adapter.most_common(): print('  ', v, k)
rs = Counter((r.get('opus_divergence') or {}).get('stage') for r in R.values())
print('reverse: Opus divergence stages', dict(rs), '| principled/luck', dict(Counter(str(r.get('right_by_principled_choice_or_luck', ''))[:9] for r in R.values())))
