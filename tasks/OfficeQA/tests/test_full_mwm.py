"""Synthetic verification of unlimited inference and executable MWM isolation."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import acting
import broker
import client
import full_learning as F
import learning as L
import rate_control
from test_learner_client import Opener, body
from test_learning import setup_session, feedback, toolcall, FakeBroker, FakeTransport


class UnboundedTests(unittest.TestCase):
    def test_responses_omits_limits_and_rejects_other_models(self):
        payload={'model':broker.MODEL,'input':[],'tools':[],'max_output_tokens':3,
                 'reasoning':{'effort':'high'},'thinking':{'budget_tokens':4}}
        out=broker.normalized(payload,None,None)
        self.assertFalse(set(out)&{'max_tokens','max_output_tokens','max_completion_tokens','reasoning','thinking'})
        with self.assertRaises(ValueError): broker.normalized({**payload,'model':'another-model'},None,None)

    def test_chat_above_previous_token_budget_has_no_deadline_or_limits(self):
        opener=Opener(body({'prompt_tokens':9,'completion_tokens':100000,'total_tokens':100009}))
        with patch.object(client.urllib.request,'build_opener',return_value=opener), \
             patch.object(rate_control,'GLOBAL',rate_control.RateScheduler(min_interval=0,backoffs=(0,))):
            response,meta=client.complete('synthetic-key',[],timeout=None,unbounded=True)
        self.assertIsNotNone(response)
        self.assertEqual(meta['usage']['completion_tokens'],100000)
        payload=json.loads(opener.requests[0].data)
        self.assertFalse(set(payload)&{'max_tokens','max_completion_tokens','reasoning_effort','thinking'})

    def test_explicit_refusals_retry_beyond_five_without_deadline(self):
        def operation(timeout,n):
            self.assertIsNone(timeout)
            if n<9: raise rate_control.AttemptFailure('rate refusal',retryable=True,details={'usage_known':True})
            return rate_control.Outcome('ok',{'usage_known':True})
        result,records=rate_control.run(operation,timeout=None,max_attempts=None,request_sha256='fixture',
            scheduler=rate_control.RateScheduler(min_interval=0,backoffs=(0,)))
        self.assertEqual(result,'ok'); self.assertEqual(len(records),9)

    def test_full_loop_accepts_python_artifact_without_legacy_read_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=setup_session(Path(tmp).resolve()/'session')
            transport=FakeTransport([
                {'role':'assistant','tool_calls':[toolcall('write_file',{'area':'adapter','path':'tools/toy/run.py','content':'def run(): return 1\n'})]},
                {'role':'assistant','content':'Synthetic complete.'}])
            state=asyncio.run(F.conversation('synthetic-key',FakeBroker(session),session,'Synthetic only',transport))
            self.assertEqual(state['status'],'completed')
            self.assertTrue((session/'adapter/tools/toy/run.py').is_file())
            self.assertTrue(all(r['timeout'] is None and r['unbounded'] for r in transport.options))


@unittest.skipUnless(os.environ.get('OFFICEQA_RUN_NATIVE_LEARNER_TEST')=='1','Native synthetic checks opt-in')
class FullNativeTests(unittest.TestCase):
    def test_linux_execution_has_no_host_files_or_network(self):
        from full_runtime import DockerPythonSandbox
        sandbox=DockerPythonSandbox(L.GENERIC_SOURCE)
        code='''import os,socket
result={'outside_exists':os.path.exists(args['outside']), 'docker_socket':os.path.exists('/var/run/docker.sock')}
try:
 socket.create_connection(('1.1.1.1',443),timeout=1); result['network']='allowed'
except OSError: result['network']='denied'
result['value']=7**4%5
'''
        out=sandbox.run_code(code,{'outside':str(Path(__file__).resolve())})
        self.assertTrue(out['passed'],out)
        self.assertEqual(json.loads(out['stdout']),{'outside_exists':False,'docker_socket':False,'network':'denied','value':1})

    def test_native_learning_admits_tested_program_then_actor_calls_it(self):
        import officeqa_tools
        from full_runtime import DockerPythonSandbox
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters
        fixture_root=F.REPOSITORY/'tasks/OfficeQA/.verification'
        fixture_root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='full-native-',dir=fixture_root) as tmp:
            base=Path(tmp).resolve(); fixture=setup_session(base/'fixture')
            corpus=base/'prepared/agent/corpus'; corpus.mkdir(parents=True)
            (corpus/'fixture.txt').write_text('Synthetic document: count = 7.\n')
            officeqa_tools.build_index(corpus,corpus.parent/'corpus.sqlite',expected_documents=1)
            shutil.copy2(Path(F.__file__).parent/'officeqa_tools.py',base/'officeqa_tools.py')
            # Full learner runtime files are explicit and contain no test key.
            for name in ('full_sandbox.py','full_runtime.py','paths.py','experiment.py'):
                shutil.copy2(Path(F.__file__).parent/name,base/name)
            config={'framework':str(F.REPOSITORY),'docker_host':os.environ['OFFICEQA_DOCKER_HOST'],
                    'docker_binary':shutil.which('docker')}
            (base/'.task-paths.json').write_text(json.dumps(config))
            program='SCHEMA = {"name":"toy_add","description":"Synthetic addition","parameters":{"type":"object","properties":{"x":{"type":"number"},"y":{"type":"number"}},"required":["x","y"]}}\ndef run(x,y): return x+y\n'
            calls=[toolcall('write_file',{'area':'adapter','path':path,'content':content},i) for i,(path,content) in enumerate([
                ('tools/toy_add/run.py',program),('tools/toy_add/test.py','from run import run\nassert run(2,3)==5\nprint("PASS")\n'),
                ('tools/toy_add/TOOL.md','# Synthetic addition\nderived_from: none (test fixture)\n'),
                ('agents/toy_reader/SPEC.md','# Synthetic reader\nRead provided files and report observations.\n')])]
            check='import pathlib; print(pathlib.Path('+repr(str(corpus/'fixture.txt'))+').read_text())'
            private=base/'future-answers.txt'; private.write_text('SYNTHETIC PRIVATE CANARY')
            probe='''import pathlib,json,socket
out={}
def attempt(name,fn):
 try: fn(); out[name]='allowed'
 except OSError: out[name]='denied'
attempt('future_answer',lambda:pathlib.Path(%r).read_text())
attempt('protected_core_write',lambda:pathlib.Path('CONTRACT.md').write_text('bad'))
attempt('git_write',lambda:pathlib.Path('.git/config').write_text('bad'))
attempt('network',lambda:socket.create_connection(('1.1.1.1',443),timeout=1))
print(json.dumps(out))
''' % str(private)
            transport=FakeTransport([
                {'role':'assistant','content':'Read released fixture answer.', 'tool_calls':[toolcall('read_file',{'area':'evidence','path':'001/reference_answer.json'})]},
                {'role':'assistant','content':'Create synthetic artifacts.', 'tool_calls':calls},
                {'role':'assistant','content':'Test artifacts and corpus access.','tool_calls':[
                    toolcall('captest',{'directory':'tools/toy_add'},21),
                    toolcall('run_command',{'command':sys.executable+' -c '+__import__('shlex').quote(check)},22),
                    toolcall('run_command',{'command':sys.executable+' -c '+__import__('shlex').quote(probe)},24),
                    toolcall('write_file',{'area':'scratch','path':'feedback.json','content':json.dumps(feedback())},23)]},
                {'role':'assistant','content':'Synthetic completed.'}])
            full_deployment=Path(F.__file__).parent.parent/'deployments/officeqa-full'
            with patch.object(F,'ROOT',base),patch.object(F,'DEPLOYMENT',full_deployment), \
                 patch.object(L,'SEAL_ROOT',base/'trusted-seals'),patch.object(client,'complete',transport.complete):
                result=F.run_learning('synthetic-key',L.GENERIC_SOURCE,fixture/'evidence',base/'evolution/01',1)
            self.assertTrue(result['activated'],result)
            records=[json.loads(p.read_text()) for p in (Path(result['session'])/'control/fireworks-tool-results').glob('*.json')]
            tested=next(r for r in records if r['name']=='captest')
            self.assertTrue(tested['ok'],tested)
            self.assertTrue(json.loads(tested['response'])['passed'],tested)
            explored=next(r for r in records if r['name']=='run_command')
            self.assertTrue(explored['ok'],explored)
            self.assertIn('count = 7',explored['response'])
            boundary=next(r for r in records if r['tool_call_id']=='fixture-call-24')
            self.assertTrue(boundary['ok'],boundary)
            self.assertEqual(json.loads(json.loads(boundary['response'])['stdout']),
                {'future_answer':'denied','protected_core_write':'denied','git_write':'denied','network':'denied'})
            adapter=Path(result['candidate'])
            workspace=base/'actor'; output=base/'records'; output.mkdir()
            with patch.object(acting,'FULL',True):
                acting.prepare_actor_workspace(workspace,adapter)
                with broker.Broker('synthetic-key',base/'relay',max_completion=None,max_response=None,
                        request_timeout=None,max_attempts=None,local_sandbox=DockerPythonSandbox(adapter)) as relay:
                    settings=acting.build_settings(workspace,output,corpus,corpus.parent/'corpus.sqlite',adapter,endpoint=relay.endpoint)
                    self.assertIn('agents.toy_reader',settings)
                    self.assertTrue(acting.validate_config_offline(settings)['passed'])
                    command=[settings['mcp_servers.mwm.command'],*settings['mcp_servers.mwm.args']]
                    argv=acting.client_command(command,workspace,output,corpus,corpus.parent/'corpus.sqlite',adapter,endpoint=relay.endpoint)
                    params=StdioServerParameters(command=argv[0],args=argv[1:],cwd=str(workspace))
                    async def check_mcp():
                        async with Client(params,read_timeout_seconds=45) as connection:
                            value=await connection.call_tool('wm',{'name':'toy_add','args':{'x':2,'y':3}})
                            self.assertFalse(value.is_error,value)
                            self.assertEqual(json.loads(value.content[0].text)['text'],'5')
                    asyncio.run(check_mcp())
            self.assertEqual(result['response_models'],[L.MODEL])
