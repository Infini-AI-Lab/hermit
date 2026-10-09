"""The acting agent's small fixed file/browser harness (arms B and C), ported from the 2026-09-19 experiment's
harness.py; model code runs only in the browser.

Tool schemas, descriptions, budgets, error texts and the image packing are the old ones, byte for byte (a test checks
the schemas against the old freeze.json). The Three.js files in runtime/ are byte copies; the capturer is the old one
with the same capture protocol, except that it finds Playwright, pngjs and Chrome on any machine. All are checked
against their sha256 before use.
"""
from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime"
CAPTURER = RUNTIME / "browser_capture.cjs"
VENDOR = RUNTIME / "vendor" / "three.module.min.js"
# node on PATH unless VISUAL_NODE names one. The capturer loads Playwright 1.62.1 and pngjs from runtime/node_modules
# (npm ci) or from VISUAL_NODE_PATH, and drives the installed Google Chrome unless VISUAL_CHROME names an executable.
# runtime/package.json pins the module versions and the lowest Node version; check_browser enforces both.
NODE = os.environ.get("VISUAL_NODE") or "node"
PACKAGE = RUNTIME / "package.json"
# Where Playwright's "chrome" channel finds Google Chrome; checked before a run when VISUAL_CHROME is unset.
CHROME = {"darwin": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
          "linux": "/opt/google/chrome/chrome"}
CAPTURE_LOCK = Path(os.environ.get("VISUAL_CAPTURE_LOCK") or Path.home() / ".cache" / "visual_showcase" / "capture.lock")
RUNTIME_SHA256 = {
    "browser_capture.cjs": "57ef214b42582081f47bbc66fa8f2d788c9a64e9ac1dd0bb870a31f4a1c65bb5",
    "vendor/three.module.min.js": "e2b5ee6bccd38fd6d8a2428546b83c5f2426d84b152ef82be8055556e3b40eb6",
    "vendor/three.core.min.js": "61ba0df005b05991361d040d8ff670e1aadfd0ce7aeebd1fdb0725957a8957de",
}
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


def verify_runtime() -> None:
    """The capturer and the vendor files must be the frozen bytes; raises before anything is run otherwise."""
    for relative, expected in RUNTIME_SHA256.items():
        path = RUNTIME / relative
        if not path.is_file() or sha(path) != expected:
            raise RuntimeError(f"runtime/{relative} is missing or differs from the frozen bytes")


def node_env() -> dict:
    """The capturer's environment: this one without the key, and NODE_PATH from VISUAL_NODE_PATH when that is set.
    NODE_PATH entries are made absolute, so check_browser (run in runtime/) and the capture (run in the caller's
    directory) resolve the same folders."""
    env = dict(os.environ)
    env.pop("FIREWORKS_API_KEY", None)
    paths = os.environ.get("VISUAL_NODE_PATH") or os.environ.get("NODE_PATH")
    if paths:
        env["NODE_PATH"] = os.pathsep.join(os.path.abspath(p) for p in paths.split(os.pathsep) if p)
    return env


# Run by node in runtime/, where browser_capture.cjs resolves its modules: each module's version, then the module
# itself loaded (Playwright checks the Node version only when it is loaded, so it is loaded only on a new enough Node).
PROBE = """const out = {node: process.versions.node, versions: {}, errors: {}};
const loadable = Number(process.versions.node.split('.')[0]) >= %d;
for (const m of ['playwright', 'pngjs']) {
  try { out.versions[m] = require(m + '/package.json').version; if (loadable) require(m); }
  catch (e) { out.errors[m] = String((e && e.message) || e).split('\\n')[0].slice(0, 300); }
}
console.log(JSON.stringify(out));"""


