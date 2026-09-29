"""Read-only operational status. Never reads questions, predictions or gold."""
from pathlib import Path
from datetime import datetime, timezone
import json

ROOT = Path(__file__).resolve().parent
def read(path):
    try: return json.loads(path.read_text())
    except (OSError, ValueError): return {}

def status():
    state = read(ROOT / 'state.json')
    result = {'status':state.get('status'), 'arms':{}, 'learning':[]}
    if state.get('started_utc'):
        result['minutes'] = round((datetime.now(timezone.utc)-datetime.fromisoformat(state['started_utc'])).total_seconds()/60,1)
    for arm in ('baseline','adapted'):
        rows = state.get('arms',{}).get(arm,{}).get('results',[])
        folders = sorted((ROOT/'runs'/arm).glob('[0-9][0-9][0-9]'))
        current = folders[-1] if folders else None
        attempts=[read(p) for p in (ROOT/'runs'/arm).glob('*/api/*/attempts/*/transport.json')]
        result['arms'][arm]={'ended':len(rows),'statuses':{v:sum(r.get('status')==v for r in rows) for v in sorted({r.get('status','unknown') for r in rows})},
          'current':current.name if current else None,'current_calls':len(list((current/'api').glob('*/request.json'))) if current else 0,
          'rate_refusals':sum(a.get('status')=='rate_refused' for a in attempts),'waiting':sum(a.get('phase')=='waiting' for a in attempts)}
    learned=state.get('learning',[])
    result['learning']={'ended':len(learned),'admitted':sum(bool(x['activated']) for x in learned),
                        'last':{k:learned[-1].get(k) for k in ('block','status','model_calls')} if learned else None}
    if state.get('active_learning_block'):
        block=state['active_learning_block'];files=list((ROOT/'evolution'/f'{block:02}').glob('sessions/*/control/fireworks-result.json'))
        result['active_learning']={'block':block}
        if files:
            current=read(files[0]);result['active_learning'].update({k:current.get(k) for k in ('model_calls','requested_tools','in_flight_request','rate_refusals')})
    return result

if __name__=='__main__':print(json.dumps(status(),ensure_ascii=False))
