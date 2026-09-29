import json, collections, glob, os
BASE='/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd'
ROOTS={'none':'officeqa-pro133-none-v8-work/runs/baseline','prompt':'officeqa-pro133-prompt-v8-work/runs/adapted','cards':'officeqa-pro133-cards-v8-work/runs/adapted'}
rows=json.load(open('rows.json'))
idx=json.load(open(f'{BASE}/officeqa-divergence-analysis/index.json'))
correct={}
for e in idx:
    for arm in ROOTS: correct[(arm,e['ordinal'])]=e['correct'][arm]
by=collections.defaultdict(list)
for r in rows: by[(r['arm'],r['q'])].append(r)
R=[]
for arm,rel in ROOTS.items():
    for run in sorted(glob.glob(f'{BASE}/{rel}/[0-9][0-9][0-9]')):
        q=int(os.path.basename(run))
        s=json.load(open(f'{run}/summary.json'))
        rs=by[(arm,q)]
        ncomp=sum(1 for r in rs if r.get('n_tools')==0)
        heads=0; meta=0
        for l in open(f'{run}/records/events.jsonl'):
            if '"type": "error"' in l or '"type":"error"' in l:
                e=json.loads(l); m=(e.get('item') or {}).get('message','')
                if m.startswith('Heads up: Long threads'): heads+=1
                if 'fallback metadata' in m: meta+=1
        maxin=max([a.get('in') or 0 for a in rs] or [0])
        R.append(dict(arm=arm,q=q,status=s['status'],elapsed=s['elapsed_seconds'],requests=s['requests'],ncomp=ncomp,heads=heads,meta=meta,correct=correct[(arm,q)],agents=s.get('agents_md_bytes'),answer_empty=not (s.get('answer') or '').strip(),in_total=s['usage']['input_tokens']))
json.dump(R,open('runs.json','w'))
C=collections.Counter
print('meta warning present', C(r['meta'] for r in R))
print('ncomp vs heads', C((r['ncomp'],r['heads']) for r in R))
