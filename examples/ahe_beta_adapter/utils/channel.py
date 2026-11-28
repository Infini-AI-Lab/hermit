"""Conversation API backed by either a legacy mailbox or a host-owned bridge."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Protocol


class ConversationTransport(Protocol):
    """Host-owned transport used when the mailbox must stay outside the Actor's view."""

    def send(self, record: dict) -> None: ...

    async def asend(self, record: dict) -> None: ...

    def read(self, since: int, wait: float = 0.0) -> tuple[int, list[dict]]: ...

    def close(self) -> None: ...


class ConversationClosed(RuntimeError):
    """Codex is no longer running (it exited, or the trial was cancelled)."""


class Conversation:
    """The running session, as the user's code holds it. Called from the thread `simulate` runs in."""

    capabilities = frozenset({"turns", "insert", "interrupt", "progress"})
    wait_s = 5.0             # the longest a transport's read waits for new events (one call), while poll waits

    def __init__(self, mail: Path | None, start, alive, stop, *, transport: ConversationTransport | None = None):
        if mail is None and transport is None:
            raise ValueError("Conversation needs a mailbox path or a transport")
        self.mail, self._transport = mail, transport
        self._start, self._alive, self._stop = start, alive, stop
        self.transcript: list[dict] = []
        self.closed = False
        self.gone = False        # the session found Codex gone without a reply (its clock, its failure)
        self._owed: list[str] = []      # interrupts whose stop reply (or acknowledgement) nobody waits for any more
        self._waiting: set[str] = set()   # interrupts a caller of interrupt() waits on
        self._rids: set[str] = set()      # the replies read, by the id the hook gives each (a retried one is one)
        self._started = False
        self._seen: set[str] = set()
        self._events: list[dict] = []
        self._replies: list[dict] = []
        self._n = 0
        self._transport_seq = 0
        self._lock = threading.Lock()
        if self.mail is not None:
            for name in ("to-agent", "to-user", "taken"):
                (self.mail / name).mkdir(parents=True, exist_ok=True)
                os.chmod(
                    self.mail / name, 0o777
                )  # the container's user is not this process's user
            os.chmod(self.mail, 0o777)

    # ----- to the agent -----------------------------------------------------------------------------------------------
    def _drop(self, kind: str, **fields) -> dict:
        with self._lock:
            self._n += 1
            record = {
                "id": f"u{self._n}",
                "kind": kind,
                "t": round(time.time(), 3),
                **fields,
            }
            if self._transport is not None:
                self._transport.send(record)
                self.transcript.append({"from": "user", **record})
                return record
            name = f"{time.time():017.6f}-{kind}.json"
            tmp = self.mail / "to-agent" / ("." + name)
            tmp.write_text(json.dumps(record))
            os.chmod(tmp, 0o666)
            os.replace(tmp, self.mail / "to-agent" / name)
            self.transcript.append({"from": "user", **record})
        return record

    def write(self, text: str) -> None:
        """Say something. The first message starts Codex with it as the prompt; a later one reaches the model after
        the tool call that is running, or as the next turn when the agent has stopped."""
        if self.closed:
            raise ConversationClosed("the session is over")
        if any(r.get("terminal") for r in self.transcript if r.get("from") == "agent"):
            raise ConversationClosed("the adapter's Stop hook ended the session")   # Codex is gone: none would read it
        if not self._started:
            self._started = True
            self._n += 1
            self.transcript.append(
                {
                    "from": "user",
                    "id": f"u{self._n}",
                    "kind": "message",
                    "t": round(time.time(), 3),
                    "text": text,
                    "first": True,
                }
            )
            self._start(text)
            return
        self._drop("message", text=text)

    def interrupt(self, text: str = "", timeout: float = 300.0) -> dict | None:
        """Stop the agent at its next tool call (it is told why) and wait for it to stop: the reply it stops with,
        or None when it has not stopped within `timeout`. A stop reply nobody waited for comes as an
        `interrupted_reply` event, never as the answer to a later message."""
        if self.closed or not self._started:
            raise ConversationClosed("there is no active request to interrupt")
        mine = self._drop("interrupt", text=text)["id"]

        def heard():                                     # stopped at its Stop already: no reply of its own follows
            return any(e.get("kind") == "interrupt_ack" and mine in (e.get("ids") or ()) for e in self._events)
        reply, deadline = None, time.monotonic() + max(0.0, timeout)
        self._waiting.add(mine)                          # a stop reply that answers it is this caller's (_reply)
        try:
            while timeout > 0 and reply is None and not heard() and time.monotonic() < deadline:
                reply = self.poll(min(1.0, max(0.0, deadline - time.monotonic())))
        finally:
            self._waiting.discard(mine)
        answered = bool(reply and reply.get("interrupted") and mine in (reply.get("interrupt_ids") or [mine]))
        if not answered and not heard():                 # its stop reply, or its acknowledgement, still to come
            self._owed.append(mine)
        return reply

    def _reply(self) -> dict | None:
        """The next reply a caller is owed: an unclaimed stop reply goes to the events instead."""
        while self._replies:
            reply = self._replies.pop(0)
            if reply.get("interrupted"):                 # the interrupts it answers: those it names, else (no names)
                ids = reply.get("interrupt_ids")         # as many of the owed as it counts
                ids = list(ids) if ids is not None else self._owed[:int(reply.get("interrupts") or 1)]
                owed = [i for i in ids if i in self._owed]
                self._owed = [i for i in self._owed if i not in ids]
                if owed and not self._waiting.intersection(ids):
                    self._events.append({**reply, "kind": "interrupted_reply"})
                    continue
            return reply
        return None

    def end(self) -> None:
        """Let the agent stop for good. Called by the framework side when `simulate` returns."""
        if self._started and not self.closed:
            self._drop("end")
        self.closed = True

    async def end_async(self) -> None:
        """End from the Harbor event loop without synchronously waiting on that same loop."""
        with self._lock:
            if self.closed:
                return
            record = None
            if self._started:
                self._n += 1
                record = {
                    "id": f"u{self._n}",
                    "kind": "end",
                    "t": round(time.time(), 3),
                }
            self.closed = True
        if record is None:
            return
        if self._transport is not None:
            asend = getattr(self._transport, "asend", None)
            if callable(asend):
                await asend(record)
            else:
                self._transport.send(record)
            self.transcript.append({"from": "user", **record})
            return
        name = f"{time.time():017.6f}-end.json"
        tmp = self.mail / "to-agent" / ("." + name)
        tmp.write_text(json.dumps(record))
        os.chmod(tmp, 0o666)
        os.replace(tmp, self.mail / "to-agent" / name)
        self.transcript.append({"from": "user", **record})

    def close(self) -> None:
        """End the conversation and synchronously stop the Actor before capture."""
        self.end()
        self._stop()
        if self._transport is not None:
            self._transport.close()

    # ----- from the agent ---------------------------------------------------------------------------------------------
    def _read(self, wait: float = 0.0) -> None:
        """What the agent did since the last read; a transport may wait up to `wait` seconds for it (one call)."""
        if self._transport is not None:
            self._transport_seq, events = self._transport.read(self._transport_seq, wait)
            for event in events:
                if event.get("kind") == "reply" and event.get("rid"):
                    if event["rid"] in self._rids:   # a reply the hook sent again, its first report having reached
                        continue
                    self._rids.add(event["rid"])
                self.transcript.append({"from": "agent", **event})
                acked = [i for i in event.get("ids") or () if i in self._owed] if event.get("ids") else self._owed[:1]
                if event.get("kind") == "interrupt_ack" and acked:   # stopped at its Stop already: the reply it
                    self._owed = [i for i in self._owed if i not in acked]   # published there, not handed out yet,
                    if self._replies:                                        # is the interrupt's
                        self._events.append({**self._replies.pop(), "kind": "interrupted_reply"})
                (self._replies if event.get("kind") == "reply" else self._events).append(
                    event
                )
            return
        for path in sorted((self.mail / "to-user").glob("[!.]*.json")):
            if path.name in self._seen:
                continue
            try:
                event = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            self._seen.add(path.name)
            self.transcript.append({"from": "agent", **event})
            (self._replies if event.get("kind") == "reply" else self._events).append(
                event
            )

    def drain(self) -> None:
        """Read what the agent reported since the last read into the transcript, kept for events() and poll()."""
        self._read()

    def poll(self, timeout: float = 0.0) -> dict | None:
        """The agent's next reply ({"kind": "reply", "text": ..., "interrupted": bool}), waiting up to `timeout`
        seconds; None when it is still working. Raises ConversationClosed when Codex is gone."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            left = deadline - time.monotonic()
            self._read(min(max(0.0, left), self.wait_s) if self._transport is not None and self._replies == [] else 0.0)
            reply = self._reply()
            if reply is not None:
                return reply
            if self.closed or not self._alive():
                self._read()
                reply = self._reply()
                if reply is not None:
                    return reply
                self.gone = not self.closed
                raise ConversationClosed("Codex exited without a reply")
            if time.monotonic() >= deadline:
                return None
            if self._transport is None:
                time.sleep(0.2)

    def send(self, text: str, timeout: float | None = None) -> str:
        """Say something and wait for the whole reply."""
        self.write(text)
        reply = self.poll(timeout if timeout is not None else 10**9)
        if reply is None:
            raise TimeoutError(f"no reply within {timeout} s")
        return reply["text"]

    def events(self) -> list[dict]:
        """What the agent did since the last call: tool_start, tool (with its output), denied, delivered (a message
        of the user's reached the model, and where), adapter_block, timeout."""
        self._read()
        out, self._events = self._events, []
        return out
