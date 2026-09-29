import json, os, glob, sys
BASE='/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd'
ROOTS={'none':'officeqa-pro133-none-v8-work/runs/baseline','prompt':'officeqa-pro133-prompt-v8-work/runs/adapted','cards':'officeqa-pro133-cards-v8-work/runs/adapted'}
rows=[]
for arm,rel in ROOTS.items():
    for run in sorted(glob.glob(f'{BASE}/{rel}/[0-9][0-9][0-9]')):
        q=int(os.path.basename(run))
        for api in sorted(glob.glob(f'{run}/api/[0-9][0-9][0-9]')):
            n=int(os.path.basename(api))
            r={'arm':arm,'q':q,'n':n}
            try:
                t=json.load(open(f'{api}/transport.json'))
            except Exception as e:
                r['transport_err']=str(e); rows.append(r); continue
            u=t.get('usage') or {}
            r['in']=u.get('input_tokens'); r['out']=u.get('output_tokens')
            r['status']=t.get('response_status'); r['http']=t.get('http_status')
            atts=t.get('attempts') or []
            r['n_att']=len(atts)
            r['att']=[{k:a.get(k) for k in ('attempt','http_status','response_status','status','retryable','stream_errors','usage','phase','error','defect','provider_defect','failure','reason') if k in a} for a in atts]
            r['att_keys']=sorted(set(k for a in atts for k in a))
            try:
                q_=json.load(open(f'{api}/request.json'))
                tools=q_.get('tools')
                r['n_tools']=len(tools) if isinstance(tools,list) else None
                inp=q_.get('input') or []
                r['n_items']=len(inp)
                r['req_bytes']=os.path.getsize(f'{api}/request.json')
                # last user message text
                last=None
                for it in inp:
                    if it.get('type')=='message' and it.get('role')=='user':
                        last=it
                txt=''
                if last:
                    txt=' '.join(c.get('text','') for c in last.get('content') or [] if isinstance(c,dict))
                r['last_user_head']=txt[:160]
                r['last_user_len']=len(txt)
                r['last_item_type']=(inp[-1].get('type'),inp[-1].get('role')) if inp else None
                r['has_compaction_item']=any(it.get('type') in ('compaction','compaction_summary') for it in inp)
                r['item_types']=sorted(set(str(it.get('type'))+':'+str(it.get('role')) for it in inp))
            except Exception as e:
                r['req_err']=str(e)
            rows.append(r)
json.dump(rows,open('rows.json','w'))
print(len(rows))
