"""Task-owned bridge to native Linux/bubblewrap executable capabilities.

Actors never receive the Docker socket. Only the controller can start an
ephemeral, networkless container, bound to that actor's read-only adapter.
No evidence, corpus, credentials, host checkout or Docker socket is mounted.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request


def local_operation(sandbox, payload):
    if not isinstance(payload, dict) or set(payload) != {'operation','arguments'}:
        raise ValueError('Invalid local execution request')
    operation, args = payload['operation'], payload['arguments']
    if not isinstance(args, dict): raise ValueError('Arguments must be an object')
    allowed = {'status': set(), 'test': {'directory'}, 'invoke': {'directory','arguments'},
               'run_code': {'code','arguments'}}
    if operation not in allowed or set(args) != allowed[operation]:
        raise ValueError('Unknown local execution operation or arguments')
    return getattr(sandbox, operation)(**args)


class DockerPythonSandbox:
    def __init__(self, root):
        self.root = Path(root).resolve()
        from paths import REPOSITORY, _config
        from experiment import EXECUTION_IMAGE
        self.repository, self.config, self.image = REPOSITORY, _config, EXECUTION_IMAGE

    def status(self):
        return {'available': True, 'backend': 'linux-bubblewrap-in-networkless-container',
                'reason': '', 'image': self.image}

    def _call(self, operation, arguments):
        if 'directory' in arguments:
            relative = arguments['directory']
            target = (self.root / relative).resolve()
            if not isinstance(relative, str) or not target.is_relative_to(self.root) or not target.is_dir():
                raise ValueError('Tool directory must belong to the bound adapter')
            if not (self.root / relative).absolute() == target:
                raise ValueError('Tool directory must not escape through links')
        native = (self.repository / 'harnessadapter/runtime/python_sandbox.py').read_text()
        script = native + '\nspec=json.loads(sys.stdin.read())\nsandbox=PythonSandbox(Path("/adapter"))\n'
        script += 'print(json.dumps(getattr(sandbox,spec["operation"])(**spec["arguments"])))\n'
        docker = self.config.get('docker_binary') or shutil.which('docker')
        if not docker: raise RuntimeError('Docker is required for native Linux executable capabilities')
        args = [docker]
        docker_host=self.config.get('docker_host') or os.environ.get('OFFICEQA_DOCKER_HOST')
        if docker_host: args += ['--host', docker_host]
        args += ['run', '--rm', '-i', '--network=none', '--read-only',
                 '--cap-add=SYS_ADMIN', '--security-opt', 'seccomp=unconfined',
                 '--security-opt','apparmor=unconfined','--security-opt','systempaths=unconfined',
                 '--tmpfs', '/tmp:rw,nosuid,nodev',
                 '--mount', f'type=bind,source={self.root},target=/adapter,readonly',
                 self.image, '-c', script]
        env = {'PATH': os.environ.get('PATH','/usr/bin:/bin'),
               'HOME': os.environ.get('HOME','/tmp')}
        result = subprocess.run(args, input=json.dumps({'operation':operation,'arguments':arguments}),
                                capture_output=True, text=True, env=env)
        if result.returncode:
            raise RuntimeError('Isolated Linux executor failed: ' + result.stderr[-1600:])
        return json.loads(result.stdout)

    def test(self, directory): return self._call('test', {'directory':directory})
    def invoke(self, directory, arguments): return self._call('invoke', {'directory':directory,'arguments':arguments})
    def run_code(self, code, arguments): return self._call('run_code', {'code':code,'arguments':arguments})


class RelayPythonSandbox:
    def __init__(self, endpoint):
        from urllib.parse import urlparse
        parsed=urlparse(endpoint)
        if (parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path.rstrip('/')!='/v1'):
            raise ValueError('Only the assigned localhost relay is permitted')
        self.endpoint = endpoint + '/local-mwm'

    def _call(self, operation, arguments):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(self.endpoint,
            data=json.dumps({'operation':operation,'arguments':arguments}).encode(),
            headers={'Content-Type':'application/json'})
        with opener.open(request, timeout=None) as response:
            return json.load(response)

    def status(self): return self._call('status', {})
    def test(self, directory): return self._call('test', {'directory':directory})
    def invoke(self, directory, arguments): return self._call('invoke', {'directory':directory,'arguments':arguments})
    def run_code(self, code, arguments): return self._call('run_code', {'code':code,'arguments':arguments})


def render_exports(adapter, manifest, python):
    from harnessadapter.adapter.loader import Adapter
    value = Adapter(Path(adapter), resources={'python_sandbox': DockerPythonSandbox(adapter)})
    context = value.context() + '\n\nLocal execution: wm(name="run", args={"code": "...", "args": {}}) also executes Python in the same Linux isolation; set result in code. No external environment is mounted. Native learned roles can run through Codex.\n'
    return {'context':context, 'agents':value.agent_definitions()}


async def serve(adapter_path, endpoint):
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations
    from harnessadapter.adapter.loader import Adapter
    sandbox = RelayPythonSandbox(endpoint)
    adapter = Adapter(Path(adapter_path), resources={'python_sandbox':sandbox})
    async def list_tools(context, params):
        return ListToolsResult(tools=[Tool(name=t['name'], description=t['description'],
            inputSchema=t['input_schema'], outputSchema=t['output_schema'],
            annotations=ToolAnnotations(**t['annotations'])) for t in adapter.tools.values()])
    async def call_tool(context, params):
        try:
            arguments = params.arguments or {}
            if params.name == 'wm' and arguments.get('name') == 'run':
                inner = arguments.get('args') or {}
                result = {'text':json.dumps(await asyncio.to_thread(sandbox.run_code,
                    inner.get('code',''), inner.get('args') or {}))}
            else:
                result = await adapter.call(params.name, arguments)
            return CallToolResult(content=[TextContent(text=json.dumps(result))], structuredContent=result)
        except Exception as exc:
            return CallToolResult(content=[TextContent(text=str(exc))], isError=True)
    server = Server('mwm', on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == '__main__':
    import argparse
    p=argparse.ArgumentParser(); p.add_argument('--adapter',required=True); p.add_argument('--endpoint',required=True)
    args=p.parse_args(); asyncio.run(serve(args.adapter,args.endpoint))
