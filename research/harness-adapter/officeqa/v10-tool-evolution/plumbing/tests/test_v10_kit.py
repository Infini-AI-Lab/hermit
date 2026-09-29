"""Tier-0 tests of the v10 L0 kit and sandbox (port of the v9 kit tests). No model call, no network except 127.0.0.1.

Run:  <venv>/bin/python -m pytest -q tests/test_v10_kit.py          (tier 0; about 10 s)
      V9_LIVE_CODEX=1 <venv>/bin/python -m pytest -q tests/test_v10_kit.py   (+ the two real `codex exec` episodes, $0)
CLI:  tests/test_v10_kit.py make SCRATCH ARM   builds a probe workspace/profile/env (used by probe_kit.sh)
      tests/test_v10_kit.py diff               prints the v8 -> v10 argv/profile/env differences (markdown)

What is covered:
- the frozen runtime copy (manifest, versions, no dependency on the original codex-runtimes path);
- kit.build_kit / verify_kit / tool_environment / readme_l0 and their defensive checks, including the v10
  rules: the submit wrapper and HARNESS_SUBMIT_URL exist iff the arm's gate is on (never keyed on
  `controller`), tools come only from caller-declared code roots (the adapter's tools/ copy), a tool name
  that resolves on the actor BASE_PATH is refused, and the harness W0 manifest wraps as the kit expects;
- acting: narrowed runtime roots, protected `.kit`, the declared settings (multi-agent off, explicit
  model_auto_compact_token_limit=244800, include_permissions_instructions=false by default), arm/kit re-checks;
- tests/probe_kit.sh: every probe command under `/usr/bin/sandbox-exec -p <profile> /bin/zsh -lc|-c`;
- acting.validate_config_offline (Codex app-server --strict-config, config/read only) outside and inside
  the profile;
- the pinned real `codex exec` inside the profile against a local fake Responses server (127.0.0.1);
- the v8 -> v10 difference set equals the documented, intentional one (DIFF_* constants).

The six standard arms have the gate off; `harness-gate` (ARMS) is a test-only custom arm with the gate on,
so the submit path (wrapper, env, README grant, /v1/submit) stays covered.
"""
from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading

import pytest

PLUMBING = Path(__file__).resolve().parents[1]
PROJECT = PLUMBING.parent
sys.path.insert(0, str(PLUMBING))
import acting  # noqa: E402
import arms  # noqa: E402
import kit  # noqa: E402
import paths  # noqa: E402
from harness_core.contracts import CONTRACT_VERSION, STANDARD_ARMS, SUBMIT_ENV, ArmSpec  # noqa: E402

CORPUS = PROJECT / 'opus-officeqa-133/corpus'
INDEX = PROJECT / 'opus-officeqa-133/corpus.sqlite'
CORPUS_FILE = CORPUS / 'treasury_bulletin_1939_01.txt'
V8_RUNS = PROJECT / 'officeqa-pro133-none-v8-work/runs'
V8_INVOCATION = V8_RUNS / 'baseline/001/records/invocation.json'
VENV = PROJECT / 'adapter-comparison-work/.venv'
HARNESSADAPTER_V8 = PROJECT / 'frameworks-v8/next-a48c0f9/harnessadapter'
CUA_NODE = Path('/Applications/ChatGPT.app/Contents/Resources/cua_node')
REAL_HOME = Path.home()
PROBE_SH = PLUMBING / 'tests/probe_kit.sh'
PROBE_PORT = 1                     # the actor's only allowed TCP port in the probe profile (nothing listens)
VERSIONS = {'python': '3.12.14', 'numpy': '2.3.5', 'pandas': '2.2.3'}
SANDBOX = Path('/usr/bin/sandbox-exec')
W0_TOOLS = PLUMBING / 'adapters/harness-w0/tools'
# The arms the kit tests run: the six standard (gate off) + a test-only gate-on harness arm.
ARMS = {**STANDARD_ARMS,
        'harness-gate': replace(STANDARD_ARMS['harness-static'], name='harness-gate', gate=True)}

needs_sandbox = pytest.mark.skipif(not SANDBOX.exists() or not CORPUS_FILE.is_file() or not INDEX.is_file(),
                                   reason='needs macOS sandbox-exec and the opus-officeqa-133 corpus')

# ---------------------------------------------------------------------------------------------
# Probe workspace (shared by probe_kit.sh and the tests)
# ---------------------------------------------------------------------------------------------

PROBE_TOOL = '''import json, os, sys
import numpy
try:
    from . import probe_helper
except ImportError:                      # as tools/cli.py: put its own directory on sys.path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import probe_helper
print(json.dumps({"argv": sys.argv[1:], "isolated": sys.flags.isolated, "no_bytecode": sys.dont_write_bytecode,
                  "executable": sys.executable, "prefix": sys.prefix, "numpy": numpy.__version__,
                  "helper": probe_helper.VALUE, "submit_url": os.environ.get("HARNESS_SUBMIT_URL")}))
'''
PROBE_HELPER = 'VALUE = "helper-ok"\n'
# Next to the tool but never imported by it: the actor must not read these (seed files, engine code).
PROBE_SECRET = '{"seed": "not for the actor"}\n'
PROBE_UNUSED = 'RULES = "not for the actor"\n'
IMPORTS = ('python3 -c \'import json, sys, sqlite3, numpy, pandas; c = sqlite3.connect(":memory:"); '
           'c.execute("create virtual table t using fts5(x)"); print(json.dumps({"python": sys.version.split()[0], '
           '"prefix": sys.prefix, "numpy": numpy.__version__, "pandas": pandas.__version__, "fts5": True, '
           '"foreign": [p for p in sys.path if "codex-runtimes" in p]}))\'')
NET = ('python3 -c \'import socket, sys; s = socket.socket(); s.settimeout(2)\n'
       'try:\n    s.connect((sys.argv[1], int(sys.argv[2]))); print("OPEN")\n'
       'except Exception as e:\n    print("blocked", type(e).__name__, getattr(e, "errno", None))\' ')
KIT_WRITES = '''k=.kit
touch $k/bin/new 2>&1; echo "rc=$?"
echo x >> $k/zdot/.zprofile 2>&1; echo "rc=$?"
rm -f $k/zdot/.zprofile 2>&1; echo "rc=$?"
chmod u+w $k/zdot/.zprofile 2>&1; echo "rc=$?"
mv $k kit-moved 2>&1; echo "rc=$?"
ln $k/zdot/.zprofile hardlink 2>&1; echo "rc=$?"
ln -s /tmp $k/bin/evil 2>&1; echo "rc=$?"
rm -rf $k 2>&1; echo "rc=$?"
echo x >> AGENTS.md 2>&1; echo "rc=$?"'''
HEREDOC = """python3 - <<'PY'
print('heredoc-ok', 6 * 7)
PY
cat <<'X'
cat-heredoc-ok
X"""


def _denied(out, rc):
    assert 'Operation not permitted' in out, out


def _json_line(out):
    return json.loads([line for line in out.splitlines() if line.startswith('{')][-1])


def _check_imports(out, rc, ctx):
    data = _json_line(out)
    assert data['prefix'] == str(paths.ACTOR_PYTHON_PREFIX) and not data['foreign'], data
    assert (data['python'], data['numpy'], data['pandas'], data['fts5']) == (*VERSIONS.values(), True), data


def _check_rc_all_failed(out, rc, ctx):
    codes = re.findall(r'rc=(\d+)', out)
    assert len(codes) == 9 and all(code != '0' for code in codes), out


def _check_tool(out, rc, ctx):
    if not ctx['arm'].l1_tools:
        assert out.strip() == 'none', out
        return
    data = _json_line(out)
    assert data['argv'] == ['sub', 'alpha', 'b c'] and data['isolated'] == 1 and data['no_bytecode'], data
    assert data['helper'] == 'helper-ok', data
    assert data['executable'] == str(paths.ACTOR_PYTHON) and data['numpy'] == VERSIONS['numpy'], data
    assert data['submit_url'] == (ctx['submit_url'] if ctx['arm'].gate else None), data


