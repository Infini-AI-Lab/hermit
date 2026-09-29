"""GLM-only full native learning (MWM or prompt-only), with no inference/session budget.

The controller grades the sealed block for the learner, drives one post-mortem
phase per failed episode, then replays every lesson's checks before admission.
"""
import asyncio
import importlib
import json
from pathlib import Path
import shutil
import sys
import time

import experiment
import learning as legacy
from learning import MODEL, GENERIC_SOURCE, ROOT, save, utc, snapshot, revision, digest
from paths import REPOSITORY, DEPLOYMENT, VENDOR
import postmortem

FEEDBACK_PATHS = ('scratch/feedback.json', 'scratch/scratch/feedback.json', 'adapter/scratch/feedback.json')


def validate_source(source, block):
    before = snapshot(source)
    if Path(source) == GENERIC_SOURCE.resolve():
        return before, {'kind':'generic_'+experiment.ARCHITECTURE+'_architecture', 'revision':revision(before)}
    receipt = Path(source).parents[2] / 'result.json'
    prior = json.loads(receipt.read_text())
    if (block <= 1 or prior.get('profile') != experiment.PROFILE or not prior.get('activated')
            or prior.get('candidate') != str(source) or prior.get('revision') != revision(before)
            or not 0 < prior.get('block',0) < block or prior.get('model') != MODEL):
        raise ValueError('Source must be this campaign\'s previous admitted GLM candidate')
    return before, {'kind':'previous_glm_candidate', 'receipt_sha256':digest(receipt.read_bytes())}


