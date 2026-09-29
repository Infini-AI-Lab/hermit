"""L0 environment kit (v9): the per-episode, write-protected `<workspace>/.harness/` directory.

Every arm gets the same L0 (DESIGN.md §2 L0; understand/acting.md §3). The kit holds:
- bin/python, bin/python3: symlinks to the frozen actor python (frameworks-v9/actor-python, a hashed
  copy of the codex runtime python 3.12 with numpy, pandas and sqlite3 FTS5);
- bin/rg: a symlink to the frozen ripgrep copy (frameworks-v9/bin/rg);
- one POSIX sh wrapper per L1 tool: `exec <frozen python> -I -B <script> "$@"`, and `submit`
  (harness_core/tools/submit.py) for controller arms only;
- zdot/.zprofile: zsh reads it (via ZDOTDIR) after /etc/zprofile ran path_helper, which had pushed the
  harness entries behind /usr/local/bin and /opt/homebrew/bin; it restores the kit PATH.

tool_environment() is the actor shell environment (Codex shell_environment_policy.set) and readme_l0()
the arm-independent README lines. The Seatbelt profile (acting.client_policy) denies writes to `.harness`.

Code the actor may read (review fix, S1-env-review.md D1): only the files the kit's wrappers execute —
each script plus the sibling modules it imports (static `ast` closure) — and, for a script that imports
siblings, a listing of its directory. build_kit records them with sha256 under meta['code'];
acting.client_policy grants exactly those paths (never the whole of packs/ or harness_core/, so seed
files, the rule engine and other packs stay unreadable, and an arm without wrappers reads no code).
The frozen runtime itself is built and checked here too (freeze_runtime / verify_frozen, CLI at the end).
Importing this module never calls a model, never touches the network and never reads a pack.
"""
from __future__ import annotations

import ast
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
from typing import Mapping, Sequence
from urllib.parse import urlparse

from harness_core.contracts import ArmSpec, SUBMIT_ENV, SUBMIT_PATH
from paths import (ACTOR_PYTHON, ACTOR_PYTHON_MANIFEST, ACTOR_PYTHON_PREFIX, ACTOR_RG,
                   ACTOR_RG_SHA256_FILE, HARNESS_CORE, PACKS)

KIT = '.harness'
BASE_PATH = ('/usr/bin', '/bin', '/usr/sbin', '/sbin')
SHIMS = ('python', 'python3', 'rg')
SUBMIT = 'submit'
SUBMIT_SCRIPT = HARNESS_CORE / 'tools/submit.py'
KIT_FILE_MAX = 1 << 20
_TOOL_NAME = re.compile(r'[a-z][a-z0-9_-]{0,31}\Z')
_ENV_NAME = re.compile(r'[A-Z][A-Z0-9_]{0,63}\Z')
_FIXED_ARG = re.compile(r'[A-Za-z0-9_.:=-]{1,64}\Z')
# Paths are embedded in sh wrappers, zsh startup files, PATH and Seatbelt profile text: no quotes, spaces,
# newlines, and no ':' (a ':' in the workspace path would split the kit PATH entry).
_SAFE_PATH = re.compile(r'/[A-Za-z0-9_./@+,=-]*\Z')
# Credentials never enter the actor environment (the controller's own env is never inherited either).
_SECRET_WORDS = ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'PASSWD', 'AUTH', 'CREDENTIAL', 'COOKIE', 'SESSION')
SOURCE_PYTHON_PREFIX = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/python'
SOURCE_RG = Path('/Applications/ChatGPT.app/Contents/Resources/rg')


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha_file(path, *, limit=None):
    """sha256 of a regular file, opened without following a final symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'Not a regular file: {path}')
        if limit is not None and info.st_size > limit:
            raise ValueError(f'File too large: {path}')
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1 << 20):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _safe_path(path, what):
    text = str(path)
    if not _SAFE_PATH.match(text):
        raise ValueError(f'{what} path has characters unsafe for sh/Seatbelt text: {text!r}')
    return text


def _canonical_dir(workspace):
    workspace = Path(workspace).absolute()
    if workspace.is_symlink() or workspace.resolve() != workspace or not workspace.is_dir():
        raise ValueError('Workspace must be an existing directory with a canonical, non-symlink path')
    return workspace


def _check_arm(arm):
    if not isinstance(arm, ArmSpec):
        raise TypeError('arm must be a harness_core.contracts.ArmSpec')
    arm.validate()
    return arm


def kit_dir(workspace):
    return Path(workspace).absolute() / KIT


def actor_path(workspace):
    """The actor PATH: kit first, then only the system directories (no venv, Homebrew or cua_node)."""
    return ':'.join([str(kit_dir(workspace) / 'bin'), *BASE_PATH])


def submit_url_for(endpoint):
    """The broker's submit route for an actor endpoint http://127.0.0.1:PORT/v1."""
    parsed = urlparse(endpoint)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port or parsed.path.rstrip('/') != '/v1':
        raise ValueError('Endpoint must be the local broker http://127.0.0.1:PORT/v1')
    return f'http://127.0.0.1:{parsed.port}{SUBMIT_PATH}'


