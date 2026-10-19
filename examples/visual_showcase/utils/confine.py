"""Confined execution for the visual showcase: every command the acting agent, a learned tool or the toolsmith's
self-tests run goes through one bubblewrap recipe (allowlist of read-only paths, private /tmp, no network, the GPU
nodes visible so Chromium can render WebGL, an explicit environment without any key).

Importing this module only resolves paths (node, the venv python, Chromium, node_modules, the wrapper) and raises when
one is missing; nothing is executed. `run` is the primitive, `run_tool` its JSON-safe form declared as the toolsmith
evolver's task tool, `selftest` the pre-flight (`python confine.py selftest <scratch>` prints it as JSON).
"""
from __future__ import annotations

import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime"
REFERENCE = HERE / "reference"


def _realpath(path) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def _resolve_node() -> str:
    candidate = os.environ.get("VISUAL_NODE") or str(Path.home() / ".nvm" / "versions" / "node" / "v22.23.3" / "bin" / "node")
    real = _realpath(candidate)
    if not os.path.isfile(real) or not os.access(real, os.X_OK):
        raise RuntimeError(f"confine: node is missing at {candidate} (set VISUAL_NODE to a node executable)")
    return real


def _resolve_chrome() -> str:
    override = os.environ.get("VISUAL_CHROME_BIN")
    if override:
        real = _realpath(override)
        if not os.path.isfile(real):
            raise RuntimeError(f"confine: VISUAL_CHROME_BIN={override} is not a file")
        return real
    browsers = _realpath(Path.home() / ".cache" / "ms-playwright")
    found = glob.glob(os.path.join(browsers, "chromium-*", "chrome-linux64", "chrome"))

    def build(path: str) -> int:    # chromium-1234 -> 1234; the highest build wins
        match = re.search(r"chromium-(\d+)", path)
        return int(match.group(1)) if match else -1
    found = [p for p in found if os.path.isfile(p)]
    if not found:
        raise RuntimeError(f"confine: no Playwright Chromium under {browsers}/chromium-*/chrome-linux64/chrome "
                           "(npx playwright install chromium, or set VISUAL_CHROME_BIN)")
    return _realpath(max(found, key=build))


def _resolve_dir(path: Path, hint: str) -> str:
    real = _realpath(path)
    if not os.path.isdir(real):
        raise RuntimeError(f"confine: {path} is missing ({hint})")
    return real


NODE: str = _resolve_node()
PREFIX: str = _realpath(sys.prefix)
BASE_PREFIX: str = _realpath(sys.base_prefix)
# The venv's bin/python is a symlink to the base interpreter; its realpath would start Python without the venv (the
# venv is found from the symlink's location), so the venv path stays unresolved and only its directory is canonical.
PYTHON: str = os.path.join(PREFIX, "bin", os.path.basename(sys.executable)) if PREFIX != BASE_PREFIX \
    else _realpath(sys.executable)
if not os.path.isfile(PYTHON):
    raise RuntimeError(f"confine: the python interpreter {PYTHON} is missing")
CHROME_BIN: str = _resolve_chrome()
NODE_MODULES: str = _resolve_dir(RUNTIME / "node_modules", "run npm ci --prefix examples/visual_showcase/utils/runtime")
WRAPPER: str = _realpath(RUNTIME / "chrome-gpu.sh")
if not os.path.isfile(WRAPPER):
    raise RuntimeError(f"confine: the Chromium wrapper {WRAPPER} is missing")
LOCK: str = os.environ.get("VISUAL_CAPTURE_LOCK") or str(Path.home() / ".cache" / "visual_showcase" / "capture.lock")
PLAYWRIGHT_DIR: str = os.path.dirname(os.path.dirname(os.path.dirname(CHROME_BIN)))     # parent of chromium-*/
NODE_DIR: str = os.path.dirname(os.path.dirname(NODE))                                  # the node version dir
BASH = "/usr/bin/bash"
INTERPRETERS = {"python3": PYTHON, "node": NODE, "bash": BASH}

