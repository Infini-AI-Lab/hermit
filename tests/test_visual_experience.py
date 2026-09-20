"""Real MCP image delivery, bounded reads, and explicit Codex effort selection."""
import base64
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from harnessadapter.policy.execution import ExecutionPolicy
from harnessadapter.evolution.backends import command_for, codex_turn_outcome


def make_png():
    def chunk(kind,data):
        return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    pixels=b''.join(b'\0'+os.urandom(128*3) for _ in range(128))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',128,128,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b'')


@unittest.skipUnless(shutil.which('codex') and sys.platform in ('darwin','linux'),'requires Codex sandbox')
class VisualExperienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_blocks_and_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            for name in ('adapter','scratch','control','host','evidence','method','environment','history'):(root/name).mkdir()
            (root/'control/policy.json').write_text(json.dumps({'requested':ExecutionPolicy(network=False).serialize()}))
            data=make_png();self.assertGreater(len(data),30000)
            (root/'evidence/frame.png').write_bytes(data)
            (root/'control/private.png').write_bytes(data)
            (root/'evidence/escape.png').symlink_to(root/'control/private.png')
            (root/'evidence/fake.png').write_text('not an image')
            (root/'evidence/huge.png').write_bytes(b'\x89PNG\r\n\x1a\n'+bytes(4*1024*1024))
            params=StdioServerParameters(command=sys.executable,args=['-m','harnessadapter.evolution.server','--session',str(root)])
            async with Client(params,read_timeout_seconds=30) as client:
                reply=await client.call_tool('read_image',{'area':'evidence','path':'frame.png'})
                self.assertFalse(reply.is_error,reply)
                image=next(x for x in reply.content if x.type=='image')
                self.assertEqual(image.mime_type,'image/png')
                self.assertEqual(base64.b64decode(image.data),data)
                canonical=await client.call_tool('read_image',{'area':'learning_material','path':'frame.png'})
                self.assertFalse(canonical.is_error)
                self.assertEqual(base64.b64decode(next(x for x in canonical.content if x.type=='image').data),data)
                blocked=await client.call_tool('write_file',{'area':'learning_material','path':'frame.png','content':'bad'})
                self.assertTrue(blocked.is_error)
                for area,path in [('control','private.png'),('evidence','../control/private.png'),('evidence','escape.png'),('learning_material','escape.png'),('evidence','fake.png'),('evidence','huge.png')]:
                    with self.subTest(area=area,path=path):
                        rejected=await client.call_tool('read_image',{'area':area,'path':path})
                        self.assertTrue(rejected.is_error)
            records=[json.loads(s) for s in (root/'control/file-calls.jsonl').read_text().splitlines()]
            self.assertEqual(records[0]['image']['bytes'],len(data))
            self.assertNotIn('data',records[0])

    def test_explicit_effort_is_in_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();config=root/'mcp.json'
            config.write_text(json.dumps({'mcpServers':{'evolution':{'command':sys.executable,'args':[]}}}))
            argv=command_for(root,ExecutionPolicy(),config,3,model='gpt-5.6-sol',reasoning_effort='high')
            efforts=[a for a in argv if a.startswith('model_reasoning_effort=')]
            self.assertEqual(efforts[-1],'model_reasoning_effort="high"')
            self.assertIn('model="gpt-5.6-sol"',argv)
            with self.assertRaises(ValueError):command_for(root,ExecutionPolicy(backend='claude-code'),config,3,reasoning_effort='high')


class CompletionTests(unittest.TestCase):
    def test_recovered_transport_error_does_not_reject_completed_turn(self):
        self.assertFalse(codex_turn_outcome([{'type':'error','message':'Reconnecting'}, {'type':'turn.completed'}])['is_error'])
        for events in ([{'type':'error'}], [{'type':'turn.completed'},{'type':'turn.failed'}], [{'type':'turn.completed'},{'type':'error'}]):
            self.assertTrue(codex_turn_outcome(events)['is_error'])
