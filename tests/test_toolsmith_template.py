"""The toolsmith template: the catalog, check 6 and repair on a candidate, tool_call through a fake confined runner
(and once through a real bubblewrap, when available), and finalize. Scratch directories live outside /tmp because
check 6 runs under bubblewrap with an empty tmpfs at /tmp."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import types
import zlib

import pytest

from hermit import Session
from hermit.spec import check_spec, repair

ROOT = Path(__file__).resolve().parents[1] / "hermit" / "algorithms" / "templates" / "toolsmith"
NODE = Path.home() / ".nvm" / "versions" / "node" / "v22.23.3" / "bin" / "node"

WORDCOUNT = {"name": "wordcount", "description": "Bytes and lines of a file; default index.html.",
             "parameters": {"type": "object", "properties": {"file": {"type": "string"}}, "required": [],
                            "additionalProperties": False},
             "entry": "run.py", "test": "test.py", "example_args": {}, "timeout_s": 10}
RUN_PY = ("import json, sys\n"
          "raw = sys.stdin.read()\n"
          "args = json.loads(raw) if raw.strip() else {}\n"
          "name = args.get('file', 'index.html')\n"
          "data = open(name, 'rb').read()\n"
          "print('progress line')\n"
          "lines = data.count(b'\\n')\n"
          "print(json.dumps({'text': f'{name}: {len(data)} bytes, {lines} lines', 'images': args.get('images', [])}))\n")
TEST_PY = ("import json, subprocess, sys\n"
           "open('wc_test.txt', 'w').write('one\\ntwo\\n')\n"
           "argv = [sys.executable, '-I', '-B', '/tool/run.py']\n"
           "r = subprocess.run(argv, input=b'{\"file\": \"wc_test.txt\"}', capture_output=True)\n"
           "assert r.returncode == 0, r.stderr\n"
           "assert json.loads(r.stdout.decode().strip().splitlines()[-1])['text'] == 'wc_test.txt: 8 bytes, 2 lines'\n")


@pytest.fixture
def scratch():
    base = Path(os.path.realpath(Path.home() / ".cache" / "hermit-tests" / "toolsmith"))
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def interface():
    """The template's interface.py from source (as the loader and check.py do: no bytecode written into the template)."""
    path = ROOT / "interface.py"
    module = types.ModuleType("toolsmith_interface_under_test")
    module.__file__ = str(path)
    exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    return module


def bundle(adapter: Path, spec: dict, files: dict[str, str]) -> Path:
    directory = adapter / "tools" / spec["name"]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "TOOL.json").write_text(json.dumps(spec))
    for name, text in files.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(text)
    return directory


def png(width: int = 4, height: int = 4) -> bytes:
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes((30, 120, 200)) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_catalog_admits_alphabetically_and_ignores_non_tools(scratch):
    iface = interface()
    adapter = scratch / "adapter"
    shutil.copytree(ROOT, adapter)
    (adapter / "tools" / "_drafts").mkdir()
    (adapter / "tools" / "_drafts" / "run.py").write_text("x = 1\n")
    (adapter / "tools" / "notes").mkdir()
    (adapter / "tools" / "notes" / "ideas.md").write_text("no TOOL.json here\n")
    for i in range(10):
        bundle(adapter, {**WORDCOUNT, "name": f"tool_{i:02d}", "test": None}, {"run.py": RUN_PY})
    bundle(adapter, {**WORDCOUNT, "name": "broken", "entry": "missing.py", "test": None}, {})
    admitted, rejected = iface.catalog(adapter)
    assert [t["name"] for t in admitted] == [f"tool_{i:02d}" for i in range(8)]
    assert admitted[0]["timeout_s"] == 10 and admitted[0]["entry"] == "run.py" and admitted[0]["test"] is None
    assert {r["name"]: r["problem"] for r in rejected} == {
        "broken": "\"entry\" names 'missing.py', which does not exist in the bundle",
        "tool_08": "too many tools", "tool_09": "too many tools"}
    listing = Session(adapter).call("tool_catalog", {})
    assert [t["name"] for t in listing["tools"]] == [f"tool_{i:02d}" for i in range(8)]
    assert set(listing["tools"][0]) == {"name", "description", "parameters"} and len(listing["rejected"]) == 3