def validate_submit_url(url):
    parsed = urlparse(url)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port or parsed.path != SUBMIT_PATH
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError(f'Submit URL must be http://127.0.0.1:PORT{SUBMIT_PATH}')
    return url


def _wrapper(python, script, submit_url=None, args=()):
    lines = ['#!/bin/sh', '# harness kit wrapper (generated at preparation; write-protected by the OS sandbox)']
    if submit_url is not None:
        lines.append(f'{SUBMIT_ENV}={shlex.quote(submit_url)}; export {SUBMIT_ENV}')
    command = ' '.join(shlex.quote(str(part)) for part in (python, '-I', '-B', script, *args))
    lines.append(f'exec {command} "$@"')
    return '\n'.join(lines) + '\n'


def _import_closure(script):
    """The script plus the sibling modules it imports, transitively (static `ast` scan, every import
    statement anywhere in the file). Kit tools run with `python -I`, so only a sibling import the script
    arranges itself (cli.py inserts its own directory) can resolve outside the frozen stdlib; other
    names (stdlib, harness_core.* fallbacks) have no sibling file and are ignored. A sibling *package*
    is refused: the kit grants files, not trees."""
    base, seen, todo = script.parent, [], [script]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.append(path)
        for node in ast.walk(ast.parse(path.read_bytes(), filename=str(path))):
            if isinstance(node, ast.Import):
                names = [alias.name.split('.')[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module.split('.')[0]] if node.module else [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                if (base / name).is_dir() and not (base / name).is_symlink():
                    raise ValueError(f'Kit tool imports a local package (not granted): {base / name}')
                candidate = base / f'{name}.py'
                if candidate.is_file() and not candidate.is_symlink():
                    todo.append(candidate)
    return sorted(seen)


def _code_hashes(files):
    return {str(path): _sha_file(path) for path in sorted(map(str, files))}


def code_grants(meta):
    """(files, dirs) the actor may read for this kit: exactly meta['code'] (see module docstring)."""
    code = meta.get('code') if isinstance(meta, dict) else None
    if not isinstance(code, dict) or not isinstance(code.get('files'), dict) or not isinstance(code.get('dirs'), list):
        raise ValueError('Kit metadata has no code grants')
    files, dirs = sorted(code['files']), sorted(code['dirs'])
    for path in (*files, *dirs):
        _safe_path(path, 'Code grant')
    return files, dirs


def kit_hashes(workspace):
    """Every entry under <ws>/.harness: regular files by sha256+mode, symlinks by target (+ target sha)."""
    root = kit_dir(workspace)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Kit directory is missing or a symlink')
    entries = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                resolved = path.parent / target
                direct = resolved.is_file() and not resolved.is_symlink()     # kit links point at files
                entries[relative] = {'type': 'symlink', 'target': target,
                                     'target_sha256': _sha_file(resolved) if direct else None}
            elif stat.S_ISDIR(info.st_mode):
                entries[relative] = {'type': 'dir'}
            elif stat.S_ISREG(info.st_mode):
                entries[relative] = {'type': 'file', 'mode': oct(stat.S_IMODE(info.st_mode)),
                                     'sha256': _sha_file(path, limit=KIT_FILE_MAX)}
            else:
                raise ValueError(f'Unexpected kit entry type: {relative}')
    return entries


def _digest(entries):
    return _sha_bytes(json.dumps(entries, sort_keys=True).encode())


def build_kit(workspace, arm: ArmSpec, tool_wrappers: Mapping[str, str | Sequence[str]], submit_url: str | None, *,
              python=None, rg=None, code_roots=None) -> dict:
    """Create <ws>/.harness/{bin,zdot} and return its metadata (sha256 of every kit file).

    tool_wrappers maps a CLI name to the script it runs, or to [script, *fixed leading args] for a
    multi-command CLI (packs/officeqa/tools/cli.py takes the tool name first). Each script must be a regular
    file inside one of `code_roots` (default: harness_core/ and packs/; disjoint from the workspace).
    `submit` is added for controller arms only;
    submit_url, when given, is embedded in that wrapper (otherwise it relies on HARNESS_SUBMIT_URL in the env).
    meta['code'] lists the only code the actor may read (scripts + sibling-import closure, sha256 each)
    and the directories it may list; acting.client_policy grants exactly these.
    """
    workspace = _canonical_dir(workspace)
    arm = _check_arm(arm)
    python = Path(python or ACTOR_PYTHON).absolute()
    rg = Path(rg or ACTOR_RG).absolute()
    roots = [Path(p).resolve() for p in (code_roots if code_roots is not None else (HARNESS_CORE, PACKS))]
    for path, what in ((workspace, 'Workspace'), (python, 'Python'), (rg, 'rg'), *[(r, 'Code root') for r in roots]):
        _safe_path(path, what)
    for r in roots:
        # A code root in (or above) the actor workspace would grant actor-writable or whole-tree reads.
        if r == workspace or r in workspace.parents or workspace in r.parents:
            raise ValueError(f'Code root must be disjoint from the workspace: {r}')
    for path, what in ((python, 'Frozen actor python'), (rg, 'Frozen rg')):
        if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f'{what} must be an executable regular file (not a symlink): {path}')
    root = kit_dir(workspace)
    if root.exists() or root.is_symlink():
        raise ValueError('Kit already exists; a workspace is prepared once')
    tool_wrappers = dict(tool_wrappers or {})
    if tool_wrappers and not arm.l1_tools:
        raise ValueError(f'Arm {arm.name!r} has no L1 tools; no tool wrappers allowed')
    scripts, fixed = {}, {}
    for name, spec in sorted(tool_wrappers.items()):
        if not isinstance(name, str) or not _TOOL_NAME.match(name) or name in (*SHIMS, SUBMIT):
            raise ValueError(f'Invalid or reserved kit tool name: {name!r}')
        # A script path, or [script, *fixed leading args] for multi-command CLIs (e.g. [cli.py, 'show_table']).
        script, *args = [spec] if isinstance(spec, (str, os.PathLike)) else list(spec)
        script = Path(script)
        if not script.is_absolute() or script.is_symlink() or script.resolve() != script or not script.is_file():
            raise ValueError(f'Tool script must be an existing canonical regular file: {script}')
        if not any(r == script or r in script.parents for r in roots):
            raise ValueError(f'Tool script is outside the read-only code roots: {script}')
        if not all(isinstance(arg, str) and _FIXED_ARG.match(arg) for arg in args):
            raise ValueError(f'Fixed tool arguments must be plain tokens: {args!r}')
        scripts[name], fixed[name] = Path(_safe_path(script, 'Tool script')), args
    if submit_url is not None:
        if not arm.controller:
            raise ValueError('Only controller arms get a submit wrapper / submit URL')
        validate_submit_url(submit_url)
    if arm.controller:
        scripts[SUBMIT] = Path(_safe_path(SUBMIT_SCRIPT, 'Submit script'))
    code_files, code_dirs = set(), set()
    for script in scripts.values():
        if not script.is_file():          # only submit.py (S4) may be absent; its wrapper then fails loudly
            continue
        closure = _import_closure(script)
        code_files.update(closure)
        if len(closure) > 1:              # the script puts its own directory on sys.path: it must be listable
            code_dirs.add(script.parent)
    for path in (*code_files, *code_dirs):
        _safe_path(path, 'Code grant')
    code = {'files': _code_hashes(code_files), 'dirs': sorted(map(str, code_dirs))}
    bin_dir, zdot = root / 'bin', root / 'zdot'
    bin_dir.mkdir(parents=True)
    zdot.mkdir()
    for name in ('python', 'python3'):
        (bin_dir / name).symlink_to(python)
    (bin_dir / 'rg').symlink_to(rg)
    for name, script in scripts.items():
        wrapper = bin_dir / name
        wrapper.write_text(_wrapper(python, script, submit_url if name == SUBMIT else None, fixed.get(name, ())))
        wrapper.chmod(0o555)
    profile = zdot / '.zprofile'
    profile.write_text('# harness kit: /etc/zprofile ran path_helper first, which put the system and Homebrew\n'
                       '# directories ahead of the kit. Restore the harness PATH (generated; write-protected).\n'
                       f'export PATH={shlex.quote(actor_path(workspace))}\n')
    profile.chmod(0o444)
    files = kit_hashes(workspace)
    return {'kit': KIT, 'kit_dir': str(root), 'path': actor_path(workspace), 'arm': asdict(arm),
            'python': str(python), 'rg': str(rg), 'code_roots': [str(r) for r in roots],
            'tools': {name: {'script': str(script), 'args': fixed.get(name, []),
                             'script_sha256': _sha_file(script) if script.is_file() else None}
                      for name, script in scripts.items()},
            'submit': ({'script': str(SUBMIT_SCRIPT), 'present': SUBMIT_SCRIPT.is_file(),
                        'url_embedded': submit_url is not None, 'url': submit_url} if arm.controller else None),
            'code': code, 'files': files, 'kit_sha256': _digest(files)}


def verify_kit(workspace, meta) -> str:
    """Raise if any kit entry, or any granted code file (meta['code']), differs from the prepared
    metadata; return the kit digest. Code files live outside the workspace (controller-owned)."""
    current = kit_hashes(workspace)
    if current != meta.get('files') or _digest(current) != meta.get('kit_sha256'):
        changed = sorted(k for k in set(current) | set(meta.get('files') or {})
                         if current.get(k) != (meta.get('files') or {}).get(k))
        raise ValueError(f'Prepared kit was changed: {changed[:8]}')
    files, _ = code_grants(meta)
    try:
        code_now = _code_hashes(files)
    except (OSError, ValueError) as exc:
        raise ValueError(f'Prepared kit code is unreadable: {exc}') from exc
    if code_now != meta['code']['files']:
        changed = sorted(k for k in code_now if code_now[k] != meta['code']['files'].get(k))
        raise ValueError(f'Prepared kit code was changed: {changed[:8]}')
    return meta['kit_sha256']


def tool_environment(workspace, arm: ArmSpec, extra: Mapping[str, str] | None = None) -> dict:
    """The complete actor shell environment (Codex inherits nothing: shell_environment_policy.inherit=none).

    v8 keys plus TMPPREFIX (zsh writes here-documents to ${TMPPREFIX}XXXXXX and ignores TMPDIR; the default
    /tmp/zsh is denied), ZDOTDIR (the kit's .zprofile) and a PATH without the controller venv or cua_node.
    `extra` may add task variables (e.g. OFFICEQA_CORPUS); HARNESS_SUBMIT_URL is required for controller
    arms and refused for the others. Base keys cannot be overridden and credential-like names are refused.
    """
    arm = _check_arm(arm)
    workspace = Path(workspace).resolve()
    env = {'HOME': str(workspace / '.actor-home'), 'TMPDIR': str(workspace / '.actor-tmp'),
           'TMPPREFIX': str(workspace / '.actor-tmp/zsh'), 'ZDOTDIR': str(workspace / KIT / 'zdot'),
           'PATH': actor_path(workspace), 'LANG': 'en_US.UTF-8',
           'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
           'MPLCONFIGDIR': str(workspace / '.actor-tmp/matplotlib')}
    extra = dict(extra or {})
    for key, value in extra.items():
        if not isinstance(key, str) or not _ENV_NAME.match(key) or key in env:
            raise ValueError(f'Invalid or reserved actor environment name: {key!r}')
        if any(word in key for word in _SECRET_WORDS):
            raise ValueError(f'Credential-like names never enter the actor environment: {key}')
        if not isinstance(value, str) or '\0' in value or '\n' in value:
            raise ValueError(f'Actor environment value must be a one-line string: {key}')
    if arm.controller != (SUBMIT_ENV in extra):
        raise ValueError(f'{SUBMIT_ENV} is required for controller arms and only for them')
    if SUBMIT_ENV in extra:
        validate_submit_url(extra[SUBMIT_ENV])
    env.update(extra)
    return env


def readme_l0(arm: ArmSpec, corpus, index) -> str:
    """L0 README lines, identical for every arm (pack/tool usage text is appended by orchestration)."""
    _check_arm(arm)
    return f'''# Environment

- Shell commands run in this workspace. `python3` (also `python`) is Python 3.12 with numpy, pandas and
  sqlite3 (FTS5); `rg` is ripgrep. Both are on PATH; nothing else needs installing.
- Here-documents work, for example:
  python3 - <<'PY'
  print("hello")
  PY
- The text corpus is read-only at:
  {corpus}
- Its SQLite FTS5 index is read-only at:
  {index}
- Only this workspace accepts writes. `.harness/` (the tool kit), AGENTS.md and README.md are read-only;
  `.client-home/` belongs to the agent client and is not part of the task.
- Every other path is denied by the operating system (`Operation not permitted`): your home directory,
  other tasks, /tmp, /opt/homebrew and the rest of the disk. There is no internet access. Do not search
  the disk; everything you may use is listed here.
'''

# ---------------------------------------------------------------------------------------------
# Frozen runtime (frameworks-v9): copy once, hash by manifest, make read-only, verify per episode.
# ---------------------------------------------------------------------------------------------

_VERSION_PROBE = ('import json, sys, sqlite3, numpy, pandas\n'
                  'c = sqlite3.connect(":memory:"); c.execute("create virtual table t using fts5(x)")\n'
                  'print(json.dumps({"python": sys.version.split()[0], "numpy": numpy.__version__,'
                  ' "pandas": pandas.__version__, "sqlite": sqlite3.sqlite_version, "fts5": True,'
                  ' "prefix": sys.prefix}))\n')


def runtime_versions(python=None):
    """Run the frozen python (isolated, empty env, no bytecode) and report its versions."""
    python = Path(python or ACTOR_PYTHON)
    completed = subprocess.run([str(python), '-I', '-B', '-c', _VERSION_PROBE], env={'LANG': 'en_US.UTF-8'},
                               capture_output=True, text=True, timeout=60, cwd='/')
    if completed.returncode:
        raise RuntimeError('Frozen python failed its version probe: ' + completed.stderr[-2000:])
    return json.loads(completed.stdout)


def tree_manifest(prefix, *, exclude=('manifest.json',)):
    """sha256 per regular file and the target of every symlink under prefix (symlinks never followed)."""
    prefix = Path(prefix)
    files, links, total = {}, {}, 0
    for directory, dirnames, filenames in os.walk(prefix, followlinks=False):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            path = Path(directory) / name
            relative = path.relative_to(prefix).as_posix()
            if relative in exclude:
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                links[relative] = os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                files[relative] = _sha_file(path)
                total += info.st_size
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError(f'Unexpected entry in frozen runtime: {relative}')
    tree = _sha_bytes(json.dumps({'files': files, 'symlinks': links}, sort_keys=True).encode())
    return {'files': files, 'symlinks': links, 'file_count': len(files), 'total_bytes': total, 'tree_sha256': tree}


def _make_read_only(root):
    for directory, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        for name in filenames:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.lstat().st_mode) & ~0o222)
        Path(directory).chmod(stat.S_IMODE(Path(directory).lstat().st_mode) & ~0o222)


