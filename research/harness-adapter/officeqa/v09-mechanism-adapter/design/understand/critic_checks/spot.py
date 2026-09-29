import json, os, re, glob, collections
P='/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/'
idx={r['ordinal']:r for r in json.load(open(P+'officeqa-divergence-analysis/index.json'))}
ARMS={'none':'baseline','prompt':'adapted','cards':'adapted'}
out={}
tot_cmds=0
comp_stats=collections.Counter()
max_in=0; min_comp=10**9; max_comp=0
streak_runs=collections.Counter(); streak_right=collections.Counter()
third_runs=collections.Counter(); third_right=collections.Counter()
timeouts=collections.Counter(); tmo_comp=0
sibling_hits=[]
for arm,sub in ARMS.items():
    root=f'{P}officeqa-pro133-{arm}-v8-work/runs/{sub}'
    for n in range(1,134):
        base=f'{root}/{n:03}'
        right=idx[n]['correct'][arm]
        s=json.load(open(base+'/summary.json'))
        # compaction via transport + request tools
        comps=0
        for call in sorted(glob.glob(base+'/api/[0-9][0-9][0-9]')):
            t=call+'/transport.json'
            if not os.path.exists(t): continue
            tr=json.load(open(t))
            ins=[ (a.get('usage') or {}).get('input_tokens') or 0 for a in tr.get('attempts',[])]
            mi=max(ins) if ins else 0
            max_in=max(max_in,mi)
            with open(call+'/request.json','rb') as f:
                raw=f.read()
            if b'"tools": []' in raw:
                comps+=1
                if mi: min_comp=min(min_comp,mi); max_comp=max(max_comp,mi)
        if s['status']=='timeout':
            timeouts[arm]+=1
            if comps: tmo_comp+=1
        comp_stats[(arm,'comp' if comps else 'nocomp',right)]+=1
        # commands
        cmds=[]; outs=[]
        own=f'/runs/{sub}/{n:03}'
        for line in open(base+'/records/events.jsonl'):
            try: e=json.loads(line)
            except: continue
            it=e.get('item',{})
            if e.get('type')=='item.started' and it.get('type')=='command_execution':
                cmds.append(' '.join(it.get('command','').split()))
            if e.get('type')=='item.completed' and it.get('type')=='command_execution':
                o=it.get('aggregated_output') or ''
                for m in re.finditer(r'officeqa-pro133-(none|prompt|cards)-v8-work/runs/(baseline|adapted)/(\d{3})', o):
                    if m.group(3)!=f'{n:03}' or m.group(1)!=arm:
                        sibling_hits.append((arm,n,m.group(0)))
        tot_cmds+=len(cmds)
        c=collections.Counter()
        fired=False
        for x in cmds:
            c[x]+=1
            if c[x]==3: fired=True
        if fired: third_runs[arm]+=1; third_right[arm]+=right
        st=1; best=1
        for i in range(1,len(cmds)):
            st = st+1 if cmds[i]==cmds[i-1] else 1
            best=max(best,st)
        if cmds and best>=3: streak_runs[arm]+=1; streak_right[arm]+=right
print('total commands (item.started)',tot_cmds)
print('max attempt input',max_in,'compaction attempt input range',min_comp,max_comp)
print('timeouts',dict(timeouts),'timeouts that compacted',tmo_comp)
for arm in ARMS:
    print(arm,'comp right/total',comp_stats[(arm,'comp',1)],'/',comp_stats[(arm,'comp',1)]+comp_stats[(arm,'comp',0)],
          'nocomp right/total',comp_stats[(arm,'nocomp',1)],'/',comp_stats[(arm,'nocomp',1)]+comp_stats[(arm,'nocomp',0)])
print('streak>=3 runs',dict(streak_runs),'right',dict(streak_right))
print('3rd-issue runs',dict(third_runs),'right',dict(third_right))
print('sibling path hits',len(sibling_hits), sorted(set((a,n) for a,n,_ in sibling_hits))[:20], sibling_hits[:5])
