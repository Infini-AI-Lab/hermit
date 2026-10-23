"""The toolsmith evolver: the multi-file reply grammar, path and size validation, the self-tests through a fake confined
runner (the bundle's test, then the contract call through Session), the repair turn and keep-last-passing, the
trajectory digest and its truncation, the ADAPTER.md lint. No model and no bubblewrap: the llm is a scripted closure
and the runner a fake that writes a PNG into the workspace it is given."""
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


def trajectory(d: Path, records: list[dict], result: dict, final: str = "I could not see the rendered page.") -> None:
    (d / "tools").mkdir(parents=True)
    (d / "workspace").mkdir()
    for i, record in enumerate(records, 1):
        (d / "tools" / f"{i:02d}.json").write_text(json.dumps(record))
    (d / "result.json").write_text(json.dumps(result))
    (d / "final.md").write_text(final)
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


def context(tmp: Path, replies: list[str], runner=None, params: dict | None = None, previous_tests: dict | None = None,
            score: float = 0.4) -> tuple[StepContext, list]:
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
         "judge": {"score": score, "feedback": "rubric 1: the canvas is blank white"},
         "trajectory": "trajectories/waves", "pipeline_extra": {"calls": 5}}))
    if previous_tests is not None:
        (ws.state / "tests.json").write_text(json.dumps(previous_tests))
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


# --- grammar ----------------------------------------------------------------------------------------------------------
def test_reply_grammar_is_tolerant():
    parse = load("evolver").parse_reply
    reply = parse("Thinking first.\n<<<FILE ADAPTER.md>>>\nCall `shot`.\n<<<END FILE>>>\n"
                  "<<<FILE tools/shot/run.py>>>\n```python\nprint(1)\n```\n<<<END>>>\n"
                  "<<<FILE `tools/shot/notes.txt`>>>\nno end marker\n<<<DELETE tools/old>>>\n**SUMMARY:** did things\n")
    assert reply.files == [("ADAPTER.md", "Call `shot`."), ("tools/shot/run.py", "print(1)"),
                           ("tools/shot/notes.txt", "no end marker")]
    assert reply.deletes == ["tools/old"] and reply.summary == "did things"
    assert len(reply.problems) == 1 and "no <<<END FILE>>>" in reply.problems[0]
    empty = parse("I cannot decide.")
    assert not empty.files and not empty.deletes and empty.summary is None


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


def test_retry_when_the_first_reply_holds_nothing(tmp_path):
    ctx, calls = context(tmp_path, ["", block("ADAPTER.md", "Be careful.") + "SUMMARY: a rule\n"])
    evolver, _ = instantiate(find("toolsmith"), {})
    result = evolver.step(ctx)
    assert len(calls) == 2 and "no file block and no SUMMARY" in calls[1][-1]["content"]
    assert (ctx.workspace.adapter / "ADAPTER.md").read_text() == "Be careful.\n" and result.summary == "a rule"
    assert (ctx.workspace.output / "llm_replies" / "reply.1.txt").exists()


def test_lint_notices_name_tools_that_do_not_exist(tmp_path):
    lint = load("evolver").lint_context
    assert lint("Run `require()` with NODE_PATH; then call the tool `render_page`.", {"render_page"}) == []
    text = ("Call `inspect_page` after writing index.html, then `shot()`.\n"
            "Use `run_command` to list files; the `pause` button and the `intensity` slider must stay visible.\n"
            "`three_js` is loaded from /vendor.\n")
    found = lint(text, {"shot", "run_command", "write_file"})
    assert [n.split("`")[1] for n in found] == ["inspect_page", "three_js"]
    reply = block("ADAPTER.md", "First call `inspect_page`, then the tool `shot`.") + "SUMMARY: x\n"
    ctx, _ = context(tmp_path, [reply], FakeRunner())
    bundle(ctx.workspace.adapter, SHOT, {"run.py": RUN_V1, "test.py": TEST_PY})
    evolver, _ = instantiate(find("toolsmith"), {})
    evolver.step(ctx)
    lints = [n for n in notes(ctx)["notices"] if "is not an available tool" in n]
    assert len(lints) == 1 and "`inspect_page`" in lints[0] and "run_command, shot" in lints[0]


