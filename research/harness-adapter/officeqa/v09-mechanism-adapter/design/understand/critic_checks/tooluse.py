import json,re,collections
P='/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/'
ARMS={'none':'baseline','prompt':'adapted','cards':'adapted'}
for arm,sub in ARMS.items():
    c=collections.Counter(); runs=collections.Counter(); tot=0
    for n in range(1,134):
        seen=set()
        for line in open(f'{P}officeqa-pro133-{arm}-v8-work/runs/{sub}/{n:03}/records/events.jsonl'):
            try: e=json.loads(line)
            except: continue
            it=e.get('item',{})
            if e.get('type')=='item.started' and it.get('type')=='command_execution':
                cmd=it.get('command',''); tot+=1
                for k,pat in (('officeqa.py',r'officeqa\.py|OfficeQACorpus|import officeqa'),('grep',r'\bgrep\b'),('rg',r'(^|[\s;|&(])rg\s'),('sed/head/tail/cat',r'\b(sed|head|tail|cat|awk)\b'),('python',r'\bpython3?\b'),('sqlite3',r'sqlite3')):
                    if re.search(pat,cmd): c[k]+=1; seen.add(k)
        for k in seen: runs[k]+=1
    print(arm,'commands',tot,{k:(c[k],runs[k]) for k in c})
