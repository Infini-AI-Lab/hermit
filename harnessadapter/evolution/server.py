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
            elif name == 'try_tool':
                specs = policy_document.get('bindings') or []
                if not specs:
                    raise ValueError('No resource bindings were supplied to this session')
                # Candidate code runs in a child under the OS sandbox with no
                # network, never in this process; its binding calls come back
                # here over a pipe and this process (the only one that may
                # reach the services) performs them on the real bindings.
                if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', str(args['tool'])):
                    raise ValueError('tool must be a declared tool name')
                from harnessadapter.adapter.bindings import parse_bindings
                real = parse_bindings(specs)
                argv = [sys.executable, '-m', 'harnessadapter.adapter.try_tool', '--adapter', str(root / 'adapter'),
                        '--tool', str(args['tool']), '--arguments', json.dumps(args.get('arguments') or {})]
                for binding_name, obj in real.items():
                    argv += ['--binding', f"{binding_name}:{obj.describe() if hasattr(obj, 'describe') else ''}"]

                def serve(request):
                    obj = real[request['binding']]
                    method = str(request.get('method') or '')
                    if method.startswith('_') or not callable(getattr(obj, method, None)):
                        raise ValueError(f'binding {request["binding"]!r} has no method {method!r}')
                    return getattr(obj, method)(*request.get('args') or [], **(request.get('kwargs') or {}))

                child = sandbox.run_rpc(argv, serve, cwd=root / 'adapter', timeout=120)
                if child['timed_out']:
                    raise ValueError('try_tool timed out after 120 s')
                response = child['done']
                if not isinstance(response, dict):
                    raise ValueError('try_tool produced no result: ' + child['stderr'][-1200:])
                if 'error' in response:
                    raise ValueError(response['error'])
                result = response['result']
            elif name == 'sql':
                specs = [b for b in (policy_document.get('bindings') or []) if str(b).startswith('sql=')]
                if not specs:
                    raise ValueError('No sql binding was supplied to this session')
                from harnessadapter.adapter.bindings import parse_bindings
                result = parse_bindings(specs)['sql'].query(args['query'])
                with (root / 'control' / 'environment-calls.jsonl').open('a') as stream:
                    stream.write(json.dumps({'time': record['time'], 'kind': 'sql', 'query': args['query'],
                                             'rows': len(result.get('rows', [])) if isinstance(result, dict) else None,
                                             'error': result.get('error') if isinstance(result, dict) else None}) + '\n')
            elif name == 'history':
                from harnessadapter.adapter import versioning
                if args.get('path'):
                    if not re.fullmatch(r'[A-Za-z0-9_./-]{1,200}', args['path']) or '..' in args['path']:
                        raise ValueError('path must be a relative adapter path')
                    result = {'diff': versioning.diff(root / 'adapter', args.get('ref') or 'HEAD~1', args['path'])}
                elif args.get('ref'):
                    result = {'diff': versioning.diff(root / 'adapter', args['ref'])}
                else:
                    result = {'log': versioning.log(root / 'adapter', args.get('n', 10))}
            elif name == 'small_model':
                specs = [b for b in (policy_document.get('bindings') or []) if str(b).startswith('small_model=')]
                if not specs:
                    raise ValueError('No small_model binding was supplied to this session')
                from harnessadapter.adapter.bindings import parse_bindings
                reader = parse_bindings(specs)['small_model']
                messages = args['messages']
                if not isinstance(messages, list) or not all(isinstance(m, dict) and 'role' in m and 'content' in m for m in messages):
                    raise ValueError('messages must be a list of {role, content}')
                result = reader.chat(messages, max_tokens=min(max(int(args.get('max_tokens', 1500)), 1), 4000), temperature=0.0)
            elif name == 'run_command':
                result = sandbox.run(['/bin/sh', '-c', args['command']], cwd=args.get('cwd') or None,
                                     timeout=min(max(args.get('timeout_seconds', 30), 1), 600))
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
    def try_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Load the current candidate adapter with the session's resource bindings and call one declared tool. Edits to the candidate are picked up on every call. Returns the tool's JSON output (reader usage included when the tool reports it)."""
        return call('try_tool', tool=name, arguments=arguments)

    @server.tool(structured_output=True)
    def bash(command: str, cwd: str = '', timeout_seconds: int = 30) -> dict[str, Any]:
        """Run a shell command in the sandbox (the native name of run_command): no network, adapter and scratch writable, everything else read-only."""
        return call('run_command', command=command, cwd=cwd, timeout_seconds=timeout_seconds)

    @server.tool(structured_output=True)
    def captest(directory: str) -> dict[str, Any]:
        """Run a learned tool's own test (tools/<name>/test.py) in the admission sandbox; the native name of test_python. Passing is what admits the tool."""
        return call('test_python', directory=directory)

    @server.tool(structured_output=True)
    def sql(query: str) -> dict[str, Any]:
        """Run one read-only SQL statement (SELECT/WITH/PRAGMA/EXPLAIN) against the bound environment database. Rows and characters are capped by the service; every statement is logged to the session. Available only when the session bound `sql`."""
        return call('sql', query=query)

    @server.tool(structured_output=True)
    def history(n: int = 10, ref: str = '', path: str = '') -> dict[str, Any]:
        """Read the candidate adapter's revision history (git, framework-maintained). No arguments: the last n commits with file stats. `ref` (e.g. HEAD~1): the diff from that revision to the working candidate; add `path` to limit it to one file."""
        return call('history', n=n, ref=ref or None, path=path or None)

    @server.tool(structured_output=True)
    def small_model(messages: list[dict[str, Any]], max_tokens: int = 1500) -> dict[str, Any]:
        """Call the bound reader model directly with chat messages (temperature 0). Use it to try a reader prompt on document text taken from the learning material."""
        return call('small_model', messages=messages, max_tokens=max_tokens)

    @server.tool(structured_output=True)
    def list_files(area: str) -> dict[str, Any]:
        """List adapter, learning_material (legacy evidence), history, scratch, method, environment, observations, deployment (when supplied), or bound validation files."""
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
