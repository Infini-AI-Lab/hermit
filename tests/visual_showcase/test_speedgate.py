import contextlib
import io
import json
from pathlib import Path
import sys
import threading

import pytest

TASK = Path(__file__).resolve().parents[2] / "examples" / "tasks" / "visual_showcase"
sys.path.insert(0, str(TASK))
import speedgate  # noqa: E402


def stream(tokens: int, usage: bool = True) -> io.BytesIO:
    lines = [b'data: {"choices": [{"delta": {"content": "x"}}]}\n' for _ in range(tokens)]
    if usage:
        lines.append(f'data: {{"choices": [], "usage": {{"prompt_tokens": 30, "completion_tokens": {tokens}}}}}\n'.encode())
    lines.append(b"data: [DONE]\n")
    return io.BytesIO(b"".join(lines))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREWORKS_API_KEY", "SYNTHETIC_TEST_KEY")
    monkeypatch.setenv("VISUAL_SPEED_LOG", str(tmp_path / "gate.jsonl"))
    monkeypatch.setenv("VISUAL_SPEED_RETRY", "0.01")
    return tmp_path


def test_off_without_a_minimum(env, monkeypatch):
    monkeypatch.delenv("VISUAL_SPEED_MIN", raising=False)
    monkeypatch.setattr(speedgate.urllib.request, "urlopen", lambda *a, **k: pytest.fail("no probe when off"))
    assert speedgate.wait_until_fast("x") is None


def test_waits_until_a_probe_is_fast(env, monkeypatch):
    monkeypatch.setenv("VISUAL_SPEED_MIN", "100")
    speeds = iter([20.0, 0.0, 150.0])           # slow, error, fast
    clock = {"t": 0.0}

    def fake_time():
        return clock["t"]

    def fake_urlopen(request, timeout):
        assert request.headers["Authorization"] == "Bearer SYNTHETIC_TEST_KEY"
        speed = next(speeds)
        if speed == 0.0:
            raise OSError("boom SYNTHETIC_TEST_KEY")

        def lines():                            # 600 tokens arriving at `speed` tokens/s
            for line in stream(600):
                clock["t"] += 1 / speed
                yield line
        return contextlib.nullcontext(lines())
    monkeypatch.setattr(speedgate.time, "time", fake_time)
    monkeypatch.setattr(speedgate.urllib.request, "urlopen", fake_urlopen)
    result = speedgate.wait_until_fast("waves-harness")
    assert result["tries"] == 3 and result["probe"]["tok_per_s"] >= 100
    log = [json.loads(line) for line in (env / "gate.jsonl").read_text().splitlines()]
    assert [r["tok_per_s"] >= 100 for r in log] == [False, False, True]
    assert "SYNTHETIC_TEST_KEY" not in (env / "gate.jsonl").read_text()


def test_counts_chunks_when_usage_is_missing(env, monkeypatch):
    monkeypatch.setattr(speedgate.urllib.request, "urlopen", lambda *a, **k: stream(250, usage=False))
    assert speedgate.probe("x")["completion_tokens"] == 250


def test_cancel_ends_the_wait(env, monkeypatch):
    monkeypatch.setenv("VISUAL_SPEED_MIN", "100")
    monkeypatch.setenv("VISUAL_SPEED_RETRY", "60")
    monkeypatch.setattr(speedgate.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("slow")))
    cancel = threading.Event()
    cancel.set()
    result = speedgate.wait_until_fast("learner-step-1", cancel=cancel)
    assert result["cancelled"] and result["tries"] == 1