def _check_submit(out, rc, ctx):
    lines = out.strip().splitlines()
    if ctx['arm'].gate:
        assert lines == [ctx['kit_bin'] + '/submit', ctx['submit_url']], out
    else:
        assert lines == ['none', 'unset'], out


# name, zsh modes, command (with @TOKENS@), check(out, rc, ctx) or None (informational)
PROBES = [
    ('path', ('lc', 'c'), 'print -r -- "$PATH"', lambda out, rc, ctx: out.strip() == ctx['path'] or _fail(out)),
    ('whence', ('lc',), 'whence -p python python3 rg',
     lambda out, rc, ctx: out.split() == [ctx['kit_bin'] + '/' + n for n in ('python', 'python3', 'rg')] or _fail(out)),
    ('python3_version', ('lc',), 'python3 --version',
     lambda out, rc, ctx: out.strip() == 'Python ' + VERSIONS['python'] or _fail(out)),
    ('imports', ('lc', 'c'), IMPORTS, _check_imports),
    ('rg_file', ('lc', 'c'), 'rg -c -i treasury @CORPUS_FILE@',
     lambda out, rc, ctx: (rc == 0 and int(out.strip()) > 0) or _fail(out)),
    ('rg_corpus', ('lc',), "rg -l -i 'public debt' @CORPUS@ | wc -l",
     lambda out, rc, ctx: int(out.strip()) > 100 or _fail(out)),
    ('heredoc', ('lc',), HEREDOC,
     lambda out, rc, ctx: ('heredoc-ok 42' in out and 'cat-heredoc-ok' in out and rc == 0) or _fail(out)),
    ('shell_env', ('lc',), 'print -r -- "$TMPPREFIX|$ZDOTDIR|$HOME|$TMPDIR"',
     lambda out, rc, ctx: out.strip() == ctx['shell_env'] or _fail(out)),
    ('write_workspace', ('lc',), 'echo ok > note.txt && cat note.txt', lambda out, rc, ctx: out.strip() == 'ok' or _fail(out)),
    ('write_outside', ('lc',), 'for p in /private/tmp/probe-kit-outside @PARENT@/probe-kit-outside; do '
     ': > "$p" 2>/dev/null && echo "WROTE $p" || echo "denied $p"; done',
     lambda out, rc, ctx: (out.count('denied') == 2 and 'WROTE' not in out) or _fail(out)),
    ('read_documents', ('lc',), 'ls @HOME@/Documents 2>&1 | head -3', lambda out, rc, ctx: _denied(out, rc)),
    ('read_v8_runs', ('lc',), 'ls @V8_RUNS@ 2>&1 | head -3', lambda out, rc, ctx: _denied(out, rc)),
    ('read_source_runtime', ('lc',), 'ls @SOURCE_RUNTIME@ 2>&1 | head -3', lambda out, rc, ctx: _denied(out, rc)),
    ('read_venv', ('lc',), 'ls @VENV@ 2>&1 | head -3', lambda out, rc, ctx: _denied(out, rc)),
    ('read_harnessadapter', ('lc',), 'ls @HARNESSADAPTER@ 2>&1 | head -3', lambda out, rc, ctx: _denied(out, rc)),
    ('read_cua_node', ('lc',), 'ls @CUA_NODE@ 2>&1 | head -3', lambda out, rc, ctx: _denied(out, rc)),
    ('read_homebrew', ('lc',), 'ls /opt/homebrew/bin 2>&1 | head -3', lambda out, rc, ctx: _denied(out, rc)),
    ('net_1111', ('lc',), NET + '1.1.1.1 443',
     lambda out, rc, ctx: out.strip() == 'blocked PermissionError 1' or _fail(out)),
    ('net_local_other_port', ('lc',), NET + '127.0.0.1 22',
     lambda out, rc, ctx: out.strip() == 'blocked PermissionError 1' or _fail(out)),
    ('kit_write', ('lc',), KIT_WRITES, _check_rc_all_failed),
    # The marker (arm name, learner, note ids) is listable but its content is unreadable.
    ('read_marker', ('lc',), 'ls -a | grep -c acting-prepared; cat .acting-prepared.json 2>&1 | head -c 200',
     lambda out, rc, ctx: (out.splitlines()[0] == '1' and 'Operation not permitted' in out and 'harness' not in out
                           and '"arm"' not in out) or _fail(out)),
    # Only the code the wrappers run is readable; seed files, the rule engine, the adapters and files next to
    # a tool that it does not import are denied in every arm.
    ('read_code_other', ('lc',), 'for p in @TOOLS@/secret.json @TOOLS@/unused.py @PACKS@/officeqa/seed_notes.json '
     '@PACKS@/officeqa/seed_rules.json @HARNESS_CORE@/rules.py @PACKS@/toy/items.py @ADAPTERS@/harness-w0/SEED.md '
     '@ADAPTERS@/harness-w0/controls.json; do head -c 1 "$p" >/dev/null 2>&1 '
     '&& echo "READ $p" || echo "denied"; done',
     lambda out, rc, ctx: (out.split() == ['denied'] * 8) or _fail(out)),
    ('sh_quiet', ('lc',), "sh -c 'echo sh-ok' 2>&1", lambda out, rc, ctx: out.strip() == 'sh-ok' or _fail(out)),
    ('tool_wrapper', ('lc',), "if whence -p probe_tool >/dev/null; then probe_tool alpha 'b c'; else echo none; fi",
     _check_tool),
    ('submit', ('lc',), 'whence -p submit || echo none; print -r -- "${HARNESS_SUBMIT_URL:-unset}"', _check_submit),
    # Informational: a *bash* login shell runs /etc/profile's path_helper and has no kit profile (known gap).
    ('bash_login', ('lc',), "/bin/bash -lc 'echo \"$PATH\"; command -v python3' 2>&1", None),
]


def _fail(out):
    raise AssertionError(out)


def make_probe(scratch, arm_name, *, port=PROBE_PORT):
    """Build <scratch>/{ws, tools, v9.sb, env.args, probes/} exactly as run_actor would, and return ctx."""
    scratch = Path(scratch).resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    arm = ARMS[arm_name]
    tools = scratch / 'tools'
    tools.mkdir()
    for name, text in (('probe_tool.py', PROBE_TOOL), ('probe_helper.py', PROBE_HELPER), ('secret.json', PROBE_SECRET),
                       ('unused.py', PROBE_UNUSED)):
        (tools / name).write_text(text)
    workspace = scratch / 'ws'
    # [script, fixed args] form, as a multi-command manifest tool is wrapped (cli.py show_table ...).
    wrappers = {'probe_tool': [str(tools / 'probe_tool.py'), 'sub']} if arm.l1_tools else {}
    marker = acting.prepare_actor_workspace(workspace, None, common_instructions='# Probe workspace\n', arm=arm,
                                            tool_wrappers=wrappers, code_roots=[tools])
    (workspace / 'README.md').write_text(kit.readme_l0(arm, CORPUS, INDEX))
    endpoint = f'http://127.0.0.1:{port}/v1'
    # No `data` grant for the tool directory: the kit's own code grants (marker['kit']['code']) must suffice.
    settings = acting.build_settings(workspace, scratch / 'records', CORPUS, INDEX, endpoint=endpoint, arm=arm)
    profile = acting.client_policy(workspace, CORPUS, INDEX, endpoint=endpoint, code=marker['kit'])
    env = settings['shell_environment_policy.set']
    if any(re.search(r'\s', value) for value in env.values()):
        raise ValueError('probe_kit.sh passes env values unquoted; whitespace is not allowed')
    (scratch / 'v9.sb').write_text(profile)
    (scratch / 'env.args').write_text(''.join(f'{k}={v}\n' for k, v in env.items()))
    tokens = {'@CORPUS_FILE@': str(CORPUS_FILE), '@CORPUS@': str(CORPUS), '@PARENT@': str(scratch),
              '@HOME@': str(REAL_HOME), '@V8_RUNS@': str(V8_RUNS), '@SOURCE_RUNTIME@': str(kit.SOURCE_PYTHON_PREFIX),
              '@VENV@': str(VENV), '@HARNESSADAPTER@': str(HARNESSADAPTER_V8), '@CUA_NODE@': str(CUA_NODE),
              '@TOOLS@': str(tools), '@PACKS@': str(paths.PACKS), '@HARNESS_CORE@': str(paths.HARNESS_CORE),
              '@ADAPTERS@': str(arms.ADAPTERS)}
    probes = scratch / 'probes'
    probes.mkdir()
    number = 0
    for name, modes, command, _ in PROBES:
        for mode in modes:
            number += 1
            text = command
            for token, value in tokens.items():
                text = text.replace(token, value)
            (probes / f'{number:02}_{name}__{mode}.zsh').write_text(text + '\n')
    kit_bin = str(workspace / kit.KIT / 'bin')
    return {'arm': arm, 'workspace': workspace, 'kit_bin': kit_bin, 'path': kit.actor_path(workspace),
            'submit_url': env.get(SUBMIT_ENV), 'env': env, 'profile': profile, 'settings': settings, 'marker': marker,
            'shell_env': '|'.join(env[k] for k in ('TMPPREFIX', 'ZDOTDIR', 'HOME', 'TMPDIR'))}


