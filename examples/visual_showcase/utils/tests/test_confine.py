"""confine: the bubblewrap recipe and child environment, the argv rules, output caps, the timeout path, the shared
lock, read-only binds, browser-launch detection, the wrapper, and (one real launch, VISUAL_SKIP_BROWSER=1 skips it)
GPU WebGL through the wrapper. Scratch directories live outside /tmp because the sandbox replaces /tmp."""
from __future__ import annotations

import fcntl
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

import pytest

UTILS = Path(__file__).resolve().parents[1]
HAS_BWRAP = shutil.which("bwrap") is not None
needs_bwrap = pytest.mark.skipif(not HAS_BWRAP, reason="bubblewrap is not installed")
needs_browser = pytest.mark.skipif(not HAS_BWRAP or bool(os.environ.get("VISUAL_SKIP_BROWSER")),
                                   reason="one real Chromium launch; VISUAL_SKIP_BROWSER=1 skips it")


def _load():
    spec = importlib.util.spec_from_file_location("visual_confine", UTILS / "confine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    confine = _load()
except RuntimeError as exc:          # node, Chromium or node_modules missing on this machine
    pytest.skip(f"confine cannot resolve its paths here: {exc}", allow_module_level=True)


@pytest.fixture
def scratch():
    base = Path(os.path.realpath(Path.home() / ".cache" / "hermit-tests" / "confine"))
    base.mkdir(parents=True, exist_ok=True)
    path = Path(os.path.realpath(tempfile.mkdtemp(dir=base)))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)


def _ws(scratch: Path, name: str = "ws") -> str:
    path = scratch / name
    path.mkdir(exist_ok=True)
    return str(path)


def test_import_resolved_real_paths_without_running_anything():
    assert os.path.isfile(confine.NODE) and os.access(confine.NODE, os.X_OK)
    assert os.path.isfile(confine.PYTHON) and confine.PYTHON.startswith(confine.PREFIX)
    assert os.path.isfile(confine.CHROME_BIN) and "chromium-" in confine.CHROME_BIN
    assert os.path.isdir(confine.NODE_MODULES) and os.path.isfile(confine.WRAPPER)
    assert confine.LOCK.endswith("capture.lock")
    assert confine.PLAYWRIGHT_DIR == os.path.dirname(os.path.dirname(os.path.dirname(confine.CHROME_BIN)))
    assert confine.INTERPRETERS == {"python3": confine.PYTHON, "node": confine.NODE, "bash": "/usr/bin/bash"}


