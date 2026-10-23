"""The toolsmith evolver: path and size validation of the learner's files, the self-tests through a fake confined runner
(the bundle's test, then the contract call through Session), blank images and lost probes as failures, the repair turn
and keep-last-passing, and a step without a runner. No model and no bubblewrap: the llm is a scripted closure and the
runner a fake that writes a PNG into the workspace it is given."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import struct
import zlib

from hermit.evolver.base import Access, Progress, StepContext, Workspace
from hermit.evolver.loader import find, instantiate

PACKAGE = Path(__file__).resolve().parents[1] / "hermit"
TEMPLATE = PACKAGE / "algorithms" / "templates" / "toolsmith"
EVOLVER = PACKAGE / "algorithms" / "evolvers" / "toolsmith"

SHOT = {"name": "shot", "description": "Render index.html and write shot.png.",
        "parameters": {"type": "object", "properties": {"tag": {"type": "string"}}, "required": [],
                       "additionalProperties": False},
        "entry": "run.py", "test": "test.py", "example_args": {"tag": "t"}, "timeout_s": 30}
RUN_V1 = "import json\nprint(json.dumps({'text': 'v1', 'images': ['shot.png']}))\n"
RUN_V2 = "import json\nprint(json.dumps({'text': 'v2', 'images': ['shot.png']}))\n"
RUN_V3 = "import json\nprint(json.dumps({'text': 'v3', 'images': ['shot.png']}))\n"
TEST_PY = "import sys\nsys.exit(0)\n"
OLD = {"name": "old", "description": "An older tool.", "parameters": {"type": "object", "properties": {},
       "required": [], "additionalProperties": False}, "entry": "run.py", "example_args": {}, "timeout_s": 10}


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"toolsmith_{name}_under_test", EVOLVER / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def png(width: int = 320, height: int = 240, blank: bool = False) -> bytes:
    """An 8-bit RGB PNG (filter 0) that pngstats decodes: a pattern, or a solid frame with four patterned rows
    (1.7 % of the pixels: blank by the 2 % rule, yet well above 1 kB)."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    rows = []
    for y in range(height):
        if blank and y >= 4:
            rows.append(b"\x00" + bytes((30, 120, 200)) * width)
        else:
            rows.append(b"\x00" + bytes(v for x in range(width)
                                        for v in ((x * 7 + y) & 255, (x ^ y) & 255, (y * 3 + x // 2) & 255)))
    head = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    return head + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


def block(path: str, body: str) -> str:
    return f"<<<FILE {path}>>>\n{body}\n<<<END FILE>>>\n"


def bundle(adapter: Path, spec: dict, files: dict[str, str]) -> Path:
    directory = adapter / "tools" / spec["name"]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "TOOL.json").write_text(json.dumps(spec))
    for name, text in files.items():
        (directory / name).write_text(text)
    return directory


class FakeRunner:
    """Stands in for the confine task tool: records every call; a test program passes, an entry writes shot.png into
    the working directory and prints the result line; or everything fails with a traceback on stderr."""

    def __init__(self, fail: bool = False, blank: bool = False):
        self.calls: list[dict] = []
        self.fail, self.blank = fail, blank

    def __call__(self, *, argv, cwd, timeout_s, ro=(), lock=None, **_):
        self.calls.append({"argv": argv, "cwd": cwd, "timeout_s": timeout_s, "ro": ro, "lock": lock})
        base = {"exit_code": 0, "stdout": "", "stderr": "", "seconds": 0.2, "timed_out": False, "lock_wait_s": 0.0,
                "error": None, "browser_launched": True}
        if self.fail:
            return {**base, "exit_code": 1, "stderr": "Traceback (most recent call last):\n  boom: node not found\n"}
        if argv[-2].endswith("test.py"):
            return base
        (Path(cwd) / "shot.png").write_bytes(png(blank=self.blank))
        return {**base, "stdout": "progress\n" + json.dumps({"text": "rendered", "images": ["shot.png"]}) + "\n"}


def trajectory(d: Path, records: list[dict], result: dict) -> None:
    (d / "tools").mkdir(parents=True)
    (d / "workspace").mkdir()
    for i, record in enumerate(records, 1):
        (d / "tools" / f"{i:02d}.json").write_text(json.dumps(record))
    (d / "result.json").write_text(json.dumps(result))
    (d / "final.md").write_text("I could not see the rendered page.")
    (d / "workspace" / "index.html").write_text(
        "<!doctype html><html><body><canvas data-testid='scene'></canvas></body></html>")
    (d / "workspace" / "README.md").write_text("# The brief\n")


RECORDS = [
    {"name": "write_file", "arguments": {"path": "index.html", "content": "<html>" * 100},
     "result": {"ok": True, "file": "index.html"}, "seconds": 0.0},
    {"name": "run_command", "arguments": {"command": "ls -la && python3 -c 'print(1)'"},
     "result": {"exit_code": 0, "stdout": "index.html\n1\n", "stderr": "", "seconds": 0.4,
                "timed_out": False, "lock_wait_s": 0.0, "images": []},
     "image_records": [], "browser_launched": False},
    {"name": "run_command", "arguments": {"command": "node render.js"},
     "result": {"exit_code": 127, "stdout": "", "stderr": "bash: node: command not found\n", "seconds": 0.1,
                "timed_out": False, "lock_wait_s": 0.0, "images": []}, "image_records": []},
    {"name": "inspect_page", "arguments": {}, "result": {"ok": False, "error": "ValueError: Unknown tool"},
     "seconds": 0.0},
    {"name": "shot", "arguments": {"tag": "a"},
     "result": {"text": "rendered", "exit_code": 0, "seconds": 3.0, "timed_out": False, "lock_wait_s": 0.0,
                "error": None, "images": [{"file": "shot.png", "nonblank": True}]},
     "image_records": [{"file": "shot.png", "bytes": 5000, "width": 320, "height": 240, "nonblank": True,
                        "packed_for_model": True}], "browser_launched": True},
]
RESULT = {"case": "waves", "status": "ok", "stop_reason": "final", "model_calls": 4, "tool_calls": 5, "run_commands": 2,
          "adapter_tool_calls": 1, "image_bearing_calls": 1, "looked_calls": 1, "first_image_call": 5,
          "tools_exported": ["shot"], "valid_submission": True, "elapsed_seconds": 120.5}


def context(tmp: Path, replies: list[str], runner=None, params: dict | None = None) -> tuple[StepContext, list]:
    ws = Workspace(*(tmp / n for n in ("adapter", "material", "info", "history", "state", "scratch", "output")))
    shutil.copytree(TEMPLATE, ws.adapter)
    for d in (ws.task_info, ws.history, ws.state, ws.scratch, ws.output, ws.learning_material / "step_0001" / "tasks"):
        d.mkdir(parents=True, exist_ok=True)
    (ws.learning_material / "current").symlink_to("step_0001")
    (ws.task_info / "README.md").write_text("# Task README\nThe agent's machine has node at /opt/node.\n")
    (ws.task_info / "environment.md").write_text("F1 headless only.\n")
    trajectory(ws.learning_material / "step_0001" / "trajectories" / "waves", RECORDS, RESULT)
    (ws.learning_material / "step_0001" / "tasks" / "waves.json").write_text(json.dumps(
        {"id": "waves", "input": {"id": "waves", "prompt": "Ocean waves"}, "output": {"case": "waves", "status": "ok"},
         "judge": {"score": 0.4, "feedback": "rubric 1: the canvas is blank white"},
         "trajectory": "trajectories/waves", "pipeline_extra": {"calls": 5}}))
    calls = []

    def llm(messages, max_tokens=None, temperature=None):
        calls.append([dict(m) for m in messages])
        return {"content": replies[len(calls) - 1], "usage": {}}
    tools = {"llm": llm, "check_spec": lambda **_: {"passed": True, "issues": []}}
    if runner is not None:
        tools["confine"] = runner
    evolver, merged = instantiate(find("toolsmith"), {"runner_tool": "confine" if runner is not None else None,
                                                      **(params or {})})
    ctx = StepContext(workspace=ws, access=Access(), tools=tools, params=merged, progress=Progress(1, 3, False))
    return ctx, calls


def notes(ctx: StepContext) -> dict:
    return json.loads((ctx.workspace.state / "notes.jsonl").read_text().strip().splitlines()[-1])


def saved_tests(ctx: StepContext) -> dict:
    return json.loads((ctx.workspace.state / "tests.json").read_text())


# --- one step: apply, validate, self-test ----------------------------------------------------------------------------
def test_step_applies_valid_blocks_skips_invalid_ones_and_marks_a_tool_working(tmp_path):
    runner = FakeRunner()
    reply = (block("ADAPTER.md", "Write index.html, then call `shot` and look at the image.")
             + block("tools/shot/TOOL.json", json.dumps(SHOT)) + block("tools/shot/run.py", RUN_V1)
             + block("tools/shot/test.py", TEST_PY)
             + block("tools/shot/fixture.png", "not text") + block("EVOLVER.md", "rewritten")
             + block("../escape.md", "x") + block("tools/.hidden/run.py", "x")
             + block("tools/shot/notes.txt", "x" * 61_000) + block("tools/run_command/run.py", "x")
             + "<<<DELETE tools/old>>>\n<<<DELETE tools/none>>>\nSUMMARY: added a shot tool\n")
    ctx, calls = context(tmp_path, [reply], runner)
    bundle(ctx.workspace.adapter, OLD, {"run.py": "print('{}')\n"})
    evolver, _ = instantiate(find("toolsmith"), {})
    result = evolver.step(ctx)
    adapter = ctx.workspace.adapter
    assert result.status == "done" and result.summary == "added a shot tool" and result.usage == []
    assert (adapter / "ADAPTER.md").read_text() == "Write index.html, then call `shot` and look at the image.\n"
    assert (adapter / "tools" / "shot" / "run.py").read_text() == RUN_V1
    assert json.loads((adapter / "tools" / "shot" / "TOOL.json").read_text()) == SHOT
    for rel in ("tools/shot/fixture.png", "../escape.md", "tools/.hidden/run.py", "tools/shot/notes.txt",
                "tools/run_command/run.py"):
        assert not (adapter / rel).exists(), rel
    assert (adapter / "EVOLVER.md").read_text() == (TEMPLATE / "EVOLVER.md").read_text()
    assert not (adapter / "tools" / "old").exists()
    skipped = [n for n in notes(ctx)["notices"] if n.startswith("skipped")]
    assert len(skipped) == 7 and any("61000 bytes exceeds max_bytes 60000" in n for n in skipped)
    assert any("reserved name" in n for n in skipped) and any("no such bundle" in n for n in skipped)
    # One llm call: the self-tests passed, so no repair turn. The prompt carried the material.
    assert len(calls) == 1
    prompt = calls[0][-1]["content"]
    for text in ("# Task README", "F1 headless only", "# Toolsmith adapter: the learner's contract", "Run: case=waves",
                 "error: ValueError: Unknown tool", "bash: node: command not found", "rubric 1: the canvas is blank",
                 "I could not see the rendered page.", "Pipeline extra", "(none yet)"):
        assert text in prompt, text
    # The test ran, then the contract call, both in a workspace seeded with the trajectory's page.
    assert [c["argv"][-2] for c in runner.calls] == ["/tool/test.py", "/tool/run.py"]
    assert all(c["ro"] == [[str(adapter / "tools" / "shot"), "/tool"]] and c["lock"] == "shared" for c in runner.calls)
    assert runner.calls[0]["timeout_s"] == 300 and runner.calls[1]["timeout_s"] == 30
    ws = Path(runner.calls[1]["cwd"])
    assert ws == ctx.workspace.scratch / "ws" / "shot" and "data-testid='scene'" in (ws / "index.html").read_text()
    assert json.loads(runner.calls[1]["argv"][-1]) == {"tag": "t"}
    state = saved_tests(ctx)["shot"]
    assert state["working"] is True and state["test"]["passed"] and state["contract"]["ok"]
    image = state["contract"]["images"][0]
    assert image["file"] == "shot.png" and image["width"] == 320 and image["blank"] is False
    assert image["problem"] is None
    assert state["restored_from_last_passing"] is False and len(state["sha256"]) == 64
    assert (ctx.workspace.state / "seed" / "index.html").is_file()
    assert (ctx.workspace.output / "llm_replies" / "reply.0.txt").read_text() == reply
    report = (ctx.workspace.output / "report.md").read_text()
    assert "wrote tools/shot/run.py" in report and "deleted bundle tools/old" in report and "working: yes" in report


def test_failing_self_test_gets_a_repair_turn_and_the_last_passing_version_is_restored(tmp_path):
    runner = FakeRunner(fail=True)
    ctx, calls = context(tmp_path, [block("tools/shot/run.py", RUN_V2) + "SUMMARY: faster render\n",
                                    block("tools/shot/run.py", RUN_V3) + "SUMMARY: fixed the path\n"], runner)
    adapter = ctx.workspace.adapter
    directory = bundle(adapter, SHOT, {"run.py": RUN_V1, "test.py": TEST_PY})
    sha = load("evolver").bundle_sha(directory)
    (ctx.workspace.state / "tests.json").write_text(json.dumps(
        {"shot": {"sha256": sha, "step": 2, "test": {"passed": True, "exit_code": 0, "seconds": 0.1, "timed_out": False,
                                                     "stdout_tail": "", "stderr_tail": "", "error": None},
                  "contract": {"ok": True, "problem": None, "args": {}, "images": []}, "working": True,
                  "restored_from_last_passing": False, "notices": []}}))
    evolver, _ = instantiate(find("toolsmith"), {})
    result = evolver.step(ctx)
    assert len(calls) == 2
    repair = calls[1][-1]["content"]
    assert "Self-test results" in repair and "boom: node not found" in repair and "working: no" in repair
    assert "test: FAILED (exit 1" in repair
    # Both versions failed: the tool goes back to the version that worked, and the state says so.
    assert (adapter / "tools" / "shot" / "run.py").read_text() == RUN_V1
    state = saved_tests(ctx)["shot"]
    assert state["working"] is True and state["restored_from_last_passing"] is True and state["sha256"] == sha
    assert any("restored the last passing version" in n for n in notes(ctx)["notices"])
    assert result.summary == "fixed the path"
    # Round one tested the changed bundle (test + contract), round two again: four runner calls.
    assert [c["argv"][-2] for c in runner.calls] == ["/tool/test.py", "/tool/run.py"] * 2
    prompt = calls[0][-1]["content"]
    assert "### tools/shot — working: yes" in prompt and "tools/shot/run.py" in prompt and "   1| import json" in prompt


def test_blank_images_count_as_a_failure_and_images_param_attaches_them(tmp_path):
    runner = FakeRunner(blank=True)
    reply = block("tools/shot/TOOL.json", json.dumps({**SHOT, "test": None})) + block("tools/shot/run.py", RUN_V1) \
        + "SUMMARY: new tool\n"
    ctx, calls = context(tmp_path, [reply, "SUMMARY: nothing more to try\n"], runner, params={"images": 2})
    evolver, _ = instantiate(find("toolsmith"), {})
    evolver.step(ctx)
    assert len(calls) == 2
    content = calls[1][-1]["content"]
    assert isinstance(content, list) and content[0]["type"] == "text" and "BLANK" in content[0]["text"]
    assert content[1]["type"] == "image_url" and content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    state = saved_tests(ctx)["shot"]
    assert state["working"] is False and state["contract"]["ok"] and state["contract"]["images"][0]["blank"] is True
    assert any("every image is blank" in n for n in state["notices"])
    assert state["test"] is None and [c["argv"][-2] for c in runner.calls] == ["/tool/run.py", "/tool/run.py"]
    assert list((ctx.workspace.state / "images").glob("*.png"))     # kept for the next step's first turn


def test_without_a_runner_tool_the_self_tests_are_skipped_with_a_notice(tmp_path):
    ctx, calls = context(tmp_path, [block("tools/shot/TOOL.json", json.dumps({**SHOT, "test": None}))
                                    + block("tools/shot/run.py", RUN_V1) + "SUMMARY: new tool\n"])
    evolver, _ = instantiate(find("toolsmith"), {})
    result = evolver.step(ctx)
    assert result.summary == "new tool" and len(calls) == 1
    assert any(n.startswith("self-tests skipped") for n in notes(ctx)["notices"])
    assert (ctx.workspace.adapter / "tools" / "shot" / "run.py").read_text() == RUN_V1
    assert saved_tests(ctx) == {}


class ProbeRunner(FakeRunner):
    """The bundle's test writes a fixture index.html; the entry draws the probe colour when the page is the probe."""

    def __init__(self, sees_probe: bool = True):
        super().__init__()
        self.sees_probe = sees_probe

    def __call__(self, *, argv, cwd, timeout_s, ro=(), lock=None, **_):
        self.calls.append({"argv": argv, "cwd": cwd, "timeout_s": timeout_s, "ro": ro, "lock": lock,
                           "page": (Path(cwd) / "index.html").read_text()})
        base = {"exit_code": 0, "stdout": "", "stderr": "", "seconds": 0.2, "timed_out": False, "lock_wait_s": 0.0,
                "error": None, "browser_launched": True}
        if argv[-2].endswith("test.py"):
            (Path(cwd) / "index.html").write_text("FIXTURE")
            return base
        probe = "PROBE" in (Path(cwd) / "index.html").read_text()
        (Path(cwd) / "shot.png").write_bytes(png(blank=probe and self.sees_probe))   # blank = solid rgb(30,120,200)
        return {**base, "stdout": json.dumps({"text": "rendered", "images": ["shot.png"]}) + "\n"}


def probe_context(tmp_path, replies, runner):
    ctx, calls = context(tmp_path, replies, runner)
    probes = ctx.workspace.task_info / "probes"
    probes.mkdir()
    (probes / "probe.html").write_text("<p>PROBE</p>")
    (probes / "probes.json").write_text(json.dumps([{"page": "probe.html", "label": "solid", "expect_rgb": [30, 120, 200],
                                                     "tolerance": 10, "note": "a solid page"}]))
    return ctx, calls


def test_a_version_that_loses_a_probe_is_restored(tmp_path):
    class Regressed(ProbeRunner):        # the agent's page renders; the probe page comes out patterned (wrong colour)
        def __init__(self):
            super().__init__(sees_probe=False)
    runner = Regressed()
    ctx, calls = probe_context(tmp_path, [block("tools/shot/run.py", RUN_V2) + "SUMMARY: a\n",
                                          block("tools/shot/run.py", RUN_V3) + "SUMMARY: b\n"], runner)
    adapter = ctx.workspace.adapter
    directory = bundle(adapter, {**SHOT, "test": None}, {"run.py": RUN_V1})
    sha = load("evolver").bundle_sha(directory)
    (ctx.workspace.state / "tests.json").write_text(json.dumps({"shot": {
        "sha256": sha, "working": True, "test": None, "contract": {"ok": True, "images": []},
        "probes": [{"label": "solid", "ok": True}], "restored_from_last_passing": False, "notices": []}}))
    evolver, _ = instantiate(find("toolsmith"), {})
    evolver.step(ctx)
    assert (adapter / "tools" / "shot" / "run.py").read_text() == RUN_V1
    assert saved_tests(ctx)["shot"]["restored_from_last_passing"] is True
