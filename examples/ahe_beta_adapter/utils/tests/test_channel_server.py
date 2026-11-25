"""The simulated-user bridge keeps its queue and event log outside Actor writes."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

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


def test_a_process_start_time_is_read_as_the_number_a_marker_holds():
    """A `pid:start` marker is compared with /proc's start time as numbers: read as text, none ever matched."""
    if not Path(f"/proc/{os.getpid()}/stat").exists():
        pytest.skip("no /proc on this platform")
    start = int(Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19])
    assert processes.processes()[os.getpid()][1] == start


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


def test_a_root_owned_marker_with_its_start_time_keeps_its_service(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("a marker counts only when root owns it: run as root")
    marker_dir = tmp_path / "services"
    marker_dir.mkdir()
    (marker_dir / "demo.pid").write_text("4242:77\n")
    (marker_dir / "demo.pid").chmod(0o600)
    assert 4242 in processes.exclusions({1: (0, 1), 4242: (1, 77)}, (), marker_dir)


def test_an_idle_drain_reads_the_stream_again_only_when_it_has_grown(tmp_path, monkeypatch, capsys):
    """A host read waits in the environment up to `wait` seconds; it used to parse the whole event log every 0.2 s."""
    channel_server._report(tmp_path, {"kind": "tool", "tool": "cat"})
    calls, real = [], channel_server._drain
    monkeypatch.setattr(channel_server, "_drain", lambda root, since: calls.append(since) or real(root, since))
    channel_server.host_command(tmp_path, "drain", "1", wait=0.7)
    assert calls == [] and json.loads(capsys.readouterr().out)["events"] == []


def test_the_bridge_hands_over_no_message_after_the_end_or_while_an_interrupt_waits(tmp_path):
    """Checked in the same operation that claims the message: a hook's own check, a request earlier, can be stale."""
    channel_server._enqueue(tmp_path, {"id": "u1", "kind": "message", "text": "go on"})
    channel_server._enqueue(tmp_path, {"id": "u2", "kind": "interrupt", "text": "stop"})
    assert channel_server._take(tmp_path, "message") == []
    channel_server._take(tmp_path, "interrupt")                                  # the Stop takes the interrupt
    channel_server._enqueue(tmp_path, {"id": "u3", "kind": "end"})
    assert channel_server._take(tmp_path, "message") == []
    (next((tmp_path / "to-agent").glob("*-end.json"))).unlink()
    assert [m["id"] for m in channel_server._take(tmp_path, "message")] == ["u1"]


def test_a_reply_holding_a_unicode_line_separator_is_drained_whole(tmp_path):
    channel_server._report(tmp_path, {"kind": "reply", "text": "a\u2028b\u2029c"})
    assert [e["text"] for e in channel_server._drain(tmp_path, 0)["events"]] == ["a\u2028b\u2029c"]


def test_a_drain_reads_the_stream_from_where_its_new_events_start(tmp_path):
    for kind in ("tool_start", "tool", "reply"):
        channel_server._report(tmp_path, {"kind": kind})
    assert [e["kind"] for e in channel_server._drain(tmp_path, 2)["events"]] == ["reply"]
    assert channel_server._drain(tmp_path, 3) == {"next": 3, "events": []}
    assert [e["seq"] for e in channel_server._drain(tmp_path, 0)["events"]] == [1, 2, 3]
    (tmp_path / "events.idx").unlink()                                         # no index: the whole stream, filtered
    assert [e["seq"] for e in channel_server._drain(tmp_path, 1)["events"]] == [2, 3]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the bridge checks its peer with Linux's SO_PEERCRED "
                    "and /proc")
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
        codex = tmp_path / "codex"      # a parent named codex that stays (no exec), as Codex stays a hook's parent
        codex.write_text(f"#!/bin/sh\n{sys.executable} \"$1\" Stop\n")
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
