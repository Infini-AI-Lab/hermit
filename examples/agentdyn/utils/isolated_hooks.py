"""Untrusted hooks in a networkless Docker container, exchanging bounded JSON only.

The trusted caller owns tools, classifier credentials, environment and scoring.
Docker is required; failure never falls back to executing a hook on the host.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import select
import subprocess
import tempfile
import time
import uuid

from hermit.agent.evolver.methods.evosafe.hooks import ToolCallContext, ToolCallResult

MAX_MESSAGE = 2 * 1024 * 1024

def remove_container(name):
    removed = subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, timeout=20)
    if removed.returncode:
        # --rm can remove the container first. Distinguish that race from a
        # Docker error before the caller clears the cleanup registration.
        remaining = subprocess.check_output(
            ['docker', 'ps', '-aq', '--filter', f'name=^/{re.escape(name)}$'],
            text=True, timeout=20)
        if remaining.strip():
            raise RuntimeError(f'episode container cleanup incomplete: {name}')

class IsolatedDefense:
    def __init__(self, adapter, *, image='python:3.12-slim', timeout=90, name=None):
        self.name = name or 'hermit-hook-' + uuid.uuid4().hex
        self.timeout = float(timeout)
        self.buffer = b''
        self.temp = tempfile.TemporaryDirectory(prefix='hermit-hook-')
        self.proc = None
        registry = os.environ.get('HERMIT_EPISODE_REGISTRY')
        self.registration = Path(registry) / self.name if registry else None
        if self.registration is not None:
            self.registration.parent.mkdir(parents=True, exist_ok=True)
            self.registration.touch()
        root = Path(self.temp.name)
        root.chmod(0o755)
        source = Path(adapter) / 'defense.py'
        if source.is_symlink() or not source.is_file():
            self.temp.cleanup()
            raise ValueError('defense.py must be a regular file')
        (root / 'defense.py').write_bytes(source.read_bytes())
        from hermit.agent.evolver.methods.evosafe import hooks
        (root / 'sdk.py').write_bytes(Path(hooks.__file__).read_bytes())
        (root / 'worker.py').write_bytes(Path(__file__).with_name('hook_worker.py').read_bytes())
        for path in root.iterdir():
            path.chmod(0o444)
        command = ['docker', 'run', '--rm', '-i', '--name', self.name, '--network=none',
                   '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                   '--pids-limit=32', '--memory=256m', '--cpus=1', '--user=65534:65534',
                   '--log-driver=none', '--tmpfs=/tmp:rw,noexec,nosuid,size=16m']
        for filename in ('defense.py', 'sdk.py', 'worker.py'):
            command += ['--mount', f'type=bind,src={root / filename},dst=/{filename},readonly']
        command += [image, 'python', '-I', '-B', '-u', '/worker.py']
        try:
            self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL)
            os.set_blocking(self.proc.stdin.fileno(), False)
            os.set_blocking(self.proc.stdout.fileno(), False)
            ready = self._read(time.monotonic() + self.timeout)
            self.hook_api = ready['ready']
            if self.hook_api not in ('dict-v1', 'structured-v1'):
                raise ValueError('unsupported hook API')
        except BaseException:
            self.close()
            raise

    def _write(self, value, deadline):
        data = (json.dumps(value) + '\n').encode()
        if len(data) > MAX_MESSAGE:
            raise ValueError('hook message exceeds limit')
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.proc.stdin], [], remaining)[1]:
                raise TimeoutError('hook deadline exceeded')
            data = data[os.write(self.proc.stdin.fileno(), data):]

    def _read(self, deadline):
        while b'\n' not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.proc.stdout], [], [], remaining)[0]:
                raise TimeoutError('hook deadline exceeded')
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError('isolated hook exited; Docker and worker startup must succeed')
            self.buffer += chunk
            if len(self.buffer) > MAX_MESSAGE:
                raise ValueError('hook response exceeds limit')
        line, self.buffer = self.buffer.split(b'\n', 1)
        reply = json.loads(line)
        if not isinstance(reply, dict):
            raise ValueError('invalid hook response')
        return reply

    def _call(self, method, args, classifier=None):
        deadline = time.monotonic() + self.timeout
        try:
            self._write({'method': method, 'args': args, 'classifier': classifier is not None}, deadline)
            calls = 0
            while True:
                reply = self._read(deadline)
                if 'classify' not in reply:
                    break
                calls += 1
                if classifier is None or calls > 16 or not isinstance(reply['classify'], str):
                    raise ValueError('invalid classifier RPC')
                self._write({'classification': classifier(reply['classify'])}, deadline)
            if 'refused' in reply:
                raise PermissionError(str(reply['refused'])[:1024])
            if 'error' in reply:
                raise RuntimeError('isolated hook failed: ' + str(reply['error'])[:128])
            return reply
        except PermissionError:
            raise
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _context(ctx):
        return {k: getattr(ctx, k) for k in ('framework', 'server', 'tool_name', 'arguments', 'trace_id', 'metadata')}

    def system_prompt_transform(self, prompt):
        return self._call('system_prompt_transform', [prompt])['value']

    def on_pre_tool_call(self, ctx):
        structured = isinstance(ctx, ToolCallContext)
        reply = self._call('on_pre_tool_call', [self._context(ctx) if structured else ctx],
                           ctx.classify_output if structured else None)
        if 'context' in reply:
            revised = ToolCallContext(**reply['context'])
            revised.classify_output = ctx.classify_output
            return revised
        if 'metadata' in reply:
            if not isinstance(reply['metadata'], dict):
                raise ValueError('invalid hook metadata')
            ctx.metadata = reply['metadata']
        return reply['value']

    def on_post_tool_call(self, ctx, result):
        if not isinstance(ctx, ToolCallContext):
            return self._call('on_post_tool_call', [ctx, result])['value']
        def encode(value):
            if hasattr(value, 'model_dump'):
                return value.model_dump(mode='json')
            raise TypeError('unsupported native tool result')
        raw = json.loads(json.dumps(result.raw, default=encode))
        payload = {'raw': raw, 'is_error': result.is_error, 'duration': result.duration,
                   'error': result.error, 'exception': None}
        reply = self._call('on_post_tool_call', [self._context(ctx), payload], ctx.classify_output)
        value = reply['result']
        # Never deserialize candidate-provided objects or exception types on the host.
        if value.get('exception') is not None:
            raise ValueError('invalid hook exception')
        if value['raw'] == raw:
            value['raw'] = result.raw
        elif not isinstance(value['raw'], str):
            raise ValueError('rewritten observation must be text')
        return ToolCallResult(**value)

    def close(self):
        try:
            remove_container(self.name)
            if self.registration is not None:
                self.registration.unlink(missing_ok=True)
        finally:
            if self.proc is not None:
                self.proc.kill() if self.proc.poll() is None else None
                self.proc.communicate(timeout=5)
                self.proc = None
            self.temp.cleanup()