@needs_bwrap
def test_bwrap_argv_is_the_pinned_allowlist_and_the_env_is_explicit(scratch, monkeypatch):
    monkeypatch.setenv("FIREWORKS_API_KEY", "not-for-the-sandbox")
    monkeypatch.setenv("SOME_HOST_VARIABLE", "leak")
    cwd, tmp = _ws(scratch), str(scratch / "tmp")
    tool = scratch / "tool"
    tool.mkdir()
    argv, env = confine.bwrap_argv(cwd, [(str(tool), "/tool")], tmp, {"NODE_PATH": "/x", "FIREWORKS_API_KEY": "no",
                                                                        "HF_TOKEN": "no"}, trace=str(scratch / "trace"))
    assert argv[:4] == ["bwrap", "--unshare-all", "--die-with-parent", "--new-session"]
    joined = " ".join(argv)
    for piece in ("--dev /dev", "--proc /proc", "--ro-bind /sys /sys", "--ro-bind /usr /usr", "--clearenv",
                  f"--ro-bind {confine.PREFIX} {confine.PREFIX}", f"--ro-bind {confine.NODE_DIR} {confine.NODE_DIR}",
                  f"--ro-bind {confine.PLAYWRIGHT_DIR} {confine.PLAYWRIGHT_DIR}",
                  f"--ro-bind {confine.NODE_MODULES} {confine.NODE_MODULES}", f"--ro-bind {confine.WRAPPER} {confine.WRAPPER}",
                  f"--bind {os.path.realpath(tmp)} /tmp", f"--bind {scratch / 'trace'} /tmp/trace",
                  f"--ro-bind {tool} /tool", f"--bind {cwd} {cwd} --chdir {cwd}"):
        assert piece in joined, piece
    assert joined.index("--bind " + os.path.realpath(tmp) + " /tmp") < joined.index("--ro-bind " + str(tool) + " /tool")
    for link in ("/bin", "/lib", "/lib64", "/sbin"):
        if os.path.islink(link):
            assert f"--symlink {os.readlink(link)} {link}" in joined
    if os.path.exists("/dev/dri"):
        assert "--dev-bind /dev/dri /dev/dri" in joined
    gpus = [i for i in range(16) if f"--dev-bind /dev/nvidia{i} /dev/nvidia{i}" in joined]
    assert gpus == ([confine.GPU_INDEX] if os.path.exists(f"/dev/nvidia{confine.GPU_INDEX}") else [])   # one GPU only
    assert "--ro-bind / /" not in joined and "/etc/resolv.conf" not in joined and "/etc/ssl" not in joined
    for name in confine.HIDDEN_UNDER_BASE:                 # other conda environments and the package cache are hidden
        if os.path.isdir(os.path.join(confine.BASE_PREFIX, name)):
            assert f"--tmpfs {os.path.join(confine.BASE_PREFIX, name)}" in joined
            assert joined.index(f"--ro-bind {confine.BASE_PREFIX} ") < joined.index(f"--tmpfs {os.path.join(confine.BASE_PREFIX, name)}")
    assert env == {"PATH": f"{confine.PREFIX}/bin:/usr/bin:/bin", "HOME": "/tmp/home", "TMPDIR": "/tmp",
                   "XDG_CACHE_HOME": "/tmp/cache", "XDG_CONFIG_HOME": "/tmp/config", "LANG": "C.UTF-8", "TERM": "dumb",
                   "VISUAL_TRACE": "/tmp/trace", "NODE_PATH": "/x"}
    for key, value in env.items():
        assert f"--setenv {key} {value}" in joined
    with pytest.raises(ValueError, match="/tmp"):
        confine.bwrap_argv("/tmp", [], tmp, None)
    with pytest.raises(ValueError, match="does not exist"):
        confine.bwrap_argv(cwd, [(str(scratch / "missing"), "/tool")], tmp, None)


def test_argv0_rules(scratch):
    cwd, tmp = _ws(scratch), str(scratch / "tmp")
    assert confine._resolve_argv0(["python3", "-c", "1"], cwd, [], tmp) == [confine.PYTHON, "-c", "1"]
    assert confine._resolve_argv0(["node", "x.cjs"], cwd, [], tmp)[0] == confine.NODE
    assert confine._resolve_argv0(["bash", "-c", "true"], cwd, [], tmp)[0] == "/usr/bin/bash"
    assert confine._resolve_argv0(["/usr/bin/ls", "-l"], cwd, [], tmp) == ["/usr/bin/ls", "-l"]
    assert confine._resolve_argv0([confine.WRAPPER, "--version"], cwd, [], tmp)[0] == confine.WRAPPER
    tool = scratch / "tool"
    tool.mkdir()
    (tool / "run.sh").write_text("#!/usr/bin/bash\n")
    assert confine._resolve_argv0(["/tool/run.sh"], cwd, [(str(tool), "/tool")], tmp) == ["/tool/run.sh"]
    with pytest.raises(ValueError, match="absolute path"):
        confine._resolve_argv0(["ls"], cwd, [], tmp)
    with pytest.raises(ValueError, match="not inside"):
        confine._resolve_argv0([str(Path.home() / ".bashrc")], cwd, [], tmp)
    with pytest.raises(ValueError, match="not a file"):
        confine._resolve_argv0(["/tool/missing.sh"], cwd, [(str(tool), "/tool")], tmp)
    with pytest.raises(ValueError, match="empty"):
        confine._resolve_argv0([], cwd, [], tmp)