ETC = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/alternatives", "/etc/nsswitch.conf",
       "/etc/passwd", "/etc/group", "/etc/localtime", "/etc/os-release", "/etc/fonts")
# One GPU only: Chromium's EGL initialises every GPU it can open, which on this 10-GPU machine made the first WebGL
# context take 6-9 s inside the sandbox; with one GPU node it takes 2.5-4 s, and the sandbox never touches the other
# GPUs (other users' jobs run there). $VISUAL_GPU picks the index (default 0); /dev/dri stays whole (render nodes).
GPU_INDEX = int(os.environ.get("VISUAL_GPU", "0"))
DEVICES = ("/dev/dri", "/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools", "/dev/nvidia-modeset",
           f"/dev/nvidia{GPU_INDEX}")
# runtime/chrome-gpu.sh falls back to this directory when VISUAL_CHROME_BIN is unset; the variable is passed into the
# sandbox only when the resolved Chromium lies elsewhere, so the agent's environment names no browser on this machine.
WRAPPER_DEFAULT_BROWSERS = "/scratch/haizhonz/.cache/ms-playwright"
SYSTEM_LINKS = ("/bin", "/sbin", "/lib", "/lib64")
# The venv's base prefix is a whole conda installation: its envs/ (other environments, some with Python Playwright and
# their own browsers) and pkgs/ (the package cache) are not part of the venv and are hidden under an empty tmpfs.
HIDDEN_UNDER_BASE = ("envs", "pkgs")
INNER_TMP = "/tmp"
INNER_TRACE = "/tmp/trace"
HEAD_BYTES, TAIL_BYTES = 12_000, 4_000        # of stdout and of stderr each; the rest is counted as dropped
LOCK_WAIT_S = 360.0         # why: the host's final capture holds the lock exclusively for up to
                            # harness.CAPTURE_TIMEOUT_S (300 s); a shorter wait would fail other runs' commands
TERM_GRACE_S = 3.0
FSIZE_LIMIT = 256 * 1024 * 1024
# RLIMIT_NPROC counts every thread of the uid (not of the sandbox): this uid already runs ~1000 threads outside and one
# Chromium adds a few hundred, so the contract's 1024 would make the browser's forks fail; 8192 still stops a fork bomb.
NPROC_LIMIT = 8192
SECRET_MARKERS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD")
PROFILE_MARKERS = ("DevToolsActivePort",)
PROFILE_DIR_PREFIX = "playwright_chromiumdev_profile-"
SCAN_DEPTH = 6
BROWSER_POLL_S = 0.25         # a browser lives for seconds; four looks a second do not miss one


def _system_visible() -> list[str]:
    """Read-only roots every sandbox shows (host path == inner path)."""
    out = ["/usr", "/sys", *SYSTEM_LINKS, *[p for p in ETC if os.path.exists(p)]]
    for path in (PREFIX, BASE_PREFIX, NODE_DIR, PLAYWRIGHT_DIR, NODE_MODULES, WRAPPER):
        if path not in out and not path.startswith("/usr/"):
            out.append(path)
    return out


