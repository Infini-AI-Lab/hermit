import json, collections, statistics as st
BASE='/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd'
ROOTS={'none':'officeqa-pro133-none-v8-work/runs/baseline','prompt':'officeqa-pro133-prompt-v8-work/runs/adapted','cards':'officeqa-pro133-cards-v8-work/runs/adapted'}
rows=json.load(open('rows.json'))
by=collections.defaultdict(list)
for r in rows: by[(r['arm'],r['q'])].append(r)
def cmds(path):
    inp=json.load(open(path))['input']
    out=[]
    for it in inp:
        if it.get('type')=='function_call':
            try: a=json.loads(it.get('arguments') or '{}')
            except Exception: a={'raw':it.get('arguments')}
            out.append(a.get('cmd') or json.dumps(a,sort_keys=True))
    return out
res=[]
for (arm,q),rs in by.items():
    rs.sort(key=lambda r:r['n'])
    comps=[r['n'] for r in rs if r.get('n_tools')==0]
    if not comps: continue
    api=f"{BASE}/{ROOTS[arm]}/{q:03d}/api"
    last=rs[-1]['n']
    # windows: segment i = commands in compaction request i (since previous compaction)
    segs=[]
    for c in comps: segs.append(cmds(f"{api}/{c:03d}/request.json"))
    tail_req = last if last not in comps else None
    tail=cmds(f"{api}/{tail_req:03d}/request.json") if tail_req and tail_req>comps[-1] else []
    for i,c in enumerate(comps):
        pre=segs[i]
        cnt=collections.Counter(pre)
        post=segs[i+1] if i+1<len(segs) else tail
        seen=set(x for s in segs[:i+1] for x in s)
        res.append(dict(arm=arm,q=q,n=c,k=i+1,pre_n=len(pre),pre_distinct=len(cnt),pre_maxrep=max(cnt.values()) if cnt else 0,
                        post_n=len(post),post_repeat_of_pre=sum(1 for x in post if x in seen)))
json.dump(res,open('loops.json','w'))
print('compactions analysed',len(res))
dr=[r['pre_distinct']/r['pre_n'] for r in res if r['pre_n']]
print('pre-window commands median',st.median(r['pre_n'] for r in res),'distinct ratio median %.2f'%st.median(dr))
print('pre-window max identical repeats: >=10:',sum(r['pre_maxrep']>=10 for r in res),'>=5:',sum(r['pre_maxrep']>=5 for r in res),'>=3:',sum(r['pre_maxrep']>=3 for r in res),'of',len(res))
print('distinct ratio <0.5:',sum(1 for x in dr if x<0.5))
pr=[r for r in res if r['post_n']>0]
print('post windows',len(pr),'post cmds that repeat a pre-compaction cmd: total %d of %d'%(sum(r['post_repeat_of_pre'] for r in pr),sum(r['post_n'] for r in pr)), 'median frac %.2f'%st.median(r['post_repeat_of_pre']/r['post_n'] for r in pr))
# non-compacted runs: max identical repeat in final request
nonc=[]
for (arm,q),rs in by.items():
    if any(r.get('n_tools')==0 for r in rs): continue
    rs.sort(key=lambda r:r['n'])
    api=f"{BASE}/{ROOTS[arm]}/{q:03d}/api"
    c=cmds(f"{api}/{rs[-1]['n']:03d}/request.json")
    cnt=collections.Counter(c)
    nonc.append(max(cnt.values()) if cnt else 0)
print('non-compacted runs',len(nonc),'max identical repeats >=10:',sum(x>=10 for x in nonc),'>=5:',sum(x>=5 for x in nonc),'>=3:',sum(x>=3 for x in nonc))
