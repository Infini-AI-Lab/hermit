"""Mailbox races and adapter hook decisions must not fabricate a successful turn."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

UTILS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ahe_channel_test", UTILS / "channel.py")
channel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(channel)


def test_close_before_first_message_does_not_wait(tmp_path):
    stopped = []
    c = channel.Conversation(
        tmp_path, lambda text: None, lambda: True, lambda: stopped.append(True)
    )
    c.close()
    with pytest.raises(channel.ConversationClosed):
        c.poll(30)
    assert stopped == [True]


def hook(tmp_path, *, incoming, adapter=()):
    for folder in ("to-agent", "to-user", "taken"):
        (tmp_path / folder).mkdir(exist_ok=True)
    script = tmp_path / "user_hook.py"
    script.write_bytes((UTILS / "user_hook.py").read_bytes())
    script.with_suffix(".json").write_text(
        json.dumps(
            {
                "mail": str(tmp_path),
                "wait_s": 0.01,
                "adapter_stop": list(adapter),
                "reply_frame": "{text}",
                "insert_frame": "{text}",
                "interrupt_frame": "{text}",
            }
        )
    )
    for i, (kind, text) in enumerate(incoming):
        (tmp_path / "to-agent" / f"{i:04}-{kind}.json").write_text(
            json.dumps({"id": str(i), "kind": kind, "text": text})
        )
    return subprocess.run(
        [sys.executable, str(script), "Stop"],
        input=json.dumps({"last_assistant_message": "done"}),
        text=True,
        capture_output=True,
        timeout=3,
    )


def test_end_wins_over_queued_followup(tmp_path):
    result = hook(tmp_path, incoming=[("message", "continue"), ("end", "")])
    assert result.returncode == 0 and not result.stdout.strip()
    assert len(list((tmp_path / "to-agent").glob("*-message.json"))) == 1


def test_adapter_stop_session_is_preserved(tmp_path):
    result = hook(
        tmp_path,
        incoming=[],
        adapter=[
            {"command": 'printf \'%s\' \'{"continue":false,"stopReason":"done"}\''}
        ],
    )
    assert json.loads(result.stdout) == {"continue": False, "stopReason": "done"}
    assert any(
        json.loads(p.read_text()).get("terminal") is True
        for p in (tmp_path / "to-user").glob("*.json")
    )
