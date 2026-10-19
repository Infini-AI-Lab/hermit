"""tool_works.py and emergence.py with fakes, no browser and no bwrap: a scratch toolsmith adapter with one bundle, a
fake confined runner that writes PNGs into the workspace, a fake host capture, the Tree export path on a tiny git
repository, and emergence metrics over a hand-made run record. Scratch lives outside /tmp like the real runs."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import zlib

import pytest

UTILS = Path(__file__).resolve().parents[1]
REPO = UTILS.parents[2]
TEMPLATE = REPO / "hermit" / "algorithms" / "templates" / "toolsmith"
WIDTH, HEIGHT = 400, 300        # above pngstats' 320x240 floor, small enough to keep the pure-python decoder fast


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool_works = _load("visual_tool_works", UTILS / "tool_works.py")
emergence = _load("visual_emergence", UTILS / "emergence.py")


# --- PNG fixtures -----------------------------------------------------------------------------------------------------
def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def png(pixel, width: int = WIDTH, height: int = HEIGHT) -> bytes:
    """An 8-bit RGB PNG whose pixel (x, y) is pixel(x, y)."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw.extend(pixel(x, y))
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw))) + _chunk(b"IEND", b""))


def probe_pixel(x, y):            # the probe's clear colour with a white control bar at the bottom (not blank)
    return (255, 255, 255) if y >= HEIGHT - 24 else (30, 120, 200)


def pattern_pixel(x, y):          # structured content: what a real page looks like to thumb_corr
    return ((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256)


def white_pixel(x, y):            # SwiftShader's silent failure
    return (255, 255, 255)


PNGS = {"probe": png(probe_pixel), "pattern": png(pattern_pixel), "white": png(white_pixel)}


@pytest.fixture
def scratch():
    base = Path(os.path.realpath(Path.home() / ".cache" / "hermit-tests" / "tool_works"))
    base.mkdir(parents=True, exist_ok=True)
    path = Path(os.path.realpath(tempfile.mkdtemp(dir=base)))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)


def adapter_with(scratch: Path, bundles: dict[str, dict] | None = None) -> Path:
    """A copy of the toolsmith template plus one bundle per name (TOOL.json + run.py)."""
    root = scratch / "adapter"
    shutil.copytree(TEMPLATE, root)
    for name, extra in (bundles if bundles is not None else {"shot": {}}).items():
        bundle = root / "tools" / name
        bundle.mkdir(parents=True)
        spec = {"name": name, "description": f"{name}: draws index.html and returns the image", "entry": "run.py",
                "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                "example_args": {}, "timeout_s": 30, **extra}
        (bundle / "TOOL.json").write_text(json.dumps(spec))
        (bundle / "run.py").write_text('import json\nprint(json.dumps({"text": "ok", "images": ["shot.png"]}))\n')
    return root


class FakeRunner:
    """confine.run's keyword signature; writes shot.png into cwd by what index.html is, never runs anything."""

    def __init__(self, mode: str = "good"):
        self.mode, self.calls = mode, []

    def __call__(self, argv, cwd, timeout_s, *, ro=(), extra_env=None, lock="shared", tmp_dir=None, trace=None):
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout_s": timeout_s, "ro": [list(p) for p in ro],
                           "lock": lock})
        page = (Path(cwd) / "index.html").read_text()
        reply = {"text": "drew index.html", "images": ["shot.png"]}
        if self.mode == "none":
            reply = {"text": "nothing drawn"}
        elif self.mode == "white":
            (Path(cwd) / "shot.png").write_bytes(PNGS["white"])
        else:
            (Path(cwd) / "shot.png").write_bytes(PNGS["probe"] if "WebGL probe" in page else PNGS["pattern"])
        return {"exit_code": 0, "stdout": json.dumps(reply) + "\n", "stderr": "", "seconds": 0.01, "timed_out": False,
                "lock_wait_s": 0.0, "dropped_bytes": 0, "bwrap_argv_sha": "fake", "browser_launched": True,
                "error": None}


def fake_capture(case: str, page: Path, out_dir: Path) -> Path:
    shot = Path(out_dir) / "capture" / "desktop-t0.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(PNGS["pattern"])
    return shot


