"""The generic acting agent (utils/agent/agent.py + generic_harness.py) without a model and without bubblewrap: a
scripted ChatClient replaces pipeline.make_client, a fake confine.run writes images into the workspace, a fake final
capture writes capture/summary.json. Scratch directories live outside /tmp."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import time
import zlib

import pytest
import yaml

from hermit.evolver.base import Usage

UTILS = Path(__file__).resolve().parents[1]
REPO = UTILS.parents[2]
TOOLSMITH = REPO / "hermit" / "algorithms" / "templates" / "toolsmith"


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
def make_png(path: Path, width: int = 320, height: int = 240) -> Path:
    """An 8-bit RGB PNG pngstats decodes: a gradient (non-blank)."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        rows += bytes(v for x in range(width)
                      for v in (x * 255 // max(1, width - 1), y * 255 // max(1, height - 1), (x + y) & 255))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b""))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(data)
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
    """A confine.run stand-in. bash -c commands are a tiny language, one statement per ';': `png NAME`, `echo TEXT`.
    Toolsmith bundle calls (python3 bootstrap) write tool.png and print the JSON result line."""

    def __init__(self):
        self.calls: list[dict] = []
        self.tool_images = ["tool.png"]
        self.outside: Path | None = None        # a tool listing "link.png" plants a symlink to this file

    def __call__(self, argv, cwd, timeout_s, **kw):
        cwd = Path(cwd)
        record = {"exit_code": 0, "stdout": "", "stderr": "", "seconds": 0.01, "timed_out": False, "lock_wait_s": 0.0,
                  "dropped_bytes": 0, "bwrap_argv_sha": "ab" * 32, "browser_launched": True, "error": None}
        call = {"argv": list(argv), "cwd": str(cwd), "timeout_s": timeout_s, **kw}
        self.calls.append(call)
        out = []
        if argv[0] == "bash" and argv[1] == "-c":
            for statement in argv[2].split(";"):
                words = statement.split()
                if not words:
                    continue
                if words[0] == "png":
                    make_png(cwd / words[1])
                elif words[0] == "echo":
                    out.append(" ".join(words[1:]))
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


def toolsmith_adapter(scratch: Path, *, card: bool = True, tools=("wordcount",)) -> Path:
    adapter = scratch / "adapter"
    shutil.copytree(TOOLSMITH, adapter, ignore=shutil.ignore_patterns("__pycache__"))
    for name in tools:
        bundle(adapter, name)
    (adapter / "ADAPTER.md").write_text("Learned: call wordcount before you finish.\n")
    if card:
        (adapter / "CARD.md").write_text("spending: 1.23 USD\n")
    data = yaml.safe_load((adapter / "adapter.yaml").read_text())
    data["origin"] = {"template": "toolsmith", "version": "1.1"}
    (adapter / "adapter.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
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


def new_agent(scratch: Path) -> agent.VisualAgent:
    return agent.VisualAgent({"logs": str(scratch / "logs")}, base=scratch)


PAGE = "<!doctype html><html><body><canvas data-testid='scene'></canvas></body></html>"


# --- the run ---------------------------------------------------------------------------------------------------------
def test_harness_arm_writes_the_page_runs_a_command_and_packs_its_images(scratch, monkeypatch, runner):
    client = scripted(monkeypatch, [
        {"tool_calls": [tc("write_file", {"path": "index.html", "content": PAGE})]},
        {"tool_calls": [tc("run_command", {"command": "png shot.png; echo rendered", "timeout_s": 30}, "c2")]},
        {"content": "I wrote the page and looked at shot.png; no unresolved limitations."}])
    out = new_agent(scratch).run(task(), None)
    base = Path(out.log)
    result = json.loads((base / "result.json").read_text())
    assert set(agent.RESULT_KEYS) <= set(result)
    # no time limit on the run; a request is abandoned only when it hangs for REQUEST_GUARD_S
    assert agent.RUN_SECONDS is None and client.timeout == agent.REQUEST_GUARD_S == 1800
    assert result["arm"] == "harness" and result["status"] == "completed" and result["valid_submission"] is True
    assert result["model_calls"] == 3 and result["tool_calls"] == 2 and result["run_commands"] == 1
    assert result["image_bearing_calls"] == 1 and result["looked_calls"] == 1 and result["looked_blank_calls"] == 0
    assert result["first_image_call"] == 2 and result["first_look_call"] == 2 and result["looked_final"] is True
    assert result["images_total"] == 1 and result["confined"] is True
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
                               "tools_exported", "tools_called", "tool_errors", "seen_final", "finalize",
                               "final_message"}
    assert out.extra["calls"] == [{"call": 1, "name": "write_file", "exit_code": None, "images": 0, "looked": False},
                                  {"call": 2, "name": "run_command", "exit_code": 0, "images": 1, "looked": True}]
    assert out.cost_usd == pytest.approx(0.003) and out.tokens == 3150 and out.log == base


def test_api_error_is_recorded_with_the_key_redacted(scratch, monkeypatch, runner):
    monkeypatch.setenv(pipeline.KEY_ENV, "sk-secret")
    scripted(monkeypatch, [])
    out = new_agent(scratch).run(task(), None)
    assert out.extra["status"] == "api_error" and "sk-secret" not in out.extra["stop_reason"]
    assert "[REDACTED]" in json.loads((Path(out.log) / "calls" / "01.json").read_text())["error"]


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
    assert system.endswith(texts.ADAPTER_TOOLS_NOTE) and "spending" not in system
    # The tool ran through the confined runner in the workspace with the bundle bound at /tool.
    call = runner.calls[0]
    assert call["argv"][0] == "python3" and call["argv"][-2] == "/tool/run.py"
    assert json.loads(call["argv"][-1]) == {"file": "index.html"}
    assert call["cwd"] == str(base / "workspace") and call["ro"][0][1] == "/tool" and call["ro"][0][0].endswith("tools/wordcount")
    assert call["lock"] == "shared" and call["timeout_s"] == 30
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


PAGE2 = PAGE.replace("<body>", "<body><p>edited after the last look</p>")


def test_toolsmith_finalize_submits_the_last_version_the_model_saw(scratch, monkeypatch, runner):
    adapter = toolsmith_adapter(scratch)
    scripted(monkeypatch, [
        {"tool_calls": [tc("write_file", {"path": "index.html", "content": PAGE})]},
        {"tool_calls": [tc("wordcount", {}, "c2")]},                                   # seen: request 3 carries it
        {"tool_calls": [tc("write_file", {"path": "index.html", "content": PAGE2}, "c3")]},
        {"content": "done"}])
    out = new_agent(scratch).run(task(), adapter)
    base = Path(out.log)
    result = json.loads((base / "result.json").read_text())
    assert result["seen_pages"] == [{"call": 2, "sha": hashlib.sha256(PAGE.encode()).hexdigest()}]
    assert result["finalize"]["replaced"] is True and result["finalize"]["submitted_call"] == 2
    assert result["finalize"]["final_sha"] == hashlib.sha256(PAGE2.encode()).hexdigest()
    assert (base / "submission" / "index.html").read_text() == PAGE and result["seen_final"] is True
    assert (base / "unseen_final" / "index.html").read_text() == PAGE2
    assert (base / "workspace" / "index.html").read_text() == PAGE2      # the agent's workspace is left as it was


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


# --- the harness on its own ----------------------------------------------------------------------------------------
def host_for(scratch: Path) -> tuple[gh.Harness, Path]:
    workspace, run = scratch / "ws", scratch / "run"
    workspace.mkdir()
    run.mkdir()
    (workspace / "README.md").write_text("the task\nline two\nline three\n")
    (workspace / "index.html").write_text(pipeline.PLACEHOLDER)
    return gh.Harness(workspace, run, runner=gh.ConfinedRunner(FakeRun())), workspace


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
