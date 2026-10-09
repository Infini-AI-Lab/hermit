"""harness.py: the acting agent's tools, ported from the old test_harness.py. Fake capture; never launches Chrome."""
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

TASK = Path(__file__).resolve().parents[2] / "examples" / "tasks" / "visual_showcase"
sys.path.insert(0, str(TASK))
import harness  # noqa: E402

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")
HTML = "<!doctype html><html><body>ORIGINAL_MARK</body></html>"


def synthetic_capture(workspace, output, case, timeout=90):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "desktop-t5.png").write_bytes(PNG)
    summary = {"case": case, "capture_ok": True, "status": "synthetic_test_only",
               "input": {"index_sha256": harness.sha(Path(workspace) / "index.html")},
               "screenshots": [{"id": "desktop-t5", "file": "desktop-t5.png", "label": "Synthetic fixture",
                                "sha256": hashlib.sha256(PNG).hexdigest()}]}
    harness.save(output / "summary.json", summary)
    return summary


class Host:
    def __init__(self, root: Path, adapter=None):
        self.root = root
        self.workspace = root / "workspace"
        self.workspace.mkdir(exist_ok=True)
        (self.workspace / "index.html").write_text(HTML)
        (self.workspace / "README.md").write_text("line one\nline two\nline three")
        self.harness = harness.Harness(self.workspace, root / "run", "waves", adapter)
        self.deadline = time.monotonic() + 600

    def call(self, name, args, deadline=None):
        text, images = self.harness.dispatch(name, args, self.deadline if deadline is None else deadline)
        return json.loads(text), images

    def index(self) -> str:
        return (self.workspace / "index.html").read_text()


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def test_extraction_raw_and_single_fence_only():
    assert harness.extract_html("\n " + HTML + " \n") == (HTML, "raw_html")
    assert harness.extract_html("Here is the file:\n```html\n" + HTML + "\n```") == (HTML, "single_html_fence")
    for value in ["done", "<div>fragment</div>", "```html\n" + HTML + "\n```\n```html\n" + HTML + "\n```"]:
        with pytest.raises(ValueError):
            harness.extract_html(value)


def test_write_and_read_numbered_lines(host):
    result, _ = host.call("write_file", {"content": HTML + "\n"})
    assert result["ok"] and host.index() == HTML + "\n"
    result, _ = host.call("read_file", {"path": "README.md", "start_line": 2, "max_lines": 1})
    assert result["text"] == "2: line two"


def test_write_file_rejects_a_path_argument(host):
    """The old friction, kept: write_file has no `path`; GLM's first write with one fails and changes nothing."""
    result, _ = host.call("write_file", {"path": "index.html", "content": "changed"})
    assert not result["ok"] and "Additional properties" in result["error"]
    assert host.index() == HTML
    record = json.loads((host.root / "run" / "tools" / "01.json").read_text())
    assert record["name"] == "write_file" and not record["result"]["ok"]


def test_schema_rejects_traversal_unknown_keys_and_non_object_args(host):
    for name, args in [("read_file", {"path": "../outside.txt"}), ("read_file", {"path": "/etc/passwd"}),
                       ("read_file", {"path": "README.md", "extra": "x"}), ("write_file", None),
                       ("execute_python", {"code": "1+1"}), ("wm", {"name": "list"})]:
        result, images = host.call(name, args)
        assert not result["ok"] and images == []
    assert host.index() == HTML


def test_byte_caps_and_replace_exactly_once(host):
    for content in ["x" * 500001, "中" * 200000]:
        result, _ = host.call("write_file", {"content": content})
        assert not result["ok"] and host.index() == HTML
    result, _ = host.call("replace_text", {"old": "ORIGINAL_MARK", "new": "x" * 500001})
    assert not result["ok"] and host.index() == HTML
    result, _ = host.call("replace_text", {"old": "ORIGINAL_MARK", "new": "UPDATED_MARK"})
    assert result["ok"]
    updated = host.index()
    for old in ["not present", "html"]:
        result, _ = host.call("replace_text", {"old": old, "new": "changed"})
        assert not result["ok"] and host.index() == updated


def test_expired_deadline_prevents_write(host):
    result, _ = host.call("write_file", {"content": "changed"}, time.monotonic() - 1)
    assert not result["ok"] and host.index() == HTML


