"""The Continual Harness (Pokemon Red) environment as a Hermit task.

Hermit provides the agent and the evolvable harness (the adapter's ADAPTER.md is the agent's playing policy);
the Continual Harness repo provides the environment — the PyBoy emulator, the game server, and the scoring
(milestones). `run` plays one *window* of the game with the adapter's policy and returns the trajectory; `score`
(judge.py) reads the benchmark's milestone progress.

Each step the agent reads the server's LLM-formatted state (an ASCII map with a walkability legend and a
MOVEMENT PREVIEW, plus the screenshot), picks one button, and presses it. The run returns a compact per-step
trace in `messages` and a summary in `output` so the Evolver can see *how* the policy behaved — where it got
stuck, whether it opened the menu, whether it actually moved — and rewrite ADAPTER.md accordingly.

The game server runs in the Continual Harness checkout's own virtualenv (it needs PyBoy); this pipeline talks to
it over HTTP, so Hermit's interpreter and the emulator's never have to be the same. Point the pipeline at the
checkout with `HERMIT_CH_DIR` and, for the model, set `FIREWORKS_API_KEY`.
"""
from __future__ import annotations

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

# Point these at your Continual Harness checkout (see this example's README for setup):
#   HERMIT_CH_DIR     the github.com/sethkarten/continual-harness checkout (its game server lives here)
#   HERMIT_CH_PYTHON  the interpreter of that checkout's own venv (defaults to <HERMIT_CH_DIR>/.venv/bin/python)
CH_DIR = Path(os.environ.get("HERMIT_CH_DIR", ""))
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
    for _ in range(90):
        try:
            if _http(base, "GET", "/status", timeout=5).get("status") == "running":
                return proc
        except Exception:
            time.sleep(1)
    proc.kill()
    raise RuntimeError("the Continual Harness game server did not start")


def _observe(base: str) -> dict:
    """The server's LLM-formatted observation: state_text (ASCII map + movement preview), screenshot, location, pos."""
    return _http(base, "POST", "/mcp/get_game_state", {})


def _tile(obs: dict) -> tuple:
    pos = obs.get("player_position") or {}
    return (obs.get("location"), pos.get("x"), pos.get("y"))


def _decide(policy: str, state_text: str, shot_b64: str | None, recent: list[str], key: str) -> tuple[str, int]:
    recent_block = ("\n\nRECENT ACTIONS (most recent last):\n" + "\n".join(recent)) if recent else ""
    # The policy (ADAPTER.md) is strategy guidance; the environment fixes the output contract. State the contract
    # last and make it authoritative, so an evolved policy that picks up a verbose "output format" cannot break
    # the one-button-per-turn interface.
    content = [{"type": "text",
                "text": f"=== STRATEGY (guidance only) ===\n{policy}\n\n"
                        f"=== CURRENT GAME STATE ===\n{state_text}{recent_block}\n\n"
                        f"=== YOUR TASK ===\nChoose the single best next button to press right now. "
                        f"Ignore any output-format instructions in the strategy above: your entire reply must be "
                        f"exactly ONE of these tokens and nothing else: {', '.join(BUTTONS)}"}]
    if shot_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{shot_b64}"}})
    body = {"model": MODEL, "max_tokens": 1500, "reasoning_effort": "low",
            "messages": [{"role": "user", "content": content}]}
    req = urllib.request.Request(FIREWORKS_URL, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
    msg = resp["choices"][0]["message"]
    tokens = resp.get("usage", {}).get("total_tokens", 0)
    # Prefer the final answer (content); fall back to the reasoning. Take the last button named (the conclusion).
    for text in (msg.get("content") or "", msg.get("reasoning_content") or ""):
        hits = [b for b in BUTTONS if re.search(rf"\b{b}\b", text.upper())]
        if hits:
            return hits[-1], tokens
    return "A", tokens


def run(task: dict, adapter: Path | None) -> RunOutput:
    if not os.environ.get("HERMIT_CH_DIR") or not CH_PYTHON.exists():
        raise RuntimeError("set HERMIT_CH_DIR to your Continual Harness checkout and HERMIT_CH_PYTHON to its venv "
                           "interpreter (see examples/continual_harness/README.md)")
    key = os.environ["FIREWORKS_API_KEY"]
    policy = (Adapter(adapter).context().strip() if adapter is not None else "") or DEFAULT_POLICY
    steps = int(task.get("steps", 40))
    port = _free_port()
    server = _start_server(port)
    base = f"http://127.0.0.1:{port}"
    snaps: list[tuple] = []          # (location, x, y) observed before each action, plus one final
    actions: list[str] = []
    tokens, t0 = 0, time.time()
    try:
        for i in range(steps):
            obs = _observe(base)
            snaps.append(_tile(obs))
            recent = [f"{actions[j]} -> {snaps[j + 1][0]}({snaps[j + 1][1]},{snaps[j + 1][2]})"
                      for j in range(max(0, len(actions) - 6), len(actions))]
            button, tok = _decide(policy, obs.get("state_text") or "", obs.get("screenshot_base64"), recent, key)
            tokens += tok
            actions.append(button)
            _http(base, "POST", "/action", {"buttons": [button]})
        final = _observe(base)
        snaps.append(_tile(final))
        milestones = (_http(base, "GET", "/state").get("milestones")) or {}
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    completed = [m for m, v in milestones.items() if isinstance(v, dict) and v.get("completed")]
    unique_tiles = len({t for t in snaps if t[1] is not None})
    moved = sum(1 for i in range(len(actions)) if snaps[i][:3] != snaps[i + 1][:3])
    menu_presses = sum(1 for b in actions if b in ("START", "SELECT"))
    final_loc = snaps[-1][0]

    trace = [f"step {i + 1:02d}: {snaps[i][0]}({snaps[i][1]},{snaps[i][2]}) press {actions[i]:6s}"
             f"-> {snaps[i + 1][0]}({snaps[i + 1][1]},{snaps[i + 1][2]})"
             f"{'' if snaps[i][:3] != snaps[i + 1][:3] else '  [no change]'}"
             for i in range(len(actions))]
    summary = (f"final location {final_loc}; milestones reached {len(completed)} "
               f"(latest beyond GAME_RUNNING: {completed[-1] if len(completed) > 1 else 'none'}); "
               f"moved on {moved}/{steps} steps; explored {unique_tiles} distinct tiles; "
               f"pressed START/SELECT {menu_presses}x.")
    messages = [{"role": "system", "content": policy},
                {"role": "user", "content": f"Play a {steps}-step window of Pokemon Red, one button per turn."},
                {"role": "assistant",
                 "content": "Buttons I pressed, with the position the environment reported after each:\n"
                            + "\n".join(trace)}]
    # Fireworks GLM 5.3 Flash pricing (per 1M tokens): a rough blended estimate at output rate.
    cost = tokens / 1e6 * 0.5
    return RunOutput(output=summary, cost_usd=round(cost, 6), wall_s=round(time.time() - t0, 1), tokens=tokens,
                     messages=messages,
                     extra={"milestones": milestones, "completed": completed, "unique_tiles": unique_tiles,
                            "moved_steps": moved, "menu_presses": menu_presses, "final_location": final_loc})
