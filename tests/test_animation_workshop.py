import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from harnessadapter.paths import resolve_architecture
from harnessadapter.evolution.definition import EvolverDefinition
from harnessadapter.evolution.workspace import Workspace
from harnessadapter.runtime.source_text import read_text_page
from harnessadapter.policy.operation import execute
from harnessadapter.adapter.loader import Adapter

class WorkshopTests(unittest.TestCase):
    def test_full_unicode_paging_and_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); raw='α水🌊\n'*2100
            (root/'code.js').write_text(raw)
            page=read_text_page(root,'code.js',limit=1000); parts=[page['text']]
            while not page['eof']:
                page=read_text_page(root,'code.js',page['next_offset'],1000,page['sha256'])
                parts.append(page['text'])
            self.assertEqual(''.join(parts),raw)
            (root/'code.js').write_text('changed')
            with self.assertRaises(ValueError):read_text_page(root,'code.js',expected_sha256=page['sha256'])
            for path in ('../else','/etc/passwd','.git/config'):
                with self.assertRaises(ValueError):read_text_page(root,path)
            (root/'escape').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):read_text_page(root,'escape')

    def test_architecture_is_generic_and_empty(self):
        source=resolve_architecture('animation-workshop')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            shutil.copytree(source,root/'adapter')
            for name in ('control','evidence','scratch','environment','history','observations'):(root/name).mkdir()
            d=EvolverDefinition('animation-workshop')
            d.prepare_session(root/'adapter',root,block=1,tick=0)
            self.assertFalse((root/'adapter/meta/docket').exists())
            self.assertEqual(json.loads((root/'adapter/index.json').read_text()),[])
            self.assertTrue(Workspace(root).validate()['passed'])
            a=Adapter(root/'adapter')
            self.assertIn('No learned entries',a.context())
            with self.assertRaises(ValueError):Workspace(root).write_file('observations','x','bad')

    def test_preview_snapshot_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in ('control','adapter','scratch','evidence','host'):(root/name).mkdir()
            (root/'control/policy.json').write_text(json.dumps({'requested':{'backend':'codex','network':False}}))
            (root/'scratch/test.html').write_text('<canvas></canvas>')
            self.assertEqual(execute(root,'snapshot_preview',{'area':'scratch','path':'test.html'})['html'],'<canvas></canvas>')
            for area,path in [('evidence','test.html'),('scratch','../control/policy.json')]:
                with self.assertRaises(ValueError):execute(root,'snapshot_preview',{'area':area,'path':path})

if __name__=='__main__':unittest.main()