def test_check_six_names_the_bundle_and_repair_keeps_the_rest(scratch):
    parent = scratch / "parent"
    shutil.copytree(ROOT, parent)
    adapter = scratch / "adapter"
    shutil.copytree(ROOT, adapter)
    (adapter / "ADAPTER.md").write_text("Call wordcount before you finish.\n")
    bundle(adapter, WORDCOUNT, {"run.py": RUN_PY, "test.py": TEST_PY})
    bundle(adapter, {**WORDCOUNT, "name": "bad", "test": None, "timeout_s": 0}, {"run.py": RUN_PY})
    (adapter / "tools" / "loose.md").write_text("notes with <html> inside\n")
    result = check_spec(adapter, ROOT)
    assert not result.passed
    assert {(i.path, i.check) for i in result.issues} == {("tools/bad/TOOL.json", 6), ("tools/loose.md", 6)}
    final, repaired = repair(adapter, ROOT, parent)
    assert final.passed, final.issues
    assert {r["path"] for r in repaired} == {"tools/bad/TOOL.json", "tools/loose.md"}
    # Repair removes the named file only: the bad bundle loses its TOOL.json (so it is no longer a tool; its run.py
    # stays as an ignored learnable file), the stray page note is gone, the good bundle and the instructions remain.
    assert not (adapter / "tools" / "bad" / "TOOL.json").exists() and not (adapter / "tools" / "loose.md").exists()
    assert (adapter / "tools" / "wordcount" / "run.py").exists()
    assert [t["name"] for t in interface().catalog(adapter)[0]] == ["wordcount"]
    assert (adapter / "ADAPTER.md").read_text() == "Call wordcount before you finish.\n"
    # A learner may not touch the fixed seed text under tools/ (check 1), and a check-6 issue never lacks a path.
    (adapter / "tools" / "README.md").write_text("rewritten\n")
    assert any(i.path == "tools/README.md" and i.check == 1 for i in check_spec(adapter, ROOT, deploy=False).issues)


# --- tool_call ---------------------------------------------------------------------------------------------------------
def test_tool_call_never_runs_without_a_confined_runner(scratch):
    adapter = scratch / "adapter"
    shutil.copytree(ROOT, adapter)
    bundle(adapter, WORDCOUNT, {"run.py": RUN_PY, "test.py": TEST_PY})
    with pytest.raises(RuntimeError, match="no confined runner bound"):
        Session(adapter).call("tool_call", {"name": "wordcount"})
    with pytest.raises(RuntimeError, match="no confined runner bound"):
        Session(adapter, resources={"tool_runner": lambda **kw: {}}).call("tool_call", {"name": "wordcount"})
    with pytest.raises(RuntimeError, match="not a directory"):
        Session(adapter, resources={"tool_runner": lambda **kw: {}, "workspace": str(scratch / "nope")}).call(
            "tool_call", {"name": "wordcount"})


class FakeRunner:
    """Records the call and answers with a scripted result; writes nothing itself."""

    def __init__(self, **reply):
        self.calls = []
        self.reply = {"exit_code": 0, "stdout": "", "stderr": "", "seconds": 0.1, "timed_out": False,
                      "lock_wait_s": 0.0, "dropped_bytes": 0, "bwrap_argv_sha": "abc", "browser_launched": False,
                      "error": None, **reply}

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.reply)


def session(adapter: Path, workspace: Path, runner) -> Session:
    return Session(adapter, resources={"tool_runner": runner, "workspace": str(workspace)})


