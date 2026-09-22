"""Isolated native Codex + Fireworks OfficeQA acting trials. Importing/preparing never calls a model.

Only run_actor launches inference. prepare_actor_workspace must run before the
caller copies a task starter into the workspace. This runner does not grade it.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib

from paths import CODEX
from experiment import FULL
MODEL = 'accounts/fireworks/models/glm-5p3-flash'
PROVIDER = 'officeqa_fireworks'
EFFORT = 'high'
PROFILE = 'officeqa_actor'
MARKER = '.acting-prepared.json'
COMMON_INSTRUCTIONS = """# Task workspace

Complete the user's task using the files and tools provided in this workspace.
Inspect the actual input and check the produced artifacts before finishing.
State what you verified, and distinguish observed results from assumptions.
Do not change project instructions, agent configuration, or external resources.
Do not search for other trials, private graders, reference outputs, or credentials.
"""
DISABLED_FEATURES = (
    'apps', 'plugins', 'remote_plugin', 'memories', 'external_agent_memory_import',
    'multi_agent', 'multi_agent_v2', 'hooks', 'shell_snapshot', 'image_generation',
    'browser_use', 'browser_use_external', 'computer_use', 'in_app_browser',
    'in_app_chat', 'in_app_local_automation', 'workspace_dependencies',
    'skill_search', 'skill_mcp_dependency_install', 'recommended_plugins',
    'network_proxy', 'chronicle', 'goals', 'code_mode', 'code_mode_host',
    'code_mode_only', 'tool_suggest', 'realtime_conversation', 'sleep_tool',
    'guardian_approval', 'standalone_web_search', 'api_key_model_discovery',
    'unbounded_connection_retries', 'runtime_metrics',
)


def _json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _utc():
    return datetime.now(timezone.utc).isoformat()


def toml_value(value):
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return '[' + ','.join(toml_value(v) for v in value) + ']'
    if isinstance(value, dict):
        return '{' + ','.join(toml_value(k) + '=' + toml_value(v) for k, v in value.items()) + '}'
    raise TypeError(type(value))


def _separate(a, b):
    return not (a == b or a in b.parents or b in a.parents)


def prepare_actor_workspace(workspace, adapter=None, *, common_instructions=COMMON_INSTRUCTIONS):
    """Create an EMPTY, dedicated workspace; caller may then copy task inputs.

