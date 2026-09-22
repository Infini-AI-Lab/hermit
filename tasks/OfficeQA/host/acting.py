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
import re
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib

from paths import CODEX
# Which model acts. flash: GLM 5.3 Flash on Fireworks through the per-question
# broker (endpoint required). sol: gpt-5.6-sol through codex's default OpenAI
# provider on the user's ChatGPT sign-in (no broker; a throwaway CODEX_HOME
# holding a copy of auth.json, outside the actor's readable paths).
ACTOR = os.environ.get('OFFICEQA_ACTOR', 'flash')
if ACTOR not in ('flash', 'sol'):
    raise ValueError('OFFICEQA_ACTOR must be flash or sol')
MODEL = 'accounts/fireworks/models/glm-5p3-flash' if ACTOR == 'flash' else 'gpt-5.6-sol'
LOGIN = ACTOR == 'sol'
PROVIDER = 'officeqa_fireworks'
EFFORT = 'high'
FLASH_CONTEXT_WINDOW = 1048576   # Fireworks' declared context_length for glm-5p3-flash (models API, 2026-09-22)
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


def prepare_actor_workspace(workspace, adapter=None, *, common_instructions=COMMON_INSTRUCTIONS,
                            evidence=None, deployment_host=None, context=None, python_sandbox=False):
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
        from harnessadapter.hosts.codex import prepare
        from harnessadapter.hosts.context import PROJECT_CONTEXT
        info = prepare(adapter, workspace, str(Path(sys.executable).absolute()))
        current = (workspace / 'AGENTS.md').read_text()
        if not current.startswith(PROJECT_CONTEXT):
            raise ValueError('Unexpected generated context framing')
        (workspace / 'AGENTS.md').write_text(common_instructions.rstrip() + '\n' + current[len(PROJECT_CONTEXT):])
        if context is not None:
            # A caller-rendered context (the adapter's package with the
            # deployment's host texts and the run's evidence bound) replaces
            # the connector's plain rendering.
            (workspace / 'AGENTS.md').write_text(common_instructions.rstrip() + '\n\n' + context.rstrip() + '\n')
        (workspace / 'PROMPT.md').unlink(missing_ok=True)
    else:
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'AGENTS.md').write_text(common_instructions.rstrip() + '\n')
        (workspace / '.codex').mkdir()
        (workspace / '.codex/config.toml').write_text('# Prepared baseline; runtime config is explicit.\n')
    for name in ('.actor-home', '.actor-tmp'):
        (workspace / name).mkdir()
    metadata = {
        'workspace': str(workspace), 'adapter': str(adapter) if adapter else None,
        'common_instructions_sha256': hashlib.sha256(common_instructions.rstrip().encode()).hexdigest(),
        'agents_sha256': _sha(workspace / 'AGENTS.md'),
        'prepared_utc': _utc(), 'deployment': info,
        'evidence': str(Path(evidence).resolve()) if evidence else None,
        'deployment_host': str(Path(deployment_host).resolve()) if deployment_host else None,
        'python_sandbox': bool(python_sandbox),
    }
    _json(workspace / MARKER, metadata)
    return metadata


LINUX = sys.platform.startswith('linux')


def sandbox_helper_dir(workspace):
    """Linux: codex re-executes its own binary as `codex-linux-sandbox`, found
    on PATH; the standalone release ships no such file, so the client home
    carries a link (the same arrangement as the framework's PolicySandbox)."""
    return Path(workspace).resolve() / '.client-home' / 'bin'


def runtime_roots(workspace=None):
    """Exact code/interpreter roots; never grant the whole project or home."""
    import harnessadapter
    roots = [Path(sys.prefix), Path(sys.base_prefix), Path(harnessadapter.__file__).parent]
    node = Path('/Applications/ChatGPT.app/Contents/Resources/cua_node')
    if node.is_dir():
        roots.append(node)
    roots.append(CODEX)
    if LINUX:
        roots.append(Path(CODEX).resolve().parent)
        if workspace is not None:
            roots.append(sandbox_helper_dir(workspace))
    return list(dict.fromkeys(str(p.resolve()) for p in roots))


