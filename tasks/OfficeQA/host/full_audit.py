"""Saved-artifact consistency audit for the full profiles (MWM and prompt-only); no model calls."""
from collections import Counter
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone


def build(root):
    root=Path(root); checks=[]
    def read(p): return json.loads(Path(p).read_text())
    def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    def check(name,passed): checks.append({'name':name,'passed':bool(passed)})
    # freeze.json is written with campaign.snapshot (sha per file); learning.snapshot has another shape.
    from campaign import snapshot
    from experiment import ACTOR_SECONDS
    state=read(root/'state.json'); frozen=read(root/'freeze.json')
    config=read(root/'.task-paths.json'); manifest=read(root/'prepared/manifest.json')
    profile=str(config.get('profile')); architecture='prompt_only' if 'prompt_only' in profile else 'mwm'
    check('full profile',profile.startswith('full_') and state.get('profile')==profile)
    for name,expected in frozen['files'].items(): check('frozen '+name,sha(root/name)==expected)
    check('framework unchanged',snapshot(Path(config['framework'])/'harnessadapter')==frozen['framework_files'])
    check('generic W0 unchanged',snapshot(Path(config['framework'])/'harnessadapter/architectures'/architecture)==frozen['generic_W0'])
    check('deployment unchanged',snapshot(root/'deployment')==frozen['deployment_files'])
    if frozen.get('reference_shelf') is not None:
        check('reference shelf unchanged',snapshot(root/'reference-shelf')==frozen['reference_shelf'])
    expected=manifest['task_ids']; model='accounts/fireworks/models/glm-5p3-flash'
    check('100 completed originals',state['status']=='completed' and all(len(state['arms'][a]['results'])==50 for a in ('baseline','adapted')))
    for arm in ('baseline','adapted'):
        rows=state['arms'][arm]['results']
        check(arm+' same ordered 50',[r['task_id'] for r in rows]==expected)
        for row in rows:
            task=root/'runs'/arm/f'{row["ordinal"]:03}'
            check(f'{arm}/{row["ordinal"]} prediction unchanged',sha(task/'prediction.json')==row['prediction_sha256'])
            invocation=read(task/'records/invocation.json')
            check(f'{arm}/{row["ordinal"]} recorded actor wall clock',invocation.get('timeout_seconds')==ACTOR_SECONDS)
            settings=invocation.get('settings',{})
            provider=next((v for k,v in settings.items() if k.startswith('model_providers.')),{})
            check(f'{arm}/{row["ordinal"]} retries with accounting, never a cut',
                (provider.get('request_max_retries') or 0)>0 and (provider.get('stream_max_retries') or 0)>0)
            if row.get('agents_md_bytes') is not None:
                check(f'{arm}/{row["ordinal"]} served AGENTS.md within the recorded byte limit',
                    (settings.get('project_doc_max_bytes') or 0)>=row['agents_md_bytes'])
            transport=read(task/'api/summary.json')
            limits=transport['limits']
            check(f'{arm}/{row["ordinal"]} no transport budgets',all(k in limits and limits[k] is None for k in
                ('requests','completion_tokens','per_response','attempts_per_request','request_timeout_seconds')))
            for request in (task/'api').glob('*/request.json'):
                value=read(request)
                check(str(request.relative_to(root))+' fixed unbounded model',value.get('model')==model and not set(value)&
                    {'max_output_tokens','max_tokens','max_completion_tokens','thinking','reasoning','previous_response_id'})
                meta=read(request.parent/'transport.json')
                check(str(request.relative_to(root))+' fixed upstream',meta.get('upstream')=='https://api.fireworks.ai/inference/v1/responses')
    for round in state.get('learning',[]):
        block=round['block']
        if round.get('status')=='skipped_unused':
            check(f'round {block} skipped without reference release',not (root/'private-learning-seals'/f'{block:02}.json').exists()
                and not (root/'evidence'/f'{block:02}').exists() and not round.get('activated'))
            continue
        seal=read(root/'private-learning-seals'/f'{block:02}.json')
        wanted=expected[(block-1)*5:block*5]
        check(f'round {block} exact answer scope',[e['id'] for e in seal['episodes']]==wanted)
        check(f'round {block} answers released after sealing',seal['sealed_utc']<=seal['reference_released_utc'])
        for entry in seal['episodes']:
            original=root/'runs/adapted'/f'{entry["ordinal"]:03}'/'prediction.json'
            reference=root/'evidence'/f'{block:02}'/f'{entry["ordinal"]:03}'/'reference_answer.json'
            check(f'round {block} sealed {entry["ordinal"]}',sha(original)==entry['prediction_sha256'] and sha(reference)==entry['reference_answer_sha256'])
        if round.get('session'):
            session=Path(round['session'])
            for p in (session/'control/fireworks-calls').glob('*.json'):
                value=read(p)
                check(str(p.relative_to(root))+' exact GLM/no limit',value.get('requested_model')==model and value.get('request_max_tokens') is None and value.get('reasoning_effort') is None)
                actual=value.get('meta',{}).get('response_model')
                check(str(p.relative_to(root))+' no model substitution',actual in (model,None))
            # Lessons are served only while their checks replay (absent file = legacy round).
            replay=session/'control/postmortem-replay.json'
            if replay.is_file():
                check(f'round {block} verified lessons passed their replay',all(
                    lesson.get('passed') for lesson in read(replay).get('lessons',[]) if lesson.get('status_after')=='verified'))
            if round['activated']:
                admission=read(session/'control/mechanical-admission.json')
                check(f'round {block} automatic integrity admission',admission['admitted'] and admission['operation_provenance_matches'] and
                    admission['human_semantic_gate'] is False and all(admission['protected_unchanged'].values()))
                if replay.is_file() and round.get('candidate'):
                    # The served candidate carries exactly the statuses the replay decided.
                    import postmortem
                    decided={l['id']:l.get('status_after') for l in read(replay).get('lessons',[])}
                    served={l['id']:l['status'] for l in postmortem.parse_lessons(Path(round['candidate']),architecture)}
                    check(f'round {block} served lesson statuses match the replay',all(served.get(i)==s for i,s in decided.items()))
                    edits=admission.get('controller_edits') or {}
                    check(f'round {block} controller edits end at the admitted revision',edits.get('after')==admission.get('revision'))
    counts=Counter('passed' if x['passed'] else 'failed' for x in checks)
    result={'created_utc':datetime.now(timezone.utc).isoformat(),'passed':not counts['failed'],'profile':profile,
            'architecture':architecture,'counts':dict(counts),'checks':checks,
            'scope':'Saved source/request/seal/admission/replay consistency; OS enforcement is separately tested; no independent network attestation.'}
    path=root/'preflight/final-audit.json'; path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result,indent=2)+'\n')
    return {'passed':result['passed'],'counts':result['counts'],'path':str(path)}


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    result=build(p.parse_args().root);print(json.dumps(result));raise SystemExit(0 if result['passed'] else 1)
