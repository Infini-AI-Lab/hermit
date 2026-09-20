#!/usr/bin/env python3
"""Framework-owned learner tools with enforced file, image, and execution boundaries."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent
from mcp.server.mcpserver.exceptions import ToolError

from harnessadapter.evolution.workspace import Workspace
from harnessadapter.policy.execution import ExecutionPolicy, PolicySandbox
import sys
import re
import uuid
from urllib.parse import urlparse


def create_server(root):
    workspace = Workspace(root)
    policy_text = (root / 'control/policy.json').read_text()
    policy_document = json.loads(policy_text)
    policy = ExecutionPolicy.parse(policy_document['requested'])
    sandbox = PolicySandbox(root, policy)
    audit = root / 'control' / 'file-calls.jsonl'
    server = MCPServer('harnessadapter-evolution', log_level='WARNING')

    def call(name, **args):
        record = {'time': datetime.now(timezone.utc).isoformat(), 'tool': name,
                  'arguments': {k:v for k,v in args.items() if k != 'content'}}
        try:
            if (root / 'control/policy.json').read_text() != policy_text:
                raise ValueError('Execution policy changed during the session; refusing to continue')
            if name == 'get_policy':
                result = {'requested': policy.serialize(),
                          'adapter': str(root / 'adapter'), 'learning_material': str(root / 'evidence'), 'evidence': str(root / 'evidence'),
                          'scratch': str(root / 'scratch'),
                          'validation': str(root / 'validation') if (root/'validation').is_dir() else None,
                          'working_directory': policy.working_directory or str(root / 'adapter'),
                          'write_rule': 'Only candidate adapter and isolated scratch are writable; LearningMaterial is always readable and read-only.'}
            elif name == 'request_preview':
                if not policy_document.get('preview_enabled'):
                    raise ValueError('No browser preview renderer is bound')
                queue = root / 'control/preview_jobs'
                queue.mkdir(exist_ok=True)
                if len(list(queue.iterdir())) >= 2:
                    raise ValueError('This evolution has used its two preview requests')
                snapshot = sandbox.operation('snapshot_preview', args)
                identifier = uuid.uuid4().hex
                job = queue / identifier
                job.mkdir()
                (job / 'index.html').write_text(snapshot['html'])
                result = {'id': identifier, 'status': 'pending', 'source_sha256': snapshot['sha256'],
                          'network': policy.network, 'source': args, 'poll_after_seconds': 15}
                (job / 'status.json').write_text(json.dumps(result))
            elif name == 'get_preview':
                if not re.fullmatch(r'[0-9a-f]{32}', args['identifier']):
                    raise ValueError('Invalid preview identifier')
                result = json.loads((root / 'control/preview_jobs' / args['identifier'] / 'status.json').read_text())
            elif name == 'run_command':
                result = sandbox.run(['/bin/sh', '-c', args['command']], cwd=args.get('cwd') or None,
                                     timeout=min(max(args.get('timeout_seconds', 30), 1), 90))
            elif name == 'fetch_url':
                if not policy.network:
                    raise ValueError('Network access is disabled by the execution policy')
                if urlparse(args['url']).scheme not in ('http', 'https'):
                    raise ValueError('Only HTTP and HTTPS URLs are supported')
                script = ('import json,sys,urllib.request; '
                          'r=urllib.request.urlopen(sys.argv[1],timeout=15); '
                          'b=r.read(30001); print(json.dumps({"url":r.url,"status":r.status,'
                          '"text":b[:30000].decode("utf-8",errors="replace"),"truncated":len(b)>30000})); r.close()')
                response = sandbox.run([sys.executable, '-c', script, args['url']], timeout=20)
                if response['exit_code']:
                    raise ValueError('Fetch failed: ' + response['stderr'][-1000:])
                result = json.loads(response['stdout'])
            elif name == 'test_python':
                # MWM admission intentionally has the stricter production-tool sandbox.
                result = workspace.test_python(**args)
            else:
                # Even file operations execute under the OS boundary, not just
                # a vulnerable check-then-open path test in a privileged broker.
                result = sandbox.operation(name, args)
            if name == 'read_image':
                record['image']={key:result[key] for key in ('mime_type','sha256','bytes')}
            record['status'] = 'ok'
            return result
        except Exception as exc:
            record.update(status='error', error=str(exc))
            raise ToolError(str(exc)) from exc
        finally:
            with audit.open('a') as stream:
                stream.write(json.dumps(record) + '\n')

    @server.tool(structured_output=True)
    def request_preview(area: str, path: str) -> dict[str, Any]:
        """If a renderer is bound, snapshot one self-contained HTML example (<=1MB) for asynchronous canvas frames. Max two per session; no local companion files. CDN follows network policy. These are observations, not a quality score."""
        return call('request_preview', area=area, path=path)

    @server.tool(structured_output=True)
    def get_preview(identifier: str) -> dict[str, Any]:
        """Read pending/complete/error preview status. Wait at least 15 seconds between polls; do other work while pending. Completed images are in read-only observations/."""
        return call('get_preview', identifier=identifier)

    @server.tool(structured_output=True)
    def list_files(area: str) -> dict[str, Any]:
        """List adapter, learning_material (legacy evidence), history, scratch, method, environment, observations, or bound validation files."""
        return call('list_files', area=area)

    @server.tool(structured_output=True)
    def read_file(area: str, path: str, offset: int = 0) -> dict[str, Any]:
        """Read up to 30,000 characters from a relative file; follow next_offset."""
        return call('read_file', area=area, path=path, offset=offset)

    @server.tool()
    def read_image(area: str, path: str) -> CallToolResult:
        """Display one PNG/JPEG (up to 4 MiB) from an authorized workspace area. For LearningMaterial frames, use area=learning_material and consult their timestamp manifest. Does not play videos."""
        result=call('read_image',area=area,path=path)
        metadata={key:value for key,value in result.items() if key != 'data'}
        return CallToolResult(content=[TextContent(type='text',text=json.dumps(metadata)),
            ImageContent(type='image',data=result['data'],mime_type=result['mime_type'])])

    @server.tool(structured_output=True)
    def write_file(area: str, path: str, content: str) -> dict[str, Any]:
        """Create or replace a UTF-8 file in adapter or scratch; LearningMaterial is read-only."""
        return call('write_file', area=area, path=path, content=content)

    @server.tool(structured_output=True)
    def delete_path(area: str, path: str) -> dict[str, Any]:
        """Delete a file or empty directory in adapter or scratch."""
        return call('delete_path', area=area, path=path)

    @server.tool(structured_output=True)
    def move_path(area: str, source: str, destination: str) -> dict[str, Any]:
        """Rename an adapter/scratch path to a destination that does not already exist."""
        return call('move_path', area=area, source=source, destination=destination)

    @server.tool(structured_output=True)
    def validate() -> dict[str, Any]:
        """Check candidate declarations and syntax without executing candidate code."""
        return call('validate')

    @server.tool(structured_output=True)
    def test_python(directory: str) -> dict[str, Any]:
        """Run an adapter directory's test.py in Linux bubblewrap. No unsandboxed fallback."""
        return call('test_python', directory=directory)

    @server.tool(structured_output=True)
    def get_policy() -> dict[str, Any]:
        """Show immutable grants: core read/write, LearningMaterial read-only, configured environment reads and network."""
        return call('get_policy')

    @server.tool(structured_output=True)
    def read_path(path: str, offset: int = 0) -> dict[str, Any]:
        """Read a filesystem path only within the user-defined read scope. LearningMaterial is always readable."""
        return call('read_path', path=path, offset=offset)

    @server.tool(structured_output=True)
    def list_path(path: str, offset: int = 0) -> dict[str, Any]:
        """List up to 200 entries in an authorized directory; follow next_offset."""
        return call('list_path', path=path, offset=offset)

    @server.tool(structured_output=True)
    def run_command(command: str, cwd: str = '', timeout_seconds: int = 30) -> dict[str, Any]:
        """Run a shell command in the OS sandbox. Writes: adapter/scratch only. Reads/network follow policy."""
        return call('run_command', command=command, cwd=cwd, timeout_seconds=timeout_seconds)

    @server.tool(structured_output=True)
    def fetch_url(url: str) -> dict[str, Any]:
        """Fetch an HTTP(S) resource when network is enabled; disabled sessions reject the call."""
        return call('fetch_url', url=url)

    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, required=True)
    args = parser.parse_args()
    create_server(args.session.resolve()).run(transport='stdio')