def permission_profile(workspace, corpus, index, adapter=None, *, audit=None, server=False):
    workspace = Path(workspace).resolve()
    fs = {':root': 'deny', ':minimal': 'read'}
    fs.update({str(Path(p).resolve()): 'read' for p in (corpus, index)})
    fs.update({p: 'read' for p in runtime_roots(workspace)})
    if LINUX:
        # Name lookups inside the command sandbox (`id`, shells) read these.
        fs.update({p: 'read' for p in ('/etc/passwd', '/etc/group') if Path(p).is_file()})
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
    if LINUX:
        # codex spawns its command sandbox as `codex-linux-sandbox` found on
        # the COMMAND environment's PATH; the link lives in the client home.
        paths.insert(0, str(sandbox_helper_dir(workspace)))
    return {'HOME': str(workspace / '.actor-home'), 'TMPDIR': str(workspace / '.actor-tmp'),
            'PATH': ':'.join(paths), 'LANG': 'en_US.UTF-8',
            'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
            'MPLCONFIGDIR': str(workspace / '.actor-tmp/matplotlib')}


def client_environment(home, codex_home=None):
    """Per-subprocess HOME only; does not modify os.environ or global Codex config.
    codex_home (login mode) is the throwaway CODEX_HOME holding the sign-in."""
    home = Path(home).resolve()
    path = '/usr/bin:/bin:/usr/sbin:/sbin'
    if LINUX:
        helper = home / 'bin'
        helper.mkdir(parents=True, exist_ok=True)
        for name in ('codex-linux-sandbox', 'codex-code-mode-host'):
            link = helper / name
            if link.is_symlink() or link.exists():
                link.unlink()
            target = Path(CODEX).resolve() if name == 'codex-linux-sandbox' else Path(CODEX).resolve().parent / name
            if target.exists():
                link.symlink_to(target)
        path = str(helper) + ':' + path
    env = {'HOME': str(home), 'TMPDIR': str(home),
           'PATH': path,
           'LANG': 'en_US.UTF-8', 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    if codex_home is not None:
        env['CODEX_HOME'] = str(Path(codex_home).resolve())
    return env


def login_home(directory):
    """A throwaway CODEX_HOME with a copy of the user's codex sign-in and
    nothing else (no config, no history): the same arrangement the container
    learner used for gpt-5.6-sol. It must lie outside every path the actor's
    command sandbox can read."""
    source = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'auth.json'
    if not source.is_file():
        raise RuntimeError('codex is not signed in (auth.json missing); run `codex login` first')
    directory = Path(directory).resolve()
    directory.mkdir(mode=0o700)
    shutil.copy2(source, directory / 'auth.json')
    (directory / 'auth.json').chmod(0o600)
    return directory


def validate_endpoint(endpoint):
    from urllib.parse import urlparse
    if endpoint is None:
        if not LOGIN:
            raise ValueError('The Fireworks actor needs its own local broker endpoint')
        return
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
    if LINUX:
        # No outer OS sandbox on Linux: the client process reaches only its
        # broker (no credentials, no other URL), and every command it runs
        # goes through codex's own bubblewrap sandbox with the permissions
        # profile of build_settings (root denied, corpus/index/adapter read,
        # workspace write, network off).
        return list(command)
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
    settings = {'model': MODEL, 'model_reasoning_effort': EFFORT,
                'analytics.enabled': False, 'feedback.enabled': False,
                'otel.exporter': 'none', 'otel.trace_exporter': 'none', 'otel.metrics_exporter': 'none',
                'approval_policy': 'never', 'agents.enabled': False, 'web_search': 'disabled',
                'project_root_markers': [], 'features.skip_host_skill_discovery': True,
                'shell_environment_policy.inherit': 'none',
                'shell_environment_policy.set': tool_environment(workspace),
                # CLI override splitting does not preserve quoted dotted path
                # segments. Set the entire map, including ~/.codex/... paths.
                'projects': {str(workspace): {'trust_level': 'trusted'}}}
    if endpoint is not None:
        settings.update({'model_provider': PROVIDER,
                         f'model_providers.{PROVIDER}': {
                             'name': 'GLM 5.3 Flash via local Fireworks-only broker',
                             'base_url': endpoint, 'wire_api': 'responses', 'requires_openai_auth': False,
                             'supports_websockets': False, 'supports_standalone_web_search': False,
                             'request_max_retries': 0, 'stream_max_retries': 0,
                             'stream_idle_timeout_ms': 1250000}})
    # Context window = the model's own maximum (user decision 2026-09-22), and
    # codex's default auto-compaction rule on top of it. gpt-5.6-sol carries
    # its window in codex's model metadata (272,000); GLM has no metadata in
    # codex, so its window is the one Fireworks declares for the model.
    if endpoint is not None:
        settings['model_context_window'] = FLASH_CONTEXT_WINDOW
    # Login mode: no model_provider setting, so codex uses its built-in OpenAI
    # provider with the sign-in found in CODEX_HOME (the throwaway copy).
    # On the ChatGPT sign-in the account's remote configuration routes the
    # shell tool through codex's code-mode host (observed 2026-09-22: every
    # command failed closed with "code-mode host is disabled" when the host
    # feature was off), so login mode leaves the code-mode features at their
    # defaults; commands still run under the same permission profile.
    disabled = tuple(name for name in DISABLED_FEATURES if not (LOGIN and name.startswith('code_mode')))
    settings.update({f'features.{name}': False for name in disabled})
    if LINUX:
        settings['default_permissions'] = PROFILE
        settings[f'permissions.{PROFILE}'] = permission_profile(workspace, corpus, index, adapter)
    else:
        # The inherited OS sandbox enforces the boundary for the client
        # and all its children; nested Seatbelt policies are unsupported.
        settings['sandbox_mode'] = 'danger-full-access'
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
            # The host's world-model tool serves recall/trace from the run's
            # evidence, renders the deployment's host texts, and can run
            # admitted learned tools (the same bindings as the CLBench hosts).
            for flag, value in (('--evidence', metadata.get('evidence')), ('--deployment', metadata.get('deployment_host'))):
                if value:
                    server_args += [flag, str(value)]
            # Every wm call lands in the run records, outside the actor's workspace.
            server_args += ['--audit-log', str(output / 'tool-calls.jsonl')]
            if metadata.get('python_sandbox'):
                server_args.append('--python-sandbox')
            prefix = 'mcp_servers.' + info['server_name'] + '.'
            settings.update({prefix + 'command': '/usr/bin/env', prefix + 'args': server_args,
                             prefix + 'required': True, prefix + 'startup_timeout_sec': 30,
                             prefix + 'tool_timeout_sec': 30, prefix + 'default_tools_approval_mode': 'approve'})
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
            'terminal_error': ((terminal[-1][1].get('error') or {}).get('message') if terminal and isinstance(terminal[-1][1].get('error'), dict) else None),
            'usage': dict(usage) if usage_events else None, 'usage_events': usage_events,
            'completed_item_counts': dict(Counter(i.get('type', 'unknown') for i in items.values())),
            'error_notifications': sum(e.get('type') == 'error' for e in events),
            'last_agent_message': last_agent_message,
            'thread_ids': list(dict.fromkeys(e['thread_id'] for e in events if e.get('thread_id'))),
            'reported_models': list(dict.fromkeys(e['model'] for e in events if isinstance(e.get('model'), str)))}


