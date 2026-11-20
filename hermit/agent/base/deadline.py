"""One absolute deadline for a whole agent run, the one implementation every agent uses (ReAct's loop, the answer
agent, a CLI's session and its preparation): a point in time (or none), what is left of it, a time limit cut to it,
and a chat client bound to it (`bound`: every call given up once it passes, as Harbor's agent timeout cancelled its
agents; the call in flight closed, ChatAborted, and counted as abandoned: maybe billed, its usage never reported)."""
from __future__ import annotations

import threading
import time


class Deadline:
    def __init__(self, at: float | None = None):
        self.at = None if at is None else float(at)      # time.time() seconds; None: no deadline

    @classmethod
    def within(cls, seconds: float | None) -> "Deadline":
        return cls(time.time() + float(seconds) if seconds else None)

    def remaining(self) -> float | None:
        return None if self.at is None else max(0.0, self.at - time.time())

    @property
    def expired(self) -> bool:
        return self.at is not None and time.time() >= self.at

    def cut(self, seconds: float | None):
        """`seconds` cut to what is left (what is left when none is given); False when nothing is left."""
        left = self.remaining()
        if left is None:
            return seconds
        if left <= 0:
            return False
        return min(float(seconds), left) if seconds else left

    def bound(self, client) -> "BoundClient":
        return BoundClient(client, self)


class BoundClient:
    """A chat client whose calls end at the deadline (Deadline.bound). `expired` says whether it passed."""

    def __init__(self, client, deadline: Deadline):
        self.client, self.deadline, self.abort = client, deadline, threading.Event()
        self.abandoned = 0                # calls given up in flight: sent, maybe billed, their usage never reported
        left = deadline.remaining()
        self.timer = threading.Timer(left if left is not None else 1e9, self.abort.set)
        self.timer.daemon = True
        if left is not None:
            self.timer.start()

    @property
    def expired(self) -> bool:
        return self.abort.is_set()

    def remaining(self) -> float:
        left = self.deadline.remaining()
        return float("inf") if left is None else left

    def chat(self, messages, **kwargs):
        from hermit.agent.model.llm import ChatAborted
        if self.abort.is_set():
            raise ChatAborted("the task's agent budget is spent")
        try:
            return self.client.chat(messages, abort=self.abort, **kwargs)
        except ChatAborted:
            self.abandoned += 1
            raise

    def close(self) -> None:
        self.timer.cancel()

    def __getattr__(self, name):
        return getattr(self.client, name)