@needs_bwrap
def test_run_shows_only_the_allowlist_and_writes_only_cwd_and_tmp(scratch):
    cwd, tmp = _ws(scratch), scratch / "tmp"
    repo = UTILS.parents[2]
    script = "; ".join([
        "echo home=$HOME", "echo cwd=$(pwd)", "echo names=$(env | cut -d= -f1 | sort | tr '\\n' ',')",
        f"test -e {Path.home() / '.config'} && echo config=present || echo config=absent",
        f"test -e {repo / 'hermit'} && echo repo=present || echo repo=absent",
        f"test -e {Path.home() / '.cache' / 'visual_showcase'} && echo lockdir=present || echo lockdir=absent",
        f"test -x {confine.NODE} && echo node=ok", f"test -d {confine.NODE_MODULES}/playwright && echo pw=ok",
        "echo made > made.txt", "echo scratch > /tmp/scratch.txt", "touch /usr/nope 2>/dev/null && echo usr=rw || echo usr=ro",
        "(timeout 3 bash -c 'echo > /dev/tcp/1.1.1.1/80' >/dev/null 2>&1 && echo net=open) || echo net=blocked",
        "echo pids=$(ls /proc | grep -c '^[0-9]')", "python3 -I -c 'import sys, jsonschema, yaml; print(\"py=\" + sys.prefix)'"])
    result = confine.run(["bash", "-c", script], cwd, 30, lock=None, tmp_dir=str(tmp))
    assert result["exit_code"] == 0 and result["error"] is None and not result["timed_out"], result
    out = dict(line.split("=", 1) for line in result["stdout"].splitlines() if "=" in line)
    assert out["home"] == "/tmp/home" and out["cwd"] == cwd
    names = set(out["names"].strip(",").split(","))
    assert {"PATH", "HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "LANG", "TERM"} <= names
    assert not any(n.startswith(("VISUAL_CHROME", "PLAYWRIGHT", "NODE")) for n in names)    # the agent is told nothing
    assert not any("API_KEY" in n for n in names)
    assert out["config"] == "absent" and out["repo"] == "absent" and out["lockdir"] == "absent"
    assert out["node"] == "ok" and out["pw"] == "ok" and out["usr"] == "ro" and out["net"] == "blocked"
    assert int(out["pids"]) < 10                      # its own pid namespace
    assert out["py"] == confine.PREFIX                # the venv python, not the base interpreter
    assert (Path(cwd) / "made.txt").read_text() == "made\n"
    assert (tmp / "scratch.txt").read_text() == "scratch\n" and (tmp / "home").is_dir() and (tmp / "cache").is_dir()
    assert len(result["bwrap_argv_sha"]) == 64 and result["browser_launched"] is False and result["lock_wait_s"] == 0.0


@needs_bwrap
def test_output_is_capped_to_head_and_tail_with_a_dropped_count(scratch):
    cwd = _ws(scratch)
    small = confine.run(["bash", "-c", "printf 'hello\\n'; printf 'oops\\n' >&2; exit 3"], cwd, 20, lock=None,
                        tmp_dir=str(scratch / "t1"))
    assert small["exit_code"] == 3 and small["stdout"] == "hello\n" and small["stderr"] == "oops\n" and small["dropped_bytes"] == 0
    big = confine.run(["bash", "-c", "head -c 50000 /dev/zero | tr '\\0' a; printf END"], cwd, 20, lock=None,
                      tmp_dir=str(scratch / "t2"))
    assert big["exit_code"] == 0
    assert big["stdout"].startswith("a" * confine.HEAD_BYTES) and big["stdout"].endswith("a" * (confine.TAIL_BYTES - 3) + "END")
    assert "bytes dropped" in big["stdout"] and big["dropped_bytes"] == 50003 - confine.HEAD_BYTES - confine.TAIL_BYTES
    assert len(big["stdout"]) < confine.HEAD_BYTES + confine.TAIL_BYTES + 100


@needs_bwrap
def test_timeout_kills_the_whole_tree(scratch):
    cwd = _ws(scratch)
    marker = f"sleep 1234.{os.getpid()}"

    def count() -> int:
        found = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", marker], capture_output=True, text=True).stdout.split()
        return len(found)
    before = count()
    started = time.monotonic()
    result = confine.run(["bash", "-c", f"{marker} & {marker}; echo done"], cwd, 1, lock=None, tmp_dir=str(scratch / "t"))
    elapsed = time.monotonic() - started
    assert result["timed_out"] is True and "timed out" in result["error"] and "done" not in result["stdout"]
    assert result["exit_code"] != 0 and elapsed < 1 + confine.TERM_GRACE_S + 5
    time.sleep(0.3)
    assert count() == before, "processes survived the kill"


@needs_bwrap
def test_shared_lock_waits_for_the_hosts_exclusive_lock_but_not_forever(scratch, monkeypatch):
    cwd = _ws(scratch)
    lock = scratch / "lock" / "capture.lock"
    monkeypatch.setattr(confine, "LOCK", str(lock))
    lock.parent.mkdir()

    def hold(seconds: float, started: threading.Event):
        with open(lock, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            started.set()
            time.sleep(seconds)
    ready = threading.Event()
    holder = threading.Thread(target=hold, args=(1.0, ready))
    holder.start()
    ready.wait(5)
    result = confine.run(["bash", "-c", "echo ran"], cwd, 20, lock="shared", tmp_dir=str(scratch / "t1"))
    holder.join()
    assert result["stdout"] == "ran\n" and 0.5 <= result["lock_wait_s"] <= 5 and result["seconds"] < 1
    monkeypatch.setattr(confine, "LOCK_WAIT_S", 0.5)
    ready = threading.Event()
    holder = threading.Thread(target=hold, args=(2.0, ready))
    holder.start()
    ready.wait(5)
    busy = confine.run(["bash", "-c", "echo ran"], cwd, 20, lock="shared", tmp_dir=str(scratch / "t2"))
    holder.join()
    assert busy == {**busy, "exit_code": -1, "error": "host busy, retry", "stdout": "", "browser_launched": False}
    assert busy["lock_wait_s"] >= 0.5
    free = confine.run(["bash", "-c", "echo ran"], cwd, 20, lock=None, tmp_dir=str(scratch / "t3"))
    assert free["stdout"] == "ran\n" and free["lock_wait_s"] == 0.0
    with pytest.raises(KeyError):
        confine.run(["bash", "-c", "true"], cwd, 5, lock="bogus", tmp_dir=str(scratch / "t4"))


@needs_bwrap
def test_run_tool_binds_the_tool_read_only_and_runs_interpreters(scratch):
    cwd = _ws(scratch)
    tool = scratch / "tools" / "wordcount"
    tool.mkdir(parents=True)
    (tool / "run.py").write_text("import json, os, sys\nargs = json.load(sys.stdin)\n"
                                 "print(json.dumps({'text': 'cwd=' + os.getcwd() + ' k=' + args.get('k', '-')}))\n")
    (tool / "run.cjs").write_text("console.log(JSON.stringify({text: 'node ' + process.version + ' ' + process.cwd()}))\n")
    probe = "cat /tool/run.cjs >/dev/null && echo readable; touch /tool/x 2>&1 || echo readonly"
    result = confine.run_tool(argv=["bash", "-c", probe], cwd=cwd, timeout_s=20, ro=[[str(tool), "/tool"]], lock=None,
                              tmp_dir=str(scratch / "t1"))
    assert "readable" in result["stdout"] and "readonly" in result["stdout"]
    py = confine.run_tool(argv=["python3", "-I", "-B", "/tool/run.py"], cwd=cwd, timeout_s=20, ro=[[str(tool), "/tool"]],
                          lock=None, tmp_dir=str(scratch / "t2"))
    assert py["exit_code"] != 0 or py["stdout"].startswith("{")      # stdin is /dev/null: json.load fails; the path resolved
    node = confine.run_tool(argv=["node", "/tool/run.cjs"], cwd=cwd, timeout_s=20, ro=[[str(tool), "/tool"]], lock=None,
                            tmp_dir=str(scratch / "t3"))
    assert node["exit_code"] == 0 and node["stdout"].startswith('{"text":"node v') and cwd in node["stdout"]
    assert set(node) == {"exit_code", "stdout", "stderr", "seconds", "timed_out", "lock_wait_s", "dropped_bytes",
                         "bwrap_argv_sha", "browser_launched", "error"}


@needs_bwrap
def test_browser_launched_comes_from_the_trace_or_profile_artefacts_without_following_symlinks(scratch):
    cwd = _ws(scratch)
    trace = scratch / "trace"
    traced = confine.run(["bash", "-c", "echo \"launch $(date -u +%FT%TZ) $$\" >> \"$VISUAL_TRACE\""], cwd, 20, lock=None,
                         tmp_dir=str(scratch / "t1"), trace=str(trace))
    assert traced["exit_code"] == 0 and traced["browser_launched"] is True and trace.read_text().startswith("launch ")
    profiled = confine.run(["bash", "-c", "mkdir -p /tmp/playwright_chromiumdev_profile-abc"], cwd, 20, lock=None,
                           tmp_dir=str(scratch / "t2"))
    assert profiled["browser_launched"] is True
    deep = confine.run(["bash", "-c", "mkdir -p a/b/c/Default && touch a/b/c/Default/Preferences"], cwd, 20, lock=None,
                       tmp_dir=str(scratch / "t3"))
    assert deep["browser_launched"] is True
    shutil.rmtree(Path(cwd) / "a")
    elsewhere = scratch / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "DevToolsActivePort").write_text("1\n")
    os.symlink(elsewhere, Path(cwd) / "link")
    plain = confine.run(["bash", "-c", "true"], cwd, 20, lock=None, tmp_dir=str(scratch / "t4"))
    assert plain["browser_launched"] is False
    own_tmp = confine.run(["bash", "-c", "echo x > /tmp/y && cat /tmp/y"], cwd, 20, lock=None)
    assert own_tmp["stdout"] == "x\n"


def test_wrapper_prefers_visual_chrome_bin_adds_the_angle_flags_and_traces(scratch):
    fake = scratch / "fake-chrome"
    fake.write_text("#!/usr/bin/bash\nprintf '%s\\n' \"$@\"\n")
    fake.chmod(0o755)
    trace = scratch / "trace"
    env = {"PATH": "/usr/bin:/bin", "VISUAL_CHROME_BIN": str(fake), "VISUAL_TRACE": str(trace), "HOME": "/nonexistent"}
    out = subprocess.run([confine.WRAPPER, "--headless", "--x=1"], capture_output=True, text=True, env=env, timeout=20)
    assert out.returncode == 0
    assert out.stdout.split("\n")[:5] == ["--use-gl=angle", "--use-angle=gl-egl", "--ignore-gpu-blocklist",
                                           "--headless", "--x=1"]
    lines = trace.read_text().splitlines()
    assert len(lines) == 1 and lines[0].startswith("launch 20") and lines[0].split()[2].isdigit()
    missing = subprocess.run([confine.WRAPPER], capture_output=True, text=True, timeout=20,
                             env={"PATH": "/usr/bin:/bin", "VISUAL_CHROME_BIN": str(scratch / "nope")})
    assert missing.returncode == 127 and "VISUAL_CHROME_BIN" in missing.stderr
    assert "$HOME" not in Path(confine.WRAPPER).read_text().split("exec ")[-1]


def test_isolation_targets_and_cli_usage():
    targets, skipped = confine._isolation_targets()
    assert str(Path.home() / ".config") in targets and str(UTILS.parents[2] / "hermit") in targets
    assert os.path.dirname(confine.LOCK) in targets
    for path in targets:
        assert not any(b.startswith(path + "/") for b in confine._system_visible()), path
    assert confine.main([]) == 2
    assert confine._last_json("junk\n{\"a\": 1}\nnot json\n") == {"a": 1} and confine._last_json("nothing") is None


@needs_browser
def test_gpu_webgl_renders_the_probe_through_the_wrapper(scratch):
    check = confine.pixel_check(scratch, executable=confine.WRAPPER, name="gpu")
    assert check["exit_code"] == 0 and check["ready"] is True, check
    assert check["matches_target"] and check["center"] and not check["white"], check
    assert check["browser_launched"] is True and check["launches"] == 1
    assert check["renderer"] and "SwiftShader" not in check["renderer"]
    assert Path(check["png"]).stat().st_size > 1000
