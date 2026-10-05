"""Hard budgets (usd, tokens, hours), observed wherever spending is visible; thread-safe."""
from __future__ import annotations

from dataclasses import asdict
import threading
import time

from harnessadapter.evolver.base import Usage


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, usd: float | None = None, tokens: int | None = None, hours: float | None = None,
                 spent: dict | None = None):
        self.limits = {"usd": usd, "tokens": tokens, "hours": hours}
        spent = spent or {}
        self._lock = threading.Lock()
        self.usd = {"rollout": 0.0, "evolver": 0.0, **spent.get("usd", {})}
        self.tokens = {"rollout": 0, "evolver": 0, **spent.get("tokens", {})}
        self.unknown_cost_calls = int(spent.get("unknown_cost_calls", 0))
        self.elapsed_before = float(spent.get("seconds", 0.0))   # time spent before a resume
        self.started = time.time()

    def seconds(self) -> float:
        return self.elapsed_before + (time.time() - self.started)

    def add(self, part: str, usage: Usage | None = None, *, usd: float | None = None, tokens: int | None = None):
        with self._lock:
            if usage is not None:
                self.tokens[part] += usage.input_tokens + usage.output_tokens
                if usage.cost_usd is None:
                    self.unknown_cost_calls += max(usage.calls, 1)
                else:
                    self.usd[part] += usage.cost_usd
            if usd is not None:
                self.usd[part] += usd
            if tokens is not None:
                self.tokens[part] += tokens

    def exceeded(self) -> str | None:
        with self._lock:
            l = self.limits
            if l["usd"] is not None and sum(self.usd.values()) >= l["usd"]:
                return f"usd budget {l['usd']} reached ({sum(self.usd.values()):.4f})"
            if l["tokens"] is not None and sum(self.tokens.values()) >= l["tokens"]:
                return f"token budget {l['tokens']} reached"
        if l["hours"] is not None and self.seconds() >= l["hours"] * 3600:
            return f"time budget {l['hours']} h reached"
        return None

    def check(self):
        reason = self.exceeded()
        if reason:
            raise BudgetExceeded(reason)

    def remaining(self) -> dict:
        l = self.limits
        return {"usd": None if l["usd"] is None else round(l["usd"] - sum(self.usd.values()), 4),
                "tokens": None if l["tokens"] is None else l["tokens"] - sum(self.tokens.values()),
                "hours": None if l["hours"] is None else round(l["hours"] - self.seconds() / 3600, 4)}

    def snapshot(self) -> dict:
        with self._lock:
            return {"usd": dict(self.usd), "tokens": dict(self.tokens), "unknown_cost_calls": self.unknown_cost_calls,
                    "seconds": round(self.seconds(), 1)}