def freeze_runtime(prefix=None, *, source=None, rg=None, rg_source=None):
    """Copy the codex runtime python and ChatGPT.app rg once, write manifest.json + rg.sha256, then
    remove write permission (so no interpreter can add __pycache__ files that break the manifest)."""
    prefix, source = Path(prefix or ACTOR_PYTHON_PREFIX), Path(source or SOURCE_PYTHON_PREFIX)
    rg, rg_source = Path(rg or ACTOR_RG), Path(rg_source or SOURCE_RG)
    if (prefix / 'manifest.json').exists():
        raise ValueError('Frozen runtime already has a manifest; verify it instead')
    if not prefix.exists():
        shutil.copytree(source, prefix, symlinks=True)
    if not rg.exists():
        rg.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rg_source, rg)
    versions = runtime_versions(prefix / 'bin/python3.12')
    if Path(versions.pop('prefix')).resolve() != prefix.resolve():
        raise RuntimeError('Frozen python reports a different prefix (not relocatable)')
    tree = tree_manifest(prefix)
    manifest = {'kind': 'actor-python', 'source': str(source), 'prefix': str(prefix), 'created_utc': _utc(),
                'versions': versions, 'python_sha256': tree['files']['bin/python3.12'], **tree}
    (prefix / 'manifest.json').write_text(json.dumps(manifest, indent=1, sort_keys=True) + '\n')
    rg_sha = _sha_file(rg)
    rg.with_name(rg.name + '.sha256').write_text(f'{rg_sha}  {rg.name}  (copied from {rg_source})\n')
    _make_read_only(prefix)
    for path in (rg, rg.with_name(rg.name + '.sha256')):
        path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    return verify_frozen(prefix=prefix, rg=rg, deep=False)