def collect_final(workspace, output, expected_text, *, byte_limit=1048576, accept_any=False):
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
    candidates = expected_text if isinstance(expected_text, list) else [expected_text]
    candidates = [c.strip() for c in candidates if isinstance(c, str)]
    if not text.strip():
        return {'valid': False, 'reason': 'final_empty'}
    if text.strip() not in candidates and not accept_any:
        return {'valid': False, 'reason': 'final_event_mismatch'}
    # Output belongs to the controller and is excluded from the actor sandbox.
    (Path(output) / 'final.md').write_bytes(data)
    return {'valid': True, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'matched': ('the CLI-written final of a completed turn (no agent_message event to compare; login mode has no broker record)'
                        if accept_any and text.strip() not in candidates else
                        'last item.completed agent_message.text or the broker-recorded last response message, ignoring boundary whitespace')}


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


TOOL_ITEMS = ('command_execution', 'mcp_tool_call', 'file_change', 'web_search')

# Provider-side trouble a turn can end with in login mode (no broker to
# classify it): capacity refusals, gateway pages, rate limits, disconnects
# before any completion. None of these is the model's doing.
INFRA_ERROR = re.compile(r'at capacity|rate limit|too many requests|overloaded|502|503|504|bad gateway|gateway time|service unavailable|<!DOCTYPE|error response|connection reset|stream disconnected before completion(?!.*max_output_tokens)', re.I | re.S)


