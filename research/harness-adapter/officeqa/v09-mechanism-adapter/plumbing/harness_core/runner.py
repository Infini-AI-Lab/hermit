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
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Sequence

SANDBOX_EXEC = '/usr/bin/sandbox-exec'
CODEX_PYTHON = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3'
FROZEN_PYTHON = Path(__file__).resolve().parents[2] / 'frameworks-v9/actor-python/bin/python3'
SYSTEM_READ = ('/usr/lib', '/System')


def default_python() -> str:
    """The frozen actor python (frameworks-v9/actor-python) if present, else the codex runtime python."""
    env = os.environ.get('HARNESS_RUNNER_PYTHON')
    for candidate in ([Path(env)] if env else []) + [FROZEN_PYTHON, CODEX_PYTHON]:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError('no python for the compute runner (set HARNESS_RUNNER_PYTHON)')


def _q(path: str) -> str:
    return json.dumps(str(path))


def sandbox_profile(read_roots: Sequence[str], work: str, executables: Sequence[str]) -> str:
    reads = ' '.join(f'(subpath {_q(r)})' for r in read_roots)
    parents = sorted({str(p) for r in [*read_roots, work] for p in Path(r).parents})
    meta = ' '.join(f'(literal {_q(p)})' for p in parents)
    execs = ' '.join(f'(literal {_q(e)})' for e in executables)
    return f"""(version 1)
(deny default)
(allow process-exec {execs})
(allow signal (target self))
(allow sysctl-read)
(allow file-read* file-test-existence file-map-executable {reads})
(allow file-read-metadata file-test-existence {meta})
(allow file-read* file-test-existence (literal "/"))
(allow file-read* file-write* file-test-existence (subpath {_q(work)}))
(allow file-read* (literal "/dev/urandom") (literal "/dev/random"))
(allow file-read* file-write* (literal "/dev/null"))
(deny network*)
"""


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
        """Apply the rlimits in a tiny /bin/sh wrapper that then execs sandbox-exec (no preexec_fn:
        forking a multi-threaded broker with a Python callback can deadlock the child)."""
        blocks = max(1, self.fsize // 512)            # POSIX sh: ulimit -f counts 512-byte blocks
        script = (f'ulimit -t {self.cpu_s} && ulimit -f {blocks} && ulimit -n 64 && ulimit -c 0 '
                  '&& exec /usr/bin/env -i "$0" "$@"')          # the shell's PWD/SHLVL must not leak
        return ['/bin/sh', '-c', script, *argv]

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