def verify_frozen(*, prefix=None, rg=None, deep=False):
    """Check the frozen python binary and rg against their recorded sha256 (deep: every file, the tree)
    and return the facts orchestration records in freeze.json / invocation.json. Raises on mismatch."""
    prefix, rg = Path(prefix or ACTOR_PYTHON_PREFIX), Path(rg or ACTOR_RG)
    manifest_path = prefix / 'manifest.json'
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    python_sha = _sha_file(prefix / 'bin/python3.12')
    if python_sha != manifest['python_sha256'] or manifest['files'].get('bin/python3.12') != python_sha:
        raise ValueError('Frozen actor python binary differs from its manifest')
    recorded_rg = rg.with_name(rg.name + '.sha256').read_text().split()[0]
    rg_sha = _sha_file(rg)
    if rg_sha != recorded_rg:
        raise ValueError('Frozen rg differs from its recorded sha256')
    if deep:
        tree = tree_manifest(prefix)
        if tree['tree_sha256'] != manifest['tree_sha256']:
            changed = sorted(k for k in set(tree['files']) | set(manifest['files'])
                             if tree['files'].get(k) != manifest['files'].get(k))
            raise ValueError(f'Frozen actor python tree differs from its manifest: {changed[:8]}')
    return {'actor_python_prefix': str(prefix), 'manifest_sha256': _sha_bytes(manifest_bytes),
            'tree_sha256': manifest['tree_sha256'], 'python_sha256': python_sha, 'versions': manifest['versions'],
            'file_count': manifest['file_count'], 'rg': str(rg), 'rg_sha256': rg_sha, 'deep': deep}


if __name__ == '__main__':
    import sys
    command = sys.argv[1] if len(sys.argv) > 1 else ''
    if command == 'freeze':
        print(json.dumps(freeze_runtime(), indent=1))
    elif command == 'verify':
        print(json.dumps(verify_frozen(deep='--deep' in sys.argv), indent=1))
    else:
        raise SystemExit('usage: kit.py freeze | verify [--deep]')
