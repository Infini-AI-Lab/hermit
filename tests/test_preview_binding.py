import json
import tempfile
import unittest
from pathlib import Path
from mcp import Client
from mcp.client.stdio import StdioServerParameters
import sys

class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_status_limit_and_readonly_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=Path(tmp)
            for d in ('control','adapter','scratch','host','evidence','method','environment','history','observations'):(r/d).mkdir()
            (r/'control/policy.json').write_text(json.dumps({'requested':{'backend':'codex','network':False},'preview_enabled':True}))
            (r/'scratch/trial.html').write_text('<canvas></canvas>')
            params=StdioServerParameters(command=sys.executable,args=['-m','harnessadapter.evolution.server','--session',str(r)])
            async with Client(params,read_timeout_seconds=30) as c:
                out=await c.call_tool('request_preview',{'area':'scratch','path':'trial.html'})
                self.assertFalse(out.is_error)
                result=out.structured_content;identifier=result['id']
                self.assertEqual(result['status'],'pending');self.assertFalse(result['network'])
                snapshot=r/'control/preview_jobs'/identifier/'index.html'
                self.assertEqual(snapshot.read_text(),'<canvas></canvas>')
                (r/'scratch/trial.html').write_text('changed')
                self.assertEqual(snapshot.read_text(),'<canvas></canvas>')
                status=await c.call_tool('get_preview',{'identifier':identifier})
                self.assertEqual(status.structured_content['status'],'pending')
                forbidden=await c.call_tool('write_file',{'area':'observations','path':'fake','content':'fake'})
                self.assertTrue(forbidden.is_error)
                self.assertFalse((await c.call_tool('request_preview',{'area':'scratch','path':'trial.html'})).is_error)
                self.assertTrue((await c.call_tool('request_preview',{'area':'scratch','path':'trial.html'})).is_error)
                self.assertTrue((await c.call_tool('get_preview',{'identifier':'../policy'})).is_error)

if __name__=='__main__':unittest.main()
