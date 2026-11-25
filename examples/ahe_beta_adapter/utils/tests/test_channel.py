"""Mailbox races and adapter hook decisions must not fabricate a successful turn."""

import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

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


def hook(tmp_path, *, incoming, adapter=(), event="Stop", payload=None, folders=("to-agent", "to-user", "taken")):
    for folder in folders:
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
        [sys.executable, str(script), event],
        input=json.dumps(payload or {"last_assistant_message": "done"}),
        text=True,
        capture_output=True,
        timeout=3,
    )


def test_end_wins_over_queued_followup(tmp_path):
    result = hook(tmp_path, incoming=[("message", "continue"), ("end", "")])
    assert result.returncode == 0 and not result.stdout.strip()
    assert len(list((tmp_path / "to-agent").glob("*-message.json"))) == 1


CALL = {"tool_name": "shell", "tool_input": {"command": "echo four >> /app/a.txt"}}


def test_once_the_user_ended_the_session_every_tool_call_is_denied(tmp_path):
    """The user's end reaches Codex only at its next Stop: until then no tool call changes what the user reviews."""
    result = hook(tmp_path, incoming=[("end", "")], event="PreToolUse", payload=CALL)
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny" and "ended the session" in decision["permissionDecisionReason"]
    assert list((tmp_path / "to-agent").glob("*-end.json"))                 # left for the Stop that follows


def test_a_denial_stands_though_its_report_does_not_reach_the_user(tmp_path):
    (tmp_path / "to-user").write_text("not a folder")                        # every report fails
    result = hook(tmp_path, incoming=[("interrupt", "stop")], event="PreToolUse", payload=CALL,
                  folders=("to-agent", "taken"))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_large_tool_output_reaches_the_user_cut_and_a_small_input_as_it_is(tmp_path):
    """The bridge reads 256 KiB of a request: an event over it would fail the hook before its decision."""
    hook(tmp_path, incoming=[], event="PostToolUse", payload={**CALL, "tool_response": "x" * 100000})
    tool = next(e for e in (json.loads(p.read_text()) for p in (tmp_path / "to-user").glob("*.json"))
                if e["kind"] == "tool")
    assert len(tool["output"]) < 21000 and "more characters" in tool["output"]
    assert tool["input"] == CALL["tool_input"]


def test_a_follow_up_claimed_at_stop_is_handed_over_though_its_report_fails(tmp_path, monkeypatch, capsys):
    """The message is claimed from the queue before it is reported: a report that failed then lost it."""
    hook(tmp_path, incoming=[("message", "go on")], event="PreToolUse", payload=CALL)    # the files, then import
    spec = importlib.util.spec_from_file_location("ahe_user_hook_test", tmp_path / "user_hook.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def report(kind, *args, **fields):
        if kind == "delivered":
            raise RuntimeError("the channel is gone")
    monkeypatch.setattr(module, "report", report)
    monkeypatch.setattr(sys, "argv", ["user_hook.py", "Stop"])
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps({"last_assistant_message": "done"})))
    module.main()
    assert json.loads(capsys.readouterr().out) == {"decision": "block", "reason": "go on"}


class Feed:
    """A transport that hands the agent's events over one read at a time, as the bridge would."""

    def __init__(self, batches):
        self.batches, self.sent = list(batches), []

    def send(self, record):
        self.sent.append(record)

    def read(self, since, wait=0.0):
        events = self.batches.pop(0) if self.batches else []
        return since + len(events), events

    def close(self):
        pass


def test_a_stop_reply_nobody_waited_for_is_an_event_not_the_next_answer():
    """AHE-Beta's handle interrupts without waiting (interrupt(timeout=0)), then asks something new: the reply Codex
    stopped with used to come back as the answer to that."""
    feed = Feed([[], [{"seq": 1, "kind": "reply", "text": "stopped", "interrupted": True}],
                 [{"seq": 2, "kind": "reply", "text": "the answer", "interrupted": False}]])
    c = channel.Conversation(None, lambda text: None, lambda: True, lambda: None, transport=feed)
    c.write("start")
    assert c.interrupt("stop", timeout=0) is None
    c.write("a new question")
    assert c.poll(5)["text"] == "the answer"
    assert [e["text"] for e in c.events() if e["kind"] == "interrupted_reply"] == ["stopped"]


