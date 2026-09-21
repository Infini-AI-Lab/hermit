"""Framework-owned evolution policy and a shared OS sandbox executor.

The initial executor uses the installed Codex `sandbox` subcommand (no model
call) for either learner backend. Missing/ineffective enforcement fails closed.
"""
from dataclasses import dataclass
import json
import http.server
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import tomllib
import uuid


@dataclass(frozen=True)
class ExecutionPolicy:
    backend: str = 'codex'
    read_all: bool = False
    read_paths: tuple[str, ...] = ()
    network: bool = True
    working_directory: str | None = None

    @classmethod
    def parse(cls, data=None, base=None):
        data = data or {};base=Path(base or Path.cwd())
        if not isinstance(data,dict) or set(data)-{'backend','read_access','network','working_directory'}:
            raise ValueError('Policy supports backend, working_directory, read_access, and network; writable roots are fixed by infra')
        backend=data.get('backend','codex');network=data.get('network',True)
        if backend not in ('codex','claude-code') or not isinstance(network,bool):
            raise ValueError('backend must be codex/claude-code; network must be a boolean')
        working=data.get('working_directory')
        if working is not None:
            if not isinstance(working,str) or not working.strip():raise ValueError('working_directory must be a directory path')
            path=Path(working).expanduser()
            path=(base/path).resolve() if not path.is_absolute() else path.resolve()
            if not path.is_dir():raise ValueError('working_directory does not exist')
            working=str(path)
        access=data.get('read_access',{'paths':[]})
        if access=='all':return cls(backend,True,(),network,working)
        if not isinstance(access,dict) or set(access)!={'paths'} or not isinstance(access['paths'],list):
            raise ValueError('read_access must be "all" or {"paths": [...]}')
        roots=[]
        for value in access['paths']:
            if not isinstance(value,str) or not value.strip():raise ValueError('Read paths must be nonempty strings')
            path=Path(value).expanduser()
            path=(base/path).resolve() if not path.is_absolute() else path.resolve()
            if not path.is_dir():raise ValueError(f'Read root is not a directory: {path}')
            roots.append(str(path))
        return cls(backend,False,tuple(dict.fromkeys(roots)),network,working)

    def serialize(self):
        result={'backend':self.backend,'read_access':'all' if self.read_all else {'paths':list(self.read_paths)},'network':self.network}
        if self.working_directory is not None:result['working_directory']=self.working_directory
        return result


def load_policy(path=None, *, backend=None, read_paths=None, read_all=False, network=None, defaults=None, working_directory=None):
    data=dict(defaults or {});base=Path.cwd()
    if path:
        path=Path(path).resolve();base=path.parent
        if path.suffix in ('.yaml','.yml'):
            import yaml
            override=yaml.safe_load(path.read_text())
        else:
            override=tomllib.loads(path.read_text()) if path.suffix=='.toml' else json.loads(path.read_text())
        if not isinstance(override,dict):raise ValueError('Policy configuration must be an object')
        data.update(override)
    # Validate before applying overrides so misspelled/unsupported fields never disappear.
    parsed=ExecutionPolicy.parse(data,base).serialize()
    if backend is not None:parsed['backend']=backend
    if read_all:parsed['read_access']='all'
    elif read_paths is not None:parsed['read_access']={'paths':[str(Path(p).expanduser().resolve()) for p in read_paths]}
    if network is not None:parsed['network']=network
    if working_directory is not None:parsed['working_directory']=str(Path(working_directory).expanduser().resolve())
    return ExecutionPolicy.parse(parsed)


def toml_value(value):
    if isinstance(value,bool):return str(value).lower()
    if isinstance(value,str):return json.dumps(value,ensure_ascii=False)
    if isinstance(value,(int,float)):return str(value)
    if isinstance(value,list):return '['+','.join(toml_value(v) for v in value)+']'
    if isinstance(value,dict):return '{'+','.join(json.dumps(k,ensure_ascii=False)+'='+toml_value(v) for k,v in value.items())+'}'
    raise TypeError(type(value))