def check_browser() -> dict:
    """The capture runtime outside this repository: node, the Playwright and pngjs modules as browser_capture.cjs
    resolves them, and Chrome. Node must be at least, and the modules exactly, what runtime/package.json pins. Raises
    RuntimeError when one is missing or differs, so a run fails before its first model call; returns the versions
    found (Chrome's as `chrome --version` prints it, or None), which the pipeline records in request.json."""
    pinned = json.loads(PACKAGE.read_text())
    lowest = int(re.search(r"\d+", pinned["engines"]["node"]).group())
    node = shutil.which(NODE)
    if node is None:
        raise RuntimeError(f"the browser capture runtime is missing: node ({NODE}; set VISUAL_NODE)")
    try:
        done = subprocess.run([node, "-e", PROBE % lowest], cwd=RUNTIME, env=node_env(), capture_output=True,
                              text=True, timeout=60)
        found = json.loads(done.stdout.strip().splitlines()[-1]) if done.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as exc:
        raise RuntimeError(f"the browser capture runtime does not work: node ({node}) failed to run "
                           f"({type(exc).__name__}: {str(exc)[-300:]})") from None
    if found is None:
        raise RuntimeError(f"the browser capture runtime does not work: node ({node}) failed: "
                           f"{done.stderr.strip()[-300:]}")
    problems = []
    if int(found["node"].split(".")[0]) < lowest:
        problems.append(f"node {found['node']} ({node}); Playwright needs {pinned['engines']['node']}")
    for module, version in pinned["dependencies"].items():
        have = found["versions"].get(module)
        if have is None:
            problems.append(module)
        elif have != version:
            problems.append(f"{module} {have} instead of {version}")
        elif module in found["errors"]:
            problems.append(f"{module} {have} does not load: {found['errors'][module]}")
    chrome = os.environ.get("VISUAL_CHROME") or CHROME.get(sys.platform)
    if chrome and not Path(chrome).is_file():
        problems.append(chrome)
    if problems:
        raise RuntimeError(f"the browser capture runtime is missing or differs: {problems} (npm ci in runtime/ "
                           "installs the pinned playwright and pngjs; VISUAL_NODE names another node; VISUAL_CHROME "
                           "names a Chrome executable)")
    return {"node": found["node"], **found["versions"], "chrome": chrome_version(chrome)}


def chrome_version(chrome) -> str | None:
    """What `chrome --version` prints, for the run record; None when it cannot be asked."""
    if not chrome:
        return None
    try:
        done = subprocess.run([chrome, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip()[:200] or None


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


def capture(workspace, output, case, timeout=90) -> dict:
    """Run the browser capturer on workspace/index.html into the new directory `output`; returns its summary."""
    workspace, output = Path(workspace), Path(output)
    if output.exists():
        raise ValueError("Capture output must be new")
    verify_runtime()
    env = node_env()
    # One capture at a time on this machine, so runs of different cases in parallel never share the browser and
    # CPU during a capture (the old campaign ran strictly serially). Waiting counts against the caller's timeout.
    CAPTURE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with open(CAPTURE_LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        waited = time.monotonic() - started
        result = subprocess.run([NODE, str(CAPTURER), "--workspace", str(workspace), "--output", str(output),
                                 "--case", case, "--vendor", str(VENDOR)],
                                capture_output=True, text=True, timeout=max(1.0, timeout - waited), env=env)
    output.mkdir(parents=True, exist_ok=True)
    (output / "capture-lock.json").write_text(json.dumps({"waited_seconds": round(waited, 3)}))
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

    `adapter` (arm C only) is a loaded harnessadapter Adapter; its `wm` tool is offered read-only."""

    def __init__(self, workspace, run, case, adapter=None):
        self.workspace, self.run = Path(workspace), Path(run)
        self.case = case
        self.adapter = adapter
        self.inspections = 0
        self.wm_calls = 0
        self.calls = 0
        self.last_inspected_sha = None
        self.tools = list(TOOLS)
        if adapter:
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
                summary = capture(self.workspace, out, self.case, timeout=min(90, deadline - time.monotonic()))
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