# --- tool_works -------------------------------------------------------------------------------------------------------
def test_check_passes_when_the_tool_draws_the_probe_colour_and_matches_the_reference(scratch):
    adapter = adapter_with(scratch)
    runner = FakeRunner()
    reference = fake_capture("waves", tool_works.REFERENCE / "best_waves.html", scratch / "ref")
    verdict = tool_works.check(adapter, "waves", runner=runner, reference_png=reference, scratch=scratch / "s",
                               ckpt="waves-ts/ckpt-step-3")
    assert verdict["pass"] is True and verdict["tool"] == "shot" and verdict["ckpt"] == "waves-ts/ckpt-step-3"
    shot = verdict["stats"]["tools"]["shot"]
    assert shot["probe"]["pass"] and shot["reference"]["pass"] and shot["reference"]["best_corr"] >= 0.99
    assert shot["probe"]["images"][0]["dominant_rgb"] == [30, 120, 200] and not shot["probe"]["images"][0]["blank"]
    assert verdict["stats"]["admitted"] == ["shot"] and verdict["stats"]["rejected"] == []
    # the tool ran through the template's tool_call: the bundle bound read-only at /tool, cwd = the seeded workspace
    assert len(runner.calls) == 2
    assert all(c["ro"] == [[str(adapter / "tools" / "shot"), "/tool"]] for c in runner.calls)
    assert all(c["lock"] == "shared" and c["timeout_s"] == 30 and c["argv"][0] == "python3" for c in runner.calls)
    probe_ws, reference_ws = (Path(c["cwd"]) for c in runner.calls)
    assert probe_ws == reference_ws and (reference_ws / "index.html").read_bytes() == \
        (tool_works.REFERENCE / "best_waves.html").read_bytes()


def test_check_fails_on_a_white_frame_and_on_no_image(scratch):
    adapter = adapter_with(scratch)
    reference = fake_capture("lava", tool_works.REFERENCE / "best_lava.html", scratch / "ref")
    white = tool_works.check(adapter, "lava", runner=FakeRunner("white"), reference_png=reference, scratch=scratch / "w")
    assert white["pass"] is False and white["tool"] is None
    assert any("probe: dominant colour [[255, 255, 255]] is not within 10 of [30, 120, 200]" in r
               for r in white["reasons"])
    assert any("reference: best correlation with the host capture 0.0 < 0.8" in r for r in white["reasons"])
    none = tool_works.check(adapter, "lava", runner=FakeRunner("none"), reference_png=reference, scratch=scratch / "n")
    assert none["pass"] is False and sorted(none["reasons"]) == ["shot: probe: no image returned",
                                                                 "shot: reference: no image returned"]


def test_check_reports_absent_and_unrequested_tools(scratch):
    reference = fake_capture("silk", tool_works.REFERENCE / "best_silk.html", scratch / "ref")
    empty = adapter_with(scratch / "e", bundles={})
    verdict = tool_works.check(empty, "silk", runner=FakeRunner(), reference_png=reference, scratch=scratch / "s1")
    assert verdict["pass"] is False and verdict["reasons"] == ["no admitted tools"]
    two = adapter_with(scratch / "t", bundles={"shot": {}, "zzz": {}})
    runner = FakeRunner()
    verdict = tool_works.check(two, "silk", runner=runner, reference_png=reference, scratch=scratch / "s2",
                               tools=["zzz", "nope"])
    assert verdict["pass"] is True and verdict["tool"] == "zzz" and list(verdict["stats"]["tools"]) == ["zzz"]
    assert verdict["reasons"][0] == "nope: not an admitted tool of this checkpoint"
    assert verdict["stats"]["admitted"] == ["shot", "zzz"] and len(runner.calls) == 2
    with pytest.raises(ValueError, match="case must be one of"):
        tool_works.check(two, "fire", runner=runner, reference_png=reference, scratch=scratch / "s3")


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env).stdout