def bwrap_argv(cwd: str, ro=(), tmp_dir: str | None = None, extra_env: dict | None = None, *,
               trace: str | None = None) -> tuple[list[str], dict]:
    """The bwrap prefix (everything before `-- <command>`) and the child's whole environment. `ro` holds (src, dst)
    pairs bound read-only after /tmp (so dst may lie under /tmp); `tmp_dir` is bound read-write at /tmp."""
    if not shutil.which("bwrap"):
        raise RuntimeError("confine: bubblewrap (bwrap) is not installed; nothing runs unconfined")
    cwd = _realpath(cwd)
    if not os.path.isdir(cwd):
        raise ValueError(f"confine: cwd {cwd} is not a directory")
    if cwd == INNER_TMP or cwd.startswith(INNER_TMP + "/"):
        raise ValueError(f"confine: cwd {cwd} lies under /tmp, which the sandbox replaces; use a directory elsewhere")
    if tmp_dir is None:
        raise ValueError("confine: tmp_dir is required (run() creates one when the caller passes none)")
    tmp_dir = _realpath(tmp_dir)
    args = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--dev", "/dev", "--proc", "/proc",
            "--ro-bind", "/sys", "/sys", "--ro-bind", "/usr", "/usr"]
    for link in SYSTEM_LINKS:
        if os.path.islink(link):
            args += ["--symlink", os.readlink(link), link]
        elif os.path.isdir(link):
            args += ["--ro-bind", link, link]
    for path in ETC:
        if os.path.exists(path):
            args += ["--ro-bind", path, path]
    for node in DEVICES:
        if os.path.exists(node):
            args += ["--dev-bind", node, node]
    seen: set[str] = set()
    for path in (PREFIX, BASE_PREFIX, NODE_DIR, PLAYWRIGHT_DIR, NODE_MODULES, WRAPPER):
        if path in seen or path.startswith("/usr/"):       # /usr is already there as a whole
            continue
        seen.add(path)
        args += ["--ro-bind", path, path]
    for name in HIDDEN_UNDER_BASE:                         # after the base prefix's bind, which they cover
        hidden = os.path.join(BASE_PREFIX, name)
        if os.path.isdir(hidden) and not (PREFIX + "/").startswith(hidden + "/"):
            args += ["--tmpfs", hidden]
    args += ["--bind", tmp_dir, INNER_TMP]
    if trace is not None:
        args += ["--bind", _realpath(trace), INNER_TRACE]
    for pair in ro:
        src, dst = (str(pair[0]), str(pair[1]))
        src = _realpath(src)
        if not os.path.exists(src):
            raise ValueError(f"confine: ro source {src} does not exist")
        if not os.path.isabs(dst):
            raise ValueError(f"confine: ro destination {dst} must be absolute")
        args += ["--ro-bind", src, dst]
    args += ["--bind", cwd, cwd, "--chdir", cwd, "--clearenv"]
    env = {"PATH": f"{PREFIX}/bin:/usr/bin:/bin", "HOME": "/tmp/home", "TMPDIR": "/tmp", "XDG_CACHE_HOME": "/tmp/cache",
           "XDG_CONFIG_HOME": "/tmp/config", "LANG": "C.UTF-8", "TERM": "dumb"}
    if not CHROME_BIN.startswith(WRAPPER_DEFAULT_BROWSERS + "/"):      # the wrapper would not find it by itself
        env["VISUAL_CHROME_BIN"] = CHROME_BIN
    if trace is not None:
        env["VISUAL_TRACE"] = INNER_TRACE
    for key, value in (extra_env or {}).items():
        if any(marker in key.upper() for marker in SECRET_MARKERS):   # a key never enters the sandbox
            continue
        env[str(key)] = str(value)
    for key, value in env.items():
        args += ["--setenv", key, value]
    return args, env


def _inner_roots(cwd: str, ro, tmp_dir: str) -> dict[str, str]:
    """Inner path -> host path for every directory or file visible inside."""
    roots = {p: p for p in _system_visible()}
    roots[INNER_TMP] = tmp_dir
    for src, dst in ro:
        roots[str(dst)] = _realpath(src)
    roots[cwd] = cwd
    return roots


def _resolve_argv0(argv: list[str], cwd: str, ro, tmp_dir: str) -> list[str]:
    if not argv:
        raise ValueError("confine: argv is empty")
    head = argv[0]
    if head in INTERPRETERS:
        return [INTERPRETERS[head], *argv[1:]]
    if not os.path.isabs(head):
        raise ValueError(f"confine: argv[0] must be python3, node, bash or an absolute path inside the sandbox, not {head!r}")
    for inner, host in sorted(_inner_roots(cwd, ro, tmp_dir).items(), key=lambda kv: -len(kv[0])):
        if head == inner or head.startswith(inner.rstrip("/") + "/"):
            on_host = host + head[len(inner):]
            if not os.path.isfile(on_host):
                raise ValueError(f"confine: argv[0] {head} is not a file (host path {on_host})")
            return list(argv)
    raise ValueError(f"confine: argv[0] {head} is not inside any bound directory")


