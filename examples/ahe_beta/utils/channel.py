"""File mailbox shared by the Codex hooks and a task-owned simulated user."""

from __future__ import annotations
import json
import os
from pathlib import Path
import threading
import time


class ConversationClosed(RuntimeError):
    """Codex is no longer running (it exited, or the trial was cancelled)."""


class Conversation:
    """The running session, as the user's code holds it. Called from the thread `simulate` runs in."""

    capabilities = frozenset({"turns", "insert", "interrupt", "progress"})

    def __init__(self, mail: Path, start, alive, stop):
        self.mail, self._start, self._alive, self._stop = mail, start, alive, stop
        self.transcript: list[dict] = []
        self.closed = False
        self._started = False
        self._seen: set[str] = set()
        self._events: list[dict] = []
        self._replies: list[dict] = []
        self._n = 0
        self._lock = threading.Lock()
        for name in ("to-agent", "to-user", "taken"):
            (mail / name).mkdir(parents=True, exist_ok=True)
            os.chmod(
                mail / name, 0o777
            )  # the container's user is not this process's user
        os.chmod(mail, 0o777)

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
        or None when it has not stopped within `timeout`."""
        if self.closed or not self._started:
            raise ConversationClosed("there is no active request to interrupt")
        self._drop("interrupt", text=text)
        return self.poll(timeout) if timeout > 0 else None

    def end(self) -> None:
        """Let the agent stop for good. Called by the framework side when `simulate` returns."""
        if self._started and not self.closed:
            self._drop("end")
        self.closed = True

    def close(self) -> None:
        """End the conversation and synchronously stop the Actor before capture."""
        self.end()
        self._stop()

    # ----- from the agent ---------------------------------------------------------------------------------------------
    def _read(self) -> None:
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

    def poll(self, timeout: float = 0.0) -> dict | None:
        """The agent's next reply ({"kind": "reply", "text": ..., "interrupted": bool}), waiting up to `timeout`
        seconds; None when it is still working. Raises ConversationClosed when Codex is gone."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            self._read()
            if self._replies:
                return self._replies.pop(0)
            if self.closed or not self._alive():
                self._read()
                if self._replies:
                    return self._replies.pop(0)
                raise ConversationClosed("Codex exited without a reply")
            if time.monotonic() >= deadline:
                return None
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
