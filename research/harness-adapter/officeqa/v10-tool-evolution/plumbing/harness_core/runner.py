"""Isolated compute runner (contracts.ComputeRunner) for re-executing model-authored calc code.

The trusted engine never runs model code in-process. `SandboxRunner.run(code)` starts one fresh
subprocess per call:
  /usr/bin/sandbox-exec -p <profile> <frozen python> -I -B main.py
with
- profile: deny by default; read (and map) only the python prefix, /usr/lib, /System and any
  `extra_read` roots (e.g. a corpus), plus metadata of their parent directories and the listing of
  "/" (dyld aborts without it); read/write only a
  fresh temp directory; no process-fork (no subprocesses), no network, no mach/IPC services;
- an empty environment and `-I` (isolated mode: no PYTHON* variables, no user site, no cwd on path);
- RLIMIT_CPU `cpu_s` (default 2 s; SIGXCPU), RLIMIT_FSIZE (stdout goes to a regular file,
  so a flood of output stops at `fsize`), RLIMIT_NOFILE 64, no core files — set by a /bin/sh
  `ulimit` wrapper that execs sandbox-exec (safe in the multi-threaded broker; no preexec_fn);
- stdout/stderr go to unlinked temp files outside the work dir and are read back through the held
  descriptors (never re-opened by name: the child could replace them with symlinks);
- a wall-clock timeout (default 5 s) that kills the whole process group;
- at most `output_cap` bytes of stdout returned.
The temp directory is deleted after every call. `last` keeps diagnostics of the latest run
(return code, timing, stderr tail) for records; it never contains environment values.

`ToolRunner` (DESIGN §5) runs an adapter tool script the same way for the learner's replay and `try_tool`:
  <frozen python> -I -B <tools_dir>/<script> <args>
in a profile that reads exactly the script's import closure (the script plus the sibling modules it
imports, `import_closure`: the same grant the actor kit makes, plus the listing of the tools directory when
the closure has more than one file - never the whole tools directory, so a runtime read of USAGE.md or
manifest.json fails in the replay exactly as it fails for the actor), the corpus directory and the tables
index (plus the python prefix and the system libraries). The environment is the actor kit's key set
(`actor_like_env`: HOME, TMPDIR, TMPPREFIX, ZDOTDIR, PATH, LANG, PYTHON* and MPLCONFIGDIR pointing into the
per-call work directory) plus OFFICEQA_CORPUS / TABLES_INDEX, passed through `env -i`, stdin from a pipe, the
same rlimits, a 20 s CPU and wall default, a 16 KB stdout cap and a resident-memory cap (`memory_mb`,
enforced by a watchdog that polls the process group: macOS enforces no RLIMIT_AS/RSS/DATA, so `ulimit -v`
cannot bound memory here). The caller (campaign.tool_runner) runs it from a neutral hash-named copy of the
tools under <root>/runs/code/<sha12>/tools, so `__file__` has the same shape as in the actor. What still
differs from the actor profile: no process-fork/exec (the tool screen refuses process modules for that
reason) and no TCP to the broker port. The replay is a functional screen executed in this non-actor
profile; admission rests on the paired actor re-runs (PREREG states this).
Its constructor refuses a corpus or index root that is inside, equal to or a parent of a campaign's private
trees (`evidence/`, `private-evaluation/`, `prepared/private/`, `runs/`, `evolution/`); only the tools
directory and the temp root may live under `evolution/` or `runs/code/`. The controller never imports the
tool code: it only executes it here.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Mapping, Sequence

SANDBOX_EXEC = '/usr/bin/sandbox-exec'
CODEX_PYTHON = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3'
FROZEN_PYTHON = Path(__file__).resolve().parents[2] / 'frameworks-v9/actor-python/bin/python3'
SYSTEM_READ = ('/usr/lib', '/System')
# Campaign trees a tool process must never be able to read (DESIGN §5 ToolRunner, §6 invariant 7).
PRIVATE_TREES = ('evidence', 'private-evaluation', 'prepared/private', 'runs', 'evolution')
PRIVATE_SEARCH_DEPTH = 3          # how deep below a read root the constructor looks for a private tree
CORPUS_ENV, INDEX_ENV = 'OFFICEQA_CORPUS', 'TABLES_INDEX'
TOOL_SCRIPT = re.compile(r'[a-z][a-z0-9_]{0,31}\.py\Z')
TOOL_CPU_S, TOOL_TIMEOUT_S, TOOL_OUTPUT_CAP = 20, 20.0, 16_384
TOOL_MEMORY_MB = 3072             # resident memory cap per tool run (all processes of its group)
MEMORY_POLL_S = 0.25
# The actor kit's directory name and base PATH (kit.KIT / kit.BASE_PATH; a test pins the equality): the
# ToolRunner environment carries the same keys and shapes so a tool cannot tell the replay from the actor.
KIT_DIR = '.kit'
BASE_PATH = ('/usr/bin', '/bin', '/usr/sbin', '/sbin')


def default_python() -> str:
    """The frozen actor python (frameworks-v9/actor-python) if present, else the codex runtime python."""
    env = os.environ.get('HARNESS_RUNNER_PYTHON')
    for candidate in ([Path(env)] if env else []) + [FROZEN_PYTHON, CODEX_PYTHON]:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError('no python for the compute runner (set HARNESS_RUNNER_PYTHON)')


def _q(path: str) -> str:
    return json.dumps(str(path))


def import_closure(script: str | os.PathLike) -> list[Path]:
    """The script plus the sibling modules it imports, transitively (static `ast` scan, every import
    statement anywhere in the file). Kit tools run with `python -I`, so only a sibling import the script
    arranges itself (cli.py inserts its own directory) can resolve outside the frozen stdlib; other
    names (stdlib, harness_core.* fallbacks) have no sibling file and are ignored. A sibling *package*
    is refused: the kit grants files, not trees. One definition for the actor kit (kit._import_closure)
    and the ToolRunner grants."""
    script = Path(script)
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


def actor_like_env(work: str | os.PathLike, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The actor kit environment keys (kit.tool_environment) with every path pointing into `work`: HOME,
    TMPDIR, TMPPREFIX, ZDOTDIR, PATH (kit bin first, then BASE_PATH), LANG, PYTHONNOUSERSITE,
    PYTHONDONTWRITEBYTECODE, MPLCONFIGDIR, plus `extra` (the task variables). The directories exist."""
    work = str(work)
    env = {'HOME': f'{work}/.actor-home', 'TMPDIR': f'{work}/.actor-tmp', 'TMPPREFIX': f'{work}/.actor-tmp/zsh',
           'ZDOTDIR': f'{work}/{KIT_DIR}/zdot', 'PATH': ':'.join([f'{work}/{KIT_DIR}/bin', *BASE_PATH]),
           'LANG': 'en_US.UTF-8', 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
           'MPLCONFIGDIR': f'{work}/.actor-tmp/matplotlib'}
    for key in ('HOME', 'TMPDIR', 'ZDOTDIR', 'MPLCONFIGDIR'):
        os.makedirs(env[key], exist_ok=True)
    os.makedirs(f'{work}/{KIT_DIR}/bin', exist_ok=True)
    for key, value in (extra or {}).items():
        if key in env:
            raise ValueError(f'reserved tool environment name: {key}')
        env[key] = value
    return env