def test_cli_exports_the_tag_and_writes_the_verdict(scratch, capsys):
    tree = adapter_with(scratch / "tree")            # the adapter tree is a git repository with a checkpoint tag
    _git(tree, "init", "-q", "-b", "trunk")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "step 2")
    _git(tree, "tag", "waves-ts/ckpt-step-2")
    out = scratch / "verdict.json"
    code = tool_works.main(["--tree", str(tree), "--tag", "waves-ts/ckpt-step-2", "--case", "waves", "--out", str(out),
                            "--scratch", str(scratch / "work")], runner=FakeRunner(), capture=fake_capture)
    verdict = json.loads(out.read_text())
    assert code == 0 and verdict["pass"] is True and verdict["tool"] == "shot"
    assert verdict["ckpt"] == "waves-ts/ckpt-step-2"
    assert set(verdict) >= {"ckpt", "case", "tool", "pass", "reasons", "stats", "tree", "exported_to"}
    exported = Path(verdict["exported_to"])
    assert (exported / "tools" / "shot" / "TOOL.json").is_file() and not (exported / ".git").exists()
    assert (scratch / "work" / "reference").is_dir()
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["pass"] is True and printed["tool"] == "shot"
    # a failing tool exits 1; a missing tag is a usage error (2); scratch under /tmp is refused
    code = tool_works.main(["--tree", str(tree), "--tag", "waves-ts/ckpt-step-2", "--case", "waves", "--out", str(out),
                            "--scratch", str(scratch / "work")], runner=FakeRunner("white"), capture=fake_capture)
    assert code == 1 and json.loads(out.read_text())["pass"] is False
    code = tool_works.main(["--tree", str(tree), "--tag", "waves-ts/ckpt-step-9", "--case", "waves", "--out", str(out),
                            "--scratch", str(scratch / "work")], runner=FakeRunner(), capture=fake_capture)
    assert code == 2
    with pytest.raises(SystemExit):
        tool_works.main(["--tree", str(tree), "--tag", "waves-ts/ckpt-step-2", "--case", "waves", "--out", str(out),
                         "--scratch", "/tmp/x"], runner=FakeRunner(), capture=fake_capture)


def test_host_capture_reuses_an_existing_desktop_t0(scratch):
    out = scratch / "cap"
    (out / "capture").mkdir(parents=True)
    (out / "capture" / "desktop-t0.png").write_bytes(PNGS["pattern"])
    shot = tool_works.host_capture("waves", tool_works.REFERENCE / "best_waves.html", out)
    assert shot == out / "capture" / "desktop-t0.png"


# --- emergence --------------------------------------------------------------------------------------------------------
def _record(run: Path, nn: int, name: str, images=(), *, launched=True, sha=None, snapshot: str | None = None):
    (run / "tools").mkdir(exist_ok=True)
    record = {"name": name, "arguments": {}, "image_records": [{"file": f, "packed_for_model": True} for f in images],
              "browser_launched": launched, "index_sha_at_call": sha}
    (run / "tools" / f"{nn:02d}.json").write_text(json.dumps(record))
    if snapshot is not None:
        (run / f"cmd-{nn:02d}").mkdir()
        (run / f"cmd-{nn:02d}" / "index.html").write_text(snapshot)


def make_run(root: Path) -> Path:
    run = root / "waves-harness-20261017T000000Z-abc123"
    (run / "workspace").mkdir(parents=True)
    (run / "submission").mkdir()
    (run / "judge").mkdir()
    page_a, page_b = "<!doctype html><title>A</title>", "<!doctype html><title>B</title>"
    (run / "submission" / "index.html").write_text(page_b)
    sha_a, sha_b = (hashlib.sha256(p.encode()).hexdigest() for p in (page_a, page_b))
    assert emergence.sha256(run / "submission" / "index.html") == sha_b
    (run / "workspace" / "shot1.png").write_bytes(PNGS["pattern"])
    (run / "workspace" / "shot2.png").write_bytes(PNGS["white"])
    (run / "request.json").write_text(json.dumps({"case": "waves"}))
    (run / "result.json").write_text(json.dumps({"case": "waves", "arm": "adapted", "status": "completed",
                                                 "stop_reason": "final", "submission_sha256": sha_b,
                                                 "looked_calls": 2, "image_bearing_calls": 3}))
    (run / "judge" / "judge.json").write_text(json.dumps({"score": 0.9}))
    _record(run, 1, "write_file")
    _record(run, 2, "run_command", ["shot1.png"], sha=sha_a, snapshot=page_a)            # looked, matches
    _record(run, 3, "run_command", ["shot2.png"], sha=sha_b)                   # white: looked_blank; sha = submission
    _record(run, 4, "shot", ["shot1.png"], sha=sha_b, snapshot=page_b)         # adapter tool, looked, matches, final
    _record(run, 5, "run_command", ["shot1.png"], launched=False, sha=sha_b, snapshot=page_b)  # PNG, no browser
    return run


class FakeRender:
    def __init__(self):
        self.calls = []

    def __call__(self, case: str, snapshot: Path, out_dir: Path) -> Path:
        self.calls.append((case, Path(snapshot).read_text()))
        out = Path(out_dir) / "capture" / "desktop-t0.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(PNGS["pattern"])
        return out


