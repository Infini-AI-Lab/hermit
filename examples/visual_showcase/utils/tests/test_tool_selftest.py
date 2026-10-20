"""tool_selftest.py (the Codex toolsmith's self-test tool) with a fake confined runner, no browser and no bwrap: the
scripted Evolver's report for a working bundle, the probe call, the record, and that nothing of the learner's working
copy except its learnable files is used (a planted interface.py never runs; a symbolic link is not followed)."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import zlib

UTILS = Path(__file__).resolve().parents[1]
REPO = UTILS.parents[2]
TEMPLATE = REPO / "hermit" / "algorithms" / "templates" / "toolsmith"
WIDTH, HEIGHT = 400, 300
PROBE_RGB = (30, 120, 200)
SHOT = {"name": "shot", "description": "Render index.html and write shot.png.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "entry": "run.py", "test": "test.py", "example_args": {}, "timeout_s": 30}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def png(solid: tuple | None = None) -> bytes:
    """An RGB PNG pngstats decodes: a pattern, or the solid colour with four patterned rows (over 1 kB)."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    rows = []
    for y in range(HEIGHT):
        if solid and y >= 4:
            rows.append(b"\x00" + bytes(solid) * WIDTH)
        else:
            rows.append(b"\x00" + bytes(v for x in range(WIDTH)
                                        for v in ((x * 7 + y) & 255, (x ^ y) & 255, (y * 3 + x // 2) & 255)))
    head = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0))
    return head + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


class Runner:
    """Stands in for confine.run_tool: a test passes; an entry writes shot.png (the probe's colour on the probe page)."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *, argv, cwd, timeout_s, ro=(), lock=None, **_):
        page = (Path(cwd) / "index.html").read_text() if (Path(cwd) / "index.html").is_file() else ""
        self.calls.append({"argv": argv, "cwd": cwd, "ro": ro, "page": page})
        base = {"exit_code": 0, "stdout": "", "stderr": "", "seconds": 0.2, "timed_out": False, "lock_wait_s": 0.0,
                "error": None, "browser_launched": True}
        if argv[-2].endswith("test.py"):
            return base
        (Path(cwd) / "shot.png").write_bytes(png(PROBE_RGB if "PROBE" in page else None))
        return {**base, "stdout": json.dumps({"text": "rendered", "images": ["shot.png"]}) + "\n"}


def layout(tmp: Path) -> tuple[Path, Path, Path]:
    run = tmp / "runs" / "waves-tc3a"
    adapter, root = run / "workspace" / "adapter", run / "root"
    shutil.copytree(TEMPLATE, root)
    shutil.copytree(TEMPLATE, adapter)
    shot = adapter / "tools" / "shot"
    shot.mkdir()
    (shot / "TOOL.json").write_text(json.dumps(SHOT))
    (shot / "run.py").write_text("print('{}')\n")
    (shot / "test.py").write_text("import sys\nsys.exit(0)\n")
    (shot / "outside.txt").symlink_to(tmp / "secret.txt")
    (tmp / "secret.txt").write_text("not for the candidate")
    # A working copy may hold anything; the template's interface.py must come from the run's root.
    (adapter / "interface.py").write_text(f"open({str(tmp / 'PLANTED')!r}, 'w').write('ran')\n")
    step = run / "learning_material" / "step_0001"
    traj = step / "trajectories" / "waves"
    (traj / "workspace").mkdir(parents=True)
    (traj / "workspace" / "index.html").write_text("<canvas data-testid='scene'></canvas>")
    (traj / "result.json").write_text(json.dumps({"valid_submission": True}))
    (step / "tasks").mkdir()
    (step / "tasks" / "waves.json").write_text(json.dumps({"id": "waves", "judge": {"score": 0.5},
                                                           "trajectory": "trajectories/waves"}))
    os.symlink("step_0001", run / "learning_material" / "current")
    info = tmp / "info"
    (info / "probes").mkdir(parents=True)
    (info / "README.md").write_text("# The brief\n")
    (info / "probes" / "probe.html").write_text("<p>PROBE</p>")
    (info / "probes" / "probes.json").write_text(json.dumps([{"page": "probe.html", "label": "solid",
                                                               "expect_rgb": list(PROBE_RGB), "tolerance": 10,
                                                               "note": "a solid page"}]))
    return adapter, root, info


def test_a_working_bundle_is_reported_like_the_scripted_self_test(tmp_path):
    tools = _load("visual_test_tools_under_test", UTILS / "tool_selftest.py")
    adapter, root, info = layout(tmp_path)
    runner = Runner()
    out = tools.run(["shot", "nope"], adapter, root, info, runner=runner)
    assert out["working"] == {"shot": True}
    assert "### tools/shot — working: yes" in out["report"] and "probe 'solid'" in out["report"]
    assert "no bundle (a tools/<name>/ directory with TOOL.json) named: nope" in out["report"]
    home = adapter.parent.parent / "test_tools"
    candidate = home / "adapter" / "tools" / "shot"
    assert [c["argv"][-2] for c in runner.calls] == ["/tool/test.py", "/tool/run.py", "/tool/run.py"]
    assert all(c["ro"] == [[str(candidate), "/tool"]] for c in runner.calls)
    assert "data-testid='scene'" in runner.calls[1]["page"] and runner.calls[2]["page"] == "<p>PROBE</p>"
    assert not (tmp_path / "PLANTED").exists()                       # the working copy's interface.py never ran
    assert (home / "adapter" / "interface.py").read_text() == (TEMPLATE / "interface.py").read_text()
    assert not (candidate / "outside.txt").exists()                  # a symbolic link is not followed
    record = json.loads((home / "calls.jsonl").read_text().splitlines()[-1])
    assert record["tested"] == ["shot"] and record["working"] == {"shot": True}
    assert not (adapter / "tools" / "shot" / "shot.png").exists()    # the working copy is left as it was


def test_at_most_four_bundles_per_call(tmp_path):
    tools = _load("visual_test_tools_under_test_2", UTILS / "tool_selftest.py")
    adapter, root, info = layout(tmp_path)
    for name in ("a1", "a2", "a3", "a4"):
        shutil.copytree(adapter / "tools" / "shot", adapter / "tools" / name, symlinks=True)
        (adapter / "tools" / name / "TOOL.json").write_text(json.dumps({**SHOT, "name": name}))
    out = tools.run(None, adapter, root, info, runner=Runner())
    assert sorted(out["working"]) == ["a1", "a2", "a3", "a4"]
    assert "only the first 4 named bundles are tested in one call: shot not tested" in out["report"]