def parse_probe_output(text):
    """Sections '=== NN_name__mode' ... '--- rc=N' -> {(name, mode): (output, rc)}."""
    results = {}
    for match in re.finditer(r'^=== \d+_(\w+?)__(\w+)\n(.*?)^--- rc=(\d+)$', text, re.S | re.M):
        results[(match.group(1), match.group(2))] = (match.group(3), int(match.group(4)))
    return results

# ---------------------------------------------------------------------------------------------
# Frozen runtime
# ---------------------------------------------------------------------------------------------


def test_frozen_runtime_manifest_deep():
    info = kit.verify_frozen(deep=True)
    assert info['versions']['python'] == VERSIONS['python'] and info['versions']['fts5'] is True
    assert (info['versions']['numpy'], info['versions']['pandas']) == (VERSIONS['numpy'], VERSIONS['pandas'])
    manifest = json.loads(paths.ACTOR_PYTHON_MANIFEST.read_text())
    assert manifest['file_count'] == len(manifest['files']) > 10_000
    assert all(len(sha) == 64 for sha in manifest['files'].values())
    # The copy is read-only, so no interpreter can add __pycache__ files that would break the manifest.
    assert not os.access(paths.ACTOR_PYTHON_PREFIX, os.W_OK) and not os.access(paths.ACTOR_RG, os.W_OK)


def test_frozen_python_runs_from_its_own_prefix():
    versions = kit.runtime_versions()
    assert versions['prefix'] == str(paths.ACTOR_PYTHON_PREFIX)
    completed = subprocess.run([str(paths.ACTOR_PYTHON), '-I', '-B', '-c', 'import sys, json; print(json.dumps(sys.path))'],
                               env={}, capture_output=True, text=True, check=True, cwd='/')
    assert not [p for p in json.loads(completed.stdout) if 'codex-runtimes' in p]
    # Nothing in the copy links to the original location (console-script shebangs are never used by the kit).
    assert 'codex-runtimes' not in subprocess.run(['/usr/bin/otool', '-L', str(paths.ACTOR_PYTHON)],
                                                  capture_output=True, text=True).stdout


def test_frozen_rg_matches_source():
    assert kit.verify_frozen()['rg_sha256'] == paths.ACTOR_RG_SHA256_FILE.read_text().split()[0]
    assert subprocess.run([str(paths.ACTOR_RG), '--version'], capture_output=True, text=True).stdout.startswith('ripgrep 15.2.0')

# ---------------------------------------------------------------------------------------------
# kit.py
# ---------------------------------------------------------------------------------------------


def _ws(tmp_path, name='ws'):
    workspace = (tmp_path / name).resolve()
    workspace.mkdir()
    return workspace


@pytest.mark.parametrize('arm_name', sorted(ARMS))
def test_build_kit_layout(tmp_path, arm_name):
    arm = ARMS[arm_name]
    (tmp_path / 'tools').mkdir()
    script = (tmp_path / 'tools/tool.py').resolve()
    script.write_text('print(1)\n')
    wrappers = {'find_tables': str(script), 'show_table': [str(script), 'show_table']} if arm.l1_tools else {}
    meta = kit.build_kit(_ws(tmp_path), arm, wrappers, None, code_roots=[tmp_path / 'tools'])
    if arm.l1_tools:
        assert (tmp_path / 'ws/.kit/bin/show_table').read_text().endswith(f'-I -B {script} show_table "$@"\n')
        assert meta['tools']['show_table']['args'] == ['show_table'] and meta['tools']['find_tables']['args'] == []
    names = {path.name for path in (tmp_path / 'ws/.kit/bin').iterdir()}
    # the submit wrapper exists iff the gate is on: harness-static (controller on, gate off) has none
    assert names == {'python', 'python3', 'rg'} | set(wrappers) | ({'submit'} if arm.gate else set())
    assert os.readlink(tmp_path / 'ws/.kit/bin/python3') == str(paths.ACTOR_PYTHON)
    assert os.readlink(tmp_path / 'ws/.kit/bin/rg') == str(paths.ACTOR_RG)
    profile = (tmp_path / 'ws/.kit/zdot/.zprofile').read_text()
    assert f'export PATH={kit.actor_path(tmp_path / "ws")}\n' in profile
    if arm.gate:
        text = (tmp_path / 'ws/.kit/bin/submit').read_text()
        assert text.startswith('#!/bin/sh\n') and f'-I -B {kit.SUBMIT_SCRIPT}' in text and SUBMIT_ENV not in text
        assert meta['submit'] == {'script': str(kit.SUBMIT_SCRIPT), 'present': kit.SUBMIT_SCRIPT.is_file(),
                                  'url_embedded': False, 'url': None}
    else:
        assert meta['submit'] is None
    if wrappers:
        assert (tmp_path / 'ws/.kit/bin/find_tables').read_text().endswith(f'-I -B {script} "$@"\n')
    # Code grants: exactly the wrapped scripts (tool.py imports no sibling, so no directory listing) and submit.py.
    expected = ({str(script)} if wrappers else set()) | ({str(kit.SUBMIT_SCRIPT)} if arm.gate else set())
    assert set(meta['code']['files']) == expected and meta['code']['dirs'] == [], meta['code']
    assert kit.code_grants(meta) == (sorted(expected), [])
    assert meta['arm']['gate'] == arm.gate and meta['arm']['seed'] == arm.seed
    assert kit.verify_kit(tmp_path / 'ws', meta) == meta['kit_sha256']
    assert json.loads(json.dumps(meta)) == meta          # goes into the marker as JSON