def test_a_stop_reply_already_published_when_the_interrupt_came_is_the_interrupts(tmp_path):
    """Codex had stopped and its reply was published, not yet read, when AHE-Beta's handle interrupted (timeout 0):
    the Stop acknowledged the interrupt, and that reply is the interrupt's, not the answer to the next question."""
    feed = Feed([[], [{"seq": 1, "kind": "reply", "text": "done with step 1", "interrupted": False},
                      {"seq": 2, "kind": "interrupt_ack", "ids": ["u2"], "at": "Stop"}],
                 [{"seq": 3, "kind": "reply", "text": "the answer", "interrupted": False}]])
    c = channel.Conversation(None, lambda text: None, lambda: True, lambda: None, transport=feed)
    c.write("start")
    assert c.interrupt("stop", timeout=0) is None
    c.write("a new question")
    assert c.poll(5)["text"] == "the answer"
    assert [e["text"] for e in c.events() if e["kind"] == "interrupted_reply"] == ["done with step 1"]


def test_a_waited_for_interrupt_acknowledged_with_its_stop_reply_leaves_nothing_outstanding():
    """interrupt(timeout > 0) read the stop reply and its acknowledgement in one batch: counted outstanding after, the
    next interrupt's own reply went to the events and its caller waited for nothing."""
    feed = Feed([[], [{"seq": 1, "kind": "reply", "text": "done", "interrupted": False},
                      {"seq": 2, "kind": "interrupt_ack", "ids": ["u2"], "at": "Stop"}],
                 [{"seq": 3, "kind": "reply", "text": "stopped again", "interrupted": True, "interrupts": 1}]])
    c = channel.Conversation(None, lambda text: None, lambda: True, lambda: None, transport=feed)
    c.write("start")
    assert c.interrupt("stop", timeout=1)["text"] == "done"
    c.write("go on")
    assert (c.interrupt("stop again", timeout=1) or {}).get("text") == "stopped again"


def test_an_interrupt_of_an_agent_waiting_at_its_stop_returns_at_the_acknowledgement():
    """Its reply read already, Codex waits at its Stop, which only acknowledges the interrupt: waiting for a reply would
    spend the whole timeout of the session's time."""
    feed = Feed([[], [{"seq": 1, "kind": "reply", "text": "done", "interrupted": False}], [],
                 [{"seq": 2, "kind": "interrupt_ack", "ids": ["u2"], "at": "Stop"}]])
    c = channel.Conversation(None, lambda text: None, lambda: True, lambda: None, transport=feed)
    c.write("start")
    assert c.poll(1)["text"] == "done"
    started = time.monotonic()
    assert c.interrupt("stop", timeout=10) is None and time.monotonic() - started < 5


def test_after_the_adapters_stop_hook_ended_the_session_nothing_more_is_sent():
    """Its `continue: false` ended Codex: a message written after it would wait for a process that is gone."""
    feed = Feed([[], [{"seq": 1, "kind": "reply", "text": "finished", "interrupted": False, "terminal": True}]])
    c = channel.Conversation(None, lambda text: None, lambda: True, lambda: None, transport=feed)
    c.write("start")
    assert c.poll(1)["terminal"] is True
    with pytest.raises(channel.ConversationClosed, match="ended the session"):
        c.write("one more thing")