# --- digest -----------------------------------------------------------------------------------------------------------
def test_digest_keeps_header_footer_adapter_calls_and_errors_when_truncating(tmp_path):
    digest = load("digest")
    records = [RECORDS[0]] * 20 + [RECORDS[2]] + [RECORDS[1]] * 10 + [RECORDS[3], RECORDS[4]] + [RECORDS[0]] * 5
    d = tmp_path / "waves"
    trajectory(d, records, RESULT, final="done " * 400)
    full = digest.build(d, limit=50_000)
    assert full.startswith("Run: case=waves status=ok stop_reason=final model_calls=4 tool_calls=5 run_commands=2")
    assert 'tools_exported=["shot"]' in full and "elapsed_seconds=120.5" in full
    assert f"Tool calls ({len(records)}):" in full and "omitted" not in full
    assert "01 write_file index.html (600 chars) -> ok" in full
    assert "21 run_command `node render.js` -> exit 127, 0.1 s" in full and "bash: node: command not found" in full
    assert "32 inspect_page {} -> exit ?, 0.0 s, error: ValueError: Unknown tool" in full
    assert "33 shot {\"tag\": \"a\"} -> exit 0, 3.0 s, browser_launched=True" in full and "images: 1 (shot.png)" in full
    assert full.rstrip().endswith("…[+" + str(len(("done " * 400).strip()) - 1500) + " chars]")
    short = digest.build(d, limit=3000)      # why: header + footer take ~1 700; the entries compete for the rest
    assert len(short) <= 3000 and short.startswith("Run: case=waves") and "Agent's final message:" in short
    assert "32 inspect_page" in short and "33 shot" in short and "21 run_command `node render.js`" in short
    assert "calls omitted for length" in short and "01 write_file" not in short
    assert "38 write_file" in short      # the last calls come before the rest
    assert digest.build(tmp_path / "missing", limit=1000).startswith("Run: (no result.json)")


# --- version 1.1: grammar, separate workspaces, probes, the tools-vs-host section, whole files, exact snapshots -------
def test_markers_after_a_sentence_and_content_before_the_end_marker():
    reply = load("evolver").parse_reply("Here is the file: <<<FILE ADAPTER.md>>>\nline one\nlast line<<<END FILE>>>\n"
                                        "SUMMARY: s\n")
    assert reply.files == [("ADAPTER.md", "line one\nlast line")] and not reply.problems and reply.summary == "s"


def test_reasoning_leaked_before_think_end_is_ignored():
    """A draft block inside leaked reasoning is not a file of the reply."""
    reply = load("evolver").parse_reply("Draft:\n<<<FILE tools/shot/run.py>>>\nprint(1)\n<<<END FILE>>>\nOK.</think>"
                                        "<<<FILE ADAPTER.md>>>\nreal\n<<<END FILE>>>\nSUMMARY: s\n")
    assert reply.files == [("ADAPTER.md", "real")] and not reply.problems and reply.summary == "s"


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


def test_own_test_has_its_own_workspace_and_every_tool_is_called_on_the_probe_pages(tmp_path):
    runner = ProbeRunner()
    reply = block("tools/shot/TOOL.json", json.dumps(SHOT)) + block("tools/shot/run.py", RUN_V1) \
        + block("tools/shot/test.py", TEST_PY) + "SUMMARY: new tool\n"
    ctx, calls = probe_context(tmp_path, [reply], runner)
    evolver, _ = instantiate(find("toolsmith"), {})
    evolver.step(ctx)
    scratch = ctx.workspace.scratch / "ws"
    assert [(Path(c["cwd"]), c["argv"][-2]) for c in runner.calls] == [
        (scratch / "shot.test", "/tool/test.py"), (scratch / "shot", "/tool/run.py"), (scratch / "shot.probe0", "/tool/run.py")]
    assert "data-testid='scene'" in runner.calls[1]["page"] and runner.calls[2]["page"] == "<p>PROBE</p>"
    assert (scratch / "shot.test" / "index.html").read_text() == "FIXTURE"        # the fixture stayed in its own place
    state = saved_tests(ctx)["shot"]
    assert state["working"] is True and state["probes"][0]["ok"] is True and state["probes"][0]["dominant_rgb"] == [[30, 120, 200]]
    assert len(calls) == 1      # everything passed: no repair turn


def test_a_failed_probe_gets_a_repair_turn_with_the_colours(tmp_path):
    runner = ProbeRunner(sees_probe=False)
    reply = block("tools/shot/TOOL.json", json.dumps({**SHOT, "test": None})) + block("tools/shot/run.py", RUN_V1) \
        + "SUMMARY: new tool\n"
    ctx, calls = probe_context(tmp_path, [reply, "SUMMARY: cannot fix it\n"], runner)
    evolver, _ = instantiate(find("toolsmith"), {})
    evolver.step(ctx)
    assert len(calls) == 2
    repair = calls[1][-1]["content"]
    assert "probe 'solid' (a solid page): FAILED" in repair and "expected [30, 120, 200]" in repair
    state = saved_tests(ctx)["shot"]
    assert state["working"] is False and state["probes"][0]["ok"] is False   # a missed probe: not working


