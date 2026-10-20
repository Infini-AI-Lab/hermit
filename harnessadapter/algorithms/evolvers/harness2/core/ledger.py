# Derived from google-research/harness2 core/ledger.py, Copyright 2026 Google LLC, Apache-2.0.
"""Budget accounting for one step: how many rollouts (probes of a batch) and improver sessions it spent.

Upstream's identity is R = 4k rollouts and P = 3k sessions per task (k reference executions, 2k candidate probes,
k final probes; k INV + k DOM + k compositions). In the framework's exec mode the reference set is the Trainer's
one rollout of the batch, so a step records R = 3k + 1 and P = 3k; the library mode keeps the paper's identity.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def expected_rollouts(k: int, *, reference: int = 1) -> int:
    return int(reference) + 3 * int(k)


def expected_sessions(k: int) -> int:
    return 3 * int(k)


class BudgetError(AssertionError):
    """The ledger did not land on the budget identity."""


@dataclass
class Entry:
    kind: str
    label: str
    usd: float = 0.0
    cached: bool = False
    attempts: int = 1


@dataclass
class Ledger:
    mode: str
    unit: str          # the task id (library mode) or the step label (exec mode)
    k: int
    reference: int = 1
    entries: list[Entry] = field(default_factory=list)

    def rollout(self, label: str, usd: float = 0.0, *, cached: bool = False) -> None:
        self.entries.append(Entry("rollout", label, usd, cached))

    def session(self, label: str, usd: float = 0.0, *, cached: bool = False, attempts: int = 1) -> None:
        self.entries.append(Entry("session", label, usd, cached, attempts=attempts))

    @property
    def R(self) -> int:
        return sum(1 for e in self.entries if e.kind == "rollout")

    @property
    def P(self) -> int:
        return sum(1 for e in self.entries if e.kind == "session")

    @property
    def usd(self) -> float:
        return round(sum(e.usd for e in self.entries), 6)

    def labels(self, kind: str) -> list[str]:
        return [e.label for e in self.entries if e.kind == kind]

    def assert_identity(self) -> None:
        want_r, want_p = expected_rollouts(self.k, reference=self.reference), expected_sessions(self.k)
        rl, sl = self.labels("rollout"), self.labels("session")
        dupes = sorted({x for x in rl if rl.count(x) > 1} | {x for x in sl if sl.count(x) > 1})
        if not dupes and self.R == want_r and self.P == want_p:
            return
        detail = [f"{self.mode}-{self.k} {self.unit}: R={self.R} (want {want_r}), P={self.P} (want {want_p})",
                  f"  rollouts: {', '.join(rl) or '(none)'}", f"  sessions: {', '.join(sl) or '(none)'}"]
        if dupes:
            detail.append(f"  DUPLICATE labels: {dupes}")
        raise BudgetError("\n".join(detail))

    def to_dict(self) -> dict:
        return {"mode": self.mode, "unit": self.unit, "k": self.k, "R": self.R,
                "expected_R": expected_rollouts(self.k, reference=self.reference), "P": self.P,
                "expected_P": expected_sessions(self.k),
                "session_attempts": sum(e.attempts for e in self.entries if e.kind == "session"),
                "usd": self.usd, "rollouts": self.labels("rollout"), "sessions": self.labels("session")}