class _Reader(threading.Thread):
    """Drains one pipe keeping its first HEAD_BYTES and last TAIL_BYTES; counts what it dropped in between."""

    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream, self.head, self.tail, self.total = stream, bytearray(), bytearray(), 0

    def run(self) -> None:
        fd = self.stream.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            self.total += len(chunk)
            room = HEAD_BYTES - len(self.head)
            if room > 0:
                self.head += chunk[:room]
                chunk = chunk[room:]
            if chunk:
                self.tail += chunk
                if len(self.tail) > TAIL_BYTES:
                    del self.tail[:-TAIL_BYTES]

    @property
    def dropped(self) -> int:
        return max(0, self.total - HEAD_BYTES - TAIL_BYTES)

    def text(self) -> str:
        if self.dropped:
            marker = f"\n[... {self.dropped} bytes dropped ...]\n".encode()
            return (bytes(self.head) + marker + bytes(self.tail)).decode("utf-8", "replace")
        return (bytes(self.head) + bytes(self.tail)).decode("utf-8", "replace")


def _limits() -> None:
    """preexec_fn of the bwrap child: the limits are inherited by everything inside."""
    for kind, wanted in ((resource.RLIMIT_FSIZE, FSIZE_LIMIT), (resource.RLIMIT_CORE, 0), (resource.RLIMIT_NPROC, NPROC_LIMIT)):
        _, hard = resource.getrlimit(kind)
        value = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)   # never above a hard limit
        resource.setrlimit(kind, (value, value))


def _acquire_lock(mode: str | None) -> tuple:
    """(open file or None, seconds waited, error or None). Bounded by LOCK_WAIT_S; never counted in the timeout."""
    if mode is None:
        return None, 0.0, None
    flag = {"shared": fcntl.LOCK_SH, "exclusive": fcntl.LOCK_EX}[mode]
    Path(LOCK).parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK, "a")
    started = time.monotonic()
    while True:
        try:
            fcntl.flock(handle, flag | fcntl.LOCK_NB)
            return handle, time.monotonic() - started, None
        except BlockingIOError:
            if time.monotonic() - started >= LOCK_WAIT_S:
                handle.close()
                return None, time.monotonic() - started, "host busy, retry"
            time.sleep(0.1)