def test_an_interrupt_that_comes_while_the_adapters_stop_hooks_run_marks_the_reply(tmp_path):
    """Published unmarked, with its acknowledgement after it, that reply could answer the user's next question."""
    queued = tmp_path / "to-agent" / "0009-interrupt.json"
    interrupt = json.dumps({"id": "9", "kind": "interrupt", "text": "stop"})
    command = f"printf '%s' {shlex.quote(interrupt)} > {shlex.quote(str(queued))}; echo '{{}}'"   # the user's
    hook(tmp_path, incoming=[], adapter=[{"command": command}])
    told = [json.loads(p.read_text()) for p in sorted((tmp_path / "to-user").glob("*.json"))]
    assert [(e["kind"], e.get("interrupted"), e.get("interrupts")) for e in told if e["kind"] == "reply"] == [
        ("reply", True, 1)]
    assert not any(e["kind"] == "interrupt_ack" for e in told) and not queued.exists()


def test_a_tool_call_waits_when_the_channel_cannot_be_read(tmp_path):
    """An end may be queued there: no call runs unread (the bridge's socket is gone here)."""
    (tmp_path / "to-agent").mkdir()
    script = tmp_path / "user_hook.py"
    script.write_bytes((UTILS / "user_hook.py").read_bytes())
    script.with_suffix(".json").write_text(json.dumps({"mail": str(tmp_path), "channel": str(tmp_path / "gone.sock"),
                                                       "interrupt_frame": "{text}"}))
    said = subprocess.run([sys.executable, str(script), "PreToolUse"], input=json.dumps(CALL), text=True,
                          capture_output=True, timeout=30)
    decision = json.loads(said.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny" and "could not be read" in decision["permissionDecisionReason"]


def test_a_long_reply_reaches_the_user_cut_not_lost(tmp_path):
    """Whole, a reply past the bridge's 256 KiB a request was refused there, and the user waited for it until its time
    ran out."""
    hook(tmp_path, incoming=[], payload={"last_assistant_message": "x" * 300_000})
    reply = next(e for e in (json.loads(p.read_text()) for p in (tmp_path / "to-user").glob("*.json"))
                 if e["kind"] == "reply")
    assert len(reply["text"]) < 60_000 and reply["text"].endswith("[250000 more characters]")


def test_a_question_sent_while_an_interrupt_waits_goes_to_the_next_turn(tmp_path):
    """Handed over at the tool call the interrupt stops, it would be answered in the interrupted turn, whose reply goes
    to the events: the user would wait for an answer nobody gives."""
    after = hook(tmp_path, incoming=[("interrupt", "stop"), ("message", "what about B?")], event="PostToolUse",
                 payload={**CALL, "tool_response": "ok"})
    assert after.returncode == 0 and not after.stdout.strip()
    assert list((tmp_path / "to-agent").glob("*-message.json"))               # still queued, for the next turn


def test_after_the_users_end_no_tool_call_runs_and_no_message_is_handed_over(tmp_path):
    """The end stays queued once a Stop has seen it: a hook still in flight cannot start a call or deliver a
    follow-up the user queued before ending."""
    hook(tmp_path, incoming=[("end", "")])                                     # the Stop that lets Codex stop
    denied = hook(tmp_path, incoming=[], event="PreToolUse", payload=CALL)
    assert json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    (tmp_path / "second").mkdir()
    after = hook(tmp_path / "second", incoming=[("message", "one more thing"), ("end", "")], event="PostToolUse",
                 payload={**CALL, "tool_response": "ok"})
    assert after.returncode == 0 and not after.stdout.strip()


def test_a_message_is_handed_over_at_a_tool_call_though_every_report_fails(tmp_path, monkeypatch, capsys):
    """The decision comes before the reports, which could otherwise use up the hook's own time limit."""
    hook(tmp_path, incoming=[("message", "also this")], event="PreToolUse", payload=CALL)   # the files, then import
    spec = importlib.util.spec_from_file_location("ahe_user_hook_post", tmp_path / "user_hook.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def report(kind, *args, **fields):
        raise RuntimeError("the channel is slow")
    monkeypatch.setattr(module, "report", report)
    monkeypatch.setattr(sys, "argv", ["user_hook.py", "PostToolUse"])
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps({**CALL, "tool_response": "ok"})))
    module.main()
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"] == "also this"


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
