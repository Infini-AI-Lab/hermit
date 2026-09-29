import json,glob,statistics as st
P='/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/'
ARMS={'none':'baseline','prompt':'adapted','cards':'adapted'}
secs=[]; last=[]; el=[]; late=0; comp_runs=0
out_tok=[]
for arm,sub in ARMS.items():
    for n in range(1,134):
        base=f'{P}officeqa-pro133-{arm}-v8-work/runs/{sub}/{n:03}'
        s=json.load(open(base+'/summary.json'))
        calls=sorted(glob.glob(base+'/api/[0-9][0-9][0-9]/transport.json'))
        for c in calls:
            t=json.load(open(c)); 
            if t.get('seconds') is not None: secs.append(t['seconds'])
        if s['status']=='completed' and calls:
            t=json.load(open(calls[-1])); last.append(t.get('seconds') or 0)
            el.append(s['elapsed_seconds'])
            if s['elapsed_seconds']>3600-300: late+=1
def q(xs,p): xs=sorted(xs); return xs[min(len(xs)-1,int(p*len(xs)))]
print('per-call seconds p50 %.1f p90 %.1f p99 %.1f max %.0f n=%d'%(q(secs,.5),q(secs,.9),q(secs,.99),max(secs),len(secs)))
print('final call seconds (completed runs) p50 %.1f p90 %.1f p99 %.1f'%(q(last,.5),q(last,.9),q(last,.99)))
print('completed elapsed p50 %.0f p90 %.0f p99 %.0f; completed runs ending within last 300 s: %d/%d'%(q(el,.5),q(el,.9),q(el,.99),late,len(el)))
