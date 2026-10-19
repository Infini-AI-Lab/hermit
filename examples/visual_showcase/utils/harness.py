"""The acting agent's small fixed harness (arms B and C): four file and browser tools, plus the adapter's read-only
`wm` tool in arm C when the adapter exports one, and the browser capture. Model code runs only in the browser."""
from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime"
CAPTURER = RUNTIME / "browser_capture.cjs"
# Three.js 0.180.0 from npm ci, served to the page as /vendor/three.module.min.js and /vendor/three.core.min.js.
VENDOR = RUNTIME / "node_modules" / "three" / "build" / "three.module.min.js"
NODE = os.environ.get("VISUAL_NODE") or "node"
CAPTURE_LOCK = Path(os.environ.get("VISUAL_CAPTURE_LOCK") or Path.home() / ".cache" / "visual_showcase" / "capture.lock")
MAX_TOOL_CALLS = 24
MAX_INSPECTIONS = 3
MAX_FILE_BYTES = 500000
IMAGE_BATCH_CAP = 8_000_000          # base64 characters per image batch sent to the model
WM_NAMES = {"list", "memory", "recall", "search", "read", "trace"}


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def function(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required),
                           "additionalProperties": False}}}


TOOLS = [
    function("write_file", "Write the entire index.html. This is the only writable file. The host does not execute shell/Python code.",
             {"content": {"type": "string", "maxLength": 500000}}, ["content"]),
    function("read_file", "Read your index.html or the fixed README.md. Returns numbered lines. No other files or runs are accessible.",
             {"path": {"type": "string", "enum": ["index.html", "README.md"]}, "start_line": {"type": "integer", "minimum": 1},
              "max_lines": {"type": "integer", "minimum": 1, "maximum": 500}}, ["path"]),
    function("replace_text", "Replace exactly one occurrence of old in your index.html. Fails if old is absent or appears multiple times.",
             {"old": {"type": "string", "minLength": 1}, "new": {"type": "string"}}, ["old", "new"]),
    function("inspect_page", "Run the actual current index.html in the offline Chrome browser. Returns runtime/interaction observations and up to five original PNGs (desktop around 0/2/5s, intensity 0.75, phone). You can inspect at most three times. Images arrive in the immediately following user observation. Inspect again after an edit if you want its final version checked. Checks are not aesthetic scores.", {}),
]
WM_SUFFIX = " This experiment admits only read-only knowledge operations: list, memory, recall, search, read, trace."


def extract_html(content: str) -> tuple[str, str]:
    """Mechanical direct-arm extraction only. Never repair model artwork."""
    text = content.strip()
    if re.match(r"(?is)^<!doctype\s+html|^<html(?:\s|>)", text):
        return text, "raw_html"
    blocks = re.findall(r"```(?:html)?\s*\n(.*?)\n```", text, re.S | re.I)
    if len(blocks) == 1 and re.match(r"(?is)^\s*(?:<!doctype\s+html|<html(?:\s|>))", blocks[0]):
        return blocks[0].strip(), "single_html_fence"
    raise ValueError("Expected raw HTML or exactly one HTML code fence; no automatic reconstruction")


def check_vendor() -> None:
    """Without Three.js the capturer serves no /vendor/ files and still reports a successful capture."""
    if not VENDOR.is_file():
        raise RuntimeError(f"{VENDOR} is missing: run npm ci --prefix examples/visual_showcase/utils/runtime")


def capture(workspace, output, case, timeout=90, count_wait=False) -> dict:
    """Run the browser capturer on workspace/index.html into the new directory `output`; returns its summary."""
    workspace, output = Path(workspace), Path(output)
    check_vendor()
    if output.exists():
        raise ValueError("Capture output must be new")
    env = dict(os.environ)
    env.pop("FIREWORKS_API_KEY", None)       # the key never reaches the browser
    # One capture at a time on this machine, so parallel runs never share the browser and CPU during a capture.
    # The timeout bounds the capture itself, not the wait for the lock: an agent's command in another run may hold the
    # shared lock for up to 90 s, and counting that wait starved captures into a failure with no screenshot
    # (experiment 3, silk-pg2b). count_wait=True keeps the old rule for a capture inside an agent's time budget
    # (experiment 1's inspect_page). The wait is recorded in host-lock.json.
    CAPTURE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    waited = None
    try:
        with open(CAPTURE_LOCK, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            waited = time.monotonic() - started
            result = subprocess.run([NODE, str(CAPTURER), "--workspace", str(workspace), "--output", str(output),
                                     "--case", case, "--vendor", str(VENDOR)],
                                    capture_output=True, text=True, env=env,
                                    timeout=max(1.0, timeout - waited) if count_wait else timeout)
    finally:
        output.mkdir(parents=True, exist_ok=True)
        save(output / "host-lock.json", {"lock_wait_s": None if waited is None else round(waited, 3),
                                         "capture_s": round(time.monotonic() - started - (waited or 0), 3)})
    (output / "controller-stdout.log").write_text(result.stdout)
    (output / "controller-stderr.log").write_text(result.stderr)
    if not (output / "summary.json").exists():
        save(output / "summary.json", {"status": "capture_process_failed", "capture_ok": False, "screenshots": [],
                                       "page_errors": [], "console_errors": [], "capture_errors": [result.stderr[-2000:]],
                                       "model_calls": 0, "input": {"index_sha256": sha(workspace / "index.html")}})
    return json.loads((output / "summary.json").read_text())


def concise(summary: dict) -> dict:
    keys = ["case", "status", "capture_ok", "input", "observations", "page_errors", "console_errors", "capture_errors",
            "blocked_requests", "csp_violations", "screenshots", "limitations"]
    return {k: summary[k] for k in keys if k in summary}


def images_for_model(output, summary: dict) -> tuple[list, list]:
    """Original PNG bytes, fixed order; cap below documented API payload limit."""
    content, records, total = [], [], 0
    for item in summary.get("screenshots", [])[:5]:
        path = (Path(output) / item["file"]).resolve()
        if not path.is_relative_to(Path(output).resolve()) or not path.is_file():
            raise ValueError("Image outside current capture")
        raw = path.read_bytes()
        encoded = base64.b64encode(raw).decode()
        if total + len(encoded) > IMAGE_BATCH_CAP:
            records.append({"file": item["file"], "omitted": "8,000,000 base64-character batch cap", "sha256": sha(path)})
            continue
        total += len(encoded)
        content += [{"type": "text", "text": item.get("label", item["file"]) + " — actual browser capture"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}}]
        records.append({"file": item["file"], "sha256": sha(path), "bytes": len(raw), "base64_characters": len(encoded),
                        "packed_for_model": True})
    return content, records


