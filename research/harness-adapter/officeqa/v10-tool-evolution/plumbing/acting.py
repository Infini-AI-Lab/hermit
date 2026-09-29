"""Isolated native Codex + Fireworks OfficeQA acting trials. Importing/preparing never calls a model.

Only run_actor launches inference. prepare_actor_workspace must run before the
caller copies a task starter into the workspace. This runner does not grade it.

v9 (L0, DESIGN.md §2): every episode gets the write-protected kit `<ws>/.kit/` (kit.py): frozen
python/rg shims, pack tool wrappers and a zsh profile that restores PATH after path_helper. The actor
shell environment is kit.tool_environment. The read roots are narrowed to the frozen python, the Codex
binary, the frozen rg and declared data (e.g. the tables index), plus exactly the code files the kit's
wrappers execute (kit meta['code']: scripts and their sibling imports; review fix D1: never all of
harness_core/ or packs/); the controller venv, the harnessadapter package and cua_node are granted only
when an MCP-server/roles adapter needs them (never for v9 arms). Workspace guards, the marker and the
final.md checks are unchanged from v8.
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
from dataclasses import asdict

from paths import CODEX, ACTOR_PYTHON_PREFIX, ACTOR_RG
from experiment import FULL, MWM, PROJECT_DOC_MAX_BYTES
from harness_core.contracts import ArmSpec, CONTRACT_VERSION, SUBMIT_ENV
import kit
MODEL ='accounts/fireworks/models/glm-5p3-flash'
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
    'guardian_approval', 'standalone_web_search', 'api_key_model_discovery', 'runtime_metrics',
)
# v9 settings, fixed for every arm (wave-2 decisions, S7-integrate):
# - multi-agent off: `features.multi_agent=false`, `agents.enabled=false`. v8 FULL had both on; in v9 a
#   sub-agent's requests would share the broker port and the ledger/controller cannot attribute them.
# - model_auto_compact_token_limit = 244,800, set explicitly: v8 configured 800,000, which Codex ignored for
#   this model (fallback metadata: 0.9 x 272,000 = 244,800 was the effective value, understand/compaction.md
#   §4). A lower configured limit is honoured (S3 live test), so declaring the effective value keeps v8's
#   compaction point and makes it visible in the config. model_context_window stays at the v8 value.
# - include_permissions_instructions=false (the build_settings/run_actor default): removes exactly the false
#   '<permissions instructions>' developer text (S1 §2.3, S3 §3.9).
# Temperature and top_p are not Codex settings: the broker sends the root's declared temperature on every call
# (broker.normalized; default 0, the v8 value) and its declared top_p (default: none sent, as v8); run_actor
# records both.
V9_SETTINGS = {'agents.enabled': False, 'features.multi_agent': False,
               'model_context_window': 1048576, 'model_auto_compact_token_limit': 244800}
EFFECTIVE_AUTO_COMPACT_OBSERVED = 244800
# The declared v8 -> v9 settings differences (recorded in every invocation.json; tests/test_v9_kit.py asserts
# that the argv difference against a recorded v8 invocation is exactly this set plus the kit/env/profile set).
V9_DECLARED_SETTINGS = {
    'agents.enabled': {'v8': True, 'v9': False, 'why': 'multi-agent off: sub-agent traffic would bypass the ledger'},
    'features.multi_agent': {'v8': True, 'v9': False, 'why': 'multi-agent off (removes the multi_agent_v1 tool)'},
    'model_auto_compact_token_limit': {'v8': 800000, 'v9': 244800,
                                       'why': "v8's effective limit (fallback metadata), now explicit"},
    'include_permissions_instructions': {'v8': None, 'v9': False,
                                         'why': "removes Codex's false 'no sandbox, network enabled' text"},
}
PROTECTED = ('AGENTS.md', 'README.md', 'officeqa.py', '.codex', MARKER, '.ha-demo-generated.json', kit.KIT)
# Review fix D2: the marker names the arm (name/learner/seed), the chosen note ids and the kit paths; v8
# actors printed it in 19 of 399 runs. Its content is unreadable to the actor (it stays listable).
HIDDEN = (MARKER,)


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


def _check_arm(arm):
    if not isinstance(arm, ArmSpec):
        raise TypeError('v9 acting needs an explicit harness_core.contracts.ArmSpec')
    arm.validate()
    return arm


def prepare_actor_workspace(workspace, adapter=None, *, common_instructions=COMMON_INSTRUCTIONS, arm,
                            tool_wrappers=None, submit_url=None, marker_extra=None, code_roots=None):
    """Create an EMPTY, dedicated workspace; caller may then copy task inputs.