def _kill(proc: subprocess.Popen) -> None:
    """SIGTERM the group, then SIGKILL after TERM_GRACE_S; bwrap's --die-with-parent takes the inner tree down."""
    for sig, wait in ((signal.SIGTERM, TERM_GRACE_S), (signal.SIGKILL, None)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _scan_for_profile(root: str) -> bool:
    """Chromium profile artefacts under `root` (no symlinks followed, depth <= SCAN_DEPTH)."""
    stack = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_file and entry.name in PROFILE_MARKERS:
                return True
            if is_dir:
                if entry.name.startswith(PROFILE_DIR_PREFIX):
                    return True
                if entry.name == "Default" and os.path.isfile(os.path.join(entry.path, "Preferences")) \
                        and not os.path.islink(os.path.join(entry.path, "Preferences")):
                    return True
                if depth < SCAN_DEPTH:
                    stack.append((entry.path, depth + 1))
    return False


def _descendants(pid: int) -> list[int]:
    """Host pids of every process below `pid` (/proc/<pid>/task/<tid>/children, no recursion limit needed: the tree of
    one sandboxed command is small)."""
    found, stack = [], [pid]
    while stack:
        current = stack.pop()
        try:
            tasks = os.listdir(f"/proc/{current}/task")
        except OSError:
            continue
        for tid in tasks:
            try:
                children = Path(f"/proc/{current}/task/{tid}/children").read_text().split()
            except OSError:
                continue
            for child in children:
                found.append(int(child))
                stack.append(int(child))
    return found


def _browser_running(pid: int) -> bool:
    """Whether a process from the Playwright browsers directory (Chromium, its headless shell or their helpers) runs
    below `pid` right now. Seen from the host, so it counts every way of starting the browser, wrapper or not."""
    for child in _descendants(pid):
        try:
            exe = os.readlink(f"/proc/{child}/exe")
        except OSError:
            continue
        if exe.startswith(PLAYWRIGHT_DIR + "/"):
            return True
    return False


def _launches(trace: str | None) -> int:
    if trace is None or not os.path.isfile(trace):
        return 0
    try:
        return sum(1 for line in Path(trace).read_text(errors="replace").splitlines() if line.startswith("launch "))
    except OSError:
        return 0


def run(argv: list[str], cwd: str, timeout_s: float, *, ro=(), extra_env: dict | None = None,
        lock: str | None = "shared", tmp_dir: str | None = None, trace: str | None = None) -> dict:
    """Run `argv` inside the sandbox with `cwd` read-write. argv[0] "python3" / "node" / "bash" become the host
    interpreters; otherwise it must be an absolute path to a file visible inside. Returns the record below; never
    raises for what the command does (only for a bad call)."""
    cwd = _realpath(cwd)
    ro = [(str(a), str(b)) for a, b in ro]
    own_tmp = tmp_dir is None
    if own_tmp:
        base = Path(_realpath(Path.home() / ".cache" / "visual_showcase" / "tmp"))
        base.mkdir(parents=True, exist_ok=True)
        tmp_dir = tempfile.mkdtemp(prefix="cmd-", dir=base)
    tmp_dir = _realpath(tmp_dir)
    Path(tmp_dir).mkdir(parents=True, exist_ok=True)
    for sub in ("home", "cache", "config"):
        (Path(tmp_dir) / sub).mkdir(exist_ok=True)
    if trace is not None:
        trace = _realpath(trace)
        Path(trace).parent.mkdir(parents=True, exist_ok=True)
        Path(trace).touch()
    command = _resolve_argv0(argv, cwd, ro, tmp_dir)
    prefix, env = bwrap_argv(cwd, ro, tmp_dir, extra_env, trace=trace)
    full = [*prefix, "--", *command]
    record = {"exit_code": -1, "stdout": "", "stderr": "", "seconds": 0.0, "timed_out": False, "lock_wait_s": 0.0,
              "dropped_bytes": 0, "bwrap_argv_sha": hashlib.sha256("\0".join(prefix).encode()).hexdigest(),
              "browser_launched": False, "error": None}
    handle, waited, lock_error = _acquire_lock(lock)
    record["lock_wait_s"] = round(waited, 3)
    if lock_error:
        record["error"] = lock_error
        if own_tmp:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return record
    started = time.monotonic()
    seen_browser = False
    try:
        try:
            proc = subprocess.Popen(full, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True, preexec_fn=_limits)
        except OSError as exc:
            record["error"] = f"could not start bwrap: {exc}"
            return record
        out, err = _Reader(proc.stdout), _Reader(proc.stderr)
        out.start()
        err.start()
        end = started + max(0.0, float(timeout_s))
        while True:                         # wait, and look for a browser below the command every BROWSER_POLL_S
            try:
                proc.wait(timeout=max(0.0, min(BROWSER_POLL_S, end - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                if not seen_browser:
                    seen_browser = _browser_running(proc.pid)
                if time.monotonic() >= end:
                    record["timed_out"] = True
                    _kill(proc)
                    break
        out.join()          # the pipes close when the last process inside is gone (the pid namespace dies with bwrap)
        err.join()
        proc.stdout.close()
        proc.stderr.close()
        record.update(exit_code=proc.returncode, stdout=out.text(), stderr=err.text(),
                      dropped_bytes=out.dropped + err.dropped)
        if record["timed_out"]:
            record["error"] = f"timed out after {timeout_s} s"
    finally:
        record["seconds"] = round(time.monotonic() - started, 3)
        if handle is not None:
            handle.close()
        record["browser_launched"] = (seen_browser or _launches(trace) > 0 or _scan_for_profile(tmp_dir)
                                      or _scan_for_profile(cwd))
        if own_tmp:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return record


def run_tool(argv: list, cwd: str, timeout_s: float, ro: list | None = None, lock: str | None = "shared",
             extra_env: dict | None = None, tmp_dir: str | None = None, trace: str | None = None) -> dict:
    """`run` with JSON arguments (ro as [[src, dst], ...]): the entrypoint of the toolsmith evolver's task tool."""
    pairs = [(str(pair[0]), str(pair[1])) for pair in (ro or [])]
    return run([str(a) for a in argv], str(cwd), float(timeout_s), ro=pairs, extra_env=extra_env, lock=lock,
               tmp_dir=tmp_dir, trace=trace)


# --- selftest ---------------------------------------------------------------------------------------------------

TARGET_RGB = (30, 120, 200)          # what webgl_probe.html clears its scene to
PIXEL_TOLERANCE = 10
PIXEL_SCRIPT = r"""
// node shot.cjs <executablePath> <out.png>: serve ./index.html over loopback, screenshot it, report the scene's
// centre pixel. Last stdout line is JSON.
const http = require('node:http'), fs = require('node:fs'), path = require('node:path');
const {chromium} = require('playwright'), {PNG} = require('pngjs');
const [exe, out] = process.argv.slice(2);
const html = fs.readFileSync(path.join(process.cwd(), 'index.html'));
const server = http.createServer((req, res) => {
  res.writeHead(200, {'content-type': 'text/html; charset=utf-8'});
  res.end(html);
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch({executablePath: exe, headless: true, timeout: 20000});
  const page = await browser.newPage({viewport: {width: 640, height: 400}});
  const errors = [];
  page.on('pageerror', e => errors.push(String(e.message)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.goto(url, {waitUntil: 'load', timeout: 15000});
  await page.waitForFunction(() => window.__SHOWCASE__ && window.__SHOWCASE__.getState().ready, null,
                             {timeout: 10000}).catch(() => {});
  await page.waitForTimeout(300);
  const state = await page.evaluate(() => window.__SHOWCASE__ ? window.__SHOWCASE__.getState() : null);
  const renderer = await page.evaluate(() => window.__SHOWCASE__ ? window.__SHOWCASE__.renderer || null : null);
  const box = await page.locator('[data-testid="scene"]').boundingBox();
  await page.screenshot({path: out});
  await browser.close();
  server.close();
  const png = PNG.sync.read(fs.readFileSync(out));
  const x = Math.floor(box.x + box.width / 2), y = Math.floor(box.y + box.height / 2);
  const i = (y * png.width + x) * 4;
  const center = [png.data[i], png.data[i + 1], png.data[i + 2]];
  console.log(JSON.stringify({ready: Boolean(state && state.ready), state, renderer, center,
                              size: [png.width, png.height], errors}));
})().catch(e => { console.error('ERR ' + (e && e.stack || e)); process.exit(1); });
"""


def _last_json(text: str) -> dict | None:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def pixel_check(scratch: str | Path, *, executable: str, name: str) -> dict:
    """One browser launch inside the sandbox on webgl_probe.html served over loopback; the scene's centre pixel."""
    scratch = Path(_realpath(scratch))
    ws = scratch / f"ws-{name}"
    ws.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REFERENCE / "webgl_probe.html", ws / "index.html")
    (ws / "shot.cjs").write_text(PIXEL_SCRIPT)
    tmp = scratch / f"tmp-{name}"
    if tmp.exists():
        shutil.rmtree(tmp)
    trace = scratch / f"trace-{name}"
    if trace.exists():
        trace.unlink()
    result = run(["node", "shot.cjs", executable, "shot.png"], str(ws), 60, extra_env={"NODE_PATH": NODE_MODULES},
                 lock="shared", tmp_dir=str(tmp), trace=str(trace))
    parsed = _last_json(result["stdout"]) or {}
    center = parsed.get("center")
    matches = bool(center) and all(abs(int(c) - t) <= PIXEL_TOLERANCE for c, t in zip(center, TARGET_RGB))
    white = bool(center) and all(int(c) >= 240 for c in center)
    return {"executable": executable, "exit_code": result["exit_code"], "timed_out": result["timed_out"],
            "seconds": result["seconds"], "browser_launched": result["browser_launched"],
            "launches": _launches(str(trace)), "ready": parsed.get("ready"), "renderer": parsed.get("renderer"),
            "center": center, "matches_target": matches, "white": white, "errors": parsed.get("errors"),
            "stderr_tail": result["stderr"][-600:], "png": str(ws / "shot.png")}


def _isolation_targets() -> tuple[list[str], list[str]]:
    """Host paths that must be ENOENT inside, and those skipped because a bound path lies beneath them."""
    repo = HERE.parents[2]                                   # <repo>/examples/visual_showcase/utils -> <repo>
    wanted = [str(Path.home() / ".config"), str(repo.parent / "MetaHarnessAdapter"), str(repo / "hermit"),
              os.path.dirname(LOCK), os.path.dirname(os.path.dirname(LOCK))]
    bound = _system_visible()
    targets, skipped = [], []
    for path in wanted:
        if any(b == path or b.startswith(path.rstrip("/") + "/") for b in bound):
            skipped.append(path)        # bwrap creates it as an empty mount-point parent
        elif path not in targets:
            targets.append(path)
    return targets, skipped


def selftest(scratch_dir: str) -> dict:
    """The pre-flight: isolation assertions and the environment inside, the venv python inside, the GPU WebGL pixel
    check through the wrapper, the negative control without the ANGLE flags, and the timeout path."""
    scratch = Path(_realpath(scratch_dir))
    if str(scratch).startswith(INNER_TMP + "/"):
        raise ValueError("confine.selftest: the scratch directory must not lie under /tmp")
    scratch.mkdir(parents=True, exist_ok=True)
    report = {"ok": True, "bwrap": shutil.which("bwrap"), "paths": {"NODE": NODE, "PYTHON": PYTHON, "BASE_PREFIX": BASE_PREFIX,
              "CHROME_BIN": CHROME_BIN, "NODE_MODULES": NODE_MODULES, "WRAPPER": WRAPPER, "LOCK": LOCK}, "checks": {}}
    try:
        report["bwrap"] = subprocess.run(["bwrap", "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        report["bwrap"] = f"error: {exc}"

    # 1. isolation + env + network, one bash run
    ws = scratch / "ws-isolation"
    ws.mkdir(exist_ok=True)
    targets, skipped = _isolation_targets()
    script = "\n".join([
        *[f'if [ -e "{p}" ]; then echo "present {p}"; else echo "absent {p}"; fi' for p in targets],
        'echo "home=$HOME"', 'echo "cwd=$(pwd)"', 'echo "pids=$(ls /proc | grep -c "^[0-9]")"',
        'echo "dev=$(ls /dev | tr "\\n" " ")"',
        '(timeout 3 bash -c "echo > /dev/tcp/1.1.1.1/80" >/dev/null 2>&1 && echo net=open) || echo net=blocked',
        'echo "env=$(env | cut -d= -f1 | sort | tr "\\n" " ")"',
        'echo "python=$(command -v python3)"', 'echo "node=$(command -v node || echo none)"'])
    iso = run(["bash", "-c", script], str(ws), 30, lock=None, tmp_dir=str(scratch / "tmp-isolation"))
    lines = iso["stdout"].splitlines()
    present = [ln.split(" ", 1)[1] for ln in lines if ln.startswith("present ")]
    absent = [ln.split(" ", 1)[1] for ln in lines if ln.startswith("absent ")]
    fields = dict(ln.split("=", 1) for ln in lines if "=" in ln and not ln.startswith(("present ", "absent ")))
    env_names = sorted(fields.get("env", "").split())
    expected_env = sorted(["PATH", "HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "LANG", "TERM"]
                          + ([] if CHROME_BIN.startswith(WRAPPER_DEFAULT_BROWSERS + "/") else ["VISUAL_CHROME_BIN"]))
    leaked = [n for n in env_names if any(m in n.upper() for m in SECRET_MARKERS)]
    ok = (iso["exit_code"] == 0 and not present and set(absent) == set(targets) and fields.get("net") == "blocked"
          and not leaked and set(env_names) <= set(expected_env + ["PWD", "SHLVL", "_", "OLDPWD"])   # bash adds those
          and fields.get("home") == "/tmp/home" and fields.get("cwd") == str(ws)
          and fields.get("python") == f"{PREFIX}/bin/python3" and fields.get("node") == "none")
    check = {"ok": ok, "absent": absent, "present": present, "skipped_mount_parents": skipped, "env": env_names,
             "leaked": leaked, "home": fields.get("home"), "cwd": fields.get("cwd"), "pids": fields.get("pids"),
             "dev": fields.get("dev", "").split(),
             "net": fields.get("net"), "python": fields.get("python"), "node_on_path": fields.get("node"),
             "exit_code": iso["exit_code"], "stderr_tail": iso["stderr"][-400:]}
    report["checks"]["isolation"] = check

    # 2. the venv python runs inside with its packages
    code = ("import json, sys, jsonschema, yaml; print(json.dumps({'version': sys.version.split()[0], "
            "'executable': sys.executable, 'prefix': sys.prefix}))")
    py = run(["python3", "-I", "-c", code], str(ws), 30, lock=None, tmp_dir=str(scratch / "tmp-python"))
    parsed = _last_json(py["stdout"]) or {}
    report["checks"]["python"] = {"ok": py["exit_code"] == 0 and parsed.get("prefix") == PREFIX, **parsed,
                                  "exit_code": py["exit_code"], "stderr_tail": py["stderr"][-400:]}

    # 3. GPU WebGL through the wrapper; 4. negative control: the bare binary (SwiftShader) must not show the colour
    positive = pixel_check(scratch, executable=WRAPPER, name="gpu")
    positive["ok"] = positive["matches_target"] and positive["browser_launched"] and positive["launches"] >= 1
    report["checks"]["gpu_webgl"] = positive
    negative = pixel_check(scratch, executable=CHROME_BIN, name="swiftshader")
    negative["ok"] = negative["exit_code"] == 0 and not negative["matches_target"] and negative["launches"] == 0
    report["checks"]["swiftshader_negative"] = negative

    # 5. a command that overruns its timeout is killed and reported
    ws_t = scratch / "ws-timeout"
    ws_t.mkdir(exist_ok=True)
    started = time.monotonic()
    slow = run(["bash", "-c", "sleep 30 & sleep 30; echo done"], str(ws_t), 1, lock=None, tmp_dir=str(scratch / "tmp-timeout"))
    elapsed = time.monotonic() - started
    report["checks"]["timeout"] = {"ok": slow["timed_out"] and elapsed < 1 + TERM_GRACE_S + 5 and "done" not in slow["stdout"],
                                   "timed_out": slow["timed_out"], "elapsed": round(elapsed, 2), "exit_code": slow["exit_code"]}
    report["ok"] = all(c["ok"] for c in report["checks"].values())
    return report


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "selftest":
        report = selftest(argv[1])
        print(json.dumps(report, indent=2, default=str))
        return 0 if report["ok"] else 1
    print("usage: confine.py selftest <scratch_dir>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