def test_tool_budget_24_requests_count_but_over_budget_does_not_execute(host):
    for i in range(24):
        result, _ = host.call("read_file", {"path": "README.md"})
        assert result["ok"]
    result, _ = host.call("write_file", {"content": "changed"})
    assert not result["ok"] and "budget" in result["error"]
    assert host.harness.calls == 25 and host.index() == HTML
    assert (host.root / "run" / "tools" / "25.json").is_file()


def test_symlinks_rejected(tmp_path):
    host = Host(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("SYNTHETIC_OUTSIDE_MARKER")
    (host.workspace / "README.md").unlink()
    (host.workspace / "README.md").symlink_to(outside)
    result, _ = host.call("read_file", {"path": "README.md"})
    assert not result["ok"] and "SYNTHETIC_OUTSIDE_MARKER" not in json.dumps(result)
    (host.workspace / "index.html").unlink()
    (host.workspace / "index.html").symlink_to(outside)
    result, _ = host.call("write_file", {"content": "changed"})
    assert not result["ok"] and outside.read_text() == "SYNTHETIC_OUTSIDE_MARKER"


def test_three_inspections_with_original_png_bytes(host, monkeypatch):
    calls = []
    monkeypatch.setattr(harness, "capture", lambda *a, **k: calls.append(a) or synthetic_capture(*a, **k))
    for index in range(3):
        result, images = host.call("inspect_page", {})
        assert result["ok"] and result["inspection"] == index + 1
        assert result["remaining_inspections"] == 2 - index
        encoded = next(p["image_url"]["url"] for p in images if p["type"] == "image_url")
        assert base64.b64decode(encoded.split(",", 1)[1]) == PNG
        assert calls[-1][1] == host.root / "run" / "checks" / f"check-{index + 1:02d}" / "capture"
    result, images = host.call("inspect_page", {})
    assert not result["ok"] and images == [] and len(calls) == 3
    assert host.harness.last_inspected_sha == harness.sha(host.workspace / "index.html")


def test_inspection_refused_with_less_than_50_seconds_left(host, monkeypatch):
    monkeypatch.setattr(harness, "capture", lambda *a, **k: pytest.fail("capture must not run"))
    result, _ = host.call("inspect_page", {}, time.monotonic() + 40)
    assert not result["ok"] and "50 seconds" in result["error"]


def test_inspection_failure_consumes_one_attempt(host, monkeypatch):
    def broken(*a, **k):
        raise TimeoutError("synthetic capture timeout")
    monkeypatch.setattr(harness, "capture", broken)
    result, images = host.call("inspect_page", {})
    assert not result["ok"] and images == [] and host.harness.inspections == 1
    assert host.harness.last_inspected_sha is None


def test_wm_only_read_only_names_pass_through(tmp_path):
    class FakeAdapter:
        tools = {"wm": {"description": "Synthetic read-only test adapter",
                        "input_schema": {"type": "object", "properties": {"name": {"type": "string"},
                                                                          "args": {"type": "object"}},
                                         "required": ["name"], "additionalProperties": False}}}

        def __init__(self):
            self.received = []

        async def call(self, name, args):
            self.received.append((name, args))
            return {"ok": True, "result": "SYNTHETIC_MEMORY"}
    adapter = FakeAdapter()
    host = Host(tmp_path, adapter)
    wm = host.harness.tools[-1]["function"]
    assert wm["name"] == "wm" and wm["description"].endswith(harness.WM_SUFFIX)
    for name in ["run", "write", "delegate"]:
        result, _ = host.call("wm", {"name": name, "args": {}})
        assert not result["ok"]
    assert adapter.received == []
    result, _ = host.call("wm", {"name": "memory", "args": {"id": "fixture"}})
    assert result["ok"] and adapter.received == [("wm", {"name": "memory", "args": {"id": "fixture"}})]
    assert host.harness.wm_calls == 4


def test_images_first_five_in_order_cap_and_traversal(tmp_path):
    shots = []
    for i in range(6):
        (tmp_path / f"{i}.png").write_bytes(PNG)
        shots.append({"file": f"{i}.png"})
    parts, records = harness.images_for_model(tmp_path, {"screenshots": shots})
    assert [r["file"] for r in records] == [f"{i}.png" for i in range(5)]
    assert sum(p["type"] == "image_url" for p in parts) == 5
    (tmp_path / "too-large.png").write_bytes(b"x" * 6_000_001)
    parts, records = harness.images_for_model(tmp_path, {"screenshots": [{"file": "too-large.png"}, {"file": "0.png"}]})
    assert "omitted" in records[0] and records[1]["packed_for_model"]
    assert [p for p in parts if p["type"] == "image_url"][0]["image_url"]["url"].endswith(base64.b64encode(PNG).decode())
    inside = tmp_path / "capture"
    inside.mkdir()
    (inside / "link.png").symlink_to(tmp_path / "0.png")
    for name in ["../0.png", "link.png"]:
        with pytest.raises(ValueError):
            harness.images_for_model(inside, {"screenshots": [{"file": name}]})


def test_capture_subprocess_gets_no_key_and_the_runtime_files(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, env=kwargs["env"], timeout=kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 1, "", "synthetic failure")
    monkeypatch.setenv("FIREWORKS_API_KEY", "SYNTHETIC_TEST_KEY")
    monkeypatch.setenv("VISUAL_NODE_PATH", "/opt/visual/node_modules")
    monkeypatch.setattr(harness, "CAPTURE_LOCK", tmp_path / "capture.lock")    # not the machine's shared lock
    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "index.html").write_text(HTML)
    summary = harness.capture(tmp_path / "ws", tmp_path / "out", "lava", timeout=12)
    assert "FIREWORKS_API_KEY" not in seen["env"] and seen["timeout"] == pytest.approx(12, abs=0.5)
    assert seen["env"]["NODE_PATH"] == "/opt/visual/node_modules"
    assert seen["argv"][1:] == [str(TASK / "runtime" / "browser_capture.cjs"), "--workspace", str(tmp_path / "ws"),
                                "--output", str(tmp_path / "out"), "--case", "lava", "--vendor",
                                str(TASK / "runtime" / "vendor" / "three.module.min.js")]
    assert summary["status"] == "capture_process_failed" and not summary["capture_ok"]
    assert (tmp_path / "out" / "controller-stderr.log").read_text() == "synthetic failure"
    with pytest.raises(ValueError):
        harness.capture(tmp_path / "ws", tmp_path / "out", "lava")