def test_material_puts_the_tools_last_report_next_to_the_hosts_checks(tmp_path):
    ctx, calls = context(tmp_path, ["SUMMARY: nothing\n"])
    material = ctx.workspace.learning_material / "step_0001"
    d = material / "trajectories" / "waves"
    record = json.loads((d / "tools" / "05.json").read_text())
    (d / "tools" / "05.json").write_text(json.dumps({**record, "index_sha_at_call": "abc"}))
    (d / "result.json").write_text(json.dumps({**RESULT, "submission_sha256": "abc", "finalize": {
        "replaced": True, "reason": "the final version was changed after the agent last saw it", "submitted_call": 5}}))
    (d / "capture").mkdir()
    (d / "capture" / "summary.json").write_text(json.dumps({"screenshots": [{"id": "desktop-t0"}, {"id": "phone"}]}))
    task = json.loads((material / "tasks" / "waves.json").read_text())
    task["judge"]["extra"] = {"mechanical": [
        {"item": "ready", "passed": False, "detail": "getState().ready did not become true within 4 s"},
        {"item": "capture_ok", "passed": True, "detail": "5 of 5"}]}
    (material / "tasks" / "waves.json").write_text(json.dumps(task))
    evolver, _ = instantiate(find("toolsmith"), {})
    evolver.step(ctx)
    prompt = calls[0][-1]["content"]
    for text in ("#### Your tools vs the host in this run", "Last call: 05 shot", "it showed the submitted version",
                 "Image sizes your tools returned in this run: 320x240.", "(the version from call 5)",
                 "Host views of the submitted version: desktop-t0, phone.",
                 "Host checks that failed: ready (getState().ready did not become true within 4 s).",
                 "Your tool's last report, verbatim:\n<<<\nrendered\n>>>"):
        assert text in prompt, text


def test_tool_files_are_shown_whole(tmp_path):
    ctx, calls = context(tmp_path, ["SUMMARY: nothing\n"])
    bundle(ctx.workspace.adapter, {**SHOT, "test": None}, {"run.py": "".join(f"x{i} = {i}  # line\n" for i in range(1, 801))})
    evolver, _ = instantiate(find("toolsmith"), {})
    evolver.step(ctx)
    assert " 800| x800 = 800" in calls[0][-1]["content"] and "[clipped" not in calls[0][-1]["content"]


def test_only_the_exact_version_that_passed_is_restored(tmp_path):
    runner = FakeRunner(fail=True)
    ctx, calls = context(tmp_path, [block("tools/shot/run.py", RUN_V2) + "SUMMARY: a\n",
                                    block("tools/shot/run.py", RUN_V3) + "SUMMARY: b\n"], runner)
    adapter = ctx.workspace.adapter
    bundle(adapter, SHOT, {"run.py": RUN_V1, "test.py": TEST_PY})
    (ctx.workspace.state / "tests.json").write_text(json.dumps(       # passed once, then changed untested (stale)
        {"shot": {"sha256": "0" * 64, "working": True, "stale": True, "test": None, "contract": {"ok": True}}}))
    evolver, _ = instantiate(find("toolsmith"), {})
    evolver.step(ctx)
    assert (adapter / "tools" / "shot" / "run.py").read_text() == RUN_V3
    assert saved_tests(ctx)["shot"]["restored_from_last_passing"] is False


def test_a_text_tool_is_not_probed(tmp_path):
    class TextRunner(ProbeRunner):
        def __call__(self, *, argv, cwd, timeout_s, ro=(), lock=None, **_):
            self.calls.append({"argv": argv, "cwd": cwd})
            return {"exit_code": 0, "stdout": json.dumps({"text": "ready: true"}) + "\n", "stderr": "", "seconds": 0.1,
                    "timed_out": False, "lock_wait_s": 0.0, "error": None, "browser_launched": False}
    runner = TextRunner()
    reply = block("tools/shot/TOOL.json", json.dumps({**SHOT, "test": None})) + block("tools/shot/run.py", RUN_V1) \
        + "SUMMARY: a text tool\n"
    ctx, calls = probe_context(tmp_path, [reply], runner)
    ctx.params["require_image"] = False
    evolver, _ = instantiate(find("toolsmith"), {})
    evolver.step(ctx)
    state = saved_tests(ctx)["shot"]
    assert len(runner.calls) == 1 and state["probes"] == [] and state["working"] is True and len(calls) == 1
    assert "probes not run: the call returned no image" in state["notices"]


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


def test_the_seed_is_the_submitted_version_and_only_exported_tools_are_reported(tmp_path):
    runner = FakeRunner()
    reply = block("tools/shot/TOOL.json", json.dumps({**SHOT, "test": None})) + block("tools/shot/run.py", RUN_V1) \
        + "SUMMARY: new tool\n"
    ctx, calls = context(tmp_path, [reply], runner)
    d = ctx.workspace.learning_material / "step_0001" / "trajectories" / "waves"
    (d / "submission").mkdir()
    (d / "submission" / "index.html").write_text("<p>SUBMITTED</p>")
    (d / "workspace" / "index.html").write_text("<p>UNSEEN FINAL</p>")
    (d / "tools" / "06.json").write_text(json.dumps({"name": "inspect_page", "arguments": {},
                                                    "result": {"ok": False, "error": "ValueError: Unknown tool"}}))
    evolver, _ = instantiate(find("toolsmith"), {})
    evolver.step(ctx)
    assert (Path(runner.calls[0]["cwd"]) / "index.html").read_text() == "<p>SUBMITTED</p>"
    assert (ctx.workspace.state / "seed" / "index.html").read_text() == "<p>SUBMITTED</p>"
    prompt = calls[0][-1]["content"]
    assert "Last call: 05 shot" in prompt and "Last call: 06" not in prompt