class PolicySandbox:
    PROFILE='harnessadapter_evolution'

    def __init__(self, session: Path, policy: ExecutionPolicy):
        self.session=session.resolve();self.policy=policy
        self.adapter=self.session/'adapter';self.scratch=self.session/'scratch'
        self.readable=[self.session/n for n in ('adapter','scratch','evidence','method','environment','history','observations')]
        if (self.session/'validation').is_dir():
            self.readable.append(self.session/'validation')
        if (self.session/'deployment').is_dir():
            self.readable.append(self.session/'deployment')
        self.readable += [Path(p) for p in policy.read_paths]
        if policy.working_directory:self.readable.append(Path(policy.working_directory))
        self.executor=shutil.which('codex')
        # The package and its checkout (editable installs resolve through the
        # checkout) are runtime infrastructure, read-only inside the sandbox.
        package=Path(__file__).resolve().parents[1]
        self.runtime_roots=list(dict.fromkeys([Path(sys.prefix).resolve(),Path(sys.base_prefix).resolve(),package,package.parent]))
        # Node is optional infrastructure for JavaScript examples, not a user-data grant.
        node = shutil.which('node')
        if node:
            real_node = Path(node).resolve()
            self.runtime_roots += [Path(node), real_node.parent.parent]
            if sys.platform == 'darwin':
                if Path('/opt/homebrew/etc/openssl@3').is_dir():
                    self.runtime_roots.append(Path('/opt/homebrew/etc/openssl@3'))
                # Homebrew Node uses shared libraries outside its own keg.
                pending, seen = [real_node], set()
                while pending:
                    binary = pending.pop()
                    if binary in seen: continue
                    seen.add(binary)
                    links = subprocess.run(['/usr/bin/otool', '-L', str(binary)], capture_output=True, text=True, timeout=5)
                    for line in links.stdout.splitlines()[1:]:
                        name = line.strip().split(' (', 1)[0]
                        if not name.startswith('/opt/homebrew/'): continue
                        dependency = Path(name)
                        if dependency.exists():
                            resolved = dependency.resolve()
                            self.runtime_roots += [Path('/opt/homebrew/opt'), resolved.parent.parent]
                            if resolved not in seen: pending.append(resolved)
        # Linux: the sandbox re-executes the codex binary under the name
        # codex-linux-sandbox, found on PATH inside the bwrap tree. The
        # standalone release ships no such file, so a session-local link
        # supplies it; its directory and the release directory become
        # read-only runtime infrastructure (measured 2026-09-18, codex 0.155).
        self.helper_dir=None
        if self.executor and sys.platform.startswith('linux'):
            real=Path(self.executor).resolve()
            self.helper_dir=self.session/'host/sandbox-bin';self.helper_dir.mkdir(parents=True,exist_ok=True)
            link=self.helper_dir/'codex-linux-sandbox'
            if link.is_symlink() or link.exists():link.unlink()
            link.symlink_to(real)
            self.runtime_roots+=[real.parent,self.helper_dir.resolve()]

    def can_read(self, path: Path):
        resolved=path.expanduser().resolve()
        return self.policy.read_all or any(resolved.is_relative_to(root) for root in self.readable)

    def filesystem(self, *, writable=True):
        # Runtime code and interpreter libraries are infrastructure, not user read grants.
        fs={':minimal':'read'}
        if self.policy.read_all:fs['/']='read'
        for p in self.readable+self.runtime_roots:fs[str(p)]='read'
        # The operation helper reads only its immutable policy and the
        # definition's protected-path list, not arbitrary control data.
        fs[str(self.session/'control/policy.json')]='read'
        fs[str(self.session/'control/protected.json')]='read'
        for p in (self.adapter,self.scratch):
            if p.is_symlink() or p.resolve()!=p:raise ValueError('A sandbox root was replaced; refusing execution')
            fs[str(p)]='write' if writable else 'read'
        return fs

    def profile(self, *, network=None, writable=True):
        return {'filesystem':self.filesystem(writable=writable),
                'network':{'enabled':self.policy.network if network is None else network}}

    def run(self, argv, *, cwd=None, network=None, timeout=30, output_limit=30000):
        if not self.executor or sys.platform not in ('darwin','linux'):
            raise RuntimeError('Enforcement unavailable: this release requires Codex CLI with its macOS/Linux sandbox backend')
        cwd=Path(cwd or self.policy.working_directory or self.adapter).resolve()
        if not cwd.is_dir() or not self.can_read(cwd):raise ValueError('Command cwd must be in the readable scope')
        profile=self.profile(network=network)
        command=[self.executor,'sandbox','-P',self.PROFILE,'-C',str(cwd),
                 '-c','permissions.'+self.PROFILE+'='+toml_value(profile),
                 '-c','features.network_proxy=false','--',*map(str,argv)]
        # Do not pass the learner client's tokens or arbitrary parent environment to programs.
        executables=[self.executor,str(Path(shutil.which('node')).resolve()) if shutil.which('node') else None,sys.executable]
        tool_path=':'.join(dict.fromkeys(([str(self.helper_dir)] if self.helper_dir else [])+['/usr/bin','/bin','/usr/sbin','/sbin']+[str(Path(p).parent) for p in executables if p]))
        # Codex resolves a home directory before it applies -c overrides (Linux
        # 0.155: "Could not find home directory" with no HOME). An empty
        # session-local home keeps the parent's credentials out of reach.
        home=self.session/'host/sandbox-home';home.mkdir(parents=True,exist_ok=True)
        env={'PATH':tool_path,'HOME':str(home),
             'TMPDIR':str(self.scratch),'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1',
             'LANG':'en_US.UTF-8'}
        # Files capture output without unbounded memory growth or writable control paths.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process=subprocess.Popen(command,cwd=self.session/'host',env=env,stdin=subprocess.DEVNULL,
                                     stdout=stdout,stderr=stderr,start_new_session=True)
            timed_out=False
            try:process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out=True
                os.killpg(process.pid,signal.SIGKILL);process.wait()
            # Stop background descendants in the same process group before another operation.
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            def tail(stream):
                size=stream.tell();stream.seek(max(0,size-output_limit))
                return stream.read().decode('utf-8',errors='replace'),size>output_limit
            out,out_cut=tail(stdout);err,err_cut=tail(stderr)
        return {'exit_code':process.returncode,'timed_out':timed_out,'stdout':out,'stderr':err,
                'output_truncated':out_cut or err_cut}

    def run_rpc(self, argv, handler, *, cwd=None, timeout=120, output_limit=200000):
        """Run argv under the sandbox WITHOUT network, serving it a line-JSON
        request channel over stdin/stdout: every stdout line of the form
        {"rpc": id, ...} goes to `handler(request) -> value` and the value
        comes back on stdin as {"rpc": id, "result": value} (or "error"); the
        line {"done": value} ends the exchange and is returned. The child
        never reaches a socket; the broker (this process) does its I/O."""
        if not self.executor or sys.platform not in ('darwin','linux'):
            raise RuntimeError('Enforcement unavailable: this release requires Codex CLI with its macOS/Linux sandbox backend')
        cwd=Path(cwd or self.policy.working_directory or self.adapter).resolve()
        if not cwd.is_dir() or not self.can_read(cwd):raise ValueError('Command cwd must be in the readable scope')
        profile=self.profile(network=False)
        command=[self.executor,'sandbox','-P',self.PROFILE,'-C',str(cwd),
                 '-c','permissions.'+self.PROFILE+'='+toml_value(profile),
                 '-c','features.network_proxy=false','--',*map(str,argv)]
        executables=[self.executor,str(Path(shutil.which('node')).resolve()) if shutil.which('node') else None,sys.executable]
        tool_path=':'.join(dict.fromkeys(([str(self.helper_dir)] if self.helper_dir else [])+['/usr/bin','/bin','/usr/sbin','/sbin']+[str(Path(p).parent) for p in executables if p]))
        home=self.session/'host/sandbox-home';home.mkdir(parents=True,exist_ok=True)
        env={'PATH':tool_path,'HOME':str(home),'TMPDIR':str(self.scratch),'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1','LANG':'en_US.UTF-8'}
        import threading
        with tempfile.TemporaryFile() as stderr:
            process=subprocess.Popen(command,cwd=self.session/'host',env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                     stderr=stderr,start_new_session=True,text=True,bufsize=1)
            done=None;timed_out=False;requests=0
            def expire():
                nonlocal timed_out
                timed_out=True
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            watchdog=threading.Timer(timeout,expire);watchdog.daemon=True;watchdog.start()
            try:
                for line in process.stdout:
                    if len(line)>output_limit:raise ValueError('rpc line exceeded its output limit')
                    try:message=json.loads(line)
                    except ValueError:continue
                    if not isinstance(message,dict):continue
                    if 'done' in message:done=message['done'];break
                    if 'rpc' in message:
                        requests+=1
                        try:reply={'rpc':message['rpc'],'result':handler(message)}
                        except Exception as exc:reply={'rpc':message['rpc'],'error':f'{type(exc).__name__}: {exc}'}
                        process.stdin.write(json.dumps(reply,default=str)+'\n');process.stdin.flush()
            except BrokenPipeError:pass
            finally:
                watchdog.cancel()
                try:process.stdin.close()
                except OSError:pass
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:pass
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait()
            stderr.seek(0);err=stderr.read()[-4000:].decode('utf-8',errors='replace')
        return {'exit_code':process.returncode,'timed_out':timed_out,'done':done,'requests':requests,'stderr':err}

    def operation(self, name, arguments):
        result=self.run([sys.executable,'-m','harnessadapter.policy.operation',
                         '--session',str(self.session),'--name',name,'--arguments',json.dumps(arguments)],network=False,
                        output_limit=6*1024*1024 if name in ('read_image','snapshot_preview') else 30000)
        if result['exit_code']!=0 or result['timed_out']:
            raise RuntimeError('Sandboxed file operation failed: '+result['stderr'][-1200:])
        if result['output_truncated']:
            raise ValueError('Sandboxed file result exceeded its output limit')
        response=json.loads(result['stdout'])
        if 'error' in response:raise ValueError(response['error'])
        return response['result']

    def preflight(self):
        # These fixtures contain no user data. They live outside every allowed write root.
        probe=self.session/'control/preflight';probe.mkdir(exist_ok=True)
        (probe/'private.txt').write_text('private probe')
        evidence_probe=self.session/'evidence'/('.policy-probe-'+uuid.uuid4().hex)
        evidence_probe.write_text('evidence probe')
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200);self.end_headers();self.wfile.write(b'policy-probe')
            def log_message(self,*args):pass
        listener=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=listener.serve_forever,daemon=True).start()
        script='''import json,os,socket,sys
from pathlib import Path
p=json.loads(sys.argv[1]);out={}
def attempt(name,operation):
 try:operation();out[name]=True
 except OSError:out[name]=False
attempt('candidate_write',lambda:Path(p['adapter'],'permission-probe.tmp').write_text('ok'))
attempt('scratch_write',lambda:Path(p['scratch'],'permission-probe.tmp').write_text('ok'))
attempt('outside_write',lambda:Path(p['private']).write_text('bad'))
attempt('outside_read',lambda:Path(p['private']).read_text())
attempt('evidence_read',lambda:Path(p['evidence']).read_text())
attempt('evidence_write',lambda:Path(p['evidence']).write_text('bad'))
attempt('network_connect',lambda:socket.create_connection(('127.0.0.1',p['port']),timeout=1).close())
print(json.dumps(out))
'''
        args={'adapter':str(self.adapter),'scratch':str(self.scratch),'private':str(probe/'private.txt'),
              'evidence':str(evidence_probe),'port':listener.server_port}
        try:result=self.run([str(Path(sys.executable).resolve()),'-c',script,json.dumps(args)])
        finally:
            listener.shutdown();listener.server_close()
            evidence_probe.unlink(missing_ok=True)
        if result['exit_code']!=0:raise RuntimeError('Sandbox preflight could not execute: '+result['stderr'][-1200:])
        facts=json.loads(result['stdout'])
        expected={'candidate_write':True,'scratch_write':True,'outside_write':False,
                  'outside_read':self.can_read(probe/'private.txt'),'evidence_read':True,
                  'evidence_write':False,'network_connect':self.policy.network}
        if facts!=expected:raise RuntimeError(f'Policy enforcement failed: expected {expected}, observed {facts}')
        if (probe/'private.txt').read_text()!='private probe':raise RuntimeError('Sandbox modified protected probe')
        for root in (self.adapter,self.scratch):
            (root/'permission-probe.tmp').unlink(missing_ok=True)
        return {'passed':True,'executor':'codex sandbox','platform':sys.platform,'observed':facts}
