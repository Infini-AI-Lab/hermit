"""Policy parsing plus real OS-enforced read/write/network boundary tests."""
import http.server
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'framework'))
from execution_policy import ExecutionPolicy, PolicySandbox, load_policy
from evolution_backends import command_for
from mcp import Client
from mcp.client.stdio import StdioServerParameters


def session(root,policy):
    for name in ('adapter','scratch','control','host','evidence','method','environment','history'):(root/name).mkdir()
    (root/'evidence/record.txt').write_text('raw evidence')
    (root/'control/policy.json').write_text(json.dumps({'requested':policy.serialize()}))
    return PolicySandbox(root,policy)


class PolicyConfigTests(unittest.TestCase):
    def test_defaults_and_fixed_write_scope(self):
        p=ExecutionPolicy.parse({})
        self.assertTrue(p.network);self.assertFalse(p.read_all);self.assertEqual(p.read_paths,())
        for data in ({'write_access':'all'},{'network':'false'},{'read_access':{'path':[]}}):
            with self.assertRaises(ValueError):ExecutionPolicy.parse(data)

    def test_relative_roots_and_cli_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'project').mkdir()
            config=root/'policy.toml';config.write_text('backend="codex"\nnetwork=false\n[read_access]\npaths=["project"]\n')
            p=load_policy(config)
            self.assertEqual(p.read_paths,(str((root/'project').resolve()),));self.assertFalse(p.network)
            q=load_policy(config,backend='claude-code',read_all=True,network=True)
            self.assertTrue(q.read_all and q.network);self.assertEqual(q.backend,'claude-code')


@unittest.skipUnless(shutil.which('codex') and sys.platform in ('darwin','linux'),'requires Codex sandbox executor')
class RealSandboxTests(unittest.TestCase):
    def test_three_areas_and_network_off_preflight(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();p=ExecutionPolicy(network=False);box=session(root,p)
            result=box.preflight();self.assertTrue(result['passed'])
            self.assertTrue(result['observed']['evidence_read']);self.assertFalse(result['observed']['evidence_write'])
            self.assertFalse(result['observed']['network_connect'])
            self.assertEqual(box.operation('read_file',{'area':'evidence','path':'record.txt'})['text'],'raw evidence')
            with self.assertRaises(ValueError):box.operation('write_file',{'area':'evidence','path':'record.txt','content':'bad'})
            self.assertEqual(box.operation('write_file',{'area':'adapter','path':'new.md','content':'ok'})['written'],'new.md')

    def test_external_reads_never_grant_external_writes(self):
        with tempfile.TemporaryDirectory() as d:
            top=Path(d).resolve();external=top/'external';external.mkdir();(external/'value').write_text('external')
            root=top/'session';root.mkdir();p=ExecutionPolicy(read_paths=(str(external),),network=False);box=session(root,p)
            self.assertEqual(box.operation('read_path',{'path':str(external/'value')})['text'],'external')
            attempt=box.run(['/bin/sh','-c','printf bad > '+str(external/'value')])
            self.assertNotEqual(attempt['exit_code'],0)
            self.assertEqual((external/'value').read_text(),'external')
            with self.assertRaises(ValueError):box.operation('read_path',{'path':str(root/'control/policy.json')})

    def test_read_all_still_blocks_symlink_and_hardlink_writes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();box=session(root,ExecutionPolicy(read_all=True,network=False))
            self.assertTrue(box.preflight()['observed']['outside_read'])
            script='''from pathlib import Path
import os,sys
r=Path(sys.argv[1])
for name in ['soft','hard']:
 try:
  p=r/'adapter'/name
  p.symlink_to(r/'evidence/record.txt') if name=='soft' else os.link(r/'evidence/record.txt',p)
  p.write_text('bad')
 except OSError:pass
'''
            box.run([sys.executable,'-c',script,str(root)])
            self.assertEqual((root/'evidence/record.txt').read_text(),'raw evidence')

    def test_network_on_connects_without_expanding_writes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();box=session(root,ExecutionPolicy(network=True))
            result=box.preflight()['observed']
            self.assertTrue(result['network_connect']);self.assertFalse(result['outside_write'])


@unittest.skipUnless(shutil.which('codex') and sys.platform in ('darwin','linux'),'requires Codex sandbox executor')
class PolicyMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_broker_enforces_network_off_and_exposes_policy(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();session(root,ExecutionPolicy(network=False))
            params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'framework/evolution_server.py'),'--session',str(root)])
            async with Client(params,read_timeout_seconds=30) as client:
                p=await client.call_tool('get_policy',{})
                self.assertFalse(p.structured_content['requested']['network'])
                e=await client.call_tool('read_file',{'area':'evidence','path':'record.txt'})
                self.assertEqual(e.structured_content['text'],'raw evidence')
                blocked=await client.call_tool('fetch_url',{'url':'http://127.0.0.1:9/'})
                self.assertTrue(blocked.is_error)
                write=await client.call_tool('run_command',{'command':'printf hello > from-command.txt'})
                self.assertEqual(write.structured_content['exit_code'],0,write)
                self.assertEqual((root/'adapter/from-command.txt').read_text(),'hello')
                outside=await client.call_tool('run_command',{'command':'printf bad > ../evidence/record.txt'})
                self.assertNotEqual(outside.structured_content['exit_code'],0)
                self.assertEqual((root/'evidence/record.txt').read_text(),'raw evidence')


if __name__=='__main__':unittest.main()
