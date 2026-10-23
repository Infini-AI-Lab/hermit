"""confine with the real bubblewrap: what a command sees and may write (no keys, no network), the timeout path, and the
read-only binds of a tool. Scratch directories live outside /tmp because the sandbox replaces /tmp."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import pytest

UTILS = Path(__file__).resolve().parents[1]
HAS_BWRAP = shutil.which("bwrap") is not None
needs_bwrap = pytest.mark.skipif(not HAS_BWRAP, reason="bubblewrap is not installed")


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


@needs_bwrap
def test_run_shows_only_the_allowlist_and_writes_only_cwd_and_tmp(scratch, monkeypatch):
    monkeypatch.setenv("FIREWORKS_API_KEY", "not-for-the-sandbox")
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
    result = confine.run(["bash", "-c", script], cwd, 30, lock=None, tmp_dir=str(tmp), extra_env={"HF_TOKEN": "no"})
    assert result["exit_code"] == 0 and result["error"] is None and not result["timed_out"], result
    out = dict(line.split("=", 1) for line in result["stdout"].splitlines() if "=" in line)
    assert out["home"] == "/tmp/home" and out["cwd"] == cwd
    names = set(out["names"].strip(",").split(","))
    assert {"PATH", "HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "LANG", "TERM"} <= names
    # The agent is told nothing about the browser; a Chromium outside the wrapper's default needs VISUAL_CHROME_BIN.
    wrapper_finds = confine.CHROME_BIN.startswith(confine.WRAPPER_DEFAULT_BROWSERS + "/")
    told = names - (set() if wrapper_finds else {"VISUAL_CHROME_BIN"})
    assert not any(n.startswith(("VISUAL_CHROME", "PLAYWRIGHT", "NODE")) for n in told)
    assert not any(marker in n for n in names for marker in confine.SECRET_MARKERS)       # no key, from the host or extra_env
    assert out["config"] == "absent" and out["repo"] == "absent" and out["lockdir"] == "absent"
    assert out["node"] == "ok" and out["pw"] == "ok" and out["usr"] == "ro" and out["net"] == "blocked"
    assert int(out["pids"]) < 10                      # its own pid namespace
    assert out["py"] == confine.PREFIX                # the venv python, not the base interpreter
    assert (Path(cwd) / "made.txt").read_text() == "made\n"
    assert (tmp / "scratch.txt").read_text() == "scratch\n" and (tmp / "home").is_dir() and (tmp / "cache").is_dir()
    assert len(result["bwrap_argv_sha"]) == 64 and result["browser_launched"] is False and result["lock_wait_s"] == 0.0


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