def test_tool_call_protocol_and_image_validation(scratch):
    adapter = scratch / "adapter"
    shutil.copytree(ROOT, adapter)
    bundle(adapter, WORDCOUNT, {"run.py": RUN_PY, "test.py": TEST_PY})
    ws = scratch / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "shot.png").write_bytes(png())
    (ws / "sub" / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0" + bytes(64))
    (ws / "link.png").symlink_to(ws / "shot.png")
    (ws / "text.png").write_text("not an image")
    (ws / "big.png").write_bytes(png()[:8] + bytes(2_000_001))
    outside = scratch / "outside.png"
    outside.write_bytes(png())
    (ws / "outdir").symlink_to(scratch)
    listed = ["shot.png", str(ws / "sub" / "photo.jpg"), "link.png", "text.png", "big.png", str(outside),
              "../outside.png", "outdir/outside.png", "missing.png", "shot.png", ""]
    reply = {"text": "wordcount ran", "images": listed}
    runner = FakeRunner(stdout="progress line\n{\"text\": \"not last\"}\n" + json.dumps(reply) + "\n", stderr="warn\n")
    result = session(adapter, ws, runner).call("tool_call", {"name": "wordcount", "args": {"file": "index.html"}})
    call = runner.calls[0]
    assert call["cwd"] == str(ws) and call["timeout_s"] == 10 and call["lock"] == "shared"
    assert call["ro"] == [[str(adapter / "tools" / "wordcount"), "/tool"]]
    argv = call["argv"]
    assert argv[0] == "python3" and argv[1:3] == ["-I", "-B"] and argv[-2] == "/tool/run.py"
    assert json.loads(argv[-1]) == {"file": "index.html"}
    assert result["text"] == "wordcount ran" and result["error"] is None and result["exit_code"] == 0
    assert result["images"] == ["shot.png"]                      # PNG only: the JPEG is reported, not shown
    rejected = {r["path"]: r["problem"] for r in result["images_rejected"]}
    assert rejected[str(ws / "sub" / "photo.jpg")] == "not a PNG file (only PNG images are shown)"
    assert rejected["link.png"] == "a symbolic link" and rejected["text.png"] == "not a PNG file (only PNG images are shown)"
    assert "more than" in rejected["big.png"] and rejected["missing.png"] == "does not exist"
    assert rejected[str(outside)] == "outside the workspace" and rejected["../outside.png"] == "outside the workspace"
    assert rejected["outdir/outside.png"] == "a symbolic link" and rejected[""] == "not a path"
    assert result["stderr_tail"] == "warn\n" and result["seconds"] == 0.1 and result["browser_launched"] is False
    # Interpreter tokens for the other entry kinds, and the argument-size cap.
    iface = interface()
    assert iface.program_argv("run.cjs", {})[:2] == ["node", "-e"] and iface.program_argv("run.mjs", {})[-2] == "/tool/run.mjs"
    assert iface.program_argv("run.sh", {"a": 1})[:2] == ["bash", "-c"] and iface.program_argv("run.sh", {})[-1] == "{}"
    with pytest.raises(ValueError):
        iface.program_argv("run.rb", {})


# --- one real confined run -----------------------------------------------------------------------------------------------
def bwrap_runner(argv, cwd, timeout_s, *, ro=(), extra_env=None, lock="shared", tmp_dir=None, trace=None):
    """A minimal stand-in for confine.run: the host read-only, the workspace read-write, no network, a fresh /tmp.
    Maps the interpreter tokens the way the real runner does."""
    tokens = {"python3": sys.executable, "bash": "/usr/bin/bash", "node": str(NODE)}
    command = ["bwrap", "--unshare-all", "--die-with-parent", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
               "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc"]
    for top in ("/bin", "/sbin", "/lib", "/lib64"):            # symlinks into /usr on this host; directories elsewhere
        if os.path.islink(top):
            command += ["--symlink", os.readlink(top), top]
        elif os.path.isdir(top):
            command += ["--ro-bind", top, top]
    for prefix in {sys.prefix, sys.base_prefix, str(NODE.parents[1])}:
        if os.path.isdir(prefix):
            command += ["--ro-bind", prefix, prefix]
    command += ["--bind", cwd, cwd, "--chdir", cwd, "--clearenv", "--setenv", "PATH",
                f"{sys.prefix}/bin:/usr/bin:/bin", "--setenv", "HOME", "/tmp"]
    for src, dst in ro:
        command += ["--ro-bind", src, dst]
    try:
        run = subprocess.run(command + [tokens.get(argv[0], argv[0]), *argv[1:]], capture_output=True, text=True,
                             timeout=timeout_s, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        return {"exit_code": -9, "stdout": exc.stdout or "", "stderr": exc.stderr or "", "seconds": timeout_s,
                "timed_out": True, "lock_wait_s": 0.0, "error": None}
    return {"exit_code": run.returncode, "stdout": run.stdout, "stderr": run.stderr, "seconds": 0.0,
            "timed_out": False, "lock_wait_s": 0.0, "error": None}


@pytest.mark.skipif(not shutil.which("bwrap"), reason="bubblewrap is not installed")
def test_tools_run_confined_in_every_language(scratch):
    adapter = scratch / "adapter"
    shutil.copytree(ROOT, adapter)
    shot = ("import json, struct, sys, zlib\n"
            "raw = sys.stdin.read(); args = json.loads(raw) if raw.strip() else {}\n"
            "def chunk(k, b): return struct.pack('>I', len(b)) + k + b + struct.pack('>I', zlib.crc32(k + b) & 0xffffffff)\n"
            "rows = b''.join(b'\\x00' + bytes((30, 120, 200)) * 4 for _ in range(4))\n"
            "head = b'\\x89PNG\\r\\n\\x1a\\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 4, 4, 8, 2, 0, 0, 0))\n"
            "png = head + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b'')\n"
            "open('shot.png', 'wb').write(png)\n"
            "open('/tool/TOOL.json').read()\n"
            f"import os; assert not os.path.exists({str(adapter)!r}), 'the adapter is visible only at /tool'\n"
            "size = len(open('index.html', 'rb').read())\n"
            "print(json.dumps({'text': 'tag=' + str(args.get('tag', 'none')) + ' index=' + str(size), 'images': ['shot.png']}))\n")
    bundle(adapter, {**WORDCOUNT, "name": "shot", "test": None,
                     "parameters": {"type": "object", "properties": {"tag": {"type": "string"}}, "required": []}},
           {"run.py": shot})
    open_schema = {"type": "object", "properties": {}, "required": []}        # any keys: the arguments pass through
    bundle(adapter, {**WORDCOUNT, "name": "shell", "entry": "run.sh", "test": None, "parameters": open_schema},
           {"run.sh": "read -r line\nprintf '{\"text\": \"got %s\"}\\n' \"$(printf '%s' \"$line\" | tr -d '\"{}')\"\n"})
    bundle(adapter, {**WORDCOUNT, "name": "wordcount"}, {"run.py": RUN_PY, "test.py": TEST_PY})
    if NODE.is_file():
        bundle(adapter, {**WORDCOUNT, "name": "jstool", "entry": "run.cjs", "test": None, "parameters": open_schema},
               {"run.cjs": "const raw = require('fs').readFileSync(0, 'utf8');\n"
                           "const args = raw.trim() ? JSON.parse(raw) : {};\n"
                           "console.log(JSON.stringify({text: 'js:' + JSON.stringify(args)}));\n"})
    ws = scratch / "ws"
    ws.mkdir()
    (ws / "index.html").write_text("hello\nworld\n")
    s = session(adapter, ws, bwrap_runner)
    result = s.call("tool_call", {"name": "shot", "args": {"tag": "t1"}})
    assert result["error"] is None, result
    assert result["text"] == "tag=t1 index=12" and result["images"] == ["shot.png"] and (ws / "shot.png").stat().st_size > 0
    assert s.call("tool_call", {"name": "shot"})["text"] == "tag=none index=12"
    assert s.call("tool_call", {"name": "wordcount", "args": {}})["text"] == "index.html: 12 bytes, 2 lines"
    assert s.call("tool_call", {"name": "shell", "args": {"k": "v"}})["text"] == "got k:v"
    if NODE.is_file():
        assert s.call("tool_call", {"name": "jstool", "args": {"n": 1}})["text"] == 'js:{"n":1}'
    # A test program follows the same protocol (no arguments; exit 0 = pass) and may spawn the entry itself.
    run = bwrap_runner(["python3", "-I", "-B", "/tool/test.py"], str(ws), 30,
                       ro=[[str(adapter / "tools" / "wordcount"), "/tool"]])
    assert run["exit_code"] == 0, run["stderr"]


def test_finalize_submits_the_last_version_the_agent_saw():
    session = Session(ROOT)
    assert session.has_tool("finalize")
    assert session.call("finalize", {"final_sha": "f", "seen": []}) == {
        "submit": "f", "replaced": False, "reason": "no render of the work reached the agent"}
    kept = session.call("finalize", {"final_sha": "a", "seen": [{"sha": "a", "call": 3}, {"sha": "b", "call": 7}]})
    assert kept["submit"] == "a" and kept["replaced"] is False       # an earlier version it saw: the agent went back
    replaced = session.call("finalize", {"final_sha": "x", "seen": [{"sha": "a", "call": 3}, {"sha": "b", "call": 7}]})
    assert replaced["submit"] == "b" and replaced["replaced"] is True and "last version it saw" in replaced["reason"]
