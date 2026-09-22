"""No inference: configuration, OS canaries, and native wm only."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import acting

ENDPOINT = 'http://127.0.0.1:19999/v1'


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='officeqa-sandbox-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / 'actor'
        self.corpus = self.root / 'corpus'; self.corpus.mkdir()
        (self.corpus / 'document.txt').write_text('public document')
        self.index = self.root / 'index.json'; self.index.write_text('{}')
        self.output = self.root / 'records'

    def settings(self, adapter=None):
        return acting.build_settings(self.workspace, self.output, self.corpus,
                                     self.index, adapter, endpoint=ENDPOINT)

    def run_actor(self, **kwargs):
        return acting.run_actor(self.workspace, self.output, 'Read the provided document.',
                                self.corpus, self.index, endpoint=ENDPOINT, **kwargs)


class PreparationTests(Fixture):
    def test_model_secret_environment_and_roots(self):
        acting.prepare_actor_workspace(self.workspace)
        settings = self.settings()
        self.assertEqual(settings['model'], 'accounts/fireworks/models/glm-5p3-flash')
        self.assertEqual(settings['model_provider'], 'officeqa_fireworks')
        provider = settings['model_providers.officeqa_fireworks']
        self.assertEqual(provider['wire_api'], 'responses')
        self.assertFalse(provider['requires_openai_auth'])
        self.assertNotIn('env_key', provider)
        self.assertEqual(settings['approval_policy'], 'never')
        self.assertEqual(settings['web_search'], 'disabled')
        for env in (acting.client_environment(self.output), acting.tool_environment(self.workspace)):
            self.assertFalse(any('KEY' in k or 'TOKEN' in k or k == 'CODEX_HOME' for k in env))
        if acting.LINUX:
            # Linux: no outer OS sandbox; codex's own command sandbox enforces the profile.
            self.assertNotIn('sandbox_mode', settings)
            self.assertEqual(settings['default_permissions'], acting.PROFILE)
            self.assertEqual(settings[f'permissions.{acting.PROFILE}'], acting.permission_profile(self.workspace, self.corpus, self.index))
        else:
            self.assertEqual(settings['sandbox_mode'], 'danger-full-access')
        profile = acting.permission_profile(self.workspace, self.corpus, self.index)
        self.assertFalse(profile['network']['enabled'])
        self.assertEqual(profile['filesystem'][':root'], 'deny')
        self.assertNotIn(str(self.root), profile['filesystem'])
        self.assertEqual(profile['filesystem'][str(self.corpus)], 'read')
        self.assertEqual(profile['filesystem'][str(self.index)], 'read')
        self.assertEqual(profile['filesystem'][str(self.workspace)], 'write')
        self.assertEqual(profile['filesystem'][str(self.workspace/'officeqa.py')], 'read')

    def test_endpoint_restricted_to_broker(self):
        acting.validate_endpoint(ENDPOINT)
        for endpoint in ('https://api.fireworks.ai/v1', 'http://evil:42/v1',
                         'http://127.0.0.1:42/v1?url=evil', 'http://x:y@127.0.0.1:42/v1'):
            with self.assertRaises(ValueError): acting.validate_endpoint(endpoint)

    def test_native_adapter_deployment(self):
        from harnessadapter.paths import ARCHITECTURES
        adapter = ARCHITECTURES / 'mwm'
        baseline = acting.prepare_actor_workspace(self.workspace)
        adapted = acting.prepare_actor_workspace(self.root / 'adapted', adapter)
        self.assertEqual(baseline['common_instructions_sha256'], adapted['common_instructions_sha256'])
        self.assertIn('harnessadapter:mwm:start', (self.root/'adapted/AGENTS.md').read_text())
        settings = acting.build_settings(self.root/'adapted', self.output, self.corpus,
                                         self.index, adapter, endpoint=ENDPOINT)
        self.assertEqual(settings['mcp_servers.mwm.command'], '/usr/bin/env')
        self.assertNotIn('sandbox', settings['mcp_servers.mwm.args'])
        if acting.LINUX:
            # The server runs outside the command sandbox; its call log lands in
            # the controller's records, which the actor cannot write.
            args = settings['mcp_servers.mwm.args']
            self.assertEqual(args[args.index('--audit-log') + 1], str(self.output / 'tool-calls.jsonl'))
        else:
            self.assertNotIn('--audit-log', settings['mcp_servers.mwm.args'])
        self.assertNotIn('--evidence', settings['mcp_servers.mwm.args'])
        self.assertFalse((self.root/'adapted/PROMPT.md').exists())

    def test_changed_instructions_rejected(self):
        acting.prepare_actor_workspace(self.workspace)
        (self.workspace/'AGENTS.md').write_text('changed')
        with self.assertRaises(ValueError): self.settings()

    def test_records_outside_workspace_and_no_overwrite(self):
        acting.prepare_actor_workspace(self.workspace)
        self.output = self.workspace/'records'
        with self.assertRaises(ValueError): self.run_actor()
        self.output = self.root/'records'; self.output.mkdir(); (self.output/'old').touch()
        with self.assertRaises(ValueError): self.run_actor()

    def test_launch_failure_preserved(self):
        acting.prepare_actor_workspace(self.workspace)
        with patch.object(acting, 'client_command', return_value=[str(self.root/'absent')]): result = self.run_actor()
        self.assertEqual(result['status'], 'launch_error')
        self.assertTrue((self.output/'invocation.json').is_file())
        self.assertIsNone(result['usage'])

    def test_timeout_preserved(self):
        acting.prepare_actor_workspace(self.workspace)
        with patch.object(acting, 'command_for', return_value=[sys.executable,'-B','-c','import time; time.sleep(10)']):
            result = self.run_actor(timeout=.1)
        self.assertEqual(result['status'], 'timeout')


class ProcessCleanupTests(unittest.TestCase):
    def test_orphan_child_stopped_after_leader_exits_and_other_group_survives(self):
        control = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(30)'],
                                   start_new_session=True)
        self.addCleanup(lambda: (control.kill(), control.wait()) if control.poll() is None else None)
        script = '''import subprocess,sys
child = subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(30)'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(child.pid,flush=True)
'''
        leader = subprocess.Popen([sys.executable, '-B', '-c', script], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, start_new_session=True)
        stdout, stderr = leader.communicate(timeout=5)
        self.assertEqual(leader.returncode, 0, stderr)
        child_pid = int(stdout.strip())
        self.assertEqual(os.getpgid(child_pid), leader.pid)
        try:
            started = time.monotonic()
            acting._stop_group(leader, grace=10)
            self.assertLess(time.monotonic() - started, 2)
            deadline = time.monotonic() + 3
            state = ''
            while time.monotonic() < deadline:
                probe = subprocess.run(['/bin/ps','-p',str(child_pid),'-o','stat='],
                                       capture_output=True,text=True,timeout=2)
                state = probe.stdout.strip()
                if not state or state.startswith('Z'): break
                time.sleep(.02)
            self.assertTrue(not state or state.startswith('Z'), state)
            self.assertIsNone(control.poll())
        finally:
            try: os.killpg(leader.pid, 9)
            except ProcessLookupError: pass

    def test_shared_controller_group_rejected(self):
        process = subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(30)'])
        try:
            with self.assertRaises(ValueError): acting._stop_group(process)
            self.assertIsNone(process.poll())
        finally:
            process.kill(); process.wait()


class FinalCollectionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.workspace.mkdir(); self.output.mkdir()

    def test_exact_native_file_preserved(self):
        original = b'Native answer\n'
        (self.workspace/'final.md').write_bytes(original)
        result = acting.collect_final(self.workspace,self.output,'Native answer')
        self.assertTrue(result['valid'])
        self.assertEqual((self.output/'final.md').read_bytes(),original)

    def test_symlink_to_private_gold_never_followed(self):
        private = self.root/'gold.txt'; private.write_text('private gold canary')
        (self.workspace/'final.md').symlink_to(private)
        with patch.object(acting.os, 'read', side_effect=AssertionError('must not read linked file')):
            result = acting.collect_final(self.workspace,self.output,'private gold canary')
        self.assertFalse(result['valid'])
        self.assertEqual(result['reason'],'final_open_rejected')
        self.assertFalse((self.output/'final.md').exists())

    def test_fifo_never_blocks_or_reads(self):
        os.mkfifo(self.workspace/'final.md')
        started = time.monotonic()
        with patch.object(acting.os, 'read', side_effect=AssertionError('must not read FIFO')):
            result = acting.collect_final(self.workspace,self.output,'answer')
        self.assertLess(time.monotonic()-started,1)
        self.assertEqual(result['reason'],'final_not_regular')
        self.assertFalse((self.output/'final.md').exists())

    def test_size_limit_and_event_match_fail_closed(self):
        target = self.workspace/'final.md'; target.write_bytes(b'x'*32)
        self.assertEqual(acting.collect_final(self.workspace,self.output,'x'*32,byte_limit=16)['reason'],'final_too_large')
        target.write_text('different answer')
        self.assertEqual(acting.collect_final(self.workspace,self.output,'native final answer')['reason'],'final_event_mismatch')
        self.assertFalse((self.output/'final.md').exists())

    def test_last_completed_native_agent_message_selected(self):
        events = self.output/'events.jsonl'
        rows = [{'type':'item.completed','item':{'type':'agent_message','text':'preamble'}},
                {'type':'item.completed','item':{'type':'reasoning','text':'private reasoning is not final'}},
                {'type':'item.completed','item':{'type':'agent_message','text':'final answer'}},
                {'type':'turn.completed'}]
        events.write_text('\n'.join(json.dumps(row) for row in rows))
        self.assertEqual(acting.parse_events(events)['last_agent_message'],'final answer')


@unittest.skipUnless(os.environ.get("OFFICEQA_RUN_NATIVE_ACTOR_TEST") == "1",
                     "Native macOS sandbox checks are opt-in; no paid calls")
class LocalCodexTests(Fixture):
    def test_whole_client_boundaries(self):
        acting.prepare_actor_workspace(self.workspace)
        self.output.mkdir(); home = self.workspace/'.client-home'; home.mkdir()
        outside = self.root/'gold.txt'; outside.write_text('private canary')
        script = '''import errno,json,socket,sys
from pathlib import Path
out={}
def attempt(name,fn):
 try: fn(); out[name]='allowed'
 except OSError as e: out[name]='denied' if e.errno in (errno.EPERM,errno.EACCES) else str(e)
def connect(port):
 with socket.socket() as s: s.settimeout(2); s.connect(('127.0.0.1',int(port)))
attempt('own_output',lambda:Path(sys.argv[1]).write_text('ok'))
attempt('gold_read',lambda:Path(sys.argv[2]).read_text())
attempt('broker',lambda:connect(sys.argv[3]))
attempt('other_port',lambda:connect(sys.argv[4]))
print(json.dumps(out))
'''
        with socket.socket() as broker, socket.socket() as other:
            broker.bind(('127.0.0.1',0)); broker.listen()
            other.bind(('127.0.0.1',0)); other.listen()
            endpoint = f'http://127.0.0.1:{broker.getsockname()[1]}/v1'
            command = [sys.executable,'-B','-c',script,str(self.output/'probe'),str(outside),
                       str(broker.getsockname()[1]),str(other.getsockname()[1])]
            argv = acting.client_command(command,self.workspace,self.output,self.corpus,self.index,endpoint=endpoint)
            result = subprocess.run(argv,cwd=self.workspace,env=acting.client_environment(home),capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),{'own_output':'denied','gold_read':'denied','broker':'allowed','other_port':'denied'})

    def test_strict_config_no_inference(self):
        from harnessadapter.paths import ARCHITECTURES
        for name, adapter in [('baseline',None), ('adapted',ARCHITECTURES/'mwm')]:
            with self.subTest(name=name):
                self.workspace = self.root/name
                acting.prepare_actor_workspace(self.workspace,adapter)
                result = acting.validate_config_offline(self.settings(adapter))
                self.assertTrue(result['passed'],result)
                self.assertEqual(result['model_calls'],0)

    def test_os_boundaries(self):
        """Outer controller must be allowed to create a local test socket."""
        acting.prepare_actor_workspace(self.workspace)
        for name in ('README.md','officeqa.py'): (self.workspace/name).write_text('frozen')
        adapter = self.root/'candidate'; adapter.mkdir(); (adapter/'memory.txt').write_text('public memory')
        forbidden = {}
        for name in ('parent','other_arm','gold'):
            path = self.root/(name+'.txt'); path.write_text(name+' canary'); forbidden[name] = str(path)
        (self.workspace/'escape').symlink_to(forbidden['gold'])
        profile = acting.permission_profile(self.workspace,self.corpus,self.index,adapter)
        script = '''import errno,json,socket,sys
from pathlib import Path
w,c,i,a=map(Path,sys.argv[1:5]); forbidden=json.loads(sys.argv[5]); out={}
def attempt(name,fn):
 try: fn(); out[name]='allowed'
 except OSError as exc: out[name]='denied' if exc.errno in (errno.EPERM,errno.EACCES) else str(exc)
attempt('workspace_write',lambda:(w/'probe').write_text('ok'))
attempt('corpus_read',lambda:(c/'document.txt').read_text())
attempt('corpus_write',lambda:(c/'document.txt').write_text('bad'))
attempt('index_read',lambda:i.read_text())
attempt('index_write',lambda:i.write_text('bad'))
attempt('adapter_read',lambda:(a/'memory.txt').read_text())
attempt('adapter_write',lambda:(a/'memory.txt').write_text('bad'))
for name,path in forbidden.items(): attempt(name+'_read',lambda p=path:Path(p).read_text())
attempt('symlink_read',lambda:(w/'escape').read_text())
for name in ('AGENTS.md','README.md','officeqa.py'): attempt(name+'_write',lambda n=name:(w/n).write_text('bad'))
def connect(host,port):
 with socket.socket() as sock:
  sock.settimeout(2); sock.connect((host,port))
attempt('localhost_network',lambda:connect('127.0.0.1',int(sys.argv[6])))
attempt('internet_network',lambda:connect('1.1.1.1',443))
print(json.dumps(out))
'''
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0)); listener.listen()
            command = [sys.executable,'-B','-c',script,str(self.workspace),str(self.corpus),
                       str(self.index),str(adapter),json.dumps(forbidden),str(listener.getsockname()[1])]
            endpoint = f'http://127.0.0.1:{listener.getsockname()[1]}/v1'
            argv = acting.client_command(command,self.workspace,self.output,self.corpus,self.index,adapter,endpoint=endpoint)
            result = subprocess.run(argv,cwd=self.workspace,env=acting.tool_environment(self.workspace),capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        expected = {key:'denied' for key in ('corpus_write','index_write','adapter_write',
                    'parent_read','other_arm_read','gold_read','symlink_read','AGENTS.md_write',
                    'README.md_write','officeqa.py_write','internet_network')}
        expected.update({key:'allowed' for key in ('workspace_write','corpus_read','index_read','adapter_read','localhost_network')})
        self.assertEqual(json.loads(result.stdout),expected)
        self.assertEqual((self.corpus/'document.txt').read_text(),'public document')


class NativeMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_native_wm(self):
        from harnessadapter.paths import ARCHITECTURES
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters
        with tempfile.TemporaryDirectory(prefix='officeqa-mcp-test-') as directory:
            root = Path(directory).resolve(); workspace = root/'actor'; output = root/'records'
            corpus = root/'corpus'; corpus.mkdir(); index = root/'index.json'; index.write_text('{}')
            adapter = ARCHITECTURES/'mwm'
            acting.prepare_actor_workspace(workspace,adapter); output.mkdir()
            settings = acting.build_settings(workspace,output,corpus,index,adapter,endpoint=ENDPOINT)
            command = [settings['mcp_servers.mwm.command'],*settings['mcp_servers.mwm.args']]
            argv = acting.client_command(command,workspace,output,corpus,index,adapter,endpoint=ENDPOINT)
            params = StdioServerParameters(command=argv[0],args=argv[1:],cwd=str(workspace))
            async with Client(params,read_timeout_seconds=25) as client:
                self.assertEqual({t.name for t in (await client.list_tools()).tools},{'wm'})
                result = await client.call_tool('wm',{'name':'list'})
                self.assertFalse(result.is_error,result)
            self.assertTrue(result.content)


if __name__ == '__main__': unittest.main()


class RequestCapTests(unittest.TestCase):
    """The per-question request cap counts completed tool items on the live
    --json stream and stops the client the way the timeout does."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='officeqa-cap-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / 'actor'
        self.corpus = self.root / 'corpus'; self.corpus.mkdir()
        (self.corpus / 'document.txt').write_text('public document')
        self.index = self.root / 'index.json'; self.index.write_text('{}')
        self.output = self.root / 'records'
        acting.prepare_actor_workspace(self.workspace)

    def fake(self, items, tail_sleep):
        script = ('import json,sys,time\n'
                  'for i in range(%d):\n'
                  '    print(json.dumps({"type":"item.completed","item":{"id":"c%%d"%%i,"type":"command_execution","command":"x","exit_code":0}}), flush=True)\n'
                  '    time.sleep(0.05)\n'
                  'time.sleep(%s)\n'
                  'print(json.dumps({"type":"item.completed","item":{"id":"m","type":"agent_message","text":"42"}}), flush=True)\n'
                  'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}), flush=True)\n'
                  'open(sys.argv[1]).close() if False else None\n') % (items, tail_sleep)
        return [sys.executable, '-B', '-c', script]

    def run_actor(self, **kwargs):
        return acting.run_actor(self.workspace, self.output, 'Read the provided document.',
                                self.corpus, self.index, endpoint=ENDPOINT, **kwargs)

    def test_cap_reached_stops_and_is_recorded(self):
        with patch.object(acting, 'command_for', return_value=self.fake(5, 30)):
            result = self.run_actor(timeout=20, max_requests=3)
        self.assertEqual(result['status'], 'request_cap')
        self.assertTrue(result['request_capped'])
        self.assertGreaterEqual(result['tool_items_seen'], 3)
        self.assertFalse(result['turn_completed'])
        self.assertLess(result['elapsed_seconds'], 15)
        self.assertEqual(json.loads((self.output / 'invocation.json').read_text())['max_requests'], 3)

    def test_under_cap_completes_with_streamed_events(self):
        with patch.object(acting, 'command_for', return_value=self.fake(2, 0)):
            (self.workspace / 'final.md').write_text('42')
            result = self.run_actor(timeout=20, max_requests=100)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['tool_items_seen'], 2)
        self.assertEqual(result['usage'], {'input_tokens': 1, 'output_tokens': 1})
        self.assertEqual(result['completed_item_counts'], {'command_execution': 2, 'agent_message': 1})

    def test_no_cap_by_default(self):
        with patch.object(acting, 'command_for', return_value=self.fake(4, 0)):
            (self.workspace / 'final.md').write_text('42')
            result = self.run_actor(timeout=20)
        self.assertEqual(result['status'], 'completed')
        self.assertIsNone(result['max_requests'])


