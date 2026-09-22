"""Regression checks for the MWM structure and semantics, not benchmark scores."""
import asyncio
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
from harnessadapter.adapter.loader import Adapter
from harnessadapter.paths import ARCHITECTURES as ARCH, EVOLVERS
from harnessadapter.hosts.codex import prepare as prepare_codex
from harnessadapter.hosts.claude_code import prepare as prepare_claude


def copied(root):
    target=root/'adapter'
    shutil.copytree(ARCH/'mwm',target,ignore=shutil.ignore_patterns('__pycache__'))
    return target


class MwmFidelityTests(unittest.TestCase):
    def test_initial_layout_and_no_invented_role(self):
        root=ARCH/'mwm';adapter=Adapter(root)
        for path in ['memory/_template.md','examples/_template.md','tools/_template/TOOL.md',
                     'tools/_template/run.py','tools/_template/test.py','agents/_template/SPEC.md',
                     'meta/cases/README.md','meta/journal.md','ADAPTER.md']:
            self.assertTrue((root/path).is_file(),path)
        self.assertEqual(adapter.agent_definitions(),[])
        self.assertEqual(set(adapter.tools),{'wm'})
        text=adapter.context()
        sections=['Earlier episodes','=== Environment briefing ===','=== Open questions ===',
                  '=== World-model memory (catalog) ===','=== Reaching the world model ===']
        positions=[text.index(x) for x in sections]
        self.assertEqual(positions,sorted(positions))
        self.assertNotIn('mwm_researcher',text)

    def test_spec_export_and_stale_native_role_retirement(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);core=copied(root);spec=core/'agents/policy-check/SPEC.md';spec.parent.mkdir()
            body='Investigate a recurring policy ambiguity.\nscope: policy checks\nlast_confirmed_t: 2\nderived_from: q1\n\nUse wm memory and recall; return evidence and unresolved readings.\n'
            spec.write_text(body)
            for prepare,host,role in [(prepare_codex,'codex','policy_check'),(prepare_claude,'claude','policy-check')]:
                workspace=root/host
                first=prepare(core,workspace,sys.executable)
                file=Path(first['roles'][role]);self.assertIn('derived_from',file.read_text())
                spec.rename(spec.with_suffix('.disabled'))
                second=prepare(core,workspace,sys.executable)
                self.assertEqual(second['roles'],{})
                self.assertFalse(file.exists())
                self.assertTrue(list((workspace/'.retired-roles').rglob(file.name)))
                spec.with_suffix('.disabled').rename(spec)

    def test_journal_roll_docket_and_full_method_materials(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);core=copied(root)
            (root/'control').mkdir();(root/'environment').mkdir()
            (core/'meta/journal.md').write_text('# Journal\n\n## Older sessions (digests)\n'+''.join(f'\n## Session {i}\nDigest: fact {i}\nObservation {i}.\n' for i in range(1,8)))
            (core/'meta/cases/01-open.md').write_text('Unresolved investigation.\n'+'x'*4000)
            spec=importlib.util.spec_from_file_location('method_under_test',EVOLVERS/'mwm/prompt.py')
            method=importlib.util.module_from_spec(spec);spec.loader.exec_module(method)
            result=method.prepare_session(core,root,block=8,tick=20)
            self.assertIn('meta/journal.md',result['rolled'])
            self.assertIn('## Session 1',(core/'meta/journal-archive.md').read_text())
            self.assertNotIn('## Session 1\n',(core/'meta/journal.md').read_text())
            self.assertTrue((core/'meta/cases/01-open.md').exists())
            self.assertIn('01-open.md',result['docket'])
            contract=(root/'method/CONTRACT.md').read_text()
            self.assertIn('meta/cases/',contract)
            self.assertNotIn('## Session flow',contract)
            self.assertNotIn('{env_',contract)
            self.assertTrue((root/'method/skills/author-tool/SKILL.md').is_file())
            self.assertTrue((root/'method/patterns/DECIDE.md').is_file())


class MwmAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_import_or_execution_before_admission(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);core=copied(root);tool=core/'tools/echo';tool.mkdir()
            canary=root/'imported'
            (tool/'run.py').write_text(f'from pathlib import Path\nPath({str(canary)!r}).touch()\nSCHEMA={{"name":"echo","description":"Echo","parameters":{{"type":"object","properties":{{}},"required":[]}}}}\ndef run(**kwargs):return {{"ok":True}}\n')
            (tool/'test.py').write_text('import run\nassert run.run()["ok"]\n')
            (tool/'TOOL.md').write_text('Echo a result.\nscope: test\nlast_confirmed_t: 0\nderived_from: none (test fixture)\nrepairs: 0\n')
            class FakeSandbox:
                def __init__(self,passed):self.passed=passed;self.invoked=False
                def status(self):return {'available':True}
                def test(self,directory):return {'passed':self.passed,'output':'fake test result'}
                def invoke(self,directory,args):self.invoked=True;return {'passed':True,'stdout':'{"ok":true}'}
            rejected=FakeSandbox(False);a=Adapter(core,{'python_sandbox':rejected})
            self.assertNotIn('echo  ',(await a.call('wm',{'name':'list'}))['text'])
            self.assertIn('not admitted',(await a.call('wm',{'name':'echo','args':{}}))['text'])
            self.assertFalse(rejected.invoked);self.assertFalse(canary.exists())
            accepted=FakeSandbox(True);b=Adapter(core,{'python_sandbox':accepted})
            self.assertEqual(set(b.tools),{'wm'})
            self.assertEqual(json.loads((await b.call('wm',{'name':'echo','args':{}}))['text']),{'ok':True})
            self.assertTrue(accepted.invoked);self.assertFalse(canary.exists())

    async def test_record_addresses_and_turn_windows(self):
        from harnessadapter.adapter.resources import EvidenceStore
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);core=copied(root);ev=root/'evidence/block001';ev.mkdir(parents=True)
            (ev/'q017_case.json').write_text(json.dumps([{'response':'first','observation':'old'},{'response':'second','observation':'new'}]))
            (core/'examples/q17.md').write_text('input: example\noutcome: observed\nderived_from: self\n\nDecisive: second observation.\n')
            a=Adapter(core,{'evidence':EvidenceStore(root/'evidence')})
            recalled=(await a.call('wm',{'name':'recall','args':{'address':'q17'}}))['text']
            self.assertIn('raw record:',recalled)
            traced=(await a.call('wm',{'name':'trace','args':{'address':'q17','from':1}}))['text']
            self.assertIn('turn 1',traced);self.assertNotIn('turn 0',traced)
            self.assertIn('/evidence/block001/q017_case.json',traced)


if __name__=='__main__':unittest.main()
