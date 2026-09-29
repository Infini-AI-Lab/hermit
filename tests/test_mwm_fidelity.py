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
                     'meta/cases/README.md','meta/journal.md','consumption/brief.md']:
            self.assertTrue((root/path).is_file(),path)
        self.assertEqual(adapter.agent_definitions(),[])
        self.assertEqual(set(adapter.tools),{'wm'})
        text=adapter.context()
        sections=['Earlier episodes','=== Environment briefing ===','=== Verified lessons ===','=== Open questions ===',
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


def method_module():
    spec=importlib.util.spec_from_file_location('method_under_test_v6',EVOLVERS/'mwm/prompt.py')
    method=importlib.util.module_from_spec(spec);spec.loader.exec_module(method)
    return method


LESSON='last_confirmed_t: 1\nscope: table:cpi\nderived_from: none (reasoning only)\nbrief: CPI rows are index points, not percent.\nstatus: %s\nstep: S3\ncheck: [{"kind":"cell","file":"a.txt","line":1,"contains":"1.0"}]\nverified_by: postmortem:001\n\nThe caption says index points.\n'


class MwmV6Tests(unittest.IsolatedAsyncioTestCase):
    def test_brief_served_whole(self):
        spec=importlib.util.spec_from_file_location('brief_under_test',ARCH/'mwm/mwm_core/brief.py')
        brief=importlib.util.module_from_spec(spec);spec.loader.exec_module(brief)
        text='x'*(brief.CAP+5000)
        self.assertEqual(brief.served(text),text);self.assertEqual(brief.CAP,30000)

    async def test_schema_name_differs_from_directory_and_dispatch_by_module(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);core=copied(root);tool=core/'tools/linreg-slope';tool.mkdir()
            (tool/'run.py').write_text('SCHEMA={"name":"linreg_slope","description":"Slope","parameters":{"type":"object","properties":{},"required":[]}}\ndef run(**kwargs):return {"slope":1}\n')
            (tool/'test.py').write_text('import run\nassert run.run()["slope"]==1\n')
            (tool/'TOOL.md').write_text('Slope.\nscope: test\nlast_confirmed_t: 0\nderived_from: none (test fixture)\nrepairs: 0\n')
            spec=importlib.util.spec_from_file_location('caps_under_test',core/'mwm_core/capabilities.py')
            # the package-relative copy is what Adapter loads; the bare module is enough for candidates()
            caps=importlib.util.module_from_spec(spec);spec.loader.exec_module(caps)
            found=[c for c in caps.candidates(str(core)) if c['name']=='linreg_slope']
            self.assertEqual(len(found),1);self.assertEqual(found[0]['_module'],'linreg-slope')
            class FakeSandbox:
                def __init__(self):self.tested=[];self.invoked=[]
                def status(self):return {'available':True}
                def test(self,directory):self.tested.append(directory);return {'passed':True,'output':''}
                def invoke(self,directory,args):self.invoked.append(directory);return {'passed':True,'stdout':'{"slope":1}'}
            sandbox=FakeSandbox();a=Adapter(core,{'python_sandbox':sandbox})
            self.assertEqual(json.loads((await a.call('wm',{'name':'linreg_slope','args':{}}))['text']),{'slope':1})
            self.assertEqual(sandbox.invoked,['tools/linreg-slope']);self.assertIn('tools/linreg-slope',sandbox.tested)
            self.assertIn('linreg_slope',(await a.call('wm',{'name':'list'}))['text'])

    def test_lessons_section_catalog_and_status_block(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);core=copied(root);(root/'control').mkdir()
            (core/'memory/L-cpi-units.md').write_text(LESSON%'verified')
            (core/'memory/L-draft.md').write_text(LESSON%'candidate')
            (core/'memory/L-sneaky.md').write_text(LESSON%'candidate'+'status: verified\n')
            (core/'memory/plain-fact.md').write_text('last_confirmed_t: 1\nscope: all\nderived_from: none (reasoning only)\nbrief: A plain fact.\n\nBody.\n')
            text=Adapter(core).context()
            lessons=text[text.index('=== Verified lessons ==='):text.index('=== Open questions ===')]
            self.assertIn('[L-cpi-units] table:cpi · S3 · CPI rows are index points, not percent.',lessons)
            self.assertNotIn('L-draft',lessons);self.assertNotIn('L-sneaky',lessons);self.assertIn('match the convention here',lessons)
            catalog=text[text.index('=== World-model memory (catalog) ==='):text.index('=== Reaching the world model ===')]
            self.assertIn('[plain-fact]',catalog);self.assertNotIn('[L-',catalog)
            method=method_module();state=method.prepare_session(core,root,block=2,tick=5)
            self.assertIn('1 memory entries, 1 verified lessons of 3, 0 tool candidates (admission decided at serve time)',state['wm_counts'])
            (root/'evidence').mkdir();prompt=method.build_prompt(core,root/'evidence','goal')
            self.assertIn('[Lessons index]',prompt);self.assertIn('[L-cpi-units]',prompt)
            self.assertIn('(advisory 30000; nothing is cut)',prompt);self.assertNotIn('TRUNCATED',prompt)

    def test_gates_regex_span_and_lesson_fields(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);core=copied(root)
            (core/'examples/q1.md').write_text('input: x\noutcome: y\nderived_from: self\n\nDecisive: z.\n')
            (core/'consumption/brief.md').write_text('# Briefing\n\nMatch `[0-9]+` digits; the [0-9] class; see q1 and [q1].\n')
            (core/'memory/L-bare.md').write_text('last_confirmed_t: 1\nscope: all\nderived_from: none (reasoning only)\nbrief: A lesson without its lines.\n\nBody.\n')
            advice=method_module()._gates().check(str(core))
            self.assertFalse([a for a in advice if '0-9' in a],advice)
            for field in ('status','step','check'):
                self.assertTrue([a for a in advice if a.startswith('memory/L-bare.md') and f"'{field}:'" in a],(field,advice))


if __name__=='__main__':unittest.main()