def sandbox_profile(read_roots: Sequence[str], work: str, executables: Sequence[str], *,
                    literal_files: Sequence[str] = (), literal_dirs: Sequence[str] = ()) -> str:
    reads = ' '.join(f'(subpath {_q(r)})' for r in read_roots)
    literals = ' '.join(f'(literal {_q(p)})' for p in (*literal_files, *literal_dirs))
    parents = sorted({str(p) for r in [*read_roots, work, *literal_files, *literal_dirs] for p in Path(r).parents})
    meta = ' '.join(f'(literal {_q(p)})' for p in parents)
    execs = ' '.join(f'(literal {_q(e)})' for e in executables)
    return f"""(version 1)
(deny default)
(allow process-exec {execs})
(allow signal (target self))
(allow sysctl-read)
(allow file-read* file-test-existence file-map-executable {reads} {literals})
(allow file-read-metadata file-test-existence {meta})
(allow file-read* file-test-existence (literal "/"))
(allow file-read* file-write* file-test-existence (subpath {_q(work)}))
(allow file-read* (literal "/dev/urandom") (literal "/dev/random"))
(allow file-read* file-write* (literal "/dev/null"))
(deny network*)
"""


def limited_argv(argv: Sequence[str], *, cpu_s: int, fsize: int, env: Mapping[str, str] | None = None) -> list[str]:
    """Apply the rlimits in a tiny /bin/sh wrapper that then execs sandbox-exec through `env -i` (no
    preexec_fn: forking a multi-threaded broker with a Python callback can deadlock the child). `env`
    are the only variables the child sees (the shell's PWD/SHLVL must not leak). Memory is not bounded
    here: macOS refuses RLIMIT_AS/RSS/DATA changes (`ulimit -v` fails with EINVAL), so ToolRunner polls
    the process group instead."""
    blocks = max(1, int(fsize) // 512)            # POSIX sh: ulimit -f counts 512-byte blocks
    pairs = ' '.join(shlex.quote(f'{k}={v}') for k, v in sorted((env or {}).items()))
    script = (f'ulimit -t {int(cpu_s)} && ulimit -f {blocks} && ulimit -n 64 && ulimit -c 0 '
              f'&& exec /usr/bin/env -i {pairs} "$0" "$@"')
    return ['/bin/sh', '-c', script, *argv]


def private_conflicts(root: str | os.PathLike, *, exempt: Sequence[str | os.PathLike] = (),
                      depth: int = PRIVATE_SEARCH_DEPTH) -> list[str]:
    """Why a read root touches a private campaign tree: it is inside or equal to one (a path component
    sequence of PRIVATE_TREES), or a parent of one (such a directory exists below it, searched `depth`
    levels down, skipping the exempt directories). Empty = no conflict."""
    root = Path(os.path.realpath(root))
    exempt_paths = {Path(os.path.realpath(p)) for p in exempt}
    found = []
    parts = root.parts
    for tree in PRIVATE_TREES:
        want = tuple(tree.split('/'))
        for i in range(len(parts) - len(want) + 1):
            if parts[i:i + len(want)] == want:
                found.append(f'{root} is inside or equal to a {tree}/ tree')
                break

    def walk(directory: Path, level: int) -> None:
        if directory in exempt_paths:
            return
        for tree in PRIVATE_TREES:
            want = tree.split('/')
            candidates = [directory / tree]
            # a directory that already carries the leading part of a tree name (…/prepared above private/)
            candidates += [directory.joinpath(*want[k:]) for k in range(1, len(want)) if directory.parts[-k:] == tuple(want[:k])]
            for private in candidates:
                if private.is_dir() and not private.is_symlink() and private not in exempt_paths:
                    found.append(f'{root} is a parent of the private tree {private}')
        if level >= depth:
            return
        try:
            entries = [e for e in os.scandir(directory) if e.is_dir(follow_symlinks=False)]
        except OSError:
            return
        for entry in entries:
            walk(Path(entry.path), level + 1)
    walk(root, 0)
    return list(dict.fromkeys(found))


def group_rss_kb(pgid: int) -> int:
    """Resident memory (KB) of every process of a process group (pgrep -g, ps -o rss); 0 when none."""
    try:
        pids = subprocess.run(['/usr/bin/pgrep', '-g', str(pgid)], capture_output=True, text=True, timeout=5).stdout.split()
        if not pids:
            return 0
        out = subprocess.run(['/bin/ps', '-o', 'rss=', '-p', ','.join(pids)], capture_output=True, text=True, timeout=5).stdout
        return sum(int(x) for x in out.split() if x.isdigit())
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


class ToolRunner:
    """Run adapter tool scripts (DESIGN §5 ToolRunner): `run([script, *args], stdin=..., cpu_s, timeout_s,
    output_cap, memory_mb)` -> {'exit', 'stdout', 'stderr_tail', 'seconds', 'timed_out', 'memory_capped'}.
    Read roots: the python prefix, the system libraries, `corpus` (a directory; also the CORPUS_ENV value)
    and the directory of `index` (a file; also the INDEX_ENV value); the tool files are granted per run as
    literals (the script's import closure, `import_closure`) plus the listing of `tools_dir` when the closure
    has siblings - exactly the actor kit's grant. `tmp_root` holds the per-call work directory (the only
    writable place). The corpus and index roots are refused when they touch a private campaign tree
    (private_conflicts); the tools directory and tmp root are exempt (they live under evolution/BB/ or
    runs/code/)."""

    def __init__(self, tools_dir: str | os.PathLike, corpus: str | os.PathLike | None = None,
                 index: str | os.PathLike | None = None, tmp_root: str | os.PathLike | None = None, *,
                 python: str | os.PathLike | None = None, fsize: int = 1 << 20, memory_mb: int = TOOL_MEMORY_MB) -> None:
        if not os.path.exists(SANDBOX_EXEC):
            raise RuntimeError('sandbox-exec is required for the tool runner (macOS)')
        self.python = os.path.realpath(python or default_python())
        if not os.access(self.python, os.X_OK):
            raise FileNotFoundError(f'runner python is not executable: {self.python}')
        self.prefix = str(Path(self.python).parent.parent)
        self.tools_dir = os.path.realpath(tools_dir)
        if not os.path.isdir(self.tools_dir):
            raise ValueError(f'tools directory missing: {tools_dir}')
        self.tmp_root = os.path.realpath(tmp_root or tempfile.gettempdir())
        os.makedirs(self.tmp_root, exist_ok=True)
        self.corpus = os.path.realpath(corpus) if corpus is not None else None
        self.index = os.path.realpath(index) if index is not None else None
        if self.corpus is not None and not os.path.isdir(self.corpus):
            raise ValueError(f'corpus directory missing: {corpus}')
        if self.index is not None and not os.path.isfile(self.index):
            raise ValueError(f'tables index missing: {index}')
        exempt = (self.tools_dir, self.tmp_root)
        data_roots = [r for r in (self.corpus, os.path.dirname(self.index) if self.index else None) if r]
        conflicts = [c for r in data_roots for c in private_conflicts(r, exempt=exempt)]
        if conflicts:
            raise ValueError('tool runner read root touches a private tree: ' + '; '.join(conflicts))
        self.read_roots = list(dict.fromkeys([self.prefix, *SYSTEM_READ, *data_roots]))
        self.env = {k: v for k, v in ((CORPUS_ENV, self.corpus), (INDEX_ENV, self.index)) if v is not None}
        self.fsize = int(fsize)
        self.memory_mb = int(memory_mb)

    def grants(self, script: Path) -> tuple[list[str], list[str]]:
        """(files, dirs) a run of `script` may read: its import closure and, with siblings, the tools listing."""
        closure = import_closure(script)
        files = [str(p) for p in closure]
        dirs = [self.tools_dir] if len(closure) > 1 else []
        return files, dirs

    def run(self, argv: Sequence[str], *, stdin: bytes = b'', cpu_s: int = TOOL_CPU_S,
            timeout_s: float = TOOL_TIMEOUT_S, output_cap: int = TOOL_OUTPUT_CAP,
            memory_mb: int | None = None) -> dict:
        """argv[0] is a script file name inside the tools directory (regular file, TOOL_SCRIPT), the rest
        its arguments (the manifest's fixed args first). Never raises for a tool failure: exit codes,
        timeouts, memory kills and crashes come back in the result (a bad argv is a ValueError)."""
        argv = [str(a) for a in argv]
        if not argv or not TOOL_SCRIPT.match(argv[0]):
            raise ValueError('argv[0] must be a tool script name (<module>.py) inside the tools directory')
        script = Path(self.tools_dir) / argv[0]
        if script.is_symlink() or not script.is_file():
            raise ValueError(f'tool script is not a regular file in the tools directory: {argv[0]}')
        if isinstance(stdin, str):
            stdin = stdin.encode('utf-8')
        cap_kb = int(memory_mb if memory_mb is not None else self.memory_mb) * 1024
        work = os.path.realpath(tempfile.mkdtemp(prefix='tool-', dir=self.tmp_root))
        cwd = os.path.join(work, 'workspace')            # the actor's cwd is <episode>/workspace: same basename
        os.mkdir(cwd)
        started = time.perf_counter()
        try:
            files, dirs = self.grants(script)
            env = actor_like_env(cwd, self.env)
            profile = sandbox_profile(self.read_roots, work, [self.python], literal_files=files, literal_dirs=dirs)
            command = [SANDBOX_EXEC, '-p', profile, self.python, '-I', '-B', str(script), *argv[1:]]
            with tempfile.TemporaryFile(dir=self.tmp_root) as out, tempfile.TemporaryFile(dir=self.tmp_root) as err:
                proc = subprocess.Popen(limited_argv(command, cpu_s=cpu_s, fsize=self.fsize, env=env),
                                        stdin=subprocess.PIPE, stdout=out, stderr=err, cwd=cwd, env={},
                                        start_new_session=True, close_fds=True)
                capped = threading.Event()
                stop = threading.Event()

                def watch() -> None:
                    while not stop.wait(MEMORY_POLL_S):
                        if group_rss_kb(proc.pid) > cap_kb:
                            capped.set()
                            try:
                                os.killpg(proc.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            return
                watcher = threading.Thread(target=watch, name='tool-memory-watch', daemon=True)
                watcher.start()
                timed_out = False
                try:
                    proc.communicate(stdin or b'', timeout=float(timeout_s))
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.communicate()
                    timed_out = True
                except BrokenPipeError:                 # the tool closed stdin early: not an error
                    proc.wait(timeout=float(timeout_s))
                finally:
                    stop.set()
                    watcher.join(timeout=5)
                rc = proc.returncode
                out.seek(0)
                stdout = out.read(int(output_cap)).decode('utf-8', 'replace')
                err.seek(max(0, os.fstat(err.fileno()).st_size - 600))
                err_tail = err.read().decode('utf-8', 'replace')[-600:]
            if capped.is_set():
                err_tail = (err_tail + f'\nkilled: resident memory over {cap_kb // 1024} MB')[-600:]
            return {'exit': rc, 'stdout': stdout, 'stderr_tail': err_tail, 'timed_out': timed_out,
                    'memory_capped': capped.is_set(), 'seconds': round(time.perf_counter() - started, 3)}
        finally:
            shutil.rmtree(work, ignore_errors=True)


class SandboxRunner:
    """contracts.ComputeRunner. `python` = the frozen interpreter (its prefix is the only python
    tree readable); `extra_read` = further read-only roots (e.g. the corpus)."""

    def __init__(self, python: str | os.PathLike | None = None, *, extra_read: Sequence[str | os.PathLike] = (),
                 cpu_s: int = 2, timeout_s: float = 5.0, output_cap: int = 16_384, fsize: int = 1 << 20,
                 tmp_root: str | os.PathLike | None = None) -> None:
        if not os.path.exists(SANDBOX_EXEC):
            raise RuntimeError('sandbox-exec is required for the compute runner (macOS)')
        self.python = os.path.realpath(python or default_python())
        if not os.access(self.python, os.X_OK):
            raise FileNotFoundError(f'runner python is not executable: {self.python}')
        self.prefix = str(Path(self.python).parent.parent)
        self.read_roots = list(dict.fromkeys([self.prefix, *SYSTEM_READ,
                                              *(os.path.realpath(p) for p in extra_read)]))
        self.cpu_s, self.timeout_s, self.output_cap, self.fsize = int(cpu_s), float(timeout_s), int(output_cap), int(fsize)
        self.tmp_root = os.path.realpath(tmp_root or tempfile.gettempdir())
        self.last: dict = {}

    def _limited(self, argv: list[str]) -> list[str]:
        return limited_argv(argv, cpu_s=self.cpu_s, fsize=self.fsize)

    def run(self, code: str, *, timeout_s: float = 5.0) -> tuple[bool, str]:
        timeout = min(float(timeout_s), self.timeout_s) if timeout_s else self.timeout_s
        work = os.path.realpath(tempfile.mkdtemp(prefix='harness-run-', dir=self.tmp_root))
        started = time.perf_counter()
        try:
            main = Path(work) / 'main.py'
            main.write_text(code, encoding='utf-8')
            profile = sandbox_profile(self.read_roots, work, [self.python])
            argv = [SANDBOX_EXEC, '-p', profile, self.python, '-I', '-B', str(main)]
            # stdout/stderr are anonymous temp files OUTSIDE the child's writable dir, read back through
            # the descriptors we hold: the child can write the work dir, so re-opening a file there by
            # name would follow a symlink it planted (e.g. to an answer key) outside the sandbox.
            with tempfile.TemporaryFile(dir=self.tmp_root) as out, tempfile.TemporaryFile(dir=self.tmp_root) as err:
                proc = subprocess.Popen(self._limited(argv), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                        cwd=work, env={}, start_new_session=True, close_fds=True)
                try:
                    rc = proc.wait(timeout=timeout)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:          # exited between the timeout and the kill
                        pass
                    rc = proc.wait()
                    timed_out = True
                out.seek(0)
                stdout = out.read(self.output_cap).decode('utf-8', 'replace')
                err.seek(max(0, os.fstat(err.fileno()).st_size - 600))
                err_tail = err.read().decode('utf-8', 'replace')
            ok = rc == 0 and not timed_out
            self.last = {'ok': ok, 'returncode': rc, 'timed_out': timed_out,
                         'seconds': round(time.perf_counter() - started, 3), 'stderr_tail': err_tail[-300:]}
            return ok, stdout
        finally:
            shutil.rmtree(work, ignore_errors=True)


class _Unavailable:
    """Runner used when sandbox-exec or the python is missing: offers needing execution return None."""
    last: dict = {}

    def run(self, code: str, *, timeout_s: float = 5.0) -> tuple[bool, str]:
        return False, ''


def make_runner(python: str | os.PathLike | None = None, **kwargs) -> 'SandboxRunner | _Unavailable':
    try:
        return SandboxRunner(python, **kwargs)
    except (RuntimeError, FileNotFoundError) as exc:
        print(f'compute runner unavailable: {exc}', file=sys.stderr)
        return _Unavailable()
