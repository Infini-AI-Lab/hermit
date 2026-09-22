"""GLM-only full native MWM learning, with no inference/session budget."""
import asyncio
import importlib
import json
from pathlib import Path
import shutil
import sys
import time

import learning as legacy
from learning import MODEL, GENERIC_SOURCE, ROOT, save, utc, snapshot, revision, digest
from paths import REPOSITORY, DEPLOYMENT

GOAL = '''Evolve the MWM adapter from the five just-completed GLM acting episodes.
Their original submissions are already sealed. ONLY those five reference answers
are now released in evidence; no future question/answer, baseline trace, source
mapping, grader rationale or human/Astra critique is supplied. Compare the actual
attempts with those answers and use that feedback before later questions.

All native MWM learning layers are available: memory, worked examples, briefing,
investigations, tested Python tools and agent-role specifications. The usual MWM
method and immutable serving contract apply. There is no task-specific Markdown
allowlist, 6,000-character briefing cap or mandatory full-trajectory reading gate.
You can search/read the same complete permitted corpus as actors and execute
local calculations to investigate the completed attempts. The deployment and
environment documents identify the corpus and helper. Native test_python/captest
runs real Linux bubblewrap inside a networkless container. Native roles are
delivered to the next Codex actors. small_model, when used, calls the exact same
GLM model, never a different model.

Record your own public feedback in scratch/feedback.json with label
glm_reference_answer_feedback and an episodes array. For each completed episode
include id, observations, reference_comparison, uncertainty and proposed_lesson.
Describe what you actually verified and keep unverified causes distinguishable.
This record is for inspection, not a human quality gate. Learn through the native
MWM process; there is no human-authored case diagnosis or solution guidance.

No elapsed-time, cumulative input/output-token, model-call, tool-call or dollar
budget terminates this session. Fireworks token/reasoning limit parameters are
omitted; provider defaults and intrinsic context/output limits still apply.
If the conversation approaches its context window, GLM itself makes a checkpoint
summary and continues with the same files and tools. This does not end learning.
Finish when your work is complete, with a public summary of feedback and changes.
The candidate is admitted by integrity/structure checks, not by its quality or
benchmark score. Never alter sealed evidence, scoring, controller files, the
fixed model/transport or other trials. Network tools cannot access the internet.
'''


def validate_source(source, block):
    before = snapshot(source)
    if Path(source) == GENERIC_SOURCE.resolve():
        return before, {'kind':'generic_mwm_architecture', 'revision':revision(before)}
    receipt = Path(source).parents[2] / 'result.json'
    prior = json.loads(receipt.read_text())
    if (block <= 1 or prior.get('profile') != 'full_mwm_unbounded' or not prior.get('activated')
            or prior.get('candidate') != str(source) or prior.get('revision') != revision(before)
            or not 0 < prior.get('block',0) < block or prior.get('model') != MODEL):
        raise ValueError('Source must be this campaign\'s previous admitted GLM candidate')
    return before, {'kind':'previous_glm_candidate', 'receipt_sha256':digest(receipt.read_bytes())}


async def conversation(key, connection, session, prompt, transport):
    from jsonschema import Draft202012Validator
    declarations = (await connection.list_tools()).tools
    tools = {t.name:t for t in declarations}
    schemas = {n:getattr(t,'input_schema',None) or getattr(t,'inputSchema') for n,t in tools.items()}
    functions = [{'type':'function','function':{'name':n,'description':t.description or '',
                  'parameters':schemas[n]}} for n,t in sorted(tools.items())]
    save(session/'control/fireworks-tools.json',functions)
    messages = [{'role':'user','content':prompt}]
    state = {'status':'running','model_calls':0,'requested_tools':0,'successful_tools':0,
             'usage':{'prompt_tokens':0,'completion_tokens':0,'cached_tokens':0},
             'usage_complete':True,'usage_unknown_calls':[],'wire_attempts':0,'rate_refusals':0,
             'scheduler_wait_seconds':0,'response_models':[],'compactions':0,
             'expected_candidate':snapshot(session/'adapter'), 'tool_provenance':[],
             'last_prompt_tokens':0}
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
            messages=[{'role':'user','content':prompt}, {'role':'assistant','content':text},
                      {'role':'user','content':'Continue the same learning session from your own checkpoint. Re-read files whenever needed.'}]
            state['last_prompt_tokens']=0
        message, finish=await call(messages,'learner',tools=functions)
        messages.append(legacy.continuing_message(message))
        calls=message.get('tool_calls') or []
        if not calls:
            if finish == 'length':
                messages.append({'role':'user','content':'The provider ended that response at its output limit. Continue the same session; files and tools are unchanged.'})
                continue
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
                if name not in tools: raise ValueError('Unknown native capability')
                args=json.loads(function.get('arguments') or '{}')
                Draft202012Validator(schemas[name]).validate(args)
                if name=='small_model':
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