class Harness:
    """One run's tools over `workspace` (README.md + index.html); records go under `run` (tools/, checks/).

    `adapter` (arm C only) is a loaded hermit Adapter; its `wm` tool, if it exports one, is offered read-only."""

    def __init__(self, workspace, run, case, adapter=None):
        self.workspace, self.run, self.case, self.adapter = Path(workspace), Path(run), case, adapter
        self.inspections = self.wm_calls = self.calls = 0
        self.last_inspected_sha = None
        self.tools = list(TOOLS)
        if adapter and "wm" in adapter.tools:
            tool = adapter.tools["wm"]
            self.tools.append({"type": "function", "function": {"name": "wm",
                               "description": tool["description"] + WM_SUFFIX, "parameters": tool["input_schema"]}})
        self.schemas = {t["function"]["name"]: t["function"]["parameters"] for t in self.tools}

    def dispatch(self, name, args, deadline) -> tuple[str, list]:
        """Run one requested tool call (every request counts); returns the tool message text and new image parts."""
        self.calls += 1
        start = time.monotonic()
        images, image_records = [], []
        try:
            if self.calls > MAX_TOOL_CALLS:
                raise ValueError("Tool call budget exhausted")
            if name not in self.schemas:
                raise ValueError("Unknown tool")
            Draft202012Validator(self.schemas[name]).validate(args)
            if time.monotonic() >= deadline:
                raise TimeoutError("Actor time budget exhausted")
            target = self.workspace / "index.html"
            if target.is_symlink():
                raise ValueError("Symlink not admitted")
            if name == "write_file":
                if len(args["content"].encode()) > MAX_FILE_BYTES:
                    raise ValueError("File byte limit exceeded")
                target.write_text(args["content"])
                result = {"ok": True, "file": "index.html", "bytes": target.stat().st_size, "sha256": sha(target)}
            elif name == "read_file":
                requested = self.workspace / args["path"]
                if requested.is_symlink() or not requested.resolve().is_relative_to(self.workspace.resolve()):
                    raise ValueError("Read target must be a regular file inside this workspace")
                text = requested.read_text()
                lines = text.splitlines()
                start_line = args.get("start_line", 1)
                limit = args.get("max_lines", 300)
                result = {"ok": True, "total_lines": len(lines), "text": "\n".join(
                    f"{i + start_line}: {v}" for i, v in enumerate(lines[start_line - 1:start_line - 1 + limit]))}
            elif name == "replace_text":
                text = target.read_text()
                count = text.count(args["old"])
                if count != 1:
                    raise ValueError(f"old must occur exactly once, found {count}")
                text = text.replace(args["old"], args["new"])
                if len(text.encode()) > MAX_FILE_BYTES:
                    raise ValueError("File byte limit exceeded")
                target.write_text(text)
                result = {"ok": True, "file": "index.html", "sha256": sha(target)}
            elif name == "inspect_page":
                if self.inspections >= MAX_INSPECTIONS:
                    raise ValueError("Three-inspection budget exhausted")
                if deadline - time.monotonic() < 50:
                    raise TimeoutError("Less than 50 seconds remain for a browser inspection")
                self.inspections += 1
                out = self.run / "checks" / f"check-{self.inspections:02d}" / "capture"
                summary = capture(self.workspace, out, self.case, timeout=min(90, deadline - time.monotonic()),
                                  count_wait=True)
                self.last_inspected_sha = summary.get("input", {}).get("index_sha256")
                images, image_records = images_for_model(out, summary)
                result = {"ok": summary.get("capture_ok", False), "inspection": self.inspections,
                          "observations": concise(summary), "images": image_records,
                          "remaining_inspections": MAX_INSPECTIONS - self.inspections}
            elif name == "wm":
                self.wm_calls += 1
                if args.get("name") not in WM_NAMES:
                    raise ValueError("Only read-only knowledge operations are admitted; no executable tools or delegates")
                result = asyncio.run(self.adapter.call("wm", args))
        except Exception as exc:
            result = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:2000]}
        save(self.run / "tools" / f"{self.calls:02d}.json", {"name": name, "arguments": args, "result": result,
             "image_records": image_records, "seconds": round(time.monotonic() - start, 3)})
        rendered = json.dumps(result, ensure_ascii=False)
        if len(rendered) > 32000:
            rendered = json.dumps({"truncated": True, "original_characters": len(rendered), "prefix": rendered[:30000]})
        return rendered, images