async def conversation(key, connection, session, prompt, transport, *, phases=(), extras=None):
    from jsonschema import Draft202012Validator
    declarations = (await connection.list_tools()).tools
    tools = {t.name:t for t in declarations}
    schemas = {n:getattr(t,'input_schema',None) or getattr(t,'inputSchema') for n,t in tools.items()}
    functions = [{'type':'function','function':{'name':n,'description':t.description or '',
                  'parameters':schemas[n]}} for n,t in sorted(tools.items())]
    if extras is not None:
        # Controller checkers answered here, never by the sandbox broker.
        schemas.update(postmortem.TOOLS); functions += postmortem.DECLARATIONS
    save(session/'control/fireworks-tools.json',functions)
    messages = [{'role':'user','content':prompt}]
    queue, current = list(phases), None
    state = {'status':'running','model_calls':0,'requested_tools':0,'successful_tools':0,
             'usage':{'prompt_tokens':0,'completion_tokens':0,'cached_tokens':0},
             'usage_complete':True,'usage_unknown_calls':[],'wire_attempts':0,'rate_refusals':0,
             'scheduler_wait_seconds':0,'response_models':[],'compactions':0,
             'expected_candidate':snapshot(session/'adapter'), 'tool_provenance':[],
             'last_prompt_tokens':0,'phases_completed':0,'phases_total':len(queue)}
    def checkpoint(): save(session/'control/fireworks-result.json',state)
    async def call(history, purpose, **options):
        state['model_calls'] += 1
        number = state['model_calls']; state['in_flight_request']=number; checkpoint()
        response, meta = await asyncio.to_thread(transport.complete,key,history,timeout=None,unbounded=True,**options)
        state.pop('in_flight_request',None)
        usage=meta.get('usage',{})
        for k in ('prompt_tokens','completion_tokens'):
            if legacy.token_count(usage.get(k)): state['usage'][k]+=usage[k]
        cached=(usage.get('prompt_tokens_details') or {}).get('cached_tokens',0)
        if legacy.token_count(cached): state['usage']['cached_tokens']+=cached
        if meta.get('usage_complete') is False:
            state['usage_complete']=False; state['usage_unknown_calls'].append(number)
        state['wire_attempts']+=meta.get('wire_attempts',1)
        state['rate_refusals']+=sum(a.get('status')=='rate_refused' for a in meta.get('attempts',[]))
        state['scheduler_wait_seconds']+=sum(a.get('wait_seconds',0) for a in meta.get('attempts',[]))
        public_history=[legacy.public_message(m) if m.get('role')=='assistant' else m for m in history]
        record={'request':number,'purpose':purpose,'requested_model':MODEL,'meta':meta,
                'public_request_sha256':revision(public_history),'public_message_count':len(history),
                'request_max_tokens':None,'reasoning_effort':None}
        choice=(response.get('choices') or [{}])[0] if response else {}
        message=choice.get('message') or {}
        record.update(message=legacy.public_message(message),finish_reason=choice.get('finish_reason'))
        save(session/f'control/fireworks-calls/{number:03}.json',record)
        checkpoint()
        if response is None: raise RuntimeError('GLM transport failed; see recorded provider metadata')
        actual=meta.get('response_model') or response.get('model')
        if actual != MODEL or message.get('role') != 'assistant' or meta.get('http_status') != 200:
            raise ValueError('Unexpected model or response')
        if actual not in state['response_models']: state['response_models'].append(actual)
        state['last_prompt_tokens']=usage.get('prompt_tokens',0)
        return message, choice.get('finish_reason')
    while True:
        # A rolling context, not a token budget: the same learner continues.
        if state['last_prompt_tokens'] >= 600000:
            summary, finish = await call(messages+[{'role':'user','content':
                'Write a checkpoint for your own continuation: evidence inspected, facts versus hypotheses, files changed, tests performed, open work and next actions. Do not call tools in this checkpoint. The original assignment and all files remain available.'}], 'glm_context_checkpoint')
            text=summary.get('content') or ''
            state['compactions']+=1
            (session/f'control/glm-checkpoint-{state["compactions"]:02}.md').write_text(text)
            resume='Continue the same learning session from your own checkpoint. Re-read files whenever needed.'
            if current is not None: resume+='\n\nCurrent phase:\n'+current
            messages=[{'role':'user','content':prompt}, {'role':'assistant','content':text}, {'role':'user','content':resume}]
            state['last_prompt_tokens']=0
        message, finish=await call(messages,'learner',tools=functions)
        messages.append(legacy.continuing_message(message))
        calls=message.get('tool_calls') or []
        if not calls:
            if finish == 'length':
                messages.append({'role':'user','content':'The provider ended that response at its output limit. Continue the same session; files and tools are unchanged.'})
                continue
            if finish == 'stop' and queue:
                state['phases_completed']+=1
                (session/f'control/fireworks-report-{state["phases_completed"]:02}.md').write_text(message.get('content') or '')
                current=queue.pop(0); messages.append({'role':'user','content':current})
                checkpoint(); continue
            state['status']='completed' if finish=='stop' else 'provider_incomplete'
            (session/'control/fireworks-report.md').write_text(message.get('content') or '')
            break
        for tool_call in calls:
            state['requested_tools']+=1; index=state['requested_tools']
            function=tool_call.get('function',{}); name=function.get('name'); args=None
            before=snapshot(session/'adapter')
            if before != state['expected_candidate']: raise ValueError('Unattributed candidate change before GLM tool operation')
            record={'index':index,'request':state['model_calls'],'name':name,'tool_call_id':tool_call['id'],'started_utc':utc()}
            try:
                if name not in schemas: raise ValueError('Unknown native capability')
                args=json.loads(function.get('arguments') or '{}')
                Draft202012Validator(schemas[name]).validate(args)
                if name in postmortem.TOOLS:
                    text, ok=postmortem.call(name,args,extras)
                elif name=='small_model':
                    response, _=await call(args['messages'],'glm_requested_reader')
                    text=json.dumps(legacy.public_message(response)); ok=True
                else:
                    reply=await connection.call_tool(name,args)
                    text='\n'.join(i.text for i in reply.content if i.type=='text')
                    ok=not (getattr(reply,'is_error',False) or getattr(reply,'isError',False))
                    for item in reply.content:
                        if item.type=='image':
                            # The same model receives the image after this tool result.
                            record.setdefault('images',[]).append({'mime_type':item.mimeType,'data':item.data})
                state['successful_tools']+=int(ok)
                record.update(ok=ok,response=text)
            except Exception as exc:
                text=json.dumps({'error':str(exc).replace(key,'[REDACTED]')}); record.update(ok=False,response=text)
            after=snapshot(session/'adapter')
            state['tool_provenance'].append({'index':index,'before':revision(before),'after':revision(after)})
            state['expected_candidate']=after
            record.update(arguments=args,candidate_before=revision(before),candidate_after=revision(after))
            images=record.pop('images',[])
            save(session/f'control/fireworks-tool-results/{index:03}.json',record)
            messages.append({'role':'tool','tool_call_id':tool_call['id'],'content':text})
            if images:
                # Append after every pending tool result below, for valid tool sequencing.
                state.setdefault('_pending_images',[]).extend(images)
            checkpoint()
        images=state.pop('_pending_images',[])
        if images:
            messages.append({'role':'user','content':[{'type':'image_url','image_url':{'url':'data:'+i['mime_type']+';base64,'+i['data']}} for i in images]})
        checkpoint()
    checkpoint(); return state


async def bridge(key, session, prompt, transport, *, phases=(), extras=None):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env=legacy.broker_environment(session)
    import os
    if os.environ.get('OFFICEQA_DOCKER_HOST'):
        env['OFFICEQA_DOCKER_HOST']=os.environ['OFFICEQA_DOCKER_HOST']
    env['PYTHONPATH']=str(ROOT)+':'+str(REPOSITORY)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'full_sandbox.py'),str(session)],
        env=env,cwd=str(session/'host'))
    with (session/'control/fireworks-mcp-stderr.log').open('w') as err:
        async with stdio_client(params,errlog=err) as (read,write):
            async with ClientSession(read,write,read_timeout_seconds=None) as connection:
                await connection.initialize()
                return await conversation(key,connection,session,prompt,transport,phases=phases,extras=extras)