Uses the repository's real Codex connector, then replaces its demo framing with
the exact common instructions used in the baseline. No demonstration prompt is
left behind. Candidate Python is trusted code: review it before this call.
"""
    workspace = Path(workspace).absolute()
    if workspace.is_symlink() or workspace.resolve() != workspace:
        raise ValueError('Workspace must have a canonical, non-symlink path')
    adapter = Path(adapter).resolve() if adapter is not None else None
    if adapter is not None and not _separate(workspace, adapter):
        raise ValueError('Adapter and workspace must be disjoint')
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError('Workspace must be empty before preparation')
    info = None
    if adapter is not None:
        if FULL:
            from full_runtime import render_exports
            import harnessadapter.hosts.codex as native_connector
            original_render = native_connector.render_exports
            native_connector.render_exports = render_exports
        from harnessadapter.hosts.codex import prepare
        from harnessadapter.hosts.context import PROJECT_CONTEXT
        try:
            info = prepare(adapter, workspace, str(Path(sys.executable).absolute()))
        finally:
            if FULL: native_connector.render_exports = original_render
        current = (workspace / 'AGENTS.md').read_text()
        if not current.startswith(PROJECT_CONTEXT):
            raise ValueError('Unexpected generated context framing')
        (workspace / 'AGENTS.md').write_text(common_instructions.rstrip() + '\n' + current[len(PROJECT_CONTEXT):])
        (workspace / 'PROMPT.md').unlink(missing_ok=True)
    else:
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'AGENTS.md').write_text(common_instructions.rstrip() + '\n')
        (workspace / '.codex').mkdir()
        (workspace / '.codex/config.toml').write_text('# Prepared baseline; runtime config is explicit.\n')
    for name in ('.actor-home', '.actor-tmp'):
        (workspace / name).mkdir()
    if FULL:
        shutil.copy2(Path(__file__).parent/'full_runtime.py',workspace/'.codex/mwm-runtime.py')
    metadata = {
        'workspace': str(workspace), 'adapter': str(adapter) if adapter else None,
        'common_instructions_sha256': hashlib.sha256(common_instructions.rstrip().encode()).hexdigest(),
        'agents_sha256': _sha(workspace / 'AGENTS.md'),
        'prepared_utc': _utc(), 'deployment': info,
    }
    _json(workspace / MARKER, metadata)
    return metadata


def runtime_roots():
    """Exact code/interpreter roots; never grant the whole project or home."""
    import harnessadapter
    roots = [Path(sys.prefix), Path(sys.base_prefix), Path(harnessadapter.__file__).parent]
    node = Path('/Applications/ChatGPT.app/Contents/Resources/cua_node')
    if node.is_dir():
        roots.append(node)
    roots.append(CODEX)
    return list(dict.fromkeys(str(p.resolve()) for p in roots))


def permission_profile(workspace, corpus, index, adapter=None, *, audit=None, server=False):
    workspace = Path(workspace).resolve()
    fs = {':root': 'deny', ':minimal': 'read'}
    fs.update({str(Path(p).resolve()): 'read' for p in (corpus, index)})
    fs.update({p: 'read' for p in runtime_roots()})
    fs[str(workspace)] = 'read' if server else 'write'
    for name in ('AGENTS.md', 'README.md', 'officeqa.py', '.codex', MARKER, '.ha-demo-generated.json'):
        fs[str(workspace / name)] = 'read'
    if adapter is not None:
        fs[str(Path(adapter).resolve())] = 'read'
    if server:
        if audit is None:
            raise ValueError('MCP server requires an external audit file')
        fs[str(Path(audit).resolve())] = 'write'
        # MCP libraries may need temporary files, never external campaign files.
        fs[str(workspace / '.actor-tmp')] = 'write'
    return {'filesystem': fs, 'network': {'enabled': False}}


def tool_environment(workspace):
    workspace = Path(workspace).resolve()
    paths = [str(Path(sys.executable).absolute().parent),
             '/Applications/ChatGPT.app/Contents/Resources/cua_node/bin',
             '/usr/bin', '/bin', '/usr/sbin', '/sbin']
    return {'HOME': str(workspace / '.actor-home'), 'TMPDIR': str(workspace / '.actor-tmp'),
            'PATH': ':'.join(paths), 'LANG': 'en_US.UTF-8',
            'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
            'MPLCONFIGDIR': str(workspace / '.actor-tmp/matplotlib')}


def client_environment(home):
    """Per-subprocess HOME only; does not modify os.environ or global Codex config."""
    return {'HOME': str(Path(home).resolve()), 'TMPDIR': str(Path(home).resolve()),
            'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
            'LANG': 'en_US.UTF-8', 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}


def validate_endpoint(endpoint):
    from urllib.parse import urlparse
    parsed = urlparse(endpoint)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path.rstrip('/') != '/v1'):
        raise ValueError('Actor endpoint must be its own local broker http://127.0.0.1:PORT/v1')


def client_policy(workspace, corpus, index, adapter=None, *, endpoint):
    """One inherited OS profile; macOS does not permit nested sandbox_apply.

    Runtime allowances are explicit and intentionally exclude host home,
    parent workspace, other trials, control logs, gold, and general temp roots.
    The only TCP egress is the actor's own fixed Fireworks broker port.
    """
    from urllib.parse import urlparse
    validate_endpoint(endpoint)
    workspace = Path(workspace).resolve()
    read_roots = list(dict.fromkeys([
        '/System', '/usr/lib', '/usr/share', '/usr/libexec', '/bin', '/sbin',
        '/usr/bin', '/usr/sbin', '/Library/Apple', '/Library/Preferences',
        '/private/etc', '/private/var/db',
        *runtime_roots(), str(Path(corpus).resolve()), str(Path(index).resolve()),
        *([str(Path(adapter).resolve())] if adapter is not None else []),
    ]))
    def filt(path):
        return '(subpath ' + json.dumps(str(path)) + ')'
    read_filters = ' '.join(filt(path) for path in read_roots)
    parents = sorted({str(parent) for path in [workspace, *map(Path, read_roots)] for parent in path.parents})
    metadata_filters = ' '.join('(literal ' + json.dumps(path) + ')' for path in parents)
    protected = ['AGENTS.md', 'README.md', 'officeqa.py', '.codex', MARKER, '.ha-demo-generated.json']
    protected_filters = ' '.join(filt(workspace / name) for name in protected)
    return f"""(version 1)
