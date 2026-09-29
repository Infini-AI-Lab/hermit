import json, collections
BASE='/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd'
ROOTS={'none':'officeqa-pro133-none-v8-work/runs/baseline','prompt':'officeqa-pro133-prompt-v8-work/runs/adapted','cards':'officeqa-pro133-cards-v8-work/runs/adapted'}
rows=json.load(open('rows.json')); A=json.load(open('attempts.json'))
fin={}
for a in A:
    if a['status']=='succeeded': fin[(a['arm'],a['q'],a['n'])]=(a['inn'],a['out'])
by=collections.defaultdict(list)
for r in rows: by[(r['arm'],r['q'])].append(r)
trig=[];non=[]
for (arm,q),rs in by.items():
    rs.sort(key=lambda r:r['n'])
    for i in range(1,len(rs)):
        p,c=rs[i-1],rs[i]
        if p.get('n_tools')==0: continue
        pu=fin.get((arm,q,p['n']))
        if not pu: continue
        tot=pu[0]+pu[1]
        if tot<225000: continue
        api=f"{BASE}/{ROOTS[arm]}/{q:03d}/api"
        pi=json.load(open(f"{api}/{p['n']:03d}/request.json"))['input']
        ci=json.load(open(f"{api}/{c['n']:03d}/request.json"))['input']
        new=ci[len(pi):]
        if c.get('n_tools')==0: new=new[:-1]
        # estimate bytes of new items excluding assistant function_call (already in output tokens)
        nb=sum(len(json.dumps(it)) for it in new if it.get('type')=='function_call_output')
        nball=sum(len(json.dumps(it)) for it in new)
        rec=(tot, tot+nb//4, tot+nball//4, arm, q, p['n'])
        (trig if c.get('n_tools')==0 else non).append(rec)
trig.sort(); non.sort()
print('triggers',len(trig),'min in+out',trig[0][0],'min in+out+outputs/4',min(t[1] for t in trig),'min in+out+all/4',min(t[2] for t in trig))
print('non-triggers (in+out>=225K)',len(non),'max in+out',max(t[0] for t in non),'max in+out+outputs/4',max(t[1] for t in non),'max in+out+all/4',max(t[2] for t in non))
print("non-trig est>244800", sum(t[1]>244800 for t in non), "trig est<244900", sum(t[1]<244900 for t in trig))
print('highest non-triggers by est', sorted(non,key=lambda t:t[1])[-4:])