def test_check_browser_needs_node(monkeypatch):
    monkeypatch.setattr(harness, "NODE", "/nonexistent/node")
    with pytest.raises(RuntimeError, match="/nonexistent/node"):
        harness.check_browser()


PINNED = json.loads((TASK / "runtime" / "package.json").read_text())["dependencies"]
needs_node = pytest.mark.skipif(shutil.which(harness.NODE) is None, reason="node is not on PATH")


def fake_modules(root: Path, versions: dict) -> Path:
    """A node_modules folder with stand-in playwright and pngjs packages of the given versions."""
    for name, version in versions.items():
        (root / name).mkdir(parents=True)
        (root / name / "package.json").write_text(json.dumps({"name": name, "version": version, "main": "index.js"}))
        (root / name / "index.js").write_text("module.exports = {};\n")
    return root


@pytest.fixture
def bare_runtime(tmp_path, monkeypatch):
    """A runtime/ without node_modules, no module path from the environment, and VISUAL_CHROME that exists."""
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(harness, "RUNTIME", tmp_path / "runtime")
    monkeypatch.delenv("NODE_PATH", raising=False)
    monkeypatch.delenv("VISUAL_NODE_PATH", raising=False)
    monkeypatch.setenv("VISUAL_CHROME", sys.executable)
    return tmp_path


@needs_node
def test_check_browser_resolves_modules_like_the_capturer(bare_runtime, monkeypatch):
    """playwright and pngjs resolve from runtime/ (its node_modules) or VISUAL_NODE_PATH; VISUAL_CHROME must exist."""
    monkeypatch.setenv("VISUAL_CHROME", str(bare_runtime / "no-chrome"))
    with pytest.raises(RuntimeError) as missing:
        harness.check_browser()
    assert "'playwright', 'pngjs'" in str(missing.value) and str(bare_runtime / "no-chrome") in str(missing.value)
    modules = TASK / "runtime" / "node_modules"
    if (modules / "playwright").is_dir() and (modules / "pngjs").is_dir():     # after npm ci in runtime/
        monkeypatch.setenv("VISUAL_NODE_PATH", str(modules))
        monkeypatch.setenv("VISUAL_CHROME", sys.executable)
        found = harness.check_browser()
        assert {k: found[k] for k in PINNED} == PINNED and int(found["node"].split(".")[0]) >= 20
        assert found["chrome"] == subprocess.run([sys.executable, "--version"], capture_output=True,
                                                 text=True).stdout.strip()