(deny default)
(allow process-exec process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow sysctl-read)
(deny sysctl-read (sysctl-name-prefix "kern.proc"))
(allow sysctl-write (sysctl-name \"kern.grade_cputype\"))
(allow ipc-posix-sem)
(allow mach-lookup
  (global-name \"com.apple.system.opendirectoryd.libinfo\")
  (global-name \"com.apple.system.DirectoryService.libinfo_v1\")
  (global-name \"com.apple.logd\")
  (global-name \"com.apple.logd.events\")
  (global-name \"com.apple.bsd.dirhelper\")
  (global-name \"com.apple.secinitd\"))
(allow system-mac-syscall (mac-policy-name \"vnguard\"))
(allow system-mac-syscall (require-all (mac-policy-name \"Sandbox\") (mac-syscall-number 67)))
(allow file-read* file-test-existence file-map-executable {read_filters})
(allow file-read* file-test-existence (literal "/"))
(allow file-read-metadata file-test-existence {metadata_filters}
  (literal \"/etc\") (literal \"/var\") (literal \"/tmp\") (literal \"/dev\"))
(allow file-read* file-write* file-map-executable {filt(workspace)})
(deny file-write* {protected_filters})
(allow file-read* (literal \"/dev/random\") (literal \"/dev/urandom\"))
(allow file-read* file-write* (literal \"/dev/null\") (literal \"/dev/zero\")
  (literal \"/dev/tty\") (subpath \"/dev/fd\"))
(allow file-read-metadata (regex \"^/dev/.*$\"))
(allow pseudo-tty)
(allow file-read* file-write* file-ioctl (literal \"/dev/ptmx\") (regex \"^/dev/ttys[0-9]+$\"))
(allow network-outbound (remote tcp \"localhost:{urlparse(endpoint).port}\"))
"""


def client_command(command, workspace, output, corpus, index, adapter=None, *, endpoint):
    return ['/usr/bin/sandbox-exec', '-p', client_policy(workspace, corpus, index, adapter,
                                                       endpoint=endpoint), *command]


def build_settings(workspace, output, corpus, index, adapter=None, *, endpoint):
    workspace, output = Path(workspace).resolve(), Path(output).resolve()
    validate_endpoint(endpoint)
    for source in (corpus, index):
        if not Path(source).exists():
            raise ValueError('Corpus/index must exist before acting')
        if not _separate(workspace, Path(source).resolve()):
            raise ValueError('Read-only input roots must be separate from workspace')
    metadata = json.loads((workspace / MARKER).read_text())
    selected = str(Path(adapter).resolve()) if adapter is not None else None
    if metadata['adapter'] != selected:
        raise ValueError('Adapter argument must match prepared workspace')
    if _sha(workspace / 'AGENTS.md') != metadata['agents_sha256']:
        raise ValueError('Prepared project instructions were changed')
    settings = {'model': MODEL, 'model_provider': PROVIDER, 'model_reasoning_effort': EFFORT,
                # The inherited OS sandbox enforces the boundary for the client
                # and all its children; nested Seatbelt policies are unsupported.
                'sandbox_mode': 'danger-full-access',
                f'model_providers.{PROVIDER}': {
                    'name': 'GLM 5.3 Flash via local Fireworks-only broker',
                    'base_url': endpoint, 'wire_api': 'responses', 'requires_openai_auth': False,
                    'supports_websockets': False, 'supports_standalone_web_search': False,
                    'request_max_retries': 0, 'stream_max_retries': 0,
                    'stream_idle_timeout_ms': 1250000},
                'analytics.enabled': False, 'feedback.enabled': False,
                'otel.exporter': 'none', 'otel.trace_exporter': 'none', 'otel.metrics_exporter': 'none',
                'model_context_window': 196608, 'model_auto_compact_token_limit': 160000,
                'approval_policy': 'never', 'agents.enabled': False, 'web_search': 'disabled',
                'project_root_markers': [], 'features.skip_host_skill_discovery': True,
                'shell_environment_policy.inherit': 'none',
                'shell_environment_policy.set': tool_environment(workspace),
                # CLI override splitting does not preserve quoted dotted path
                # segments. Set the entire map, including ~/.codex/... paths.
                'projects': {str(workspace): {'trust_level': 'trusted'}}}
    settings.update({f'features.{name}': False for name in DISABLED_FEATURES})
    if FULL:
        settings.update({'agents.enabled':True, 'features.multi_agent':True,
                         'model_context_window':1048576, 'model_auto_compact_token_limit':800000})
    # Native role registrations are intentionally not enabled in this comparison.
    if adapter is not None:
        info = metadata['deployment']
        if info.get('server'):
            env = tool_environment(workspace)
            # Child inherits the already-active whole-client OS sandbox. No
            # actor-writable audit log is claimed as an immutable controller log.
            server_args = ['-i', *[f'{k}={v}' for k, v in env.items()],
                           str(Path(sys.executable).absolute()), '-B', '-m',
                           'harnessadapter.runtime.mcp_server', '--adapter', str(Path(adapter).resolve())]
            if FULL:
                server_args = ['-i', *[f'{k}={v}' for k,v in env.items()],
                    str(Path(sys.executable).absolute()), '-B', str(workspace/'.codex/mwm-runtime.py'),
                    '--adapter',str(Path(adapter).resolve()),'--endpoint',endpoint]
            prefix = 'mcp_servers.' + info['server_name'] + '.'
            settings.update({prefix + 'command': '/usr/bin/env', prefix + 'args': server_args,
                             prefix + 'required': True, prefix + 'startup_timeout_sec': 30,
                             prefix + 'tool_timeout_sec': 30, prefix + 'default_tools_approval_mode': 'approve'})
        if FULL:
            for role, path in info.get('roles', {}).items():
                role_info = tomllib.loads(Path(path).read_text())
                settings['agents.'+role] = {'description':role_info['description'], 'config_file':path}
    return settings


def command_for(workspace, output, settings, images=()):
    args = [str(CODEX), '--strict-config', '--ask-for-approval', 'never', 'exec',
            '--ignore-user-config', '--ignore-rules', '--skip-git-repo-check', '--ephemeral',
            '--json', '--cd', str(Path(workspace).resolve()),
            '--output-last-message', str(Path(workspace).resolve() / 'final.md')]
    for key, value in settings.items():
        args += ['-c', key + '=' + toml_value(value)]
    for path in images:
        args += ['--image', str(path)]
    return args + ['-']


def parse_events(path):
    """Recoverable notifications before the final completed turn are not failure."""
    events, malformed = [], []
    for number, line in enumerate(Path(path).read_text(errors='replace').splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError('Event is not an object')
            events.append(event)
        except (ValueError, json.JSONDecodeError):
            malformed.append(number)
    terminal = [(i, e) for i, e in enumerate(events) if e.get('type') in ('turn.completed', 'turn.failed')]
    completed = bool(terminal and terminal[-1][1]['type'] == 'turn.completed' and
                     not any(e.get('type') == 'error' for e in events[terminal[-1][0] + 1:]) and not malformed)
    usage = Counter()
    usage_events = 0
    for event in events:
        if event.get('type') == 'turn.completed' and isinstance(event.get('usage'), dict):
            usage_events += 1
            usage.update({k: v for k, v in event['usage'].items() if isinstance(v, int) and not isinstance(v, bool)})
    items = {}
    last_agent_message = None
    for index, event in enumerate(events):
        if event.get('type') == 'item.completed' and isinstance(event.get('item'), dict):
            item = event['item']; items[item.get('id', f'event-{index}')] = item
            if item.get('type') == 'agent_message' and isinstance(item.get('text'), str):
                last_agent_message = item['text']
    return {'turn_completed': completed, 'event_count': len(events), 'malformed_event_lines': malformed,
            'terminal_type': terminal[-1][1]['type'] if terminal else None,
            'usage': dict(usage) if usage_events else None, 'usage_events': usage_events,
            'completed_item_counts': dict(Counter(i.get('type', 'unknown') for i in items.values())),
            'error_notifications': sum(e.get('type') == 'error' for e in events),
            'last_agent_message': last_agent_message,
            'thread_ids': list(dict.fromkeys(e['thread_id'] for e in events if e.get('thread_id'))),
            'reported_models': list(dict.fromkeys(e['model'] for e in events if isinstance(e.get('model'), str)))}


def collect_final(workspace, output, expected_text, *, byte_limit=1048576):
    """Accept only a bounded regular final matching the immutable CLI event.

    The submitted path is actor-controlled: never follow symlinks, open FIFOs
    in blocking mode, or use shutil.copyfile on it from the outside controller.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(Path(workspace) / 'final.md', flags)
    except OSError as exc:
        return {'valid': False, 'reason': 'final_open_rejected', 'errno': exc.errno}
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return {'valid': False, 'reason': 'final_not_regular'}
        if info.st_size > byte_limit:
            return {'valid': False, 'reason': 'final_too_large'}
        data = bytearray()
        while len(data) <= byte_limit:
            chunk = os.read(descriptor, min(65536, byte_limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > byte_limit:
            return {'valid': False, 'reason': 'final_too_large'}
    except OSError as exc:
        return {'valid': False, 'reason': 'final_read_rejected', 'errno': exc.errno}
    finally:
        os.close(descriptor)
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return {'valid': False, 'reason': 'final_not_utf8'}
    if not isinstance(expected_text, str) or not text.strip() or text.strip() != expected_text.strip():
        return {'valid': False, 'reason': 'final_event_mismatch'}
    # Output belongs to the controller and is excluded from the actor sandbox.
    (Path(output) / 'final.md').write_bytes(data)
    return {'valid': True, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'matched': 'last item.completed agent_message.text, ignoring boundary whitespace'}


def _stop_group(process, grace=5):
    """Stop our Popen(start_new_session=True) group, including orphan children.

    The leader's return code does not establish that its process group is gone.
    A completed leader therefore still receives immediate group cleanup, without
    the grace-period wait used for a leader that is actively running.
    """
    owned_pgid = process.pid
    if owned_pgid == os.getpgrp():
        raise ValueError('Refusing to signal the controller process group')
    running = process.poll() is None
    if running:
        try:
            if os.getpgid(process.pid) != owned_pgid:
                raise ValueError('Child was not started in its own process group')
        except ProcessLookupError:
            running = False

    def signal_owned_group(sig):
        try:
            os.killpg(owned_pgid, sig)
        except ProcessLookupError:
            pass

    if running:
        signal_owned_group(signal.SIGTERM)
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    # Always target the dedicated group, even when its original leader exited.
    signal_owned_group(signal.SIGKILL)
    process.wait()


def run_actor(workspace, output, prompt, corpus, index, adapter=None, timeout=1800, *, endpoint):
    """Run exactly one fresh Codex task; no retries, grading or answer repair."""
    workspace, output = Path(workspace).resolve(), Path(output).resolve()
    if not _separate(workspace, output):
        raise ValueError('Run records must be outside the actor workspace')
    if adapter is not None and not _separate(output, Path(adapter).resolve()):
        raise ValueError('Run records and adapter must be disjoint')
    if (timeout is not None and timeout <= 0) or not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('Positive timeout and nonempty prompt required')
    settings = build_settings(workspace, output, corpus, index, adapter, endpoint=endpoint)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Run output must be empty; prior trials are never overwritten')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'prompt.md').write_text(prompt)
    args = client_command(command_for(workspace, output, settings), workspace, output,
                          corpus, index, adapter, endpoint=endpoint)
    invocation = {'argv': args, 'cwd': str(workspace), 'timeout_seconds': timeout,
                  'requested_model': MODEL, 'reasoning_effort': EFFORT, 'settings': settings,
                  'adapter': str(Path(adapter).resolve()) if adapter else None,
                  'corpus': str(Path(corpus).resolve()), 'index': str(Path(index).resolve()),
                  'sandbox_scope': 'Single inherited OS sandbox for whole Codex client/tools/MCP; network only own Fireworks broker TCP port; controller records excluded',
                  'prompt_sha256': _sha(output / 'prompt.md'), 'agents_sha256': _sha(workspace / 'AGENTS.md'),
                  'feedback_source': 'No dynamic human/Astra feedback; only model and tool outputs'}
    _json(output / 'invocation.json', invocation)
    started = _utc(); begin = time.monotonic(); timed_out = False; launch_error = None; process = None
    # Fresh subprocess environment: never inherit credentials, login, shell hooks,
    # user MCP configuration, or ambient proxy variables. Controller has a local
    # per-run home separate from the model-readable task workspace.
    client_home = workspace / '.client-home'; client_home.mkdir()
    env = client_environment(client_home)
    with (output / 'events.jsonl').open('w') as out, (output / 'stderr.log').open('w') as err:
        try:
            process = subprocess.Popen(args, cwd=workspace, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                stdout, stderr = process.communicate(prompt, timeout=timeout)
                out.write(stdout); err.write(stderr)
            except subprocess.TimeoutExpired:
                timed_out = True
                _stop_group(process)
                stdout, stderr = process.communicate(timeout=10)
                out.write(stdout); err.write(stderr)
            finally:
                _stop_group(process)
        except OSError as exc:
            launch_error = f'{type(exc).__name__}: {exc}'
    parsed = parse_events(output / 'events.jsonl')
    final_validation = collect_final(workspace, output, parsed.pop('last_agent_message'))
    exit_code = process.returncode if process is not None else None
    status = ('launch_error' if launch_error else 'timeout' if timed_out else
              'invalid_final' if exit_code == 0 and parsed['turn_completed'] and not final_validation['valid'] else
              'completed' if exit_code == 0 and parsed['turn_completed'] else 'failed')
    result = {'status': status, 'started_utc': started, 'finished_utc': _utc(),
              'elapsed_seconds': round(time.monotonic() - begin, 6), 'exit_code': exit_code,
              'timed_out': timed_out, 'launch_error': launch_error, **parsed,
              'final_validation': final_validation,
              'requested_model': MODEL, 'reasoning_effort': EFFORT,
              'final_message_present': (output / 'final.md').is_file(),
              'output_directory': str(output), 'grading': 'not performed by acting runner'}
    result['record_hashes'] = {p.name: _sha(p) for p in output.iterdir() if p.is_file()}
    _json(output / 'result.json', result)
    return result


def validate_config_offline(settings, *, timeout=25):
    """Strict local config/read RPC, using a temporary empty account home.

    This starts the CLI server and asks for configuration only. No thread or
    inference request is made. Full prompt rendering is not claimed here.
    """
    with tempfile.TemporaryDirectory(prefix='acting-config-') as directory:
        env = client_environment(Path(directory))
        args = [str(CODEX), 'app-server', '--strict-config', '--stdio']
        for key, value in settings.items():
            args += ['-c', key + '=' + toml_value(value)]
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(args, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=errors, text=True, start_new_session=True, bufsize=1)
            selector = selectors.DefaultSelector(); selector.register(process.stdout, selectors.EVENT_READ)
            replies = []; deadline = time.monotonic() + timeout
            def exchange(message):
                process.stdin.write(json.dumps(message) + '\n'); process.stdin.flush()
                while time.monotonic() < deadline:
                    if not selector.select(max(0, deadline - time.monotonic())):
                        break
                    line = process.stdout.readline()
                    if not line:
                        break
                    row = json.loads(line); replies.append(row)
                    if row.get('id') == message['id']:
                        return row
                errors.flush(); errors.seek(0)
                detail = errors.read().decode(errors='replace')[-5000:]
                raise RuntimeError('No config validation response before timeout/exit: ' + detail)
            try:
                init = exchange({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'acting-offline-validation', 'version': '1'}}})
                if 'error' in init:
                    raise RuntimeError(str(init['error']))
                process.stdin.write('{"method":"initialized"}\n'); process.stdin.flush()
                reply = exchange({'id': 2, 'method': 'config/read', 'params': {'includeLayers': False}})
                return {'passed': 'result' in reply, 'response': reply, 'model_calls': 0}
            finally:
                selector.close(); _stop_group(process)
                process.stdin.close(); process.stdout.close()
