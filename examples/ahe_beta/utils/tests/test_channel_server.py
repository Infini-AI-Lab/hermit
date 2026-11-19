"""The simulated-user bridge keeps its queue and event log outside Actor writes."""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import channel_server
import processes


def test_host_queue_and_event_stream_are_bounded_and_repeatable(tmp_path):
    record = {"id": "u1", "kind": "message", "text": "continue"}
    channel_server._enqueue(tmp_path, record)
    peeked = channel_server._peek(tmp_path, "message")
    assert peeked[0]["_file"].endswith("-message.json")
    taken = channel_server._take(tmp_path, "message", [peeked[0]["_file"]])
    assert taken == [record]
    assert channel_server._take(tmp_path, "message") == []

    channel_server._report(tmp_path, {"kind": "reply", "text": "done"})
    channel_server._report(tmp_path, {"kind": "tool", "tool": "cat"})
    first = channel_server._drain(tmp_path, 0)
    assert first["next"] == 2
    assert [event["kind"] for event in first["events"]] == ["reply", "tool"]
    assert channel_server._drain(tmp_path, first["next"])["events"] == []


def test_port_listener_is_never_an_implicit_service(monkeypatch, tmp_path):
    rows = {1: (0, 1), 4242: (1, 77)}
    monkeypatch.setattr(processes.os, "getpid", lambda: 9999)
    assert 4242 not in processes.exclusions(rows, [8080], tmp_path)


def test_service_marker_requires_root_and_matching_start_time(tmp_path):
    marker_dir = tmp_path / "services"
    marker_dir.mkdir()
    marker = marker_dir / "demo.pid"
    marker.write_text("4242:78\n")
    marker.chmod(0o600)
    rows = {1: (0, 1), 4242: (1, 77)}
    assert 4242 not in processes.exclusions(rows, (), marker_dir)
    marker.write_text("4242:77\n")
    marker.chmod(0o666)
    assert 4242 not in processes.exclusions(rows, (), marker_dir)
    if marker.stat().st_uid != 0:
        return
    marker.chmod(0o600)
    assert 4242 in processes.exclusions(rows, (), marker_dir)


def test_root_bridge_delivers_to_hook_without_a_shared_mailbox(tmp_path):
    root = tmp_path / "channel"
    server = subprocess.Popen(
        [
            sys.executable,
            str(Path(channel_server.__file__).resolve()),
            "serve",
            "--root",
            str(root),
            "--pid-file",
            str(tmp_path / "channel.pid"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(50):
            if (root / "channel.sock").exists():
                break
            time.sleep(0.02)
        else:
            raise AssertionError("channel server did not start")

        utils = Path(__file__).resolve().parents[1]
        hook = tmp_path / "user_hook.py"
        hook.write_bytes((utils / "user_hook.py").read_bytes())
        (tmp_path / "user_hook.json").write_text(
            json.dumps(
                {
                    "channel": str(root / "channel.sock"),
                    "wait_s": 0.01,
                    "adapter_stop": [],
                    "reply_frame": "{text}",
                    "insert_frame": "{text}",
                    "interrupt_frame": "{text}",
                }
            )
        )
        codex = tmp_path / "codex"
        codex.write_text(f"#!/bin/sh\nexec {sys.executable} \"$1\" Stop\n")
        codex.chmod(0o755)
        channel_server._enqueue(
            root,
            {"id": "u1", "kind": "message", "text": "continue"},
        )
        result = subprocess.run(
            [str(codex), str(hook)],
            input=json.dumps({"last_assistant_message": "done"}),
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        assert result.returncode == 0
        assert json.loads(result.stdout) == {
            "decision": "block",
            "reason": "continue",
        }
        events = channel_server._drain(root, 0)["events"]
        assert [event["kind"] for event in events] == ["reply", "delivered"]
    finally:
        server.terminate()
        server.wait(timeout=5)
