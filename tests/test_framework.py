"""Contract, edit-boundary, and actual MCP checks without model calls."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
from harnessadapter.adapter.loader import load_manifest
from harnessadapter.runtime import mcp_server as _mcp
from harnessadapter.paths import ARCHITECTURES as ARCH
from harnessadapter.evolution.workspace import Workspace
from mcp import Client
from mcp.client.stdio import StdioServerParameters


class EvolutionBoundaryTests(unittest.TestCase):
    def test_entire_core_is_writable_but_evidence_and_control_are_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for area in ('adapter', 'evidence', 'scratch', 'control'):
                (root / area).mkdir()
            (root / 'evidence/q1.json').write_text('{"observed": true}')
            workspace = Workspace(root)
            for path in ('adapter.json', 'world_model.py', 'ADAPTER.md', 'memory/x.md', 'agents/x.md'):
                workspace.write_file('adapter', path, 'candidate content')
                self.assertEqual(workspace.read_file('adapter', path)['text'], 'candidate content')
            for area, path in [('evidence','q1.json'), ('control','result.json'),
                               ('adapter','../control/result.json'), ('adapter','/tmp/escape'), ('adapter','.git/config')]:
                with self.subTest(area=area,path=path), self.assertRaises(ValueError):
                    workspace.write_file(area,path,'overwrite')
            (root/'adapter/link').symlink_to(root/'evidence/q1.json')
            with self.assertRaises(ValueError):workspace.write_file('adapter','link','overwrite')
            self.assertEqual((root/'evidence/q1.json').read_text(),'{"observed": true}')

    def test_validation_does_not_execute_candidate_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            shutil.copytree(ROOT/'examples/minimal_shop',root/'adapter')
            marker=root/'unexpected-execution'
            path=root/'adapter/adapter_tools.py'
            path.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n'+path.read_text())
            result=Workspace(root).validate()
            self.assertTrue(result['passed'],result)
            self.assertFalse(marker.exists())


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_core_edit_adds_tool_without_wrapper_edit(self):
        wrapper=Path(_mcp.__file__)
        before=hashlib.sha256(wrapper.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);core=root/'adapter'
            shutil.copytree(ROOT/'examples/minimal_shop',core)
            policy_path=core/'memory/policies.json'
            policies=json.loads(policy_path.read_text())
            policies['returns']['max_days_after_delivery']=7
            policies['returns']['fee_rate']='0.02'
            policy_path.write_text(json.dumps(policies))
            with (core/'adapter_tools.py').open('a') as stream:
                stream.write('\n\ndef check_return_eligibility(days_after_delivery: int) -> dict:\n    return {"eligible": days_after_delivery <= _memories()["returns"]["max_days_after_delivery"]}\n')
            manifest=json.loads((core/'adapter.json').read_text())
            manifest['tools'].append({'name':'check_return_eligibility','description':'Check the synthetic day limit.',
                'entrypoint':'adapter_tools.py:check_return_eligibility',
                'input_schema':{'type':'object','properties':{'days_after_delivery':{'type':'integer','minimum':0}},'required':['days_after_delivery'],'additionalProperties':False},
                'output_schema':{'type':'object','properties':{'eligible':{'type':'boolean'}},'required':['eligible']}})
            (core/'adapter.json').write_text(json.dumps(manifest))
            params=StdioServerParameters(command=sys.executable,args=['-m','harnessadapter.runtime.mcp_server','--adapter',str(core)])
            async with Client(params,read_timeout_seconds=20) as client:
                names={t.name for t in (await client.list_tools()).tools}
                self.assertIn('check_return_eligibility',names)
                yes=await client.call_tool('check_return_eligibility',{'days_after_delivery':5})
                no=await client.call_tool('check_return_eligibility',{'days_after_delivery':10})
                invalid=await client.call_tool('check_return_eligibility',{'days_after_delivery':'5'})
                self.assertTrue(yes.structured_content['eligible'])
                self.assertFalse(no.structured_content['eligible'])
                self.assertTrue(invalid.is_error)
                record=await client.call_tool('read_memory',{'memory_id':'returns'})
                self.assertEqual(record.structured_content['memory']['fee_rate'],'0.02')
        self.assertEqual(hashlib.sha256(wrapper.read_bytes()).hexdigest(),before)

    async def test_mwm_trace_requires_explicit_evidence_binding(self):
        core=ARCH/'mwm';wrapper=Path(_mcp.__file__)
        for bound in (None,'--evidence','--learning-material'):
            args=['-m','harnessadapter.runtime.mcp_server','--adapter',str(core)]
            if bound:args.extend([bound,str(ROOT/'examples/mwm_evidence')])
            async with Client(StdioServerParameters(command=sys.executable,args=args),read_timeout_seconds=20) as client:
                reply=await client.call_tool('wm',{'name':'trace','args':{'address':'q2'}})
                self.assertFalse(reply.is_error)
                if bound:self.assertIn('0.02',reply.structured_content['text'])
                else:self.assertIn('no evidence resource',reply.structured_content['text'])
                escape=await client.call_tool('wm',{'name':'read','args':{'path':'../../README.md'}})
                self.assertIn('give a /world-model/',escape.structured_content['text'])


if __name__=='__main__':unittest.main()