def previous_replay(block):
    prior=ROOT/'evolution'/f'{block-1:02}/result.json'
    if block<=1 or not prior.is_file(): return None
    path=Path(json.loads(prior.read_text()).get('session') or '/nonexistent')/'control/postmortem-replay.json'
    return json.loads(path.read_text()) if path.is_file() else None


def released_evidence(session, manifest, block):
    dirs={e['ordinal']:session/'evidence'/e['directory'] for e in manifest['episodes']}
    for bb in range(1,block):
        for d in sorted((ROOT/'evidence'/f'{bb:02}').glob('[0-9][0-9][0-9]')):
            if d.is_dir() and int(d.name)<=block*5: dirs[int(d.name)]=d
    return dirs


def run_learning(key, source, evidence, output, block):
    source,evidence,output=(Path(p).resolve() for p in (source,evidence,output))
    if output.exists(): raise FileExistsError('Learning trial already exists')
    output.mkdir(parents=True); start=time.monotonic()
    result={'status':'rejected','activated':False,'block':block,'model':MODEL,'source':str(source),
            'candidate':str(source),'started_utc':utc(),'profile':experiment.PROFILE,'architecture':experiment.ARCHITECTURE,
            'admission_mode':'automatic_mechanical_only','transport':'fireworks'}
    try:
        before,lineage=validate_source(source,block)
        evidence_before,manifest=legacy.validate_evidence(evidence,block)
        seal=legacy.validate_batch_seal(evidence,manifest,block)
        environment=output/'document-tools'; environment.mkdir()
        shutil.copy2(ROOT/'prepared/agent/corpus.sqlite',environment/'corpus.sqlite')
        shutil.copy2(ROOT/'officeqa_tools.py',environment/'officeqa.py')
        corpus=ROOT/'prepared/agent/corpus'; index=environment/'corpus.sqlite'
        pm=output/'postmortem'
        verdicts=postmortem.grade_block(evidence,pm,block,VENDOR/'reward.py')
        shelf=experiment.SHELF
        read_paths=[str(corpus),str(environment),str(pm)]+([str(shelf)] if shelf else [])
        extra=(f'\nPaths (read_path/list_path or run_command):\nCorpus: {corpus}\nRead-only helper: {environment}/officeqa.py\n'
               f'Index: {index}\nUse OfficeQACorpus(Path(corpus),Path(index)) with search/read/list_files, or read the TXT files directly.\n'
               f'Block grades and failure records: {pm}\n'
               +(f'Reference shelf: {shelf} (read {shelf}/INDEX.md first; run_command(\'grep -n -i "term" {shelf}/*.txt\'); information only, never a proof)\n' if shelf else 'Reference shelf: none this run.\n')
               +f'Served context now: {json.dumps(postmortem.served_bytes(source,experiment.ARCHITECTURE))}\n')
        goal=postmortem.panel(verdicts,previous_replay(block))+'\n'+postmortem.goal_text(experiment.ARCHITECTURE)+extra
        phases=postmortem.phases(verdicts)+[postmortem.SERVE_PHASE]
        save(output/'request.json',{'goal':goal,'source_lineage':lineage,'episode_ids':[e['id'] for e in manifest['episodes']],
            'budgets':{'seconds':None,'completion_tokens':None,'per_response':None,'model_calls':None},'model':MODEL,
            'controller_batch_seal':seal,'read_paths':read_paths,'grades_released':True,'phases':len(phases),
            'architecture':experiment.ARCHITECTURE,'reference_shelf':str(shelf) if shelf else None})
        from harnessadapter.evolution.session import evolve
        from harnessadapter.evolution.definition import EvolverDefinition
        from harnessadapter.policy.execution import ExecutionPolicy
        from full_sandbox import install
        install()
        definition=EvolverDefinition('mwm') if experiment.MWM else EvolverDefinition('basic',prompt_override=postmortem.PROMPT_ONLY_OPENER)
        prepared=evolve(source,evidence,goal,timeout=None,prepare_only=True,block=block,tick=block,
            policy=ExecutionPolicy(backend='codex',network=False,read_paths=tuple(read_paths)),
            definition=definition,output=output/'sessions',model=MODEL,deployment=DEPLOYMENT,
            source_kind='architecture' if source==GENERIC_SOURCE.resolve() else 'adapter')
        session=Path(prepared['session']); result['session']=str(session)
        prompt=(session/'control/prompt.md').read_text(); (output/'exact-prompt.md').write_text(prompt)
        result['native_prepare_result']=str(session/'control/result.json')
        extras={'evidence':session/'evidence','manifest':manifest,'corpus':corpus,'index':index,'reward':VENDOR/'reward.py'}
        state=asyncio.run(bridge(key,session,prompt,importlib.import_module('client'),phases=phases,extras=extras))
        for name in ('model_calls','requested_tools','successful_tools','usage','usage_complete','usage_unknown_calls',
                     'wire_attempts','rate_refusals','scheduler_wait_seconds','response_models','compactions','phases_completed','phases_total'):
            result[name]=state[name]
        result['learner_status']=state['status']
        from harnessadapter.evolution.workspace import Workspace, fingerprint
        from harnessadapter.paths import PACKAGE_ROOT
        base=json.loads((session/'control/baseline.json').read_text())
        workspace=Workspace(session); candidate=session/'adapter'
        provenance=snapshot(candidate)==state['expected_candidate']
        protected={'source':snapshot(source)==before,'external_evidence':snapshot(evidence)==evidence_before,
            'framework':fingerprint(PACKAGE_ROOT,exclude=('evolvers','architectures'))==base['framework'],
            'protected_core':workspace.protected_files(fingerprint(candidate))==base['protected']}
        for area in ('evidence','method','environment','history'): protected[area]=fingerprint(session/area)==base[area]
        legacy.validate_batch_seal(session/'evidence',manifest,block)
        native_finalize=definition.finalize(candidate,session)
        # Controller edits after the learner: replay rewrites lesson status lines
        # and a feedback file left inside the candidate moves to scratch.
        replay=postmortem.replay(candidate,experiment.ARCHITECTURE,block,released_evidence(session,manifest,block),corpus,index,VENDOR/'reward.py')
        feedback=next((session/p for p in FEEDBACK_PATHS if (session/p).is_file()),None)
        feedback_moved=None
        if feedback is not None and feedback.is_relative_to(candidate):
            feedback_moved=str(feedback); feedback=session/'scratch/feedback.json'
            shutil.move(feedback_moved,feedback)
            if not any((candidate/'scratch').iterdir()): (candidate/'scratch').rmdir()
        controller_edits={'before':revision(state['expected_candidate']),'after':revision(snapshot(candidate))}
        validation=workspace.validate()
        failures=[]
        if state['status']!='completed': failures.append('Learner did not complete')
        if not all(protected.values()): failures.append('Protected data changed')
        if not validation['passed']: failures.append('Native structural validation failed')
        if not provenance or state['response_models']!=[MODEL]: failures.append('Model/operation provenance failed')
        if not state['successful_tools']: failures.append('No successful native tools')
        if not experiment.MWM and any((candidate/n).read_bytes()!=(GENERIC_SOURCE/n).read_bytes() for n in ('adapter.json','EVOLVER.md')):
            failures.append('adapter.json or EVOLVER.md changed')
        public_feedback={'path':str(feedback),'sha256':digest(feedback.read_bytes()),'moved_from':feedback_moved} if feedback else None
        admission={'admitted':not failures,'failures':failures,'protected_unchanged':protected,
            'operation_provenance_matches':provenance,'controller_edits':controller_edits,'validation':validation,
            'native_finalize':native_finalize,'postmortem':replay,'human_semantic_gate':False,'model':MODEL,
            'public_feedback':public_feedback,'feedback_missing':public_feedback is None,'revision':revision(snapshot(candidate)),
            'note':'Native gates, replayed lesson checks and feedback format are reported, not a new semantic adoption filter.'}
        save(session/'control/mechanical-admission.json',admission)
        result.update(admission=str(session/'control/mechanical-admission.json'),failures=failures,
                      revision=admission['revision'],public_feedback=public_feedback,
                      postmortem={k:replay[k] for k in ('verified','candidate','demoted','promoted','wrong','records')})
        if not failures:
            from harnessadapter.adapter.versioning import commit_all
            commit=commit_all(candidate,f'OfficeQA full {experiment.ARCHITECTURE}: GLM block {block}')
            result.update(status='admitted',activated=True,candidate=str(candidate),candidate_commit=commit)
    except Exception as exc:
        result['error']=(type(exc).__name__+': '+str(exc)).replace(key,'[REDACTED]')
        result.setdefault('learner_status','failed')
        if result.get('session'):
            path=Path(result['session'])/'control/fireworks-result.json'
            if path.is_file():
                partial=json.loads(path.read_text())
                for name in ('model_calls','requested_tools','usage','usage_complete','usage_unknown_calls','wire_attempts','rate_refusals','scheduler_wait_seconds'):
                    result[name]=partial.get(name)
    result.update(finished_utc=utc(),elapsed_seconds=round(time.monotonic()-start,3))
    save(output/'result.json',result); return result