@needs_node
def test_check_browser_needs_the_pinned_module_versions(bare_runtime, monkeypatch):
    """Another Playwright or pngjs (a global node_modules, a VISUAL_NODE_PATH folder) stops the run."""
    other = fake_modules(bare_runtime / "other", {"playwright": "1.40.0", "pngjs": "6.0.0"})
    monkeypatch.setenv("VISUAL_NODE_PATH", str(other))
    with pytest.raises(RuntimeError, match="differs") as differs:
        harness.check_browser()
    assert f"playwright 1.40.0 instead of {PINNED['playwright']}" in str(differs.value)
    assert f"pngjs 6.0.0 instead of {PINNED['pngjs']}" in str(differs.value)


@needs_node
def test_check_browser_loads_the_modules_and_resolves_a_relative_module_path(bare_runtime, monkeypatch):
    """The modules are loaded, not only found; a relative VISUAL_NODE_PATH means the same folder in the check (run in
    runtime/) and in the capture (run in the caller's directory)."""
    fake_modules(bare_runtime / "nm", PINNED)
    monkeypatch.chdir(bare_runtime)
    monkeypatch.setenv("VISUAL_NODE_PATH", "nm")
    assert harness.node_env()["NODE_PATH"] == str(bare_runtime / "nm")
    found = harness.check_browser()
    assert {k: found[k] for k in PINNED} == PINNED
    (bare_runtime / "nm" / "pngjs" / "index.js").write_text("throw new Error('broken pngjs');\n")
    with pytest.raises(RuntimeError, match="pngjs 7.0.0 does not load: broken pngjs"):
        harness.check_browser()


def test_check_browser_needs_node_20(bare_runtime, monkeypatch):
    """Playwright refuses an older Node only when it is loaded; the check reports it before any run."""
    node = bare_runtime / "old-node"
    reply = {"node": "18.19.0", "versions": dict(PINNED), "errors": {}}
    node.write_text(f"#!/bin/sh\necho '{json.dumps(reply)}'\n")
    node.chmod(0o755)
    monkeypatch.setattr(harness, "NODE", str(node))
    with pytest.raises(RuntimeError, match=r"node 18\.19\.0 .*Playwright needs >=20"):
        harness.check_browser()


def test_check_browser_turns_a_node_that_cannot_run_into_a_runtime_error(bare_runtime, monkeypatch):
    """prepare.py warns and the pipeline stops training on RuntimeError; a broken node must not escape as another
    exception (one failed run each, with paid calls going on)."""
    broken = bare_runtime / "not-a-binary"
    broken.write_text("not an executable format\n")
    broken.chmod(0o755)
    monkeypatch.setattr(harness, "NODE", str(broken))
    with pytest.raises(RuntimeError, match="does not work"):
        harness.check_browser()

    def hang(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
    monkeypatch.setattr(harness, "NODE", sys.executable)
    monkeypatch.setattr(harness.subprocess, "run", hang)
    with pytest.raises(RuntimeError, match="TimeoutExpired"):
        harness.check_browser()


def test_captures_never_overlap_and_waiting_counts_against_the_timeout(tmp_path, monkeypatch):
    import threading
    active, peak, timeouts = [0], [0], []
    guard = threading.Lock()

    def fake_run(argv, **kwargs):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        timeouts.append(kwargs["timeout"])
        time.sleep(0.3)
        with guard:
            active[0] -= 1
        return subprocess.CompletedProcess(argv, 1, "", "synthetic failure")
    monkeypatch.setattr(harness, "CAPTURE_LOCK", tmp_path / "capture.lock")
    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "index.html").write_text(HTML)
    threads = [threading.Thread(target=harness.capture, args=(tmp_path / "ws", tmp_path / f"out{i}", "waves", 30))
               for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1
    assert min(timeouts) < 29.5            # a waiting capture got less time
    waits = sorted(json.loads((tmp_path / f"out{i}" / "capture-lock.json").read_text())["waited_seconds"]
                   for i in range(3))
    assert waits[0] < 0.2 and waits[-1] >= 0.5