def test_emergence_metrics_of_one_record(scratch, monkeypatch):
    monkeypatch.setattr(emergence, "_render_dir", lambda key: scratch / "renders" / key)
    run = make_run(scratch)
    render = FakeRender()
    row = emergence.analyse_run(run, render)
    flags = {c["call"]: (c["image_bearing"], c["looked"], c["looked_blank"], c["render_match"]) for c in row["calls"]}
    assert flags == {1: (False, False, False, None), 2: (True, True, False, True), 3: (True, False, True, False),
                     4: (True, True, False, True), 5: (True, False, False, True)}
    assert row["calls"][2]["snapshot"].endswith("submission/index.html")       # sha matched the submission
    assert [c[1] for c in render.calls] == ["<!doctype html><title>A</title>", "<!doctype html><title>B</title>"]
    expected = {"case": "waves", "arm": "adapted", "status": "completed", "score": 0.9, "tool_calls": 5,
                "run_commands": 3, "adapter_tool_calls": 1, "image_bearing_calls": 4, "looked_calls": 2,
                "looked_blank_calls": 1, "render_matched_calls": 3, "render_checked_calls": 4, "first_image_call": 2,
                "first_look_call": 2, "first_render_match_call": 2, "looked_final": True}
    assert {k: row[k] for k in expected} == expected
    assert row["harness_result"]["looked_calls"] == 2 and row["harness_result"]["image_bearing_calls"] == 3


def test_emergence_without_render_and_over_a_step(scratch, monkeypatch):
    monkeypatch.setattr(emergence, "_render_dir", lambda key: scratch / "renders" / key)
    run = make_run(scratch / "a")
    row = emergence.analyse_run(run, None)
    assert row["render_checked_calls"] == 0 and all(c["render_match"] is None for c in row["calls"])
    assert row["looked_calls"] == 2 and row["looked_final"] is True
    step = scratch / "step_0003"
    shutil.copytree(run, step / "trajectories" / "waves")
    (step / "trajectories" / "waves-eval").mkdir()                     # a trajectory without records: skipped
    out = scratch / "emergence.json"
    assert emergence.main(["--step", str(step), "--out", str(out)], render=FakeRender()) == 0
    report = json.loads(out.read_text())
    assert report["rendered"] is True and [r["id"] for r in report["runs"]] == ["waves"]
    assert report["summary"] == {"runs": 1, "tool_calls": 5, "image_bearing_calls": 4, "looked_calls": 2,
                                 "looked_blank_calls": 1, "render_matched_calls": 3, "runs_with_images": 1.0,
                                 "runs_looked": 1.0, "runs_looked_blank_only": 0.0, "runs_render_matched": 1.0,
                                 "runs_looked_final": 1.0, "scores": [0.9]}
    assert emergence.main(["--run", str(run), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["rendered"] is False
    with pytest.raises(SystemExit):
        emergence.main(["--step", str(scratch / "nothing"), "--out", str(out)])


def test_a_long_lock_wait_does_not_eat_the_host_capture_timeout(scratch, monkeypatch):
    """The host capture's timeout bounds the capture, not the wait for the exclusive lock (count_wait=False)."""
    import fcntl
    import subprocess as sp
    import threading
    harness = tool_works.load_by_path("visual_harness_lock_test", tool_works.HERE / "harness.py")
    lock_path = scratch / "capture.lock"
    monkeypatch.setattr(harness, "CAPTURE_LOCK", lock_path)
    monkeypatch.setattr(harness, "check_vendor", lambda: None)
    seen = {}

    def fake_run(argv, capture_output, text, env, timeout):
        seen["timeout"] = timeout
        out = Path(argv[argv.index("--output") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.json").write_text(json.dumps({"status": "capture_complete"}))
        return sp.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    holder = open(lock_path, "a")
    fcntl.flock(holder, fcntl.LOCK_SH)                         # another run's command holds the shared lock
    threading.Timer(1.5, lambda: fcntl.flock(holder, fcntl.LOCK_UN)).start()
    ws = scratch / "ws"
    ws.mkdir()
    (ws / "index.html").write_text("<p>x</p>")
    harness.capture(ws, scratch / "out", "waves", timeout=2)
    assert seen["timeout"] == 2                                # not 2 - 1.5
    lock = json.loads((scratch / "out" / "host-lock.json").read_text())
    assert lock["lock_wait_s"] >= 1.4
