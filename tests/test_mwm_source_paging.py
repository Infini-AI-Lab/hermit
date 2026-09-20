import tempfile, unittest, shutil
from pathlib import Path
from harnessadapter.adapter.loader import Adapter
from harnessadapter.paths import resolve_architecture

class SourcePagingTests(unittest.IsolatedAsyncioTestCase):
 async def test_complete_source_through_real_adapter_interface(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)/'adapter';shutil.copytree(resolve_architecture('mwm'),root)
   (root/'resources').mkdir(exist_ok=True);source='// αβ水\n'*1400
   (root/'resources/large.mjs').write_text(source)
   a=Adapter(root);r=await a.call('wm',{'name':'read','args':{'path':'/world-model/resources/large.mjs'}})
   pages=[r['text']]
   while r['next_offset'] is not None:
    r=await a.call('wm',{'name':'read','args':{'path':'/world-model/resources/large.mjs','offset':r['next_offset'],'expected_sha256':r['sha256']}})
    pages.append(r['text'])
   self.assertEqual(''.join(pages),source)
   self.assertTrue(r['eof'])

if __name__=='__main__':unittest.main()
