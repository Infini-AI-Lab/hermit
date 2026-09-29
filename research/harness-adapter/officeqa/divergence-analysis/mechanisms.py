"""Cluster the analysts' remedies into mechanism families (keyword rules) and count, per family,
how many distinct failed arm-runs it would address (first-listed remedy and any remedy)."""
import json, re
from collections import Counter, defaultdict
from pathlib import Path
OUT = Path(__file__).resolve().parent
FAM = [
 ('loop_guard', r'loop|repeat|identical command|thrash|breaker'),
 ('ledger_survives_compaction', r'compaction|ledger|persist|survive|decision record|candidate record|saved state'),
 ('forced_commit_budget', r'forced? commit|commit before|timeout|budget|deadline'),
 ('answer_format_scale_check', r'format|scale|x ?100|×100|percent|answer clause|bare|digit count|rounding'),
 ('recompute_provenance_check', r'recompute|provenance|cited cell|file:line|executed command|must (appear|come) from|last printed|traced|printed cell'),
 ('period_date_label_check', r'as-of|date|month|fiscal|calendar|period|year label|sum(s)? to|add(s)? up|annual (row|total)|label'),
 ('all_printings_vintage_tool', r'printing|vintage|revision|across issues|every issue|later issue'),
 ('table_grid_extractor', r'extractor|grid|realign|fill[- ]down|header[- ]aligned|flattened|column header|named[- ]cell|row label and a column'),
 ('table_finder_catalog', r'catalog|table[- ]title|phrase[- ]to[- ]header|header lookup|list(s)? every matching table|exact[- ]phrase|search over table'),
 ('external_reference_data', r'cpi|external|reference series|exchange rate lookup|vetted'),
 ('numeric_env', r'numpy|statsmodels|library|calculator|numeric environment|vetted .*helper'),
 ('interpretation_fork', r'both readings|every candidate reading|competing readings|interpretation fork|tie-?break|each qualifier|compute both'),
 ('adapter_scoping', r'scope|card|lesson|quarantine'),
 ('second_attempt_or_review', r'second (independent )?attempt|reviewer|reconcil|self-consistency|majority'),
 ('unit_direction_check', r'unit|direction|reciprocal|invariant|sanity|magnitude'),
]
A = {int(p.stem): json.loads(p.read_text()) for p in sorted((OUT / 'per-question').glob('*.json'))}
first, anyf, lvl = Counter(), Counter(), defaultdict(Counter)
loops, compaction = Counter(), 0
per_run = {}
for o, q in A.items():
    for arm, a in q['arms'].items():
        if a.get('correct'): continue
        rem = a.get('remedies') or []
        fams_any = set()
        for i, r in enumerate(rem):
            text = (r.get('intervention', '') + ' ' + r.get('why', '')).lower()
            fams = {name for name, pat in FAM if re.search(pat, text)}
            for f in fams: lvl[f][r.get('level')] += 1
            if i == 0:
                for f in fams: first[f] += 1
            fams_any |= fams
        for f in fams_any: anyf[f] += 1
        per_run[(o, arm)] = sorted(fams_any)
        op = a.get('operational') or {}
        loops[op.get('loop_or_thrash', '?')] += 1
        if re.search(r'compact', json.dumps(a).lower()): compaction += 1
n = len(per_run)
print('failed arm-runs', n)
print(f'{"family":30} first-listed  any-listed  levels')
for name, _ in FAM:
    print(f'{name:30} {first[name]:5}        {anyf[name]:5}      {dict(lvl[name].most_common(3))}')
print('loop/thrash on failed runs:', dict(loops), '| runs mentioning context compaction:', compaction)
json.dump({f'{o:03}-{a}': v for (o, a), v in per_run.items()}, open(OUT / 'mechanism_map.json', 'w'), indent=0)
