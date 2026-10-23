"""What the acting agent (agent/agent.py) takes from the host: the model, GLM 5.3 Flash on Fireworks, and its settings,
the message helpers, and the host's final capture of the submitted page.

The final capture (capture/) is never shown to the model. FIREWORKS_API_KEY is read from the environment and never
written.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

import yaml

from hermit.llm import ChatClient

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import harness  # noqa: E402  (this example's harness.py)

BASE_URL = "https://api.fireworks.ai/inference/v1"
MODEL = "accounts/fireworks/models/glm-5p3-flash"
KEY_ENV = "FIREWORKS_API_KEY"
PRICE = {"input": 0.15, "cached_input": 0.03, "output": 0.50}     # USD per million tokens
SAMPLING = {"temperature": 0, "top_k": 40, "presence_penalty": 0, "frequency_penalty": 0, "reasoning_effort": 4096}
MAX_TOKENS = 32768                    # per response
COMPLETION_BUDGET = 65536             # cumulative completion tokens per actor run
MAX_REQUESTS = 12                     # per run

PLACEHOLDER = '<!doctype html><html><head><meta charset="utf-8"><title>No submission</title></head><body><p>No model artifact was submitted.</p></body></html>'
IMAGES_REMOVED = 'Earlier inspection image bytes removed from the current API request to keep image payload bounded. Its recorded text observations remain above.'


def make_client() -> ChatClient:
    """The actor's transport: no retries; the agent sets the timeout before each request."""
    return ChatClient(BASE_URL, MODEL, api_key_env=KEY_ENV, price=PRICE, max_retries=0)


def continuing_message(message: dict) -> dict:
    # Preserve provider reasoning only in the private in-memory conversation.
    return {k: v for k, v in message.items() if k in ("role", "content", "reasoning_content", "tool_calls")}


def public_message(message: dict) -> dict:
    return {k: v for k, v in message.items() if k in ("role", "content", "tool_calls")}


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def arm_of(adapter: Path | None) -> str:
    """`harness` without an adapter or for the untouched root (no `origin` in adapter.yaml; the Trainer adds one only
    when it commits a learned change), else `adapted`."""
    if adapter is None:
        return "harness"
    manifest = yaml.safe_load((Path(adapter) / "adapter.yaml").read_text()) or {}
    return "adapted" if "origin" in manifest else "harness"


def _final_capture(submission: Path, output: Path, case: str) -> None:
    """The host's final capture; a capturer that crashes or times out leaves a failed summary, not an exception."""
    try:
        harness.capture(submission, output, case)
    except Exception as exc:  # noqa: BLE001
        harness.save(output / "summary.json", {
            "status": "capture_process_failed", "capture_ok": False, "screenshots": [], "page_errors": [],
            "console_errors": [], "capture_errors": [f"{type(exc).__name__}: {exc}"[-2000:]], "model_calls": 0,
            "input": {"index_sha256": harness.sha(submission / "index.html")}})
