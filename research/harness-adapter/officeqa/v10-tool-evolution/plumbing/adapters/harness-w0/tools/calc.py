"""calc — run a short Python computation and print it in the ledger's calc frame.

Usage (inside the actor workspace):
    calc -c 'print((103774 - 102150) / 102150 * 100)'
    calc script.py
    calc <<'PY'            (or: echo '...' | calc)
    ...
    PY
If the last statement is an expression, its value is printed (like a REPL). numpy/pandas work when
the kit python has them. Output (contracts.CALC_OPEN / CALC_RESULT / CALC_END; the harness ledger
parses it and links every number in the code to the tool output it came from):
    ⟦calc sha=<first 12 hex of sha256(code)>⟧
    <the code, ≤ 8 KB>
    ⟦result⟧
    <stdout, ≤ 2 KB>
    ⟦end⟧
Errors go to stderr after the frame and set a nonzero exit code. Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys

try:
    from harness_core.contracts import CALC_END, CALC_OPEN, CALC_RESULT
except ImportError:                      # installed as a standalone script in the kit
    CALC_OPEN, CALC_RESULT, CALC_END = '⟦calc sha=', '⟦result⟧', '⟦end⟧'

MAX_CODE = 8192
MAX_RESULT = 2048
TRUNCATED = '… [truncated]'

# Runs in the child: executes stdin as a module, echoes a trailing expression, keeps source lines
# available to tracebacks.
RUNNER = r'''
import ast, linecache, sys
src = sys.stdin.read()
linecache.cache['<calc>'] = (len(src), None, src.splitlines(True), '<calc>')
tree = ast.parse(src, '<calc>', 'exec')
last = None
if tree.body and isinstance(tree.body[-1], ast.Expr):
    last = ast.Expression(tree.body.pop().value)
scope = {'__name__': '__main__'}
exec(compile(tree, '<calc>', 'exec'), scope)
if last is not None:
    value = eval(compile(last, '<calc>', 'eval'), scope)
    if value is not None:
        print(value)
'''


def clip(text: str, limit: int) -> str:
    raw = text.encode('utf-8')
    if len(raw) <= limit:
        return text
    return raw[:limit].decode('utf-8', 'ignore') + TRUNCATED


def frame(code: str, stdout: str) -> str:
    """The exact frame for `code` (already normalised) and its stdout."""
    sha = hashlib.sha256(code.encode('utf-8')).hexdigest()[:12]
    out = clip(stdout.rstrip('\n'), MAX_RESULT).replace('⟦', '[').replace('⟧', ']')
    return f'{CALC_OPEN}{sha}⟧\n{clip(code, MAX_CODE)}\n{CALC_RESULT}\n{out}\n{CALC_END}'


def normalise(code: str) -> str:
    return code.replace('\r\n', '\n').strip('\n')


def execute(code: str, *, timeout: float = 60.0, python: str | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run([python or sys.executable, '-c', RUNNER], input=code, capture_output=True,
                              text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode('utf-8', 'replace') if isinstance(exc.stdout, bytes) else (exc.stdout or '')
        return 124, out, f'calc: stopped after {timeout:g} s\n'


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='calc', description='Run Python arithmetic and print a calc frame.')
    ap.add_argument('-c', dest='code', help='code to run')
    ap.add_argument('file', nargs='?', help="file with code ('-' or nothing = stdin)")
    ap.add_argument('--timeout', type=float, default=60.0, help='seconds (default 60)')
    args = ap.parse_args(argv)
    if args.code is not None and args.file:
        ap.error('give either -c CODE or FILE, not both')
    if args.code is not None:
        code = args.code
    elif args.file and args.file != '-':
        try:
            with open(args.file, encoding='utf-8') as fh:
                code = fh.read()
        except OSError as exc:
            print(f'calc: cannot read {args.file}: {exc.strerror}', file=sys.stderr)
            return 2
    elif not sys.stdin.isatty():
        code = sys.stdin.read()
    else:
        ap.error("no code: use calc -c 'print(1+1)', calc FILE, or pipe code on stdin")
    code = normalise(code)
    if not code.strip():
        print('calc: empty code', file=sys.stderr)
        return 2
    rc, out, err = execute(code, timeout=args.timeout)
    print(frame(code, out), flush=True)
    if rc != 0:
        tail = '\n'.join(err.rstrip('\n').split('\n')[-6:])
        print(clip(tail, 1000) or f'calc: exit code {rc}', file=sys.stderr)
        return rc if 0 < rc < 128 else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
