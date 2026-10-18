"""The generic acting agent (utils/agent/agent.py + generic_harness.py) without a model and without bubblewrap: a
scripted ChatClient replaces pipeline.make_client, a fake confine.run writes images into the workspace, a fake final
capture writes capture/summary.json. One bwrap-marked test runs the real confine.run (`bash -c 'echo hi'`); one runs
`hermit check --no-probe` on a scratch config naming the agent. Scratch directories live outside /tmp."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

import pytest
import yaml

from hermit.evolver.base import Usage

UTILS = Path(__file__).resolve().parents[1]
REPO = UTILS.parents[2]
TOOLSMITH = REPO / "hermit" / "algorithms" / "templates" / "toolsmith"
HAS_BWRAP = shutil.which("bwrap") is not None
needs_bwrap = pytest.mark.skipif(not HAS_BWRAP, reason="bubblewrap is not installed")


def _load_agent():
    spec = importlib.util.spec_from_file_location("visual_agent_under_test", UTILS / "agent" / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    agent = _load_agent()
except RuntimeError as exc:          # confine cannot resolve node, Chromium or node_modules on this machine
    pytest.skip(f"the agent cannot import confine here: {exc}", allow_module_level=True)

gh = agent.gh
texts = agent.texts
pipeline = agent.pipeline


@pytest.fixture
def scratch():
    base = Path(os.path.realpath(Path.home() / ".cache" / "hermit-tests" / "generic-agent"))
    base.mkdir(parents=True, exist_ok=True)
    path = Path(os.path.realpath(tempfile.mkdtemp(dir=base)))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)


# --- fakes ------------------------------------------------------------------------------------------------------------
def make_png(path: Path, width: int = 320, height: int = 240, blank: bool = False) -> Path:
    """An 8-bit RGB PNG pngstats decodes: a gradient (non-blank) or solid white."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        if blank:
            rows += b"\xff" * (width * 3)
        else:
            rows += bytes(v for x in range(width)
                          for v in (x * 255 // max(1, width - 1), y * 255 // max(1, height - 1), (x + y) & 255))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b""))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(data)
    return Path(path)


def make_jpeg(path: Path, width: int = 400, height: int = 300) -> Path:
    """SOI, one SOF0 segment with the size, EOI: enough for the header parser and the magic check."""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    Path(path).write_bytes(b"\xff\xd8" + sof + b"\xff\xd9")
    return Path(path)


def tc(name: str, args: dict, call_id: str = "call-1") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class FakeClient:
    """Scripted replies; records every request. Replies: {content, tool_calls, finish_reason, prompt_tokens}."""

    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.timeout = None
        self.total = Usage(model="fake", cost_usd=0.0)

    def chat(self, messages, **options):
        self.requests.append({"messages": copy.deepcopy(messages), "options": copy.deepcopy(options)})
        if not self.replies:
            raise RuntimeError("fake client: no scripted reply left (key sk-secret)")
        reply = self.replies.pop(0)
        calls = reply.get("tool_calls")
        message = {"role": "assistant", "content": reply.get("content"), "reasoning_content": "private",
                   "finish_reason": reply.get("finish_reason", "tool_calls" if calls else "stop")}
        if calls:
            message["tool_calls"] = calls
        usage = Usage("fake", reply.get("prompt_tokens", 1000), 100, 50, 1, 0.001)
        self.total.input_tokens += usage.input_tokens
        self.total.output_tokens += usage.output_tokens
        self.total.calls += 1
        self.total.cost_usd += usage.cost_usd
        return message, usage


class FakeRun:
    """A confine.run stand-in. bash -c commands are a tiny language, one statement per ';':
    `png NAME [W H] [blank]`, `jpeg NAME`, `fat NAME`, `huge NAME`, `symlink NAME TARGET`, `echo TEXT`, `fail`.
    Toolsmith bundle calls (python3 bootstrap) write tool.png and print the JSON result line."""

    def __init__(self, browser: bool = True):
        self.browser = browser
        self.calls: list[dict] = []
        self.tool_images = ["tool.png"]
        self.outside: Path | None = None        # a tool listing "link.png" plants a symlink to this file

    def __call__(self, argv, cwd, timeout_s, **kw):
        cwd = Path(cwd)
        record = {"exit_code": 0, "stdout": "", "stderr": "", "seconds": 0.01, "timed_out": False, "lock_wait_s": 0.0,
                  "dropped_bytes": 0, "bwrap_argv_sha": "ab" * 32, "browser_launched": self.browser, "error": None}
        call = {"argv": list(argv), "cwd": str(cwd), "timeout_s": timeout_s, **kw}
        if kw.get("ro"):
            root = Path(kw["ro"][0][0]).parents[1]
            call["card_present"] = (root / "CARD.md").exists()
        self.calls.append(call)
        out = []
        if argv[0] == "bash" and argv[1] == "-c":
            for statement in argv[2].split(";"):
                words = statement.split()
                if not words:
                    continue
                if words[0] == "png":
                    size = (int(words[2]), int(words[3])) if len(words) >= 4 and words[2].isdigit() else (320, 240)
                    make_png(cwd / words[1], *size, blank="blank" in words)
                elif words[0] == "jpeg":
                    make_jpeg(cwd / words[1])
                elif words[0] == "fat":
                    (cwd / words[1]).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 2_100_000)
                elif words[0] == "huge":
                    data = make_png(cwd / words[1], 8, 8).read_bytes()
                    (cwd / words[1]).write_bytes(data[:16] + struct.pack(">II", 3000, 3000) + data[24:])
                elif words[0] == "symlink":
                    os.symlink(words[2], cwd / words[1])
                elif words[0] == "echo":
                    out.append(" ".join(words[1:]))
                elif words[0] == "fail":
                    record.update(exit_code=1, stderr="boom")
                time.sleep(0.002)      # why: distinct mtimes between statements keep the image order deterministic
        elif argv[0] == "python3":
            for name in self.tool_images:
                if name == "link.png" and self.outside is not None:
                    os.symlink(self.outside, cwd / name)
                elif not name.startswith(".."):
                    make_png(cwd / name, 400, 300)
            out.append("progress line")
            out.append(json.dumps({"text": "wordcount: 3 lines", "images": self.tool_images}))
        record["stdout"] = "\n".join(out) + ("\n" if out else "")
        return record


def fake_capture(submission: Path, output: Path, case: str) -> None:
    output.mkdir(parents=True)
    (output / "summary.json").write_text(json.dumps({"status": "fake", "capture_ok": True, "screenshots": [],
                                                      "page_errors": [], "console_errors": [], "case": case}))


def bundle(adapter: Path, name: str, **files: str) -> None:
    directory = adapter / "tools" / name
    directory.mkdir(parents=True)
    spec = {"name": name, "description": f"The {name} tool: reports on index.html and writes a picture of it.",
            "parameters": {"type": "object", "properties": {"file": {"type": "string", "default": "index.html"}},
                           "required": [], "additionalProperties": False},
            "entry": "run.py", "example_args": {}, "timeout_s": 30}
    (directory / "TOOL.json").write_text(json.dumps(spec))
    (directory / "run.py").write_text(files.pop("run.py", "import json, sys\nprint(json.dumps({'text': 'ok'}))\n"))
    for filename, text in files.items():
        (directory / filename).write_text(text)


def toolsmith_adapter(scratch: Path, *, origin: bool = True, card: bool = True, tools=("wordcount",)) -> Path:
    adapter = scratch / "adapter"
    shutil.copytree(TOOLSMITH, adapter, ignore=shutil.ignore_patterns("__pycache__"))
    for name in tools:
        bundle(adapter, name)
    (adapter / "ADAPTER.md").write_text("Learned: call wordcount before you finish.\n")
    if card:
        (adapter / "CARD.md").write_text("spending: 1.23 USD\n")
    if origin:
        data = yaml.safe_load((adapter / "adapter.yaml").read_text())
        data["origin"] = {"template": "toolsmith", "version": "1.0"}
        (adapter / "adapter.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return adapter


def mwm_adapter(scratch: Path) -> Path:
    adapter = scratch / "mwm"
    adapter.mkdir()
    manifest = {
        "template": {"id": "fake-mwm", "version": "1.0", "contract": "1", "name": "Fake MWM"},
        "exports": {"context": "ADAPTER.md", "tools": [
            {"name": "wm", "description": "The world model.", "entrypoint": "interface.py:wm",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "args": {"type": "object"}},
                              "required": ["name"], "additionalProperties": False},
             "output_schema": {"type": "object"}},
            {"name": "context_package", "description": "The package.", "entrypoint": "interface.py:context_package",
             "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
             "output_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]},
        "structure": {"learnable": [{"path": "ADAPTER.md", "format": {"text": True}}], "fixed": ["interface.py"]},
        "origin": {"template": "fake-mwm", "version": "1.0"}}
    (adapter / "adapter.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    (adapter / "ADAPTER.md").write_text("context file\n")
    (adapter / "interface.py").write_text(
        "def wm(name, args=None):\n    return {'op': name, 'ok': True}\n\n"
        "def context_package():\n    return {'text': 'FIVE-PART PACKAGE'}\n")
    return adapter


@pytest.fixture
def runner(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(agent, "RUNNER", fake)
    monkeypatch.setattr(agent, "FINAL_CAPTURE", fake_capture)
    monkeypatch.setattr(agent.harness, "check_vendor", lambda: None)
    return fake


def scripted(monkeypatch, replies: list[dict]) -> FakeClient:
    client = FakeClient(replies)
    monkeypatch.setattr(agent.pipeline, "make_client", lambda: client)
    return client


def task() -> dict:
    return {"id": "waves", "case": "waves", "prompt": "Make waves.\n\n[data-testid=\"scene\"] must be present."}


def new_agent(scratch: Path, **params) -> agent.VisualAgent:
    return agent.VisualAgent({"logs": str(scratch / "logs"), **params}, base=scratch)


PAGE = "<!doctype html><html><body><canvas data-testid='scene'></canvas></body></html>"


# --- the run: arm B' -----------------------------------------------------------------------------------------------
def test_harness_arm_writes_the_page_runs_a_command_and_packs_its_images(scratch, monkeypatch, runner):
    client = scripted(monkeypatch, [
        {"tool_calls": [tc("write_file", {"path": "index.html", "content": PAGE})]},
        {"tool_calls": [tc("run_command", {"command": "png shot.png; echo rendered", "timeout_s": 30}, "c2")]},
        {"content": "I wrote the page and looked at shot.png; no unresolved limitations."}])
    out = new_agent(scratch).run(task(), None)
    base = Path(out.log)
    result = json.loads((base / "result.json").read_text())
    assert set(agent.RESULT_KEYS) <= set(result)
    assert result["arm"] == "harness" and result["status"] == "completed" and result["valid_submission"] is True
    assert result["model_calls"] == 3 and result["tool_calls"] == 2 and result["run_commands"] == 1
    assert result["image_bearing_calls"] == 1 and result["looked_calls"] == 1 and result["looked_blank_calls"] == 0
    assert result["first_image_call"] == 2 and result["first_look_call"] == 2 and result["looked_final"] is True
    assert result["images_total"] == 1 and result["inspection_calls"] == 0 and result["confined"] is True
    assert result["tools_called"] == {"write_file": 1, "run_command": 1} and result["tool_errors"] == 0
    assert result["context_exhausted"] is False and result["tools_exported"] == []
    request = json.loads((base / "request.json").read_text())
    assert request["system"] == texts.ACTING_SYSTEM_PROMPT
    assert [t["function"]["name"] for t in request["tools"]] == list(texts.PRIMITIVES)
    assert "node" not in request["system"].lower() and "chrom" not in request["system"].lower()
    # The command's record and per-call directory.
    record = json.loads((base / "tools" / "02.json").read_text())
    assert record["confined"] is True and record["bwrap_argv_sha"] == "ab" * 32 and record["browser_launched"] is True
    assert record["result"]["stdout"] == "rendered\n" and record["result"]["exit_code"] == 0
    image = record["image_records"][0]
    assert image["file"] == "shot.png" and image["nonblank"] is True and image["packed_for_model"] is True
    assert image["width"] == 320 and image["height"] == 240 and len(image["sha256"]) == 64
    assert record["index_sha_at_call"] == result["submission_sha256"]
    assert not (base / "cmd-02" / "tmp").exists() and (base / "cmd-02" / "index.html").read_text() == PAGE
    assert runner.calls[0]["argv"] == ["bash", "-c", "png shot.png; echo rendered"] and runner.calls[0]["timeout_s"] == 30
    assert runner.calls[0]["lock"] == "shared" and runner.calls[0]["tmp_dir"].endswith("cmd-02/tmp")
    # The model saw the image in a user message after the tool message, with the intro text.
    third = client.requests[2]["messages"]
    assert third[-1]["role"] == "user" and third[-1]["content"][0]["text"] == texts.IMAGES_INTRO
    part = third[-1]["content"][2]
    assert part["type"] == "image_url" and part["image_url"]["url"].startswith("data:image/png;base64,")
    tool_message = json.loads(third[-2]["content"])
    assert tool_message["images"][0] == {"file": "shot.png", "bytes": image["bytes"], "width": 320, "height": 240,
                                         "packed_for_model": True}
    assert "nonblank" not in tool_message["images"][0] and "reasoning_content" not in json.dumps(client.requests[2]["options"])
    assert client.requests[2]["messages"][2]["role"] == "assistant" and "reasoning_content" in client.requests[2]["messages"][2]
    assert "reasoning_content" not in json.loads((base / "calls" / "01.json").read_text())["assistant"]
    # What the judge needs and what the Trainer gets.
    assert (base / "capture" / "summary.json").is_file() and (base / "submission" / "index.html").read_text() == PAGE
    assert (base / "final.md").read_text().startswith("I wrote the page")
    assert out.output["final_message"].startswith("I wrote the page") and out.output["status"] == "completed"
    assert set(out.output) == {"case", "arm", "status", "stop_reason", "model_calls", "tool_calls", "run_commands",
                               "adapter_tool_calls", "image_bearing_calls", "looked_calls", "first_image_call",
                               "tools_exported", "tools_called", "tool_errors", "final_message"}
    assert out.extra["calls"] == [{"call": 1, "name": "write_file", "exit_code": None, "images": 0, "looked": False},
                                  {"call": 2, "name": "run_command", "exit_code": 0, "images": 1, "looked": True}]
    assert out.cost_usd == pytest.approx(0.003) and out.tokens == 3150 and out.log == base


def test_images_replace_earlier_image_messages_and_no_artifact_status(scratch, monkeypatch, runner):
    client = scripted(monkeypatch, [
        {"tool_calls": [tc("run_command", {"command": "png a.png"})]},
        {"tool_calls": [tc("run_command", {"command": "png b.png"}, "c2"), tc("read_file", {"path": "README.md"}, "c3")]},
        {"content": "done"}])
    out = new_agent(scratch).run(task(), None)
    result = out.extra
    assert result["status"] == "no_artifact" and result["valid_submission"] is False
    assert result["image_bearing_calls"] == 2 and result["looked_final"] is False
    messages = client.requests[2]["messages"]
    users = [m for m in messages if m["role"] == "user"]
    assert users[1]["content"] == pipeline.IMAGES_REMOVED and isinstance(users[2]["content"], list)
    assert sum(1 for m in messages if isinstance(m.get("content"), list)) == 1
    second = json.loads((Path(out.log) / "tools" / "02.json").read_text())
    assert [i["file"] for i in second["image_records"]] == ["b.png"]      # a.png was unchanged: not sent again


def test_facts_file_is_appended_after_the_tool_instructions(scratch, monkeypatch, runner):
    facts = scratch / "environment.md"
    facts.write_text("## Environment facts\nF1 headless only.\n")
    scripted(monkeypatch, [{"content": "nothing"}])
    out = new_agent(scratch, facts_file=str(facts)).run(task(), None)
    request = json.loads((Path(out.log) / "request.json").read_text())
    assert request["system"] == texts.ACTING_SYSTEM_PROMPT + "\n\n## Environment facts\nF1 headless only."
    assert request["facts_file"] == str(facts) and request["arm"] == "harness"


def test_context_guard_stops_after_a_large_prompt(scratch, monkeypatch, runner):
    client = scripted(monkeypatch, [
        {"tool_calls": [tc("write_file", {"path": "index.html", "content": PAGE})], "prompt_tokens": 95_000},
        {"content": "never requested"}])
    out = new_agent(scratch).run(task(), None)
    assert out.extra["status"] == "context_exhausted" and out.extra["context_exhausted"] is True
    assert out.extra["model_calls"] == 1 and out.extra["valid_submission"] is True and len(client.requests) == 1
    assert "95000" in out.extra["stop_reason"]


def test_api_error_is_recorded_with_the_key_redacted(scratch, monkeypatch, runner):
    monkeypatch.setenv(pipeline.KEY_ENV, "sk-secret")
    scripted(monkeypatch, [])
    out = new_agent(scratch).run(task(), None)
    assert out.extra["status"] == "api_error" and "sk-secret" not in out.extra["stop_reason"]
    assert "[REDACTED]" in json.loads((Path(out.log) / "calls" / "01.json").read_text())["error"]


# --- arms and adapters -----------------------------------------------------------------------------------------------
def test_arm_detection_root_without_origin_is_harness(scratch, monkeypatch, runner):
    root = toolsmith_adapter(scratch, origin=False, card=False)
    assert pipeline.arm_of(None) == "harness" and pipeline.arm_of(root) == "harness"
    scripted(monkeypatch, [{"content": "x"}])
    out = new_agent(scratch).run(task(), root)
    request = json.loads((Path(out.log) / "request.json").read_text())
    assert out.extra["arm"] == "harness" and request["system"] == texts.ACTING_SYSTEM_PROMPT
    assert request["tools_exported"] == [] and request["adapter"] == str(root)


def test_toolsmith_adapter_exposes_its_catalog_and_packs_tool_images(scratch, monkeypatch, runner):
    adapter = toolsmith_adapter(scratch, tools=("wordcount", "Shot"))
    assert pipeline.arm_of(adapter) == "adapted"
    client = scripted(monkeypatch, [
        {"tool_calls": [tc("write_file", {"path": "index.html", "content": PAGE})]},
        {"tool_calls": [tc("wordcount", {"file": "index.html"}, "c2")]},
        {"content": "looked with wordcount"}])
    out = new_agent(scratch).run(task(), adapter)
    base = Path(out.log)
    request = json.loads((base / "request.json").read_text())
    assert out.extra["arm"] == "adapted" and request["tools_exported"] == ["wordcount"]
    assert [t["function"]["name"] for t in request["tools"]] == [*texts.PRIMITIVES, "wordcount"]
    spec = request["tools"][-1]["function"]
    assert spec["parameters"] == {"type": "object", "properties": {"file": {"type": "string", "default": "index.html"}},
                                  "required": [], "additionalProperties": False}
    assert request["tools_rejected"][0]["name"] == "Shot" and request["tools_skipped"] == []
    system = request["system"]
    assert system.startswith(texts.ACTING_SYSTEM_PROMPT + "\n\n" + texts.ADAPTED + "\n\nLearned: call wordcount")
    assert system.endswith(texts.ADAPTER_TOOLS_NOTE) and "spending" not in system and texts.BRIDGE not in system
    # The tool ran through the confined runner in the workspace with the bundle bound at /tool, CARD.md hidden.
    call = runner.calls[0]
    assert call["argv"][0] == "python3" and call["argv"][-2] == "/tool/run.py"
    assert json.loads(call["argv"][-1]) == {"file": "index.html"}
    assert call["cwd"] == str(base / "workspace") and call["ro"][0][1] == "/tool" and call["ro"][0][0].endswith("tools/wordcount")
    assert call["card_present"] is False and call["lock"] == "shared" and call["timeout_s"] == 30
    assert call["tmp_dir"].endswith("cmd-02/tmp") and call["trace"].endswith("cmd-02/trace")
    record = json.loads((base / "tools" / "02.json").read_text())
    assert record["name"] == "wordcount" and record["confined"] is True and record["bwrap_argv_sha"] == "ab" * 32
    assert record["image_records"][0]["file"] == "tool.png" and record["image_records"][0]["packed_for_model"] is True
    assert record["result"]["text"] == "wordcount: 3 lines" and record["result"]["exit_code"] == 0
    assert out.extra["adapter_tool_calls"] == 1 and out.extra["tools_called"]["wordcount"] == 1
    assert out.extra["image_bearing_calls"] == 1 and out.extra["looked_calls"] == 1 and out.extra["first_image_call"] == 2
    tool_message = json.loads(client.requests[2]["messages"][-2]["content"])
    assert set(tool_message) >= {"text", "exit_code", "seconds", "timed_out", "lock_wait_s", "error", "images", "stderr_tail"}
    assert tool_message["images"][0]["file"] == "tool.png"
    assert client.requests[2]["messages"][-1]["content"][0]["text"] == texts.IMAGES_INTRO
    assert not (base / "cmd-02" / "tmp").exists() and (base / "cmd-02" / "index.html").is_file()


def test_toolsmith_tool_listing_an_outside_path_or_symlink_gets_nothing_packed(scratch, monkeypatch, runner):
    adapter = toolsmith_adapter(scratch, card=False)
    runner.tool_images = ["../outside.png", "link.png"]
    runner.outside = make_png(scratch / "outside.png")
    scripted(monkeypatch, [{"tool_calls": [tc("wordcount", {})]}, {"content": "x"}])
    out = new_agent(scratch).run(task(), adapter)
    workspace = Path(out.log) / "workspace"
    record = json.loads((Path(out.log) / "tools" / "01.json").read_text())
    assert record["image_records"] == [] and out.extra["image_bearing_calls"] == 0 and out.extra["adapter_tool_calls"] == 1
    problems = {r["path"]: r["problem"] for r in record["images_rejected"]}
    assert problems == {"../outside.png": "outside the workspace", "link.png": "a symbolic link"}
    assert record["symlinks_removed"] == ["link.png"] and not (workspace / "link.png").exists()
    assert (scratch / "outside.png").is_file()


def test_mwm_adapter_gets_bridge_package_and_read_only_wm(scratch, monkeypatch, runner):
    adapter = mwm_adapter(scratch)
    scripted(monkeypatch, [
        {"tool_calls": [tc("wm", {"name": "run", "args": {}}), tc("wm", {"name": "list"}, "c2")]},
        {"content": "x"}])
    out = new_agent(scratch).run(task(), adapter)
    base = Path(out.log)
    request = json.loads((base / "request.json").read_text())
    assert request["system"] == texts.ACTING_SYSTEM_PROMPT + "\n\n" + texts.BRIDGE + "\n\nFIVE-PART PACKAGE"
    wm = request["tools"][-1]["function"]
    assert wm["name"] == "wm" and wm["description"] == "The world model." + texts.WM_SUFFIX
    first = json.loads((base / "tools" / "01.json").read_text())["result"]
    second = json.loads((base / "tools" / "02.json").read_text())["result"]
    assert first["ok"] is False and "read-only" in first["error"] and second == {"op": "list", "ok": True}
    assert out.extra["wm_calls"] == 2 and out.extra["tool_errors"] == 1 and out.extra["tools_called"] == {"wm": 2}
    assert runner.calls == []


# --- the harness on its own ----------------------------------------------------------------------------------------
def host_for(scratch: Path, run_fn=None) -> tuple[gh.Harness, Path]:
    workspace, run = scratch / "ws", scratch / "run"
    workspace.mkdir()
    run.mkdir()
    (workspace / "README.md").write_text("the task\nline two\nline three\n")
    (workspace / "index.html").write_text(pipeline.PLACEHOLDER)
    return gh.Harness(workspace, run, "waves", runner=gh.ConfinedRunner(run_fn or FakeRun())), workspace


def far() -> float:
    return time.monotonic() + 600


def test_file_tools_paths_and_caps(scratch):
    host, ws = host_for(scratch)
    text, parts = host.dispatch("write_file", {"path": "lib/helper.js", "content": "x"}, far())
    assert json.loads(text)["file"] == "lib/helper.js" and (ws / "lib" / "helper.js").read_text() == "x" and parts == []
    for bad in ("../escape.html", "/etc/passwd", "", "lib/../../x"):
        assert json.loads(host.dispatch("write_file", {"path": bad, "content": "x"}, far())[0])["ok"] is False
    assert json.loads(host.dispatch("write_file", {"path": str(ws / "abs.txt"), "content": "ok"}, far())[0])["file"] == "abs.txt"
    big = json.loads(host.dispatch("write_file", {"path": "big.txt", "content": "x" * 500_001}, far())[0])
    assert big["ok"] is False and "ValidationError" in big["error"]
    read = json.loads(host.dispatch("read_file", {"path": "README.md", "start_line": 2, "max_lines": 1}, far())[0])
    assert read == {"ok": True, "file": "README.md", "total_lines": 3, "text": "2: line two"}
    assert json.loads(host.dispatch("read_file", {"path": "missing.txt"}, far())[0])["ok"] is False
    os.symlink("/etc/hostname", ws / "link.txt")
    assert "symbolic link" in json.loads(host.dispatch("read_file", {"path": "link.txt"}, far())[0])["error"]
    (ws / "index.html").write_text("<p>a b a</p>")
    twice = json.loads(host.dispatch("replace_text", {"path": "index.html", "old": "a", "new": "c"}, far())[0])
    assert twice["ok"] is False and "found 2" in twice["error"]
    once = json.loads(host.dispatch("replace_text", {"path": "index.html", "old": "b", "new": "c"}, far())[0])
    assert once["ok"] is True and (ws / "index.html").read_text() == "<p>a c a</p>"
    assert json.loads(host.dispatch("inspect_page", {}, far())[0])["error"] == "ValueError: Unknown tool"
    assert json.loads(host.dispatch("read_file", None, far())[0])["error"].startswith("ValueError: Tool arguments")
    assert host.calls == 14 and host.tool_errors == 10
    assert len(list((scratch / "run" / "tools").glob("*.json"))) == 14


def test_run_command_image_caps_symlinks_and_rejections(scratch):
    fake = FakeRun()
    host, ws = host_for(scratch, fake)
    outside = make_png(scratch / "outside.png")
    command = ("fat fat.png; huge huge.png; png p1.png; png p2.png; png p3.png; png p4.png; png p5.png; png p6.png; jpeg j.jpg;"
               f" symlink link.png {outside}; png sub/deep.png")
    text, parts = host.dispatch("run_command", {"command": command}, far())
    result = json.loads(text)
    record = json.loads((scratch / "run" / "tools" / "01.json").read_text())
    assert [i["file"] for i in record["image_records"]] == ["p1.png", "p2.png", "p3.png", "p4.png", "p5.png"]
    problems = {r["file"]: r["problem"] for r in record["images_rejected"]}
    assert problems["p6.png"].startswith("more than 5") and problems["j.jpg"].startswith("more than 5")
    assert problems["sub/deep.png"].startswith("more than 5")
    assert problems["fat.png"].startswith("2100008 bytes") and "3000x3000" in problems["huge.png"]
    assert "link.png" not in problems and not (ws / "link.png").exists() and record["symlinks_removed"] == ["link.png"]
    assert len(parts) == 10 and host.images_total == 5 and result["images"][4]["packed_for_model"] is True
    # The per-run budget: the next command gets nothing more.
    host.images_total = gh.MAX_IMAGES_PER_RUN
    text, parts = host.dispatch("run_command", {"command": "png late.png; jpeg late.jpg"}, far())
    rejected = {r["file"]: r["problem"] for r in json.loads(text)["images_rejected"]}
    assert rejected == {"late.png": "image budget exhausted", "late.jpg": "image budget exhausted"} and parts == []
    # A JPEG is packed with its mime type; nonblank is unknown for it.
    host.images_total = 0
    text, parts = host.dispatch("run_command", {"command": "jpeg only.jpg"}, far())
    record = json.loads((scratch / "run" / "tools" / "03.json").read_text())
    assert record["image_records"][0]["nonblank"] is None and record["image_records"][0]["width"] == 400
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert host.image_bearing_calls == 2 and host.looked_calls == 1 and host.looked_blank_calls == 1


def test_blank_images_and_no_browser_are_not_looks(scratch):
    fake = FakeRun(browser=False)
    host, _ = host_for(scratch, fake)
    host.dispatch("run_command", {"command": "png white.png 400 300 blank"}, far())
    assert host.image_bearing_calls == 1 and host.looked_calls == 0 and host.looked_blank_calls == 0
    fake.browser = True
    host.dispatch("run_command", {"command": "png white2.png 400 300 blank"}, far())
    assert host.looked_blank_calls == 1 and host.looked_calls == 0 and host.first_look_call is None
    host.dispatch("run_command", {"command": "png tiny.png 100 80"}, far())     # smaller than 320x240: blank by rule
    assert host.looked_blank_calls == 2
    host.dispatch("run_command", {"command": "png real.png 400 300"}, far())
    assert host.looked_calls == 1 and host.first_look_call == 4 and host.first_image_call == 1
    record = json.loads((scratch / "run" / "tools" / "04.json").read_text())
    assert record["image_records"][0]["stats"]["nonuniform_fraction"] > 0.5


def test_time_refusals_timeout_clamp_and_failed_commands(scratch):
    fake = FakeRun()
    host, _ = host_for(scratch, fake)
    soon = time.monotonic() + 30
    refused = json.loads(host.dispatch("run_command", {"command": "echo hi"}, soon)[0])
    assert refused["ok"] is False and "Less than 60 seconds" in refused["error"] and fake.calls == []
    host.dispatch("run_command", {"command": "echo hi", "timeout_s": 90}, time.monotonic() + 100)
    assert fake.calls[-1]["timeout_s"] == pytest.approx(80, abs=0.5)
    host.dispatch("run_command", {"command": "echo hi"}, far())
    assert fake.calls[-1]["timeout_s"] == 60
    failed = json.loads(host.dispatch("run_command", {"command": "fail"}, far())[0])
    assert failed["exit_code"] == 1 and failed["stderr"] == "boom" and host.run_commands == 3 and host.tool_errors == 1
    host.calls = gh.MAX_TOOL_CALLS
    assert "budget exhausted" in json.loads(host.dispatch("read_file", {"path": "README.md"}, far())[0])["error"]


def test_tool_text_caps(scratch):
    host, ws = host_for(scratch)
    (ws / "long.txt").write_text("\n".join("y" * 200 for _ in range(400)))
    text = host.dispatch("read_file", {"path": "long.txt", "max_lines": 500}, far())[0]
    assert json.loads(text)["truncated"] is True and len(text) <= gh.RESULT_CHARS
    host.text_chars = gh.TOOL_TEXT_CAP - 10
    text = host.dispatch("read_file", {"path": "README.md"}, far())[0]
    rendered = json.loads(text)
    assert rendered["truncated"] is True and "cumulative" in rendered["reason"] and len(rendered["tail"]) <= gh.TAIL_CHARS
    (scratch / "two").mkdir()
    host2, _ = host_for(scratch / "two", FakeRun())
    text = host2.dispatch("run_command", {"command": "echo " + "z" * 7000}, far())[0]
    shown = json.loads(text)["stdout"]
    assert len(shown) < 7000 and "clipped to 6000" in shown
    assert len(json.loads((scratch / "two" / "run" / "tools" / "01.json").read_text())["result"]["stdout"]) == 7001


def test_adapter_tool_budget(scratch):
    host, _ = host_for(scratch)
    host.tools.append({"type": "function", "function": {"name": "shot", "description": "d",
                                                        "parameters": {"type": "object", "properties": {}}}})
    host.schemas["shot"] = {"type": "object", "properties": {}}
    host.adapter_names.append("shot")
    host.adapter_calls = gh.MAX_ADAPTER_CALLS
    assert "adapter tool budget" in json.loads(host.dispatch("shot", {}, far())[0])["error"]


def test_expose_catalog_sanitises_and_skips():
    catalog = {"tools": [
        {"name": "shot", "description": "Take a picture.", "parameters": {
            "type": "object", "properties": {"width": {"type": "integer", "pattern": "x", "minimum": 1},
                                             "list": {"type": "array", "items": {"type": "string", "format": "uri"}}},
            "required": ["width", "ghost"], "additionalProperties": True, "$schema": "x"}},
        {"name": "Shot", "description": "d", "parameters": {}},
        {"name": "run_command", "description": "d", "parameters": {}},
        {"name": "shot", "description": "d", "parameters": {}},
        {"name": "silent", "description": "", "parameters": {}},
        {"name": "plain", "description": "x" * 2000, "parameters": "not a schema"}],
        "rejected": [{"name": "broken", "problem": "no entry"}]}
    specs, skipped = gh.expose_catalog(catalog)
    assert [s["function"]["name"] for s in specs] == ["shot", "plain"]
    assert specs[0]["function"]["parameters"] == {
        "type": "object", "properties": {"width": {"type": "integer", "minimum": 1},
                                         "list": {"type": "array", "items": {"type": "string"}}},
        "required": ["width"], "additionalProperties": False}
    assert specs[1]["function"]["parameters"] == {"type": "object", "properties": {}, "required": [],
                                                  "additionalProperties": False}
    assert len(specs[1]["function"]["description"]) == gh.MAX_DESCRIPTION
    assert [(s["name"], s["problem"].split(" ")[0]) for s in skipped] == [
        ("Shot", "not"), ("run_command", "collides"), ("shot", "duplicate"), ("silent", "no")]
    assert gh.expose_catalog({}) == ([], []) and gh.expose_catalog(None) == ([], [])


def test_pack_images_batch_cap(scratch):
    a, b = make_png(scratch / "a.png", 320, 240), make_png(scratch / "b.png", 320, 240)
    size = len(a.read_bytes()) * 4 // 3 + 4
    parts, records = gh.pack_images([a, b], cap=size + 10, labels=["a.png", "b.png"])
    assert len(parts) == 2 and records[0]["packed_for_model"] is True and records[1]["packed_for_model"] is False
    assert "batch cap" in records[1]["omitted"] and parts[0]["text"].startswith("a.png")


def test_scan_and_describe(scratch):
    ws = scratch / "ws"
    ws.mkdir()
    before = gh.scan_images(ws)
    make_png(ws / "one.png")
    (ws / "note.txt").write_text("not an image")
    after = gh.scan_images(ws)
    assert gh.changed_images(before, after) == ["one.png"] and gh.changed_images(after, after) == []
    (ws / "one.png").write_bytes(make_png(scratch / "tmp.png", 321, 240).read_bytes())
    assert gh.changed_images(after, gh.scan_images(ws)) == ["one.png"]
    record, problem = gh.describe_image(ws, "one.png")
    assert problem is None and record["width"] == 321 and record["nonblank"] is True
    assert gh.describe_image(ws, "note.txt") == (None, "not a PNG or JPEG file")
    assert gh.describe_image(ws, "../tmp.png")[1] == "'../tmp.png' lies outside the workspace"
    assert gh.describe_image(ws, str(ws / "one.png"))[0]["file"] == "one.png"


# --- the real sandbox and the settings check ---------------------------------------------------------------------
@needs_bwrap
def test_real_confine_run_through_the_harness(scratch):
    host, _ = host_for(scratch, agent.confine.run)
    text, parts = host.dispatch("run_command", {"command": "echo hi; pwd", "timeout_s": 20}, far())
    result = json.loads(text)
    assert result["exit_code"] == 0 and result["stdout"].startswith("hi\n") and parts == []
    assert result["timed_out"] is False and result["images"] == []
    record = json.loads((scratch / "run" / "tools" / "01.json").read_text())
    assert record["confined"] is True and len(record["bwrap_argv_sha"]) == 64 and record["browser_launched"] is False
    assert not (scratch / "run" / "cmd-01" / "tmp").exists() and (scratch / "run" / "cmd-01" / "trace").is_file()


def test_hermit_check_loads_the_agent(scratch):
    config = {
        "data": {"tasks": str(UTILS / "tasks.jsonl"), "train": str(UTILS / "splits" / "waves.txt"),
                 "test": str(UTILS / "splits" / "waves-eval.txt"), "step_size": 1},
        "task_info": str(UTILS / "task-info" / "waves"),
        "agent": {"name": f"{UTILS / 'agent' / 'agent.py'}:VisualAgent", "concurrency": 1, "params": {"facts_file": None}},
        "judge": {"path": str(UTILS / "judge.py")},
        "template": "prompt-only@1.0",
        "evolver": {"name": "prompt_only", "params": {"max_chars": 6000}},
        "llm": {"base_url": pipeline.BASE_URL, "model": pipeline.MODEL, "api_key_env": pipeline.KEY_ENV, "price": pipeline.PRICE},
        "sandbox": {"enabled": True, "network": "none", "env": []},
        "trainer": {"run_name": "wp2-check", "output_dir": str(scratch / "runs"), "tree_dir": str(scratch / "trees"),
                    "epochs": 1, "budget": {"usd": 0.1, "hours": 1}}}
    if not (UTILS / "tasks.jsonl").is_file() or not (UTILS / "task-info" / "waves" / "README.md").is_file():
        pytest.skip("utils/tasks.jsonl or task-info/waves are not prepared here (python utils/prepare.py)")
    path = scratch / "check.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    exe = Path(sys.executable).parent / "hermit"
    proc = subprocess.run([str(exe), "check", "--config", str(path), "--no-probe"], capture_output=True, text=True,
                          cwd=str(scratch), timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[ok] task" in proc.stdout and "PROBLEM" not in proc.stdout