def infra_failure(result, transport=None):
    """True when a question ended because of the transport or the provider,
    not because of the model: a failed broker transport, or (login mode) a
    failed turn whose terminal error is a provider-side refusal. A response
    the provider truncated at its output cap is the model's own outcome."""
    if transport is not None and transport.get('failed') and transport.get('failure_kind') != 'budget_exhausted':
        return True
    if result.get('status') == 'failed':
        text = result.get('terminal_error') or ''
        return bool(text) and bool(INFRA_ERROR.search(text)) and 'max_output_tokens' not in text
    return False


def run_actor(workspace, output, prompt, corpus, index, adapter=None, timeout=1800, *, endpoint=None, max_requests=None, extra_time=None):
    """Run exactly one fresh Codex task; no retries, grading or answer repair.

    max_requests caps the model requests of the task: codex makes one request
    per tool round, so the cap is enforced on the stream of completed tool
    items (a request whose tool item completes was request number
    tool_items + 1). Reaching it stops the client the way the timeout does;
    the record says request_cap. extra_time() (optional) returns seconds the
    transport spent on its own failures; the guard is extended by that much,
    so infrastructure trouble never eats the model's time."""
    workspace, output = Path(workspace).resolve(), Path(output).resolve()
    if not _separate(workspace, output):
        raise ValueError('Run records must be outside the actor workspace')
    if adapter is not None and not _separate(output, Path(adapter).resolve()):
        raise ValueError('Run records and adapter must be disjoint')
    if timeout <= 0 or not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('Positive timeout and nonempty prompt required')
    if max_requests is not None and (not isinstance(max_requests, int) or max_requests < 1):
        raise ValueError('max_requests must be a positive integer or None')
    settings = build_settings(workspace, output, corpus, index, adapter, endpoint=endpoint)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Run output must be empty; prior trials are never overwritten')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'prompt.md').write_text(prompt)
    args = client_command(command_for(workspace, output, settings), workspace, output,
                          corpus, index, adapter, endpoint=endpoint)
    invocation = {'argv': args, 'cwd': str(workspace), 'timeout_seconds': timeout, 'max_requests': max_requests,
                  'requested_model': MODEL, 'reasoning_effort': EFFORT, 'settings': settings,
                  'adapter': str(Path(adapter).resolve()) if adapter else None,
                  'corpus': str(Path(corpus).resolve()), 'index': str(Path(index).resolve()),
                  'sandbox_scope': ('Codex client unsandboxed on the OpenAI transport (ChatGPT sign-in in a throwaway CODEX_HOME); commands and MCP under codex\'s own bubblewrap profile, network off; controller records excluded'
                                    if endpoint is None else
                                    'Single inherited OS sandbox for whole Codex client/tools/MCP; network only own Fireworks broker TCP port; controller records excluded'),
                  'prompt_sha256': _sha(output / 'prompt.md'), 'agents_sha256': _sha(workspace / 'AGENTS.md'),
                  'feedback_source': 'No dynamic human/Astra feedback; only model and tool outputs'}
    _json(output / 'invocation.json', invocation)
    started = _utc(); begin = time.monotonic(); timed_out = False; capped = False; launch_error = None; process = None
    tool_items = 0
    # Fresh subprocess environment: never inherit credentials, login, shell hooks,
    # user MCP configuration, or ambient proxy variables. Controller has a local
    # per-run home separate from the model-readable task workspace.
    client_home = workspace / '.client-home'; client_home.mkdir()
    # The sign-in copy lives beside the workspace, not in it: the command
    # sandbox may read the whole workspace, never its parent.
    env = client_environment(client_home, login_home(workspace.parent / '.codex-home') if endpoint is None else None)
    with (output / 'events.jsonl').open('w') as out, (output / 'stderr.log').open('w') as err:
        try:
            process = subprocess.Popen(args, cwd=workspace, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            cap_hit = threading.Event()

            def pump_events():
                nonlocal tool_items
                for line in process.stdout:   # the CLI's --json stream, one event per line, as it happens
                    out.write(line); out.flush()
                    if max_requests is None:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    item = event.get('item') if isinstance(event, dict) and event.get('type') == 'item.completed' else None
                    if isinstance(item, dict) and item.get('type') in TOOL_ITEMS:
                        tool_items += 1
                        if tool_items >= max_requests:
                            cap_hit.set()

            def pump_stderr():
                for line in process.stderr:
                    err.write(line)

            readers = [threading.Thread(target=pump_events, daemon=True), threading.Thread(target=pump_stderr, daemon=True)]
            for reader in readers:
                reader.start()
            try:
                try:
                    process.stdin.write(prompt); process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                while process.poll() is None:
                    allowance = timeout + (float(extra_time()) if extra_time is not None else 0.0)
                    if time.monotonic() - begin >= allowance:
                        timed_out = True
                        break
                    if cap_hit.wait(0.25):
                        capped = True
                        break
                if timed_out or capped:
                    _stop_group(process)
            finally:
                _stop_group(process)
                for reader in readers:
                    reader.join(timeout=10)
        except OSError as exc:
            launch_error = f'{type(exc).__name__}: {exc}'
    parsed = parse_events(output / 'events.jsonl')
    expected = parsed.pop('last_agent_message')
    # The CLI's --json stream does not always carry the final assistant
    # message as an agent_message item (observed 2026-09-22: a correct "12"
    # written to final.md with no matching event). The broker's own record
    # of the last model response is controller-written and equally immutable
    # to the actor, so a final that matches its message text is accepted too.
    if expected is None or (output.parent / 'api').is_dir():
        expected = [expected] if expected is not None else []
        try:
            responses = sorted((output.parent / 'api').glob('[0-9]*/response.json'))
            if responses:
                last = json.loads(responses[-1].read_text())
                texts = [''.join(c.get('text', '') for c in item.get('content', []) if isinstance(c, dict))
                         for item in last.get('output', []) if isinstance(item, dict) and item.get('type') == 'message']
                expected += [t for t in texts if t]
        except (OSError, ValueError):
            pass
    # Login mode has no broker record to fall back on: when the turn completed
    # and the stream carried no agent_message at all, the final codex itself
    # wrote (--output-last-message) is the answer (user decision 2026-09-22).
    accept_any = endpoint is None and parsed['turn_completed'] and not expected
    final_validation = collect_final(workspace, output, expected, accept_any=accept_any)
    exit_code = process.returncode if process is not None else None
    status = ('launch_error' if launch_error else 'timeout' if timed_out else 'request_cap' if capped else
              'invalid_final' if exit_code == 0 and parsed['turn_completed'] and not final_validation['valid'] else
              'completed' if exit_code == 0 and parsed['turn_completed'] else 'failed')
    infra = round(float(extra_time()), 3) if extra_time is not None else 0.0
    result = {'status': status, 'started_utc': started, 'finished_utc': _utc(), 'infra_seconds': infra,
              'elapsed_seconds': round(time.monotonic() - begin, 6), 'exit_code': exit_code,
              'timed_out': timed_out, 'request_capped': capped, 'tool_items_seen': tool_items,
              'final_message_event': bool(expected),
              'max_requests': max_requests, 'launch_error': launch_error, **parsed,
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
