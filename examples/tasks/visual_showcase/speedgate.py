"""Optional speed gate: wait until the provider generates at a normal speed before a run or a learner session starts.

Serverless glm-5p3-flash shares GPUs with all Fireworks users, and its generation speed varies several-fold over a day
(18-190 tokens/s measured on 2026-10-06). The original experiment ran at about 130-160 tokens/s, where its 600 s actor
limit was never binding. When `VISUAL_SPEED_MIN` (tokens/s) is set, `wait_until_fast` streams a short probe and returns
only once the probe reaches that speed, sleeping `VISUAL_SPEED_RETRY` seconds (default 300) between tries. Callers
start their own clocks afterwards, so the gate never changes a run's budgets. Each probe is appended to
`$VISUAL_SPEED_LOG` (default `.logs/speed-gate.jsonl`). Without `VISUAL_SPEED_MIN` it returns None at once.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import time
import urllib.request

HERE = Path(__file__).resolve().parent
URL = "https://api.fireworks.ai/inference/v1/chat/completions"
MODEL = "accounts/fireworks/models/glm-5p3-flash"
KEY_ENV = "FIREWORKS_API_KEY"
PROBE_TOKENS = 600
PROMPT = "Write a plain HTML page with an inline canvas animation of bouncing balls (about 120 lines). Output only the HTML."


def _log_path() -> Path:
    return Path(os.environ.get("VISUAL_SPEED_LOG") or HERE / ".logs" / "speed-gate.jsonl")


def probe(label: str) -> dict:
    """One streamed probe of PROBE_TOKENS completion tokens; returns a record with tok_per_s (0 on any error)."""
    key = os.environ.get(KEY_ENV, "")
    body = {"model": MODEL, "stream": True, "max_tokens": PROBE_TOKENS, "temperature": 0, "reasoning_effort": 4096,
            "stream_options": {"include_usage": True}, "messages": [{"role": "user", "content": PROMPT}]}
    request = urllib.request.Request(URL, json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    record = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "label": label}
    t0, first, usage, chunks = time.time(), None, None, 0
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            for line in response:
                line = line.decode().strip()
                if not line.startswith("data:") or line.endswith("[DONE]"):
                    continue
                data = json.loads(line[5:])
                usage = data.get("usage") or usage
                if any((c.get("delta") or {}).get("content") or (c.get("delta") or {}).get("reasoning_content")
                       for c in data.get("choices", [])):
                    chunks += 1
                    first = time.time() - t0 if first is None else first
        total = time.time() - t0
        tokens = (usage or {}).get("completion_tokens") or chunks      # the usage chunk is sometimes missing
        record.update(total_s=round(total, 1), completion_tokens=tokens,
                      tok_per_s=round(tokens / max(total - (first or 0), 1e-6), 1),
                      cost_usd=round(((usage or {}).get("prompt_tokens", 30) * 0.15 + tokens * 0.50) / 1e6, 6))
    except Exception as exc:  # noqa: BLE001 - network or provider error: record it and treat the provider as slow
        record.update(error=str(exc).replace(key, "[key]") if key else str(exc), tok_per_s=0.0)
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def wait_until_fast(label: str, cancel=None) -> dict | None:
    """Block until a probe reaches VISUAL_SPEED_MIN tokens/s; returns {passed probe, tries, waited_seconds}.
    `cancel` (a threading.Event) ends the wait early; the result then has "cancelled": True."""
    minimum = os.environ.get("VISUAL_SPEED_MIN")
    if not minimum:
        return None
    retry = float(os.environ.get("VISUAL_SPEED_RETRY") or 300)
    started, tries = time.monotonic(), 0
    while True:
        tries += 1
        record = probe(label)
        if record["tok_per_s"] >= float(minimum):
            return {"probe": record, "tries": tries, "minimum": float(minimum),
                    "waited_seconds": round(time.monotonic() - started, 1)}
        if cancel is not None and cancel.wait(retry):
            return {"probe": record, "tries": tries, "minimum": float(minimum), "cancelled": True,
                    "waited_seconds": round(time.monotonic() - started, 1)}
        if cancel is None:
            time.sleep(retry)