async def bridge(key, session, prompt, transport):
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
                return await conversation(key,connection,session,prompt,transport)


def run_learning(key, source, evidence, output, block):
    source,evidence,output=(Path(p).resolve() for p in (source,evidence,output))
    if output.exists(): raise FileExistsError('Learning trial already exists')
    output.mkdir(parents=True); start=time.monotonic()
    result={'status':'rejected','activated':False,'block':block,'model':MODEL,'source':str(source),
            'candidate':str(source),'started_utc':utc(),'profile':'full_mwm_unbounded',
            'admission_mode':'automatic_mechanical_only','transport':'fireworks'}
    try:
        before,lineage=validate_source(source,block)
        evidence_before,manifest=legacy.validate_evidence(evidence,block)
        seal=legacy.validate_batch_seal(evidence,manifest,block)
        environment=output/'document-tools'; environment.mkdir()
        shutil.copy2(ROOT/'prepared/agent/corpus.sqlite',environment/'corpus.sqlite')
        shutil.copy2(ROOT/'officeqa_tools.py',environment/'officeqa.py')
        corpus=ROOT/'prepared/agent/corpus'
        extra=f'\nCorpus: {corpus}\nRead-only helper: {environment}/officeqa.py\nIndex: {environment}/corpus.sqlite\nUse OfficeQACorpus(Path(corpus),Path(index)) with search/read/list_files, or read the TXT files directly.\n'
        goal=GOAL+extra
        save(output/'request.json',{'goal':goal,'source_lineage':lineage,'episode_ids':[e['id'] for e in manifest['episodes']],
            'budgets':{'seconds':None,'completion_tokens':None,'per_response':None,'model_calls':None},'model':MODEL,
            'controller_batch_seal':seal,'read_paths':[str(corpus),str(environment)]})
        from harnessadapter.evolution.session import evolve
        from harnessadapter.evolution.definition import EvolverDefinition
        from harnessadapter.policy.execution import ExecutionPolicy
        from full_sandbox import install
        install()
        definition=EvolverDefinition('mwm')
        prepared=evolve(source,evidence,goal,timeout=None,prepare_only=True,block=block,tick=block*5,
            policy=ExecutionPolicy(backend='codex',network=False,read_paths=(str(corpus),str(environment))),
            definition=definition,output=output/'sessions',model=MODEL,deployment=DEPLOYMENT,
            source_kind='architecture' if source==GENERIC_SOURCE.resolve() else 'adapter')
        session=Path(prepared['session']); result['session']=str(session)
        prompt=(session/'control/prompt.md').read_text(); (output/'exact-prompt.md').write_text(prompt)
        result['native_prepare_result']=str(session/'control/result.json')
        state=asyncio.run(bridge(key,session,prompt,importlib.import_module('client')))
        for name in ('model_calls','requested_tools','successful_tools','usage','usage_complete','usage_unknown_calls',
                     'wire_attempts','rate_refusals','scheduler_wait_seconds','response_models','compactions'):
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
        validation=workspace.validate()
        failures=[]
        if state['status']!='completed': failures.append('Learner did not complete')
        if not all(protected.values()): failures.append('Protected data changed')
        if not validation['passed']: failures.append('Native structural validation failed')
        if not provenance or state['response_models']!=[MODEL]: failures.append('Model/operation provenance failed')
        if not state['successful_tools']: failures.append('No successful native tools')
        feedback=session/'scratch/feedback.json'
        public_feedback={'path':str(feedback),'sha256':digest(feedback.read_bytes())} if feedback.is_file() else None
        admission={'admitted':not failures,'failures':failures,'protected_unchanged':protected,
            'operation_provenance_matches':provenance,'validation':validation,'native_finalize':native_finalize,
            'human_semantic_gate':False,'model':MODEL,'public_feedback':public_feedback,
            'feedback_missing':public_feedback is None,'revision':revision(snapshot(candidate)),
            'note':'Native gates and feedback format are reported, not a new semantic adoption filter.'}
        save(session/'control/mechanical-admission.json',admission)
        result.update(admission=str(session/'control/mechanical-admission.json'),failures=failures,
                      revision=admission['revision'],public_feedback=public_feedback)
        if not failures:
            from harnessadapter.adapter.versioning import commit_all
            commit=commit_all(candidate,f'OfficeQA full MWM: GLM block {block}')
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
