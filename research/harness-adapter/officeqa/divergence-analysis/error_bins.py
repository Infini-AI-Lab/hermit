"""Add a coarse, value-free error bin for every wrong GLM answer to index.json (compares with the
reference privately; writes only the bin label)."""
import json
import math
import re
from pathlib import Path

B = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent
ROOTS = {'none': ('officeqa-pro133-none-v8-work', 'baseline'), 'prompt': ('officeqa-pro133-prompt-v8-work', 'adapted'),
         'cards': ('officeqa-pro133-cards-v8-work', 'adapted')}
key = {json.loads(l)['task_id']: json.loads(l)['answer'] for l in open(B / ROOTS['none'][0] / 'prepared/private/answers.jsonl')}
NUM = re.compile(r'-?\d[\d,]*\.?\d*(?:[eE][-+]?\d+)?')


def nums(text):
    out = []
    for m in NUM.findall(str(text)):
        try: out.append(float(m.replace(',', '')))
        except ValueError: pass
    return out


def bin_of(pred, ref):
    if not str(pred).strip(): return 'empty'
    p, r = nums(pred), nums(ref)
    if len(p) != len(r):  # "[123,456]" is two values, not one number with a thousands comma
        alt = [float(x) for x in re.findall(r'-?\d+\.?\d*(?:[eE][-+]?\d+)?', str(pred))]
        if len(alt) == len(r): p = alt
    if not r: return 'non_numeric_reference'
    if not p: return 'non_numeric_prediction'
    if len(p) != len(r): return f'count_mismatch({len(p)} vs {len(r)})'
    worst = 0.0
    kinds = set()
    for a, b in zip(p, r):
        if b == 0:
            err = abs(a)
        else:
            err = abs(a - b) / abs(b)
            if a != 0 and a * b < 0 and abs(abs(a) - abs(b)) / abs(b) < 0.01: kinds.add('sign_flip')
            if a != 0 and a * b > 0:
                ratio = math.log10(abs(a) / abs(b))
                if abs(ratio) > 0.9 and abs(ratio - round(ratio)) < 0.01: kinds.add('scale_10^%d' % round(ratio))
        worst = max(worst, err)
    if kinds: return '+'.join(sorted(kinds))
    for limit, label in ((1e-9, 'numerically_equal(format?)'), (0.001, 'within_0.1pct'), (0.01, 'within_1pct'),
                         (0.05, '1-5pct'), (0.25, '5-25pct')):
        if worst <= limit: return label
    return '>25pct'


index = json.loads((OUT / 'index.json').read_text())
for e in index:
    for arm, (root, name) in ROOTS.items():
        pred = json.loads((B / root / 'runs' / name / f'{e["ordinal"]:03}' / 'prediction.json').read_text())['answer']
        e['arms'][arm]['error_bin'] = None if e['correct'][arm] else bin_of(pred, key[e['task_id']])
(OUT / 'index.json').write_text(json.dumps(index, indent=1, ensure_ascii=False) + '\n')
from collections import Counter
focus = [e for e in index if e['correct']['opus_final']]
print('Opus-right questions, GLM wrong-answer bins:')
for arm in ROOTS:
    print(' ', arm, dict(Counter(e['arms'][arm]['error_bin'] for e in focus if not e['correct'][arm]).most_common()))