def test_build_kit_code_closure(tmp_path):
    """Sibling imports (cli.py -> otables.py pattern) are granted, the directory becomes listable, and
    files the tool does not import are not granted; a sibling package is refused."""
    tools = (tmp_path / 'tools').resolve()
    tools.mkdir()
    (tools / 'cli.py').write_text('import sys, os\ntry:\n    from . import tables\nexcept ImportError:\n'
                                  '    import tables\ndef f():\n    from helper import g\n')
    (tools / 'tables.py').write_text('import json\nfrom common import X\n')
    (tools / 'helper.py').write_text('g = 1\n')
    (tools / 'common.py').write_text('X = 1\n')
    (tools / 'seed.json').write_text('{}\n')
    (tools / 'unused.py').write_text('x = 1\n')
    meta = kit.build_kit(_ws(tmp_path), ARMS['harness-static'], {'t': [str(tools / 'cli.py'), 'sub']}, None,
                         code_roots=[tools])
    assert sorted(meta['code']['files']) == [str(tools / n) for n in ('cli.py', 'common.py', 'helper.py', 'tables.py')]
    assert meta['code']['dirs'] == [str(tools)]
    (tools / 'pkg').mkdir()
    (tools / 'cli2.py').write_text('import pkg\n')
    with pytest.raises(ValueError, match='local package'):
        kit.build_kit(_ws(tmp_path, 'ws2'), ARMS['harness-static'], {'t': str(tools / 'cli2.py')}, None,
                      code_roots=[tools])
    # The harness W0 manifest, wrapped from the adapter's own tools/ as the neutral code root would be: exactly
    # cli.py + otables.py + calc.py, the only listable directory is that tools/, and its listing holds no seed,
    # note, controls or rule file name.
    wrappers = arms.manifest_wrappers(W0_TOOLS)
    assert set(wrappers) == {'find_tables', 'show_table', 'printings', 'calc'}
    real = kit.build_kit(_ws(tmp_path, 'ws3'), ARMS['harness-static'], wrappers, None, code_roots=[W0_TOOLS])
    names = {Path(f).name for f in real['code']['files']}
    assert names == {'cli.py', 'otables.py', 'calc.py'}, names
    assert all(Path(f).parent == W0_TOOLS for f in real['code']['files'])
    assert real['code']['dirs'] == [str(W0_TOOLS)]
    listing = {p.name for p in W0_TOOLS.iterdir()}
    assert listing == {'cli.py', 'otables.py', 'calc.py', 'manifest.json', 'USAGE.md'}, listing
    assert (tmp_path / 'ws3/.kit/bin/show_table').read_text().endswith(
        f'-I -B {W0_TOOLS / "cli.py"} show_table "$@"\n')
    assert (tmp_path / 'ws3/.kit/bin/calc').read_text().endswith(f'-I -B {W0_TOOLS / "calc.py"} "$@"\n')
    gated = kit.build_kit(_ws(tmp_path, 'ws4'), ARMS['harness-gate'], wrappers, None, code_roots=[W0_TOOLS])
    assert {Path(f).name for f in gated['code']['files']} == names | {'submit.py'}


def test_build_kit_embeds_submit_url(tmp_path):
    url = 'http://127.0.0.1:4567/v1/submit'
    meta = kit.build_kit(_ws(tmp_path), ARMS['harness-gate'], {}, url, submit=True)
    assert f"{SUBMIT_ENV}={url}; export {SUBMIT_ENV}" in (tmp_path / 'ws/.kit/bin/submit').read_text()
    assert meta['submit']['url_embedded'] is True and meta['submit']['url'] == url


def test_build_kit_rejects(tmp_path):
    (tmp_path / 'tools').mkdir()
    script = (tmp_path / 'tools/tool.py').resolve()
    script.write_text('print(1)\n')
    plain, harness, gate = ARMS['plain'], ARMS['harness-static'], ARMS['harness-gate']
    roots = [tmp_path / 'tools']
    cases = [
        (plain, {'find_tables': str(script)}, None, roots, 'no adapter tools'),
        (harness, {'python3': str(script)}, None, roots, 'reserved'),
        (harness, {'submit': str(script)}, None, roots, 'reserved'),
        (harness, {'Bad Name': str(script)}, None, roots, 'reserved'),
        (harness, {'sed': str(script)}, None, roots, 'shadow the system command /usr/bin/sed'),
        (harness, {'cat': str(script)}, None, roots, 'shadow'),
        (harness, {'t': 'relative.py'}, None, roots, 'canonical regular file'),
        (harness, {'t': str(tmp_path / 'missing.py')}, None, roots, 'canonical regular file'),
        (harness, {'t': str(script)}, None, None, 'outside the read-only code roots'),   # no code root declared
        (harness, {'t': str(script)}, None, [paths.HARNESS_CORE], 'outside the read-only code roots'),
        (plain, {}, 'http://127.0.0.1:1/v1/submit', None, 'Only gate arms'),
        (harness, {}, 'http://127.0.0.1:1/v1/submit', None, 'Only gate arms'),      # controller on, gate off
        (gate, {}, 'http://127.0.0.1:1/v1/other', None, 'Submit URL'),
        (gate, {}, 'http://example.com:1/v1/submit', None, 'Submit URL'),
    ]
    for number, (arm, wrappers, url, code_roots, message) in enumerate(cases):
        workspace = _ws(tmp_path, f'ws{number}')
        with pytest.raises(ValueError, match=message):
            kit.build_kit(workspace, arm, wrappers, url, code_roots=code_roots)
    with pytest.raises(TypeError):
        kit.build_kit(_ws(tmp_path, 'wsT'), 'plain', {}, None)
    with pytest.raises(ValueError, match='prompt adapter has no tools'):
        kit.build_kit(_ws(tmp_path, 'wsV'), replace(harness, seed='text'), {}, None)
    # `submit` is explicit and must agree with the arm's gate (DESIGN §3: submit iff gate)
    for number, (arm, submit) in enumerate([(harness, True), (gate, False), (plain, True)]):
        with pytest.raises(ValueError, match='submit must be true iff the arm gate'):
            kit.build_kit(_ws(tmp_path, f'wsS{number}'), arm, {}, None, submit=submit)
    for number, bad in enumerate([[str(script), 'two words'], [str(script), '$(id)'], [str(script), 3]]):
        with pytest.raises(ValueError, match='plain tokens'):
            kit.build_kit(_ws(tmp_path, f'wsA{number}'), harness, {'t': bad}, None, code_roots=roots)
    workspace = _ws(tmp_path, 'twice')
    kit.build_kit(workspace, plain, {}, None)
    with pytest.raises(ValueError, match='prepared once'):
        kit.build_kit(workspace, plain, {}, None)
    (tmp_path / 'link').symlink_to(_ws(tmp_path, 'real'))
    with pytest.raises(ValueError, match='canonical'):
        kit.build_kit(tmp_path / 'link', plain, {}, None)
    for number, name in enumerate(('with space', 'with:colon')):     # ':' would split the kit PATH entry
        odd = tmp_path / name
        odd.mkdir()
        with pytest.raises(ValueError, match='unsafe'):
            kit.build_kit(odd, plain, {}, None)
    inside = _ws(tmp_path, 'wsI')
    (inside / 'tools').mkdir()
    (inside / 'tools/t.py').write_text('print(1)\n')
    with pytest.raises(ValueError, match='disjoint from the workspace'):
        kit.build_kit(inside, harness, {'t': str(inside / 'tools/t.py')}, None, code_roots=[inside / 'tools'])
    with pytest.raises(ValueError, match='disjoint from the workspace'):
        kit.build_kit(_ws(tmp_path, 'wsJ'), harness, {}, None, code_roots=[tmp_path])


def test_tool_name_shadow_check():
    for name in ('cat', 'sed', 'grep', 'ls', 'sh', 'env', 'python3', 'rg', 'submit'):
        with pytest.raises(ValueError):
            kit.check_tool_name(name)
    for name in ('find_tables', 'show_table', 'printings', 'calc', 'lookup', 'my-tool_2'):
        assert kit.check_tool_name(name) == name and kit.shadows_base_path(name) is None
    assert kit.shadows_base_path('sed') == '/usr/bin/sed'


def test_verify_kit_detects_tampering(tmp_path):
    arm = ARMS['harness-gate']
    for number, tamper in enumerate([
            lambda k: (k / 'bin/submit').chmod(0o755) or (k / 'bin/submit').write_text('#!/bin/sh\necho hijack\n'),
            lambda k: (k / 'bin/rg').unlink() or (k / 'bin/rg').symlink_to('/usr/bin/grep'),
            lambda k: (k / 'bin/extra').write_text('x'),
            lambda k: (k / 'zdot/.zprofile').chmod(0o644),
            lambda k: (k / 'zdot/.zprofile').unlink()]):
        workspace = _ws(tmp_path, f'ws{number}')
        meta = kit.build_kit(workspace, arm, {}, None)
        tamper(workspace / kit.KIT)
        with pytest.raises(ValueError, match='Prepared kit was changed'):
            kit.verify_kit(workspace, meta)
    # A granted code file changed after preparation (tool code edited mid-campaign) is detected too.
    tools = (tmp_path / 'tools').resolve()
    tools.mkdir()
    (tools / 't.py').write_text('print(1)\n')
    workspace = _ws(tmp_path, 'code')
    meta = kit.build_kit(workspace, ARMS['harness-static'], {'t': str(tools / 't.py')}, None, code_roots=[tools])
    (tools / 't.py').write_text('print(2)\n')
    with pytest.raises(ValueError, match='kit code was changed'):
        kit.verify_kit(workspace, meta)
    workspace = _ws(tmp_path, 'swap')
    meta = kit.build_kit(workspace, arm, {}, None)
    (workspace / kit.KIT).rename(workspace / 'elsewhere')
    (workspace / kit.KIT).symlink_to(workspace / 'elsewhere')
    with pytest.raises(ValueError, match='symlink'):
        kit.verify_kit(workspace, meta)


