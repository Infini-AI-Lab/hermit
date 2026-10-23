"""The Continual Harness (Pokemon Red) environment as a Hermit task.

Hermit provides the agent and the evolvable harness (the adapter's ADAPTER.md is the agent's playing policy);
the Continual Harness repo provides the environment — the mGBA/PyBoy emulator, the game server, and the scoring
(milestones). `run` plays one *window* of the game with the adapter's policy and returns the trajectory; `score`
reads the benchmark's milestone progress.

The game server runs in the Continual Harness checkout's own virtualenv (it needs PyBoy); this pipeline talks to it
over HTTP, so Hermit's interpreter and the emulator's never have to be the same. Point the pipeline at the checkout
with `HERMIT_CH_DIR` (default: the path below) and, for the model, set `FIREWORKS_API_KEY`.
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from hermit.adapter.loader import Adapter
from hermit.task import RunOutput

CH_DIR = Path(os.environ.get("HERMIT_CH_DIR", "/home/changyi.yang@liquid.ai/.slock/agents/"
                             "a6882392-5498-4a98-8521-96824f4d3659/scratch/continual-harness"))
CH_PYTHON = Path(os.environ.get("HERMIT_CH_PYTHON", CH_DIR / ".venv" / "bin" / "python"))
MODEL = os.environ.get("HERMIT_POKE_MODEL", "accounts/fireworks/models/glm-5p3-flash")
FIREWORKS_URL = "https://api.fireworks.ai/inference/v1/chat/completions"
BUTTONS = ["A", "B", "START", "SELECT", "UP", "DOWN", "LEFT", "RIGHT"]
DEFAULT_POLICY = ("You are playing Pokemon Red. Read the game state and press the single button that best makes "
                  "progress (exploring, advancing dialogue, winning battles).")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http(base: str, method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _start_server(port: int) -> subprocess.Popen:
    env = {**os.environ, "GAME_TYPE": "red", "SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy"}
    proc = subprocess.Popen([str(CH_PYTHON), "-m", "server.app", "--port", str(port), "--game", "red"],
                            cwd=str(CH_DIR), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            if _http(base, "GET", "/status", timeout=5).get("status") == "running":
                return proc
        except Exception:
            time.sleep(1)
    proc.kill()
    raise RuntimeError("the Continual Harness game server did not start")


def _decide(policy: str, state_text: str, shot_b64: str | None, key: str) -> tuple[str, int]:
    content = [{"type": "text", "text": f"{policy}\n\nGAME STATE:\n{state_text}\n\n"
               f"Reply with ONLY the next button, one of: {', '.join(BUTTONS)}"}]
    if shot_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{shot_b64}"}})
    body = {"model": MODEL, "max_tokens": 1500, "reasoning_effort": "low",
            "messages": [{"role": "user", "content": content}]}
    req = urllib.request.Request(FIREWORKS_URL, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
    msg = resp["choices"][0]["message"]
    text = f"{msg.get('content') or ''} {msg.get('reasoning_content') or ''}".upper()
    button = next((b for b in BUTTONS if re.search(rf"\b{b}\b", text)), "A")
    return button, resp.get("usage", {}).get("total_tokens", 0)


def run(task: dict, adapter: Path | None) -> RunOutput:
    key = os.environ["FIREWORKS_API_KEY"]
    policy = Adapter(adapter).context().strip() if adapter is not None else ""
    policy = policy or DEFAULT_POLICY
    steps = int(task.get("steps", 20))
    port = _free_port()
    server = _start_server(port)
    base = f"http://127.0.0.1:{port}"
    trajectory, tokens, t0 = [], 0, time.time()
    try:
        for i in range(steps):
            s = _http(base, "GET", "/state")
            state_text = s.get("state_text") or json.dumps({k: s.get(k) for k in ("player", "game", "map")})[:1600]
            shot = (s.get("visual") or {}).get("screenshot_base64")
            button, tok = _decide(policy, state_text, shot, key)
            tokens += tok
            _http(base, "POST", "/action", {"buttons": [button]})
            trajectory.append({"step": i + 1, "button": button,
                               "position": (s.get("player") or {}).get("position"),
                               "location": (s.get("game") or {}).get("location")})
        final = _http(base, "GET", "/state")
        milestones = final.get("milestones") or {}
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
    completed = [m for m, v in milestones.items() if isinstance(v, dict) and v.get("completed")]
    summary = f"played {steps} steps; milestones reached: {len(completed)} ({', '.join(completed[:8])})"
    # Fireworks GLM 5.3 Flash pricing (per 1M tokens): input 0.15, output 0.50; a rough blended estimate.
    cost = tokens / 1e6 * 0.5
    return RunOutput(output=summary, cost_usd=round(cost, 6), wall_s=round(time.time() - t0, 1), tokens=tokens,
                     messages=[{"role": "system", "content": policy}],
                     extra={"milestones": milestones, "completed": completed, "trajectory": trajectory})
