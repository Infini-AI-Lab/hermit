"""The host's browser capture: runtime/browser_capture.cjs runs a workspace's index.html in a headless browser (the
installed Google Chrome, or the executable VISUAL_CHROME names; experiment 4 named runtime/chrome-gpu.sh) and writes the
screenshots and summary.json the judge reads. Model code runs only in the browser."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime"
CAPTURER = RUNTIME / "browser_capture.cjs"
# Three.js 0.180.0 from npm ci, served to the page as /vendor/three.module.min.js and /vendor/three.core.min.js.
VENDOR = RUNTIME / "node_modules" / "three" / "build" / "three.module.min.js"
NODE = os.environ.get("VISUAL_NODE") or "node"
CAPTURE_LOCK = Path(os.environ.get("VISUAL_CAPTURE_LOCK") or Path.home() / ".cache" / "visual_showcase" / "capture.lock")
CAPTURE_TIMEOUT_S = 300                # why: above the capturer's own 250 s watchdog and its cleanup (experiment 4
                                       # raised the capturer's deadlines about sixfold; 90 s before)


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def check_vendor() -> None:
    """Without Three.js the capturer serves no /vendor/ files and still reports a successful capture."""
    if not VENDOR.is_file():
        raise RuntimeError(f"{VENDOR} is missing: run npm ci --prefix examples/visual_showcase/utils/runtime")


def capture(workspace, output, case, timeout=CAPTURE_TIMEOUT_S) -> dict:
    """Run the browser capturer on workspace/index.html into the new directory `output`; returns its summary."""
    workspace, output = Path(workspace), Path(output)
    check_vendor()
    if output.exists():
        raise ValueError("Capture output must be new")
    env = dict(os.environ)
    env.pop("FIREWORKS_API_KEY", None)       # the key never reaches the browser
    # One capture at a time on this machine, so parallel runs never share the browser and CPU during a capture.
    # The timeout bounds the capture itself, not the wait for the lock: an agent's command in another run may hold the
    # shared lock for up to its timeout, and counting that wait starved captures into a failure with no screenshot.
    # The wait is recorded in host-lock.json.
    CAPTURE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    waited = None
    try:
        with open(CAPTURE_LOCK, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            waited = time.monotonic() - started
            result = subprocess.run([NODE, str(CAPTURER), "--workspace", str(workspace), "--output", str(output),
                                     "--case", case, "--vendor", str(VENDOR)],
                                    capture_output=True, text=True, env=env, timeout=timeout)
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