Uses the repository's real Codex connector, then replaces its demo framing with
the exact common instructions used in the baseline. No demonstration prompt is
left behind. Candidate Python is trusted code: review it before this call.

v10 arms pass adapter=None and the rendered AGENTS.md text (common + chosen notes) as
common_instructions; the kit is built (kit.build_kit) before the marker, which records the arm,
the kit hashes and `marker_extra` (chosen note ids, the served revision and its neutral code root), so
build_settings can re-check them. `code_roots` (no default: campaign passes the neutral copy of the served
revision's tools/, DESIGN §3) bounds where tool scripts may live; the sandbox then grants only the recorded
code files (kit meta['code']), never the roots themselves. An arm without tool wrappers needs no code root.
"""
    arm = _check_arm(arm)
    if marker_extra is not None and not isinstance(marker_extra, dict):
        raise TypeError('marker_extra must be a JSON object')
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
        if FULL and MWM:
            from full_runtime import render_exports
            import harnessadapter.hosts.codex as native_connector
            original_render = native_connector.render_exports
            native_connector.render_exports = render_exports
        from harnessadapter.hosts.codex import prepare
        from harnessadapter.hosts.context import PROJECT_CONTEXT
        try:
            info = prepare(adapter, workspace, str(Path(sys.executable).absolute()))
        finally:
            if FULL and MWM: native_connector.render_exports = original_render
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
    kit_meta = kit.build_kit(workspace, arm, tool_wrappers or {}, submit_url, code_roots=code_roots)
    # The MWM runtime belongs to adapted workspaces only (v6f copied it into baseline ones too).
    if FULL and MWM and adapter is not None:
        shutil.copy2(Path(__file__).parent/'full_runtime.py',workspace/'.codex/mwm-runtime.py')
    metadata = {
        'workspace': str(workspace), 'adapter': str(adapter) if adapter else None,
        'common_instructions_sha256': hashlib.sha256(common_instructions.rstrip().encode()).hexdigest(),
        'agents_sha256': _sha(workspace / 'AGENTS.md'),
        'agents_md_bytes': (workspace / 'AGENTS.md').stat().st_size,
        'project_doc_max_bytes': PROJECT_DOC_MAX_BYTES,
        'prepared_utc': _utc(), 'deployment': info,
        'contract': CONTRACT_VERSION, 'arm': asdict(arm), 'kit': kit_meta,
        'extra': json.loads(json.dumps(marker_extra, ensure_ascii=False)) if marker_extra else None,
    }
    _json(workspace / MARKER, metadata)
    return metadata


def runtime_roots(*, server=False, data=()):
    """Exact code/interpreter roots; never grant the whole project or home.

    v9: the frozen actor python prefix, the Codex binary, the frozen rg and declared read-only data
    (e.g. the tables index). Kit code is granted file by file from the kit metadata (client_policy
    `code`), not here. The controller venv, its base interpreter, the harnessadapter package and
    cua_node are added only for an adapter that runs an MCP server or native roles (server=True;
    never a v9 arm).
    """
    roots = [ACTOR_PYTHON_PREFIX, CODEX, ACTOR_RG, *map(Path, data)]
    if server:
        import harnessadapter
        roots += [Path(sys.prefix), Path(sys.base_prefix), Path(harnessadapter.__file__).parent]
        node = Path('/Applications/ChatGPT.app/Contents/Resources/cua_node')
        if node.is_dir():
            roots.append(node)
    for root in roots:
        if not Path(root).exists():
            raise ValueError(f'Runtime/read root does not exist: {root}')
    return list(dict.fromkeys(str(Path(p).resolve()) for p in roots))


def permission_profile(workspace, corpus, index, adapter=None, *, audit=None, server=False):
    workspace = Path(workspace).resolve()
    fs = {':root': 'deny', ':minimal': 'read'}
    fs.update({str(Path(p).resolve()): 'read' for p in (corpus, index)})
    fs.update({p: 'read' for p in runtime_roots(server=server)})
    fs[str(workspace)] = 'read' if server else 'write'
    for name in PROTECTED:
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


def tool_environment(workspace, arm, extra=None):
    """v9: the kit environment (kit.tool_environment); see there for the keys and checks."""
    return kit.tool_environment(workspace, arm, extra)


def actor_env_extra(arm, endpoint, env_extra=None):
    """Caller-declared task variables plus, for controller arms, the broker submit URL derived from
    the actor's own endpoint (a caller-supplied value must agree with it)."""
    extra = dict(env_extra or {})
    if arm.gate:                      # v10: HARNESS_SUBMIT_URL iff the submit gate is on (DESIGN §3), never on `controller`
        url = kit.submit_url_for(endpoint)
        if extra.setdefault(SUBMIT_ENV, url) != url:
            raise ValueError(f'{SUBMIT_ENV} must be the broker route of this actor endpoint')
    return extra


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


def client_policy(workspace, corpus, index, adapter=None, *, endpoint, server=False, data=(), code=None):
    """One inherited OS profile; macOS does not permit nested sandbox_apply.

    Runtime allowances are explicit and intentionally exclude host home,
    parent workspace, other trials, control logs, gold, and general temp roots.
    The only TCP egress is the actor's own fixed Fireworks broker port.
    v9: read roots from runtime_roots(server, data); the kit `.kit` is write-protected like
    AGENTS.md (its files are readable/executable through the workspace grant); the marker's content is
    unreadable (HIDDEN). `code` is the kit
    metadata (marker['kit']): its code files are granted as literals and its listed directories as
    directory literals (listing only), so the actor reads exactly the code its wrappers run. None
    grants no code (an arm without wrappers).
    """
    from urllib.parse import urlparse
    validate_endpoint(endpoint)
    workspace = Path(workspace).resolve()
    code_files, code_dirs = kit.code_grants(code) if code is not None else ([], [])
    for path in (*code_files, *code_dirs):
        resolved = Path(path)
        if not resolved.is_absolute() or resolved.resolve() != resolved or not resolved.exists():
            raise ValueError(f'Code grant must be an existing canonical path: {path}')
        if not _separate(workspace, resolved):
            raise ValueError(f'Code grant must be outside the workspace: {path}')
    read_roots = list(dict.fromkeys([
        '/System', '/usr/lib', '/usr/share', '/usr/libexec', '/bin', '/sbin',
        '/usr/bin', '/usr/sbin', '/Library/Apple', '/Library/Preferences',
        '/private/etc', '/private/var/db',
        # v9: the /bin/sh trampoline reads this symlink (-> /bin/bash); without it every `sh` (and every
        # kit wrapper) prints 'Error opening /private/var/select/sh: Operation not permitted'.
        '/private/var/select/sh',
        *runtime_roots(server=server, data=data), str(Path(corpus).resolve()), str(Path(index).resolve()),
        *([str(Path(adapter).resolve())] if adapter is not None else []),
    ]))
    def filt(path):
        return '(subpath ' + json.dumps(str(path)) + ')'
    read_filters = ' '.join([*(filt(path) for path in read_roots),
                             *('(literal ' + json.dumps(path) + ')' for path in (*code_files, *code_dirs))])
    parents = sorted({str(parent) for path in [workspace, *map(Path, read_roots), *map(Path, code_files),
                                               *map(Path, code_dirs)] for parent in path.parents})
    metadata_filters = ' '.join('(literal ' + json.dumps(path) + ')' for path in parents)
    protected_filters = ' '.join(filt(workspace / name) for name in PROTECTED)
    hidden_filters = ' '.join('(literal ' + json.dumps(str(workspace / name)) + ')' for name in HIDDEN)
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
(deny file-read-data {hidden_filters})
(allow file-read* (literal \"/dev/random\") (literal \"/dev/urandom\"))
(allow file-read* file-write* (literal \"/dev/null\") (literal \"/dev/zero\")
  (literal \"/dev/tty\") (subpath \"/dev/fd\"))
(allow file-read-metadata (regex \"^/dev/.*$\"))
(allow pseudo-tty)
(allow file-read* file-write* file-ioctl (literal \"/dev/ptmx\") (regex \"^/dev/ttys[0-9]+$\"))
(allow network-outbound (remote tcp \"localhost:{urlparse(endpoint).port}\"))
"""


def client_command(command, workspace, output, corpus, index, adapter=None, *, endpoint, server=False, data=(),
                   code=None):
    return ['/usr/bin/sandbox-exec', '-p', client_policy(workspace, corpus, index, adapter, endpoint=endpoint,
                                                       server=server, data=data, code=code), *command]


def build_settings(workspace, output, corpus, index, adapter=None, *, endpoint, arm, env_extra=None, data=(),
                   permissions_instructions=False):
    """Codex -c settings. v9: shell env = kit environment (+ submit URL for controller arms), V9_SETTINGS for
    every arm (multi-agent off, explicit compaction limit 244,800), and `include_permissions_instructions=false`
    unless permissions_instructions=True (the default removes Codex's false permissions text; verified by
    the $0 real-Codex tests, which also show it changes nothing else)."""
    arm = _check_arm(arm)
    workspace, output = Path(workspace).resolve(), Path(output).resolve()
    validate_endpoint(endpoint)
    for source in (corpus, index, *data):
        if not Path(source).exists():
            raise ValueError('Corpus/index/data must exist before acting')
        if not _separate(workspace, Path(source).resolve()):
            raise ValueError('Read-only input roots must be separate from workspace')
    metadata = json.loads((workspace / MARKER).read_text())
    selected = str(Path(adapter).resolve()) if adapter is not None else None
    if metadata['adapter'] != selected:
        raise ValueError('Adapter argument must match prepared workspace')
    if _sha(workspace / 'AGENTS.md') != metadata['agents_sha256']:
        raise ValueError('Prepared project instructions were changed')
    if metadata.get('arm') != asdict(arm):
        raise ValueError('Arm argument must match prepared workspace')
    kit.verify_kit(workspace, metadata['kit'])
    kit.verify_frozen(deep=False)
    extra = actor_env_extra(arm, endpoint, env_extra)
    embedded = (metadata['kit'].get('submit') or {}).get('url')
    if embedded is not None and embedded != extra.get(SUBMIT_ENV):
        # The wrapper's embedded URL overrides the env; a stale port would send submits into the sandbox wall.
        raise ValueError('Submit URL embedded in the kit differs from this endpoint\'s broker route')
    settings = {'model': MODEL, 'model_provider': PROVIDER, 'model_reasoning_effort': EFFORT,
                # The inherited OS sandbox enforces the boundary for the client
                # and all its children; nested Seatbelt policies are unsupported.
                'sandbox_mode': 'danger-full-access',
                f'model_providers.{PROVIDER}': {
                    'name': 'GLM 5.3 Flash via local Fireworks-only broker',
                    'base_url': endpoint, 'wire_api': 'responses', 'requires_openai_auth': False,
                    'supports_websockets': False, 'supports_standalone_web_search': False,
                    # Codex clamps both at its hard maximum of 100 (MAX_*_MAX_RETRIES).
                    'request_max_retries': 100, 'stream_max_retries': 100,
                    'stream_idle_timeout_ms': 3600000},
                'project_doc_max_bytes': PROJECT_DOC_MAX_BYTES,
                'analytics.enabled': False, 'feedback.enabled': False,
                'otel.exporter': 'none', 'otel.trace_exporter': 'none', 'otel.metrics_exporter': 'none',
                'model_context_window': 196608, 'model_auto_compact_token_limit': 160000,
                'approval_policy': 'never', 'agents.enabled': False, 'web_search': 'disabled',
                'project_root_markers': [], 'features.skip_host_skill_discovery': True,
                'shell_environment_policy.inherit': 'none',
                'shell_environment_policy.set': tool_environment(workspace, arm, extra),
                # CLI override splitting does not preserve quoted dotted path
                # segments. Set the entire map, including ~/.codex/... paths.
                'projects': {str(workspace): {'trust_level': 'trusted'}}}
    settings.update({f'features.{name}': False for name in DISABLED_FEATURES})
    settings.update(V9_SETTINGS)
    if not permissions_instructions:
        settings['include_permissions_instructions'] = False
    # Native role registrations are intentionally not enabled in this comparison.
    if adapter is not None:
        info = metadata['deployment']
        if info.get('server'):
            env = settings['shell_environment_policy.set']
            # Child inherits the already-active whole-client OS sandbox. No
            # actor-writable audit log is claimed as an immutable controller log.
            server_args = ['-i', *[f'{k}={v}' for k, v in env.items()],
                           str(Path(sys.executable).absolute()), '-B', '-m',
                           'harnessadapter.runtime.mcp_server', '--adapter', str(Path(adapter).resolve())]
            if FULL and MWM:
                server_args = ['-i', *[f'{k}={v}' for k,v in env.items()],
                    str(Path(sys.executable).absolute()), '-B', str(workspace/'.codex/mwm-runtime.py'),
                    '--adapter',str(Path(adapter).resolve()),'--endpoint',endpoint]
            prefix = 'mcp_servers.' + info['server_name'] + '.'
            settings.update({prefix + 'command': '/usr/bin/env', prefix + 'args': server_args,
                             prefix + 'required': True, prefix + 'startup_timeout_sec': 60,
                             prefix + 'tool_timeout_sec': 660, prefix + 'default_tools_approval_mode': 'approve'})
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
    terminal_event = terminal[-1][1]['type'] if terminal else None
    error_text = None
    if terminal_event == 'turn.failed':
        # Only the failed turn's own error; standalone 'error' events also occur on completed runs.
        error = terminal[-1][1].get('error')
        error_text = (error.get('message') if isinstance(error, dict) else None) or (str(error) if error is not None else None)
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
            'terminal_type': terminal_event, 'terminal_event': terminal_event, 'error_text': error_text,
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


def run_actor(workspace, output, prompt, corpus, index, adapter=None, timeout=1800, *, endpoint, arm,
              env_extra=None, data=(), permissions_instructions=False, temperature=None, top_p=None):
    """Run exactly one fresh Codex task; no retries, grading or answer repair.

    v9: `arm` must match the prepared marker; `data` are extra read-only roots (e.g. the tables index);
    `env_extra` adds task variables to the kit environment. The kit is re-verified after the run.
    `temperature` and `top_p` (wave 3) are the values the episode's broker sends upstream (top_p None = not
    sent); they are only recorded here (invocation.json "temperature", "top_p"), Codex never sees them.
    """
    arm = _check_arm(arm)
    workspace, output = Path(workspace).resolve(), Path(output).resolve()
    if not _separate(workspace, output):
        raise ValueError('Run records must be outside the actor workspace')
    if adapter is not None and not _separate(output, Path(adapter).resolve()):
        raise ValueError('Run records and adapter must be disjoint')
    if (timeout is not None and timeout <= 0) or not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('Positive timeout and nonempty prompt required')
    for root in data:
        if not _separate(output, Path(root).resolve()):
            raise ValueError('Run records and read-only data must be disjoint')
    settings = build_settings(workspace, output, corpus, index, adapter, endpoint=endpoint, arm=arm,
                              env_extra=env_extra, data=data, permissions_instructions=permissions_instructions)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Run output must be empty; prior trials are never overwritten')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'prompt.md').write_text(prompt)
    # A context-only adapter (no MCP server, no roles) is already compiled into the
    # workspace's AGENTS.md; the actor gets no read access to its source files.
    marker = json.loads((workspace / MARKER).read_text())
    deployment = marker.get('deployment') or {}
    needs_runtime = bool(deployment.get('server') or deployment.get('roles'))
    readable_adapter = adapter if needs_runtime else None
    data = tuple(str(Path(root).resolve()) for root in data)
    args = client_command(command_for(workspace, output, settings), workspace, output,
                          corpus, index, readable_adapter, endpoint=endpoint, server=needs_runtime, data=data,
                          code=marker['kit'])
    invocation = {'argv': args, 'cwd': str(workspace), 'timeout_seconds': timeout,
                  'requested_model': MODEL, 'reasoning_effort': EFFORT, 'settings': settings,
                  'adapter': str(Path(adapter).resolve()) if adapter else None,
                  'corpus': str(Path(corpus).resolve()), 'index': str(Path(index).resolve()),
                  'sandbox_scope': 'Single inherited OS sandbox for whole Codex client/tools/MCP; network only own Fireworks broker TCP port; controller records excluded',
                  'prompt_sha256': _sha(output / 'prompt.md'), 'agents_sha256': _sha(workspace / 'AGENTS.md'),
                  'feedback_source': ('No human feedback; model and tool outputs plus broker controller text '
                                      + ('(loop notes, ledger splice, submit findings, enforcement re-query)'
                                         if arm.gate else '(loop notes, ledger splice; no submit gate)')
                                      if arm.controller else
                                      'No dynamic human/Astra feedback; only model and tool outputs'),
                  'arm': asdict(arm), 'contract': CONTRACT_VERSION, 'kit_sha256': marker['kit']['kit_sha256'],
                  'frozen_runtime': kit.verify_frozen(deep=False), 'data_roots': list(data),
                  'read_roots': runtime_roots(server=needs_runtime, data=data),
                  'code_read': dict(zip(('files', 'dirs'), kit.code_grants(marker['kit']))),
                  'permissions_instructions': permissions_instructions,
                  'temperature': temperature, 'top_p': top_p,
                  'auto_compact_token_limit': settings.get('model_auto_compact_token_limit'),
                  'effective_auto_compact_limit_observed': EFFECTIVE_AUTO_COMPACT_OBSERVED,
                  'v9_declared_settings': V9_DECLARED_SETTINGS}
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
    try:
        kit_after = {'intact': True, 'kit_sha256': kit.verify_kit(workspace, marker['kit'])}
    except (OSError, ValueError) as exc:
        kit_after = {'intact': False, 'error': str(exc)[:500]}
    exit_code = process.returncode if process is not None else None
    status = ('launch_error' if launch_error else 'timeout' if timed_out else
              'invalid_final' if exit_code == 0 and parsed['turn_completed'] and not final_validation['valid'] else
              'completed' if exit_code == 0 and parsed['turn_completed'] else 'failed')
    result = {'status': status, 'started_utc': started, 'finished_utc': _utc(),
              'elapsed_seconds': round(time.monotonic() - begin, 6), 'exit_code': exit_code,
              'timed_out': timed_out, 'launch_error': launch_error, **parsed,
              'final_validation': final_validation, 'kit_after': kit_after,
              'agents_md_bytes': marker.get('agents_md_bytes'),
              'project_doc_max_bytes': marker.get('project_doc_max_bytes'),
              'requested_model': MODEL, 'reasoning_effort': EFFORT,
              'final_message_present': (output / 'final.md').is_file(),
              'output_directory': str(output), 'grading': 'not performed by acting runner'}
    result['record_hashes'] = {p.name: _sha(p) for p in output.iterdir() if p.is_file()}
    _json(output / 'result.json', result)
    return result


def validate_config_offline(settings, *, timeout=25, home=None, policy=None):
    """Strict local config/read RPC, using a temporary empty account home.

    This starts the CLI server and asks for configuration only. No thread or
    inference request is made. Full prompt rendering is not claimed here.
    v9: with `policy` (a client_policy profile) the server runs under that Seatbelt profile, so it
    also shows that Codex starts inside the narrowed read roots with no egress but the broker port;
    `home` must then be an empty directory inside the profile's writable workspace.
    """
    if policy is not None and home is None:
        raise ValueError('A sandboxed validation needs a home inside the writable workspace')
    with tempfile.TemporaryDirectory(prefix='acting-config-') as directory:
        base = Path(home).resolve() if home is not None else Path(directory)
        if home is not None:
            base.mkdir(parents=True, exist_ok=True)
            if any(base.iterdir()):
                raise ValueError('Validation home must be empty')
        env = client_environment(base)
        args = [str(CODEX), 'app-server', '--strict-config', '--stdio']
        for key, value in settings.items():
            args += ['-c', key + '=' + toml_value(value)]
        if policy is not None:
            args = ['/usr/bin/sandbox-exec', '-p', policy, *args]
        with tempfile.TemporaryFile() as errors:
            # An explicit home is also the cwd: a sandboxed Codex must be able to read its cwd.
            process = subprocess.Popen(args, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=errors, text=True, start_new_session=True, bufsize=1,
                                       cwd=base if home is not None else None)
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
