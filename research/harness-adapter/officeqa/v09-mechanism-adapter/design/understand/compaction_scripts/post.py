import json, collections, glob, os, re, statistics as st
BASE='/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd'
ROOTS={'none':'officeqa-pro133-none-v8-work/runs/baseline','prompt':'officeqa-pro133-prompt-v8-work/runs/adapted','cards':'officeqa-pro133-cards-v8-work/runs/adapted'}
rows=json.load(open('rows.json'))
by=collections.defaultdict(list)
for r in rows: by[(r['arm'],r['q'])].append(r)
PREFIX='Another language model started to solve this problem'
out=[]
def msgs(inp):
    res=[]
    for it in inp:
        if it.get('type')=='message':
            res.append((it.get('role'), ''.join(c.get('text') or '' for c in it.get('content') or [])))
        else: res.append((it.get('type'),None))
    return res
for (arm,q),rs in by.items():
    rs.sort(key=lambda r:r['n'])
    comps=[r for r in rs if r.get('n_tools')==0]
    if not comps: continue
    run=f"{BASE}/{ROOTS[arm]}/{q:03d}/api"
    first=msgs(json.load(open(f"{run}/001/request.json"))['input'])
    for k,c in enumerate(comps):
        nxt=[r for r in rs if r['n']==c['n']+1]
        if not nxt: out.append(dict(arm=arm,q=q,n=c['n'],k=k+1,post=None)); continue
        nx=nxt[0]
        req=json.load(open(f"{run}/{nx['n']:03d}/request.json"))
        m=msgs(req['input'])
        creq=msgs(json.load(open(f"{run}/{c['n']:03d}/request.json"))['input'])
        summaries=[t for role,t in m if role=='user' and t and t.startswith(PREFIX)]
        s=summaries[-1][len(PREFIX):] if summaries else ''
        # summary text as produced in the compaction response
        resp=json.load(open(f"{run}/{c['n']:03d}/response.json"))
        rtext=''.join(p.get('text','') for it in resp.get('output',[]) if it.get('type')=='message' for p in it.get('content',[]))
        out.append(dict(arm=arm,q=q,n=c['n'],k=k+1,post=nx['n'],
            post_items=[role for role,t in m],
            agents_same = (m[1]==first[1]),
            dev_same = (m[0]==first[0]),
            task_same = (m[2]==first[2]),
            n_summaries=len(summaries),
            comp_req_summaries=sum(1 for role,t in creq if role=='user' and t and t.startswith(PREFIX)),
            summary_chars=len(rtext), summary_in_post = rtext.strip()[:200] in (summaries[-1] if summaries else ''),
            post_in=nx.get('in'), comp_in=c.get('in'), comp_out=c.get('out'),
            fileline=len(re.findall(r'\.txt[:\s,]*(?:line|lines|L)?\s*\d+', rtext)) , txtrefs=len(re.findall(r'treasury_bulletin_\d{4}_\d{2}\.txt', rtext)),
            nums=len(re.findall(r'\d[\d,]*\.\d+|\d{1,3}(?:,\d{3})+', rtext)),
            has_candidate=bool(re.search(r'candidate|answer\s*(is|=|:)|final answer|result', rtext, re.I)),
            agents_len=len(first[1][1] or ''),
            ))
json.dump(out,open('post.json','w'))
C=collections.Counter
done=[o for o in out if o['post']]
print('compactions',len(out),'with post request',len(done))
print('post item shapes', C(tuple(o['post_items']) for o in done).most_common(5))
print('agents same', C(o['agents_same'] for o in done), 'dev same', C(o['dev_same'] for o in done), 'task same', C(o['task_same'] for o in done))
print('n summaries in post', C(o['n_summaries'] for o in done), 'summaries inside compaction req', C(o['comp_req_summaries'] for o in done))
print('summary in post', C(o['summary_in_post'] for o in done))
sc=sorted(o['summary_chars'] for o in done); print('summary chars min/med/max', sc[0], sc[len(sc)//2], sc[-1])
so=sorted(o['comp_out'] for o in done); print('comp out tokens min/med/max', so[0], so[len(so)//2], so[-1])
pi=sorted(o['post_in'] for o in done); print('post-compaction input tokens min/med/max', pi[0], pi[len(pi)//2], pi[-1])
print('summaries with any file:line', sum(o['fileline']>0 for o in done), 'with bulletin file refs', sum(o['txtrefs']>0 for o in done), 'with numbers', sum(o['nums']>0 for o in done), 'candidate words', sum(o['has_candidate'] for o in done))
for arm in ROOTS:
    x=[o for o in done if o['arm']==arm]
    print(arm, 'agents_len set', sorted(set(o['agents_len'] for o in x))[:5], '...', max(o['agents_len'] for o in x), 'post_in med', sorted(o['post_in'] for o in x)[len(x)//2])