class LoginFinalTests(unittest.TestCase):
    """Login mode (no broker): a completed turn whose stream carries no
    agent_message keeps the final codex wrote; a mismatching final with an
    agent_message present is still refused."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='officeqa-login-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / 'actor'
        self.corpus = self.root / 'corpus'; self.corpus.mkdir()
        (self.corpus / 'document.txt').write_text('public document')
        self.index = self.root / 'index.json'; self.index.write_text('{}')
        self.output = self.root / 'records'
        acting.prepare_actor_workspace(self.workspace)
        self.home = self.root / 'fake-home'; self.home.mkdir()
        patches = [patch.object(acting, 'LOGIN', True), patch.object(acting, 'login_home', lambda d: self.home)]
        for p in patches:
            p.start(); self.addCleanup(p.stop)

    def fake(self, message):
        lines = ['{"type":"turn.started"}']
        if message is not None:
            lines.append(json.dumps({"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": message}}))
        lines.append('{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}')
        script = 'import sys\n' + ''.join(f'print({json.dumps(l)})\n' for l in lines)
        return [sys.executable, '-B', '-c', script]

    def run_actor(self):
        return acting.run_actor(self.workspace, self.output, 'Read the provided document.',
                                self.corpus, self.index, endpoint=None, timeout=20)

    def test_final_without_message_event_is_kept(self):
        with patch.object(acting, 'command_for', return_value=self.fake(None)):
            (self.workspace / 'final.md').write_text('42\n')
            result = self.run_actor()
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(result['final_message_event'])
        self.assertEqual((self.output / 'final.md').read_text(), '42\n')

    def test_mismatch_with_message_event_still_refused(self):
        with patch.object(acting, 'command_for', return_value=self.fake('41')):
            (self.workspace / 'final.md').write_text('42\n')
            result = self.run_actor()
        self.assertEqual(result['status'], 'invalid_final')
        self.assertTrue(result['final_message_event'])

    def test_settings_have_no_provider_in_login_mode(self):
        settings = acting.build_settings(self.workspace, self.output, self.corpus, self.index, None, endpoint=None)
        self.assertNotIn('model_provider', settings)
        self.assertEqual(settings['model'], acting.MODEL)
        self.assertNotIn('features.code_mode_host', settings)


class InsuranceTests(unittest.TestCase):
    """Infrastructure failures are classified apart from the model's own
    outcomes, and the transport's failure time extends the question guard."""

    def test_infra_failure_classifier(self):
        f = acting.infra_failure
        self.assertTrue(f({'status': 'failed', 'terminal_error': 'Selected model is at capacity. Please try a different model.'}))
        self.assertTrue(f({'status': 'failed', 'terminal_error': '<!DOCTYPE HTML>\n<html><title>Error response</title>'}))
        self.assertTrue(f({'status': 'failed', 'terminal_error': 'stream disconnected before completion: connection reset'}))
        self.assertFalse(f({'status': 'failed', 'terminal_error': 'stream disconnected before completion: Incomplete response returned, reason: max_output_tokens'}))
        self.assertFalse(f({'status': 'completed', 'terminal_error': None}))
        self.assertTrue(f({'status': 'completed'}, {'failed': True, 'failure_kind': 'transport_rejected'}))
        self.assertFalse(f({'status': 'completed'}, {'failed': True, 'failure_kind': 'budget_exhausted'}))
        self.assertFalse(f({'status': 'completed'}, {'failed': False, 'failure_kind': None}))

    def test_guard_extends_by_infra_time(self):
        temp = tempfile.TemporaryDirectory(prefix='officeqa-ins-'); self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve(); ws = root / 'actor'; corpus = root / 'corpus'; corpus.mkdir()
        (corpus / 'd.txt').write_text('x'); index = root / 'i.json'; index.write_text('{}'); out = root / 'records'
        acting.prepare_actor_workspace(ws)
        script = [sys.executable, '-B', '-c', 'import time,json; time.sleep(1.2); print(json.dumps({"type":"item.completed","item":{"id":"m","type":"agent_message","text":"42"}}), flush=True); print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}), flush=True)']
        with patch.object(acting, 'command_for', return_value=script):
            (ws / 'final.md').write_text('42')
            # A 0.3 s guard would time out; 2 s of infrastructure time extends it past the sleep.
            result = acting.run_actor(ws, out, 'q', corpus, index, endpoint=ENDPOINT, timeout=0.3, extra_time=lambda: 2.0)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['infra_seconds'], 2.0)
