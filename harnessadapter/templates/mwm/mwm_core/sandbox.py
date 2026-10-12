"""The host's code channel: wm(name="run", args={"code": ..., "args": {...}}).

Code a HOST writes at answer time, run once, discarded. It is the
counterpart of the Continual Harness's run_code and of Prime's IPython
cell: every baseline's host can compute, and until this channel existed
ours could not — a meta-built tool was the only way a computation reached
a host, and a wrong branch in such a tool was invisible to the host that
obeyed it (measured 2026-09-15, banking b4: episodes that called a
meta-built tool scored 55–77% against 86–100% for those that read
memory). This channel changes none of that layer; it gives the host its
own arithmetic.

The sandbox is the Continual Harness one (clbench src/systems/
continual_harness/sandbox.py, reviewed 2026-09-12/13), ported so mwm does
not import a venue package. It is deliberately NOT capabilities._bwrap,
the sandbox meta-built tools run in: that one mounts the whole world
model read-only at /w and passes the parent's environment through, which
a tool the meta-agent writes may need; a host's throwaway code needs
neither (user decision 2026-09-15: a separate sandbox, the tool one
untouched). Here:

- a CHILD PROCESS under bubblewrap: /usr and the interpreter's prefixes
  read-only, a private /tmp, no home, no repository, no data, no
  environment, no world model; every namespace unshared (no network);
  a cleared environment; a wall-clock timeout; an address-space limit;
- a builtins whitelist (no open, no exec, no __import__ beyond the list)
  and an import whitelist enforced inside the child as a second layer —
  a whitelisted module's attributes reach os (random._os), so the mount
  tree is the isolation, the whitelist only the interface;
- `args` in, `result` out, print() captured; a traceback comes back as
  such; stdout and result are capped.

Differences from the source: the default timeout is 600 s (user
decision 2026-09-15, Prime's tool timeout), and there is no `tools`
dict — the environment is reachable only through the venue's own
actions, the rule every arm's code runs under.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig

ALLOWED_MODULES = ("random", "collections", "math", "json", "re", "heapq",
                   "itertools", "functools", "statistics", "numpy")
TIMEOUT_S = 600.0           # wall clock per call (user decision 2026-09-15)
STDOUT_CAP = 50 * 1024      # printed output, characters
RESULT_CAP = 20000          # serialised `result`, characters
ERROR_CAP = 3000            # the exception's message, like the traceback
                            # (review 2026-09-15: an exception carrying a
                            # large object returned 103,063 characters)
MEM_CAP = 2 << 30           # address-space limit in the child, bytes

_CHILD = r'''
import io, json, sys, traceback
try:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (%d, %d))
except Exception:
    pass
spec = json.loads(sys.stdin.read())
code, args = spec["code"], spec.get("args") or {}
allowed = set(spec["allowed"])
captured = io.StringIO()
_real_import = __import__

def _import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0]
    if root not in allowed:
        raise ImportError(f"import of {name!r} is not allowed in this sandbox")
    return _real_import(name, globals, locals, fromlist, level)

g = {
    "__builtins__": {
        "range": range, "len": len, "int": int, "float": float,
        "str": str, "list": list, "dict": dict, "tuple": tuple,
        "set": set, "frozenset": frozenset, "type": type,
        "bool": bool, "print": lambda *a, **kw: print(*a, file=captured, **kw),
        "abs": abs, "min": min, "max": max, "sum": sum, "enumerate": enumerate,
        "zip": zip, "sorted": sorted, "reversed": reversed, "isinstance": isinstance,
        "map": map, "filter": filter, "any": any, "all": all,
        "round": round, "divmod": divmod, "pow": pow, "repr": repr,
        "ValueError": ValueError, "KeyError": KeyError, "TypeError": TypeError,
        "IndexError": IndexError, "Exception": Exception, "ZeroDivisionError": ZeroDivisionError,
        "__import__": _import,
        "True": True, "False": False, "None": None,
    },
    "args": args,
}
for m in ("random", "collections", "math", "json", "re", "heapq", "itertools", "functools", "statistics"):
    if m in allowed:
        g[m] = _real_import(m)
if "numpy" in allowed:
    try:
        np = _real_import("numpy"); g["np"] = np; g["numpy"] = np
    except Exception:
        pass
out = {}
try:
    exec(code, g)
    result = g.get("result", None)
    stdout = captured.getvalue()
    out = {"success": True}
    if result is not None:
        _s = json.dumps(result, default=str)
        if len(_s) > %d:
            out["result"] = _s[:%d]
            out["result_truncated"] = True
        else:
            out["result"] = result
    if stdout:
        out["stdout"] = stdout[:%d]
    if result is None and not stdout:
        out["note"] = "Code ran successfully but set no 'result' variable and printed nothing."
except BaseException as e:
    out = {"success": False, "error": str(e)[:%d], "traceback": traceback.format_exc()[-%d:],
           "stdout": captured.getvalue()[:%d]}
try:
    sys.stdout.write(json.dumps(out, default=str))
except Exception as e:
    sys.stdout.write(json.dumps({"success": False, "error": f"result not serialisable: {e}"[:%d]}))
''' % (MEM_CAP, MEM_CAP, RESULT_CAP, RESULT_CAP, STDOUT_CAP, ERROR_CAP, ERROR_CAP, STDOUT_CAP, ERROR_CAP)


def bwrap_command(python: str) -> list[str]:
    """The bubblewrap prefix: read-only /usr and the interpreter's prefixes,
    a private /tmp, every namespace unshared, the parent's death kills it,
    an empty environment. Nothing else of the host exists inside."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("bwrap (bubblewrap) is required for the code sandbox")
    cmd = [bwrap, "--unshare-all", "--die-with-parent", "--new-session",
           "--clearenv", "--setenv", "PATH", "/usr/bin",
           # single-threaded BLAS: OpenBLAS reserves address space per
           # thread and fails to import under the address-space limit
           "--setenv", "OPENBLAS_NUM_THREADS", "1", "--setenv", "OMP_NUM_THREADS", "1",
           "--ro-bind", "/usr", "/usr",
           "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
           "--symlink", "usr/bin", "/bin",
           "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--chdir", "/tmp"]
    real = os.path.realpath(python)
    prefixes = [os.path.realpath(p) for p in (sys.base_prefix, sys.prefix) if p]
    prefixes.append(os.path.dirname(os.path.dirname(real)))
    for prefix in dict.fromkeys(prefixes):
        if prefix != "/usr":
            cmd += ["--ro-bind", prefix, prefix]
    # the interpreter is run by its real path (a venv's python is a symlink
    # through alias directories that are not bound); the venv's packages
    # come in through PYTHONPATH — the only variable in the cleared env
    site = sysconfig.get_paths()["purelib"]
    cmd += ["--setenv", "PYTHONPATH", site]
    return cmd + ["--"]


def run_sandboxed(code: str, args: dict | None, timeout: float = TIMEOUT_S,
                  python: str | None = None) -> dict:
    """Run `code` in a bwrap-confined child interpreter; returns the result
    dict — {"success": true, "result"?, "stdout"?, "note"?} or
    {"success": false, "error", "traceback"?, "stdout"?}."""
    spec = json.dumps({"code": code, "args": args or {},
                       "allowed": list(ALLOWED_MODULES)})
    python = os.path.realpath(python or sys.executable)
    try:
        # -s -B: no user site, no bytecode files; PYTHONPATH (set above) is
        # honoured, nothing else is in the environment
        p = subprocess.run([*bwrap_command(python), python, "-s", "-B", "-c", _CHILD],
                           input=spec, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"success": False,
                "error": f"timed out after {timeout:g} s (the sandbox has a "
                         "wall-clock limit)"}
    out = (p.stdout or "").strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"success": False,
                "error": f"sandbox produced no result (exit {p.returncode}): "
                         f"{(p.stderr or out)[-500:]}"}