def test_tool_environment(tmp_path):
    workspace = _ws(tmp_path)
    plain, harness, gate = ARMS['plain'], ARMS['harness-static'], ARMS['harness-gate']
    url = 'http://127.0.0.1:9/v1/submit'
    env = kit.tool_environment(workspace, plain, {'OFFICEQA_CORPUS': str(CORPUS)})
    assert env == {'HOME': f'{workspace}/.actor-home', 'TMPDIR': f'{workspace}/.actor-tmp',
                   'TMPPREFIX': f'{workspace}/.actor-tmp/zsh', 'ZDOTDIR': f'{workspace}/.kit/zdot',
                   'PATH': f'{workspace}/.kit/bin:/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8',
                   'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                   'MPLCONFIGDIR': f'{workspace}/.actor-tmp/matplotlib', 'OFFICEQA_CORPUS': str(CORPUS)}
    # a gate-off harness arm gets exactly the plain environment (plus task variables): no HARNESS_SUBMIT_URL
    assert kit.tool_environment(workspace, harness, {'OFFICEQA_CORPUS': str(CORPUS)}) == env
    assert kit.tool_environment(workspace, gate, {SUBMIT_ENV: url})[SUBMIT_ENV] == url
    for arm, extra, message in [(gate, {}, 'required for gate'), (plain, {SUBMIT_ENV: url}, 'required for gate'),
                                (harness, {SUBMIT_ENV: url}, 'required for gate'),
                                (gate, {SUBMIT_ENV: 'http://127.0.0.1:9/v1'}, 'Submit URL'),
                                (plain, {'PATH': '/opt/homebrew/bin'}, 'reserved'), (plain, {'lower': 'x'}, 'reserved'),
                                (plain, {'FIREWORKS_API_KEY': 'x'}, 'Credential'), (plain, {'OFFICEQA_X': 'a\nb'}, 'one-line')]:
        with pytest.raises(ValueError, match=message):
            kit.tool_environment(workspace, arm, extra)
    with pytest.raises(ValueError, match='broker route'):
        acting.actor_env_extra(gate, 'http://127.0.0.1:9/v1', {SUBMIT_ENV: 'http://127.0.0.1:8/v1/submit'})
    assert acting.actor_env_extra(gate, 'http://127.0.0.1:9/v1') == {SUBMIT_ENV: url}
    assert acting.actor_env_extra(harness, 'http://127.0.0.1:9/v1') == {}       # keyed on gate, not controller
    assert acting.actor_env_extra(plain, 'http://127.0.0.1:9/v1') == {}


def test_readme_l0_identical_for_every_arm():
    texts = {kit.readme_l0(arm, CORPUS, INDEX) for arm in ARMS.values()}
    assert len(texts) == 1
    text = texts.pop()
    for needle in ('python3', 'numpy', 'pandas', 'FTS5', '`rg`', "<<'PY'", str(CORPUS), str(INDEX),
                   'Operation not permitted', 'no internet', '.kit/'):
        assert needle.lower() in text.lower(), needle

# ---------------------------------------------------------------------------------------------
# acting.py (no subprocess)
# ---------------------------------------------------------------------------------------------


def test_runtime_roots_are_narrowed():
    roots = acting.runtime_roots()
    # No harness_core/ or packs/ subtree; kit code is granted file by file (client_policy code=).
    assert roots == [str(p.resolve()) for p in (paths.ACTOR_PYTHON_PREFIX, paths.CODEX, paths.ACTOR_RG)]
    for forbidden in (sys.prefix, sys.base_prefix, str(VENV), str(CUA_NODE), 'harnessadapter', 'codex-runtimes'):
        assert not any(forbidden in root for root in roots), forbidden
    with pytest.raises(ValueError, match='does not exist'):
        acting.runtime_roots(data=['/nonexistent/tables.sqlite'])
    assert acting.runtime_roots(data=[str(INDEX)])[-1] == str(INDEX.resolve())


def test_runtime_roots_server_adds_controller_runtime():
    pytest.importorskip('harnessadapter')
    roots = acting.runtime_roots(server=True)
    assert str(Path(sys.prefix).resolve()) in roots and any(root.endswith('harnessadapter') for root in roots)


@pytest.fixture(params=['harness-static', 'harness-gate'])
def prepared(tmp_path, request):
    arm = ARMS[request.param]
    workspace = (tmp_path / 'ws').resolve()
    acting.prepare_actor_workspace(workspace, None, common_instructions='# t\n', arm=arm, marker_extra={'notes': ['n1']})
    return workspace, arm


def test_marker_records_arm_and_kit(prepared):
    workspace, arm = prepared
    marker = json.loads((workspace / acting.MARKER).read_text())
    assert marker['arm'] == {'name': arm.name, 'pack': 'officeqa', 'l1_tools': True, 'controller': True,
                             'seed': 'harness', 'learner': None, 'notes': True, 'gate': arm.gate}
    assert marker['kit']['kit_sha256'] == kit.verify_kit(workspace, marker['kit'])
    assert (marker['kit']['submit'] is not None) == arm.gate
    assert marker['extra'] == {'notes': ['n1']} and marker['contract'] == CONTRACT_VERSION == 'v10.0'


def test_build_settings_v10(prepared, tmp_path):
    workspace, arm = prepared
    endpoint = 'http://127.0.0.1:4242/v1'
    base = acting.build_settings(workspace, tmp_path / 'out', CORPUS, INDEX, endpoint=endpoint, arm=arm)
    # multi-agent off, explicit compaction limit (v8's effective value), permissions text off
    assert acting.V9_SETTINGS == {'agents.enabled': False, 'features.multi_agent': False,
                                  'model_context_window': 1048576, 'model_auto_compact_token_limit': 244800}
    assert {k: base[k] for k in acting.V9_SETTINGS} == acting.V9_SETTINGS
    assert base['include_permissions_instructions'] is False                 # the default
    assert acting.EFFECTIVE_AUTO_COMPACT_OBSERVED == base['model_auto_compact_token_limit']
    assert {k: v['v9'] for k, v in acting.V9_DECLARED_SETTINGS.items()} == {
        k: base[k] for k in acting.V9_DECLARED_SETTINGS}
    env = base['shell_environment_policy.set']
    # HARNESS_SUBMIT_URL iff the gate is on (harness-static: controller on, gate off -> absent)
    assert env.get(SUBMIT_ENV) == ('http://127.0.0.1:4242/v1/submit' if arm.gate else None)
    on = acting.build_settings(workspace, tmp_path / 'out', CORPUS, INDEX, endpoint=endpoint, arm=arm,
                               permissions_instructions=True)
    assert 'include_permissions_instructions' not in on and dict(on, include_permissions_instructions=False) == base
    with pytest.raises(ValueError, match='Arm argument'):
        acting.build_settings(workspace, tmp_path / 'out', CORPUS, INDEX, endpoint=endpoint, arm=ARMS['harness-evolve'])
    with pytest.raises(TypeError):
        acting.build_settings(workspace, tmp_path / 'out', CORPUS, INDEX, endpoint=endpoint, arm=None)
    (workspace / '.kit/zdot/.zprofile').chmod(0o644)
    with pytest.raises(ValueError, match='Prepared kit was changed'):
        acting.build_settings(workspace, tmp_path / 'out', CORPUS, INDEX, endpoint=endpoint, arm=arm)


def test_build_settings_rejects_stale_embedded_submit_url(tmp_path):
    arm = ARMS['harness-gate']
    workspace = (tmp_path / 'ws').resolve()
    acting.prepare_actor_workspace(workspace, None, common_instructions='# t\n', arm=arm,
                                   submit_url='http://127.0.0.1:4000/v1/submit')
    settings = acting.build_settings(workspace, tmp_path / 'out', CORPUS, INDEX, endpoint='http://127.0.0.1:4000/v1', arm=arm)
    assert settings['shell_environment_policy.set'][SUBMIT_ENV] == 'http://127.0.0.1:4000/v1/submit'
    with pytest.raises(ValueError, match='embedded in the kit'):
        acting.build_settings(workspace, tmp_path / 'out', CORPUS, INDEX, endpoint='http://127.0.0.1:4001/v1', arm=arm)


def test_client_policy_protects_kit(prepared):
    workspace, arm = prepared
    marker = json.loads((workspace / acting.MARKER).read_text())
    profile = acting.client_policy(workspace, CORPUS, INDEX, endpoint='http://127.0.0.1:4242/v1', code=marker['kit'])
    read = re.search(r'^\(allow file-read\* file-test-existence file-map-executable (.*)\)$', profile, re.M).group(1)
    code = [p for p in re.findall(r'\((?:subpath|literal) "([^"]+)"\)', read) if p.startswith(str(paths.SOURCE))]
    # never a whole code tree, only the wrapped script; submit.py is granted iff the gate is on
    assert code == ([str(kit.SUBMIT_SCRIPT)] if arm.gate else []), code
    assert 'harness_core/tools/submit.py' not in acting.client_policy(workspace, CORPUS, INDEX,
                                                                       endpoint='http://127.0.0.1:4242/v1')
    with pytest.raises(ValueError, match='outside the workspace'):
        acting.client_policy(workspace, CORPUS, INDEX, endpoint='http://127.0.0.1:4242/v1',
                             code={'code': {'files': {str(workspace / 'AGENTS.md'): ''}, 'dirs': []}})
    deny = re.search(r'^\(deny file-write\* (.*)\)$', profile, re.M).group(1)
    assert f'(subpath "{workspace}/.kit")' in deny and f'(subpath "{workspace}/AGENTS.md")' in deny
    assert '(remote tcp "localhost:4242")' in profile and profile.count('network-outbound') == 1
    assert '(subpath "/private/var/select/sh")' in profile
    for forbidden in (str(VENV), str(CUA_NODE), 'harnessadapter', 'codex-runtimes', str(arms.ADAPTERS)):
        assert forbidden not in profile, forbidden

# ---------------------------------------------------------------------------------------------
# Sandbox probes (tests/probe_kit.sh) and Codex offline config validation
# ---------------------------------------------------------------------------------------------


@needs_sandbox
@pytest.mark.parametrize('arm_name', ['plain', 'prompt-static', 'harness-static', 'harness-gate'])
def test_probe_kit_sh(tmp_path, arm_name):
    scratch = (tmp_path / 'probe').resolve()
    completed = subprocess.run(['/bin/sh', str(PROBE_SH), str(scratch), arm_name], capture_output=True, text=True,
                               timeout=300)
    assert completed.returncode == 0, completed.stderr[-3000:]
    assert completed.stdout.rstrip().endswith(f'=== END {arm_name}'), completed.stdout[-2000:]
    results = parse_probe_output(completed.stdout)
    arm, workspace = ARMS[arm_name], scratch / 'ws'
    ctx = {'arm': arm, 'workspace': workspace, 'path': kit.actor_path(workspace),
           'kit_bin': str(workspace / kit.KIT / 'bin'),
           'submit_url': f'http://127.0.0.1:{PROBE_PORT}/v1/submit' if arm.gate else None,
           'shell_env': '|'.join(f'{workspace}/{p}' for p in ('.actor-tmp/zsh', '.kit/zdot', '.actor-home', '.actor-tmp'))}
    expected = [(name, mode) for name, modes, _, _ in PROBES for mode in modes]
    assert sorted(results) == sorted(expected)
    failures = []
    for name, modes, _, check in PROBES:
        for mode in modes:
            out, rc = results[(name, mode)]
            if check is None:
                continue
            try:
                check(out, rc, ctx)
            except (AssertionError, ValueError, KeyError, IndexError) as exc:
                failures.append(f'{name} -{mode}: {exc!r}\n{out}')
    assert not failures, '\n\n'.join(failures)
    # Side effects: nothing written outside, the kit and AGENTS.md unchanged.
    assert not Path('/private/tmp/probe-kit-outside').exists() and not (scratch / 'probe-kit-outside').exists()
    marker = json.loads((scratch / 'ws' / acting.MARKER).read_text())
    kit.verify_kit(scratch / 'ws', marker['kit'])
    assert acting._sha(scratch / 'ws/AGENTS.md') == marker['agents_sha256']
    assert (scratch / 'ws/note.txt').read_text() == 'ok\n' and not (scratch / 'ws/hardlink').exists()


@needs_sandbox
@pytest.mark.parametrize('sandboxed', [False, True])
def test_validate_config_offline(tmp_path, sandboxed):
    ctx = make_probe(tmp_path / 'probe', 'harness-gate')
    for number, permissions in enumerate((True, False)):
        settings = acting.build_settings(ctx['workspace'], tmp_path / 'out', CORPUS, INDEX,
                                         endpoint=f'http://127.0.0.1:{PROBE_PORT}/v1', arm=ctx['arm'],
                                         data=(str(tmp_path / 'probe/tools'),), permissions_instructions=permissions)
        options = ({'policy': ctx['profile'], 'home': ctx['workspace'] / f'.client-validate-{number}'} if sandboxed else {})
        result = acting.validate_config_offline(settings, **options)
        assert result['passed'] and result['model_calls'] == 0, result
        config = result['response']['result']['config']
        assert config['include_permissions_instructions'] is permissions
        assert config['shell_environment_policy']['set'] == ctx['env'] and config['model_context_window'] == 1048576
        # the declared values are accepted by --strict-config and echoed as set
        assert config['model_auto_compact_token_limit'] == 244800
        assert config['agents']['enabled'] is False and config['features']['multi_agent'] is False

# ---------------------------------------------------------------------------------------------
# Real pinned Codex, inside the profile, against a local fake Responses server ($0, 127.0.0.1 only)
# ---------------------------------------------------------------------------------------------


def _sse(number, items):
    rid = f'resp_{number}'
    usage = {'input_tokens': 10, 'input_tokens_details': {'cached_tokens': 0}, 'output_tokens': 5,
             'output_tokens_details': {'reasoning_tokens': 0}, 'total_tokens': 15}
    events = [{'type': 'response.created', 'response': {'id': rid, 'object': 'response', 'status': 'in_progress',
                                                        'model': acting.MODEL, 'output': []}}]
    for index, item in enumerate(items):
        events += [{'type': 'response.output_item.added', 'output_index': index, 'item': item},
                   {'type': 'response.output_item.done', 'output_index': index, 'item': item}]
    events.append({'type': 'response.completed', 'response': {'id': rid, 'object': 'response', 'status': 'completed',
                                                              'model': acting.MODEL, 'output': items, 'usage': usage}})
    return b''.join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


class FakeResponses:
    """Scripted /v1/responses SSE server on 127.0.0.1; records every request body and path."""
    def __init__(self, scripted):
        self.scripted, self.bodies, self.paths = [list(items) for items in scripted], [], []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_GET(self):
                fake.paths.append(('GET', self.path)); self.send_response(404); self.end_headers()

            def do_POST(self):
                fake.paths.append(('POST', self.path))
                fake.bodies.append(json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0')))))
                items = fake.scripted.pop(0) if fake.scripted else [_message('EXHAUSTED')]
                data = _sse(len(fake.bodies), items)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.endpoint = f'http://127.0.0.1:{self.server.server_port}/v1'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _message(text):
    return {'type': 'message', 'role': 'assistant', 'id': 'msg_1', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}


CODEX_PROBE = ('print -r -- "PATH=$PATH"; whence -p python3 rg submit; ' + IMPORTS + '; '
               'rg -c -i treasury @CORPUS_FILE@; python3 - <<\'PY\'\nprint("heredoc-ok")\nPY\n'
               'print -r -- "SUBMIT=${HARNESS_SUBMIT_URL:-unset}"; env | cut -d= -f1 | sort | tr "\\n" " "')


def run_codex_probe(scratch, arm_name, *, permissions):
    """One real `codex exec` episode through acting.run_actor: exec_command(probe) then a final message."""
    arm = ARMS[arm_name]
    workspace = Path(scratch).resolve() / 'ws'
    acting.prepare_actor_workspace(workspace, None, common_instructions='# Probe workspace\n', arm=arm)
    command = CODEX_PROBE.replace('@CORPUS_FILE@', str(CORPUS_FILE))
    call = {'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1', 'name': 'exec_command', 'status': 'completed',
            'arguments': json.dumps({'cmd': command, 'yield_time_ms': 20000, 'max_output_tokens': 4000})}
    fake = FakeResponses([[call], [_message('FINAL-OK')]])
    try:
        result = acting.run_actor(workspace, Path(scratch).resolve() / 'records', 'Reply FINAL-OK.', CORPUS, INDEX,
                                  timeout=120, endpoint=fake.endpoint, arm=arm, permissions_instructions=permissions)
    finally:
        fake.close()
    output = [item['output'] for item in fake.bodies[1]['input'] if item.get('type') == 'function_call_output']
    return result, fake, output[0] if output else ''


def _content_parts(body):
    return [(item.get('role'), part.get('text', '')) for item in body['input'] for part in item.get('content') or []]


@needs_sandbox
@pytest.mark.live_codex
def test_real_codex_exec_in_sandbox(tmp_path):
    runs = {}
    for label, arm_name, permissions in (('plain', 'plain', True), ('plain-noperm', 'plain', False),
                                         ('harness', 'harness-static', True), ('gate', 'harness-gate', True)):
        result, fake, output = run_codex_probe(tmp_path / label, arm_name, permissions=permissions)
        assert result['status'] == 'completed' and result['final_validation']['valid'], result
        assert result['kit_after']['intact'] and len(fake.bodies) == 2, (result, fake.paths)
        assert fake.paths == [('POST', '/v1/responses')] * 2
        workspace = (tmp_path / label / 'ws').resolve()
        assert f'PATH={kit.actor_path(workspace)}' in output
        assert f'{workspace}/.kit/bin/python3' in output and f'{workspace}/.kit/bin/rg' in output
        assert _json_line(output)['prefix'] == str(paths.ACTOR_PYTHON_PREFIX) and 'heredoc-ok' in output
        assert re.search(r'^\d+$', output, re.M), output                     # rg count line
        gate = ARMS[arm_name].gate
        submit_url = f'{fake.endpoint}/submit' if gate else 'unset'
        assert f'SUBMIT={submit_url}' in output and (f'{workspace}/.kit/bin/submit' in output) == gate
        runs[label] = (fake.bodies[0], output)
    normalise = lambda parts: [(role, text.replace(str((tmp_path / 'plain-noperm').resolve()), '<S>')
                                .replace(str((tmp_path / 'plain').resolve()), '<S>')) for role, text in parts]
    with_text = normalise(_content_parts(runs['plain'][0]))
    without = normalise(_content_parts(runs['plain-noperm'][0]))
    removed = [part for part in with_text if part not in without]
    # include_permissions_instructions=false removes exactly the false '<permissions instructions>' part.
    assert len(removed) == 1 and removed[0][0] == 'developer' and removed[0][1].startswith('<permissions instructions>')
    assert 'No filesystem sandboxing' in removed[0][1] and 'Network access is enabled' in removed[0][1]
    assert [part for part in with_text if part != removed[0]] == without
    rest = lambda body: {k: v for k, v in body.items() if k not in ('input', 'client_metadata', 'prompt_cache_key')}
    assert rest(runs['plain'][0]) == rest(runs['plain-noperm'][0])      # tools, instructions, reasoning, ...
    # multi-agent off: Codex offers no sub-agent tool
    assert [t.get('name') for t in runs['plain'][0]['tools']] == ['exec_command', 'write_stdin', 'request_user_input',
                                                                  'view_image']
    keys = set(runs['plain'][1].rsplit('\n', 1)[-1].split())
    assert {'CODEX_CI', 'CODEX_THREAD_ID', 'TERM', 'PAGER', 'NO_COLOR', 'LC_ALL'} <= keys   # added by Codex itself
    assert not keys & {'OPENAI_API_KEY', 'FIREWORKS_API_KEY', 'VIRTUAL_ENV', 'PYTHONPATH'}


@needs_sandbox
@pytest.mark.live_codex
def test_real_codex_truncation_matches_v8(tmp_path):
    """($0) Under the v10 config the model sees the same ~10,000-char head+tail of a long exec_command output
    as in v8 (max recorded 10,216 bytes), and a model-supplied max_output_tokens does not lift it. Guards
    against config drift (e.g. adding model_catalog_json)."""
    arm = ARMS['plain']
    workspace = (tmp_path / 'ws').resolve()
    acting.prepare_actor_workspace(workspace, None, common_instructions='# t\n', arm=arm)
    command = "python3 -c 'for i in range(1000): print(f\"{i:05d}\" + \"x\" * 54)'"          # 60,000 chars
    calls = [[{'type': 'function_call', 'id': f'fc_{n}', 'call_id': f'call_{n}', 'name': 'exec_command',
               'status': 'completed', 'arguments': json.dumps(args)}]
             for n, args in enumerate(({'cmd': command}, {'cmd': command, 'max_output_tokens': 20000}), 1)]
    fake = FakeResponses([*calls, [_message('DONE')]])
    try:
        result = acting.run_actor(workspace, tmp_path / 'records', 'Reply DONE.', CORPUS, INDEX, timeout=120,
                                  endpoint=fake.endpoint, arm=arm)
    finally:
        fake.close()
    assert result['status'] == 'completed' and len(fake.bodies) == 3, result
    for body in fake.bodies[1:]:
        output = [item['output'] for item in body['input'] if item.get('type') == 'function_call_output'][-1]
        assert '…50000 chars truncated…' in output and 10_000 <= len(output.encode()) <= 10_300, len(output)

# ---------------------------------------------------------------------------------------------
# v8 -> v10 differences (argv, profile, env) must equal the documented intentional set
# ---------------------------------------------------------------------------------------------

DIFF_ENV_ADDED = {'TMPPREFIX', 'ZDOTDIR'}                         # + HARNESS_SUBMIT_URL on gate arms
DIFF_ENV_CHANGED = {'PATH'}
# acting.V9_DECLARED_SETTINGS (kept in v10): multi-agent off, explicit 244,800 compaction limit;
# + include_permissions_instructions=false (added; the default, absent only with permissions_instructions=True)
DIFF_SETTINGS = {'agents.enabled', 'features.multi_agent', 'model_auto_compact_token_limit'}
DIFF_SETTING_VALUES = {'agents.enabled': ('true', 'false'), 'features.multi_agent': ('true', 'false'),
                       'model_auto_compact_token_limit': ('800000', '244800')}
DIFF_READ_REMOVED = {'<VENV>', '<CODEX_RUNTIME_PYTHON>', '<HARNESSADAPTER>', '<CUA_NODE>'}
DIFF_READ_ADDED = {'<FROZEN_PYTHON>', '<FROZEN_RG>', '/private/var/select/sh'}  # + kit code files (below)
DIFF_READ_ADDED_GATE = {'<HARNESS_CORE>/tools/submit.py'}                       # the submit wrapper's script
DIFF_PROTECTED_ADDED = {'.kit'}
DIFF_HIDDEN_ADDED = {'.acting-prepared.json'}                     # content unreadable


def _norm(text, mapping):
    for old, new in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(old, new)
    return text


def _profile_parts(profile, mapping):
    read = re.search(r'^\(allow file-read\* file-test-existence file-map-executable (.*)\)$', profile, re.M).group(1)
    protected = re.search(r'^\(deny file-write\* (.*)\)$', profile, re.M).group(1)
    static = [line for line in profile.splitlines() if not line.startswith(
        ('(allow file-read* file-test-existence file-map-executable', '(deny file-write*', '(deny file-read-data',
         '(allow file-read-metadata file-test-existence', '(allow network-outbound'))]
    return {'read': {_norm(p, mapping) for p in re.findall(r'\((?:subpath|literal) "([^"]+)"\)', read)},
            'protected': {Path(p).name for p in re.findall(r'\(subpath "([^"]+)"\)', protected)},
            'hidden': {Path(p).name for p in re.findall(r'\(literal "([^"]+)"\)', '\n'.join(
                line for line in profile.splitlines() if line.startswith('(deny file-read-data')))},
            'static': [_norm(line, mapping) for line in static],
            'network': _norm(re.search(r'^\(allow network-outbound.*$', profile, re.M).group(0), mapping)}


def _settings_from_argv(argv):
    codex = argv[argv.index('-p') + 2:]
    settings = {codex[i + 1].split('=', 1)[0]: codex[i + 1].split('=', 1)[1]
                for i in range(len(codex)) if codex[i] == '-c'}
    flags = [arg for i, arg in enumerate(codex) if arg != '-c' and (i == 0 or codex[i - 1] != '-c')]
    return settings, flags


def v8_v10_diff(scratch, arm_name='harness-static', permissions=False):
    """Compare the recorded v8 invocation (none root, Q001) with a v10 one built the way run_actor builds it."""
    v8 = json.loads(V8_INVOCATION.read_text())
    arm = ARMS[arm_name]
    workspace = Path(scratch).resolve() / 'ws'
    acting.prepare_actor_workspace(workspace, None, common_instructions='# diff\n', arm=arm)
    port = int(re.search(r'127\.0\.0\.1:(\d+)/v1', json.dumps(v8['settings'])).group(1))
    endpoint = f'http://127.0.0.1:{port}/v1'
    settings = acting.build_settings(workspace, Path(scratch).resolve() / 'records', CORPUS, INDEX, endpoint=endpoint,
                                     arm=arm, permissions_instructions=permissions)
    marker = json.loads((workspace / acting.MARKER).read_text())
    argv = acting.client_command(acting.command_for(workspace, None, settings), workspace, None, CORPUS, INDEX,
                                 endpoint=endpoint, code=marker['kit'])
    common = {str(CUA_NODE): '<CUA_NODE>', str(paths.CODEX): '<CODEX>', str(paths.ACTOR_RG): '<FROZEN_RG>',
              str(paths.HARNESS_CORE): '<HARNESS_CORE>', str(paths.PACKS): '<PACKS>',
              str(paths.ACTOR_PYTHON_PREFIX): '<FROZEN_PYTHON>', str(kit.SOURCE_PYTHON_PREFIX): '<CODEX_RUNTIME_PYTHON>',
              str(VENV): '<VENV>', str(HARNESSADAPTER_V8): '<HARNESSADAPTER>'}
    v8_map = {**common, v8['cwd']: '<WS>', v8['corpus']: '<CORPUS>', v8['index']: '<INDEX>'}
    v10_map = {**common, str(workspace): '<WS>', str(CORPUS.resolve()): '<CORPUS>', str(INDEX.resolve()): '<INDEX>'}
    s8, f8 = _settings_from_argv([_norm(a, v8_map) for a in v8['argv']])
    s10, f10 = _settings_from_argv([_norm(a, v10_map) for a in argv])
    env8 = {k: _norm(v, v8_map) for k, v in v8['settings']['shell_environment_policy.set'].items()}
    env10 = {k: _norm(v, v10_map) for k, v in settings['shell_environment_policy.set'].items()}
    p8, p10 = _profile_parts(v8['argv'][2], v8_map), _profile_parts(argv[2], v10_map)
    env_key = 'shell_environment_policy.set'
    return {
        'flags_equal': f8 == f10, 'flags': f10,
        'settings_changed': {k for k in s8.keys() & s10.keys() if s8[k] != s10[k]} - {env_key},
        'setting_values': {k: (s8[k], s10[k]) for k in s8.keys() & s10.keys() if s8[k] != s10[k] and k != env_key},
        'settings_added': set(s10) - set(s8), 'settings_removed': set(s8) - set(s10),
        'env_added': set(env10) - set(env8), 'env_removed': set(env8) - set(env10),
        'env_changed': {k for k in env8.keys() & env10.keys() if env8[k] != env10[k]},
        'env': (env8, env10),
        'read_added': p10['read'] - p8['read'], 'read_removed': p8['read'] - p10['read'],
        'protected_added': p10['protected'] - p8['protected'], 'protected_removed': p8['protected'] - p10['protected'],
        'hidden_added': p10['hidden'] - p8['hidden'],
        'static_equal': p8['static'] == p10['static'], 'network_equal': p8['network'] == p10['network'],
    }


@pytest.mark.skipif(not V8_INVOCATION.is_file(), reason='needs the recorded v8 invocation (read-only reference)')
@pytest.mark.parametrize('arm_name, permissions', [('plain', False), ('harness-static', False), ('harness-gate', False),
                                                   ('harness-gate', True)])
def test_v8_v10_differences_are_the_documented_ones(tmp_path, arm_name, permissions):
    diff = v8_v10_diff(tmp_path, arm_name, permissions)
    gate = ARMS[arm_name].gate
    assert diff['flags_equal'] and diff['static_equal'] and diff['network_equal'], diff
    assert diff['settings_changed'] == DIFF_SETTINGS and not diff['settings_removed'], diff
    assert diff['setting_values'] == DIFF_SETTING_VALUES, diff
    assert diff['settings_added'] == (set() if permissions else {'include_permissions_instructions'}), diff
    # the recorded declaration (invocation.json v9_declared_settings) names exactly these settings
    assert set(acting.V9_DECLARED_SETTINGS) == DIFF_SETTINGS | {'include_permissions_instructions'}
    assert diff['env_added'] == DIFF_ENV_ADDED | ({SUBMIT_ENV} if gate else set()), diff
    assert diff['env_changed'] == DIFF_ENV_CHANGED and not diff['env_removed'], diff
    assert diff['env'][0]['PATH'] == '<VENV>/bin:<CUA_NODE>/bin:/usr/bin:/bin:/usr/sbin:/sbin'
    assert diff['env'][1]['PATH'] == '<WS>/.kit/bin:/usr/bin:/bin:/usr/sbin:/sbin'
    assert diff['read_removed'] == DIFF_READ_REMOVED, diff
    assert diff['read_added'] == DIFF_READ_ADDED | (DIFF_READ_ADDED_GATE if gate else set()), diff
    assert diff['protected_added'] == DIFF_PROTECTED_ADDED and not diff['protected_removed'], diff
    assert diff['hidden_added'] == DIFF_HIDDEN_ADDED, diff


def _print_diff():
    import tempfile
    with tempfile.TemporaryDirectory(prefix='v8v10-') as directory:
        diff = v8_v10_diff(directory, 'harness-gate', False)
    env8, env10 = diff.pop('env')
    print('| item | v8 (recorded none/001) | v10 (harness-gate, permissions off) |\n|---|---|---|')
    for key in sorted(set(env8) | set(env10)):
        if env8.get(key) != env10.get(key):
            print(f'| env {key} | `{env8.get(key, "-")}` | `{env10.get(key, "-")}` |')
    for key, value in diff.items():
        print(f'| {key} | | `{sorted(value) if isinstance(value, set) else value}` |')


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == 'make':
        make_probe(sys.argv[2], sys.argv[3])
    elif sys.argv[1:] == ['diff']:
        _print_diff()
    else:
        raise SystemExit('usage: test_v10_kit.py make SCRATCH ARM | diff')
