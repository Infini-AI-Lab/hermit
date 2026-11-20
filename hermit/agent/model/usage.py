"""A model's usage: tokens, calls and cost, as every layer reports it."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    cost_usd: float | None = None   # None when no price applies (for example a subscription)
    unreported_calls: int = 0       # calls whose tokens the CLI did not report: no price makes a cost of them


class Ledger:
    """One run's usage, written by every attempt, every call given up in flight and every run again, and read once:
    the one place tokens, calls and cost are summed for an agent's run and for a rollout's (hermit.trainer.rollout).
    The cost is unknown (None) when any entry's is; an entry that may have been billed but reported nothing
    (`unreported`) makes it unknown. What an empty ledger reports is the caller's word: None (nothing known: a session
    ran and said nothing) or 0 (nothing ran)."""

    def __init__(self, model: str = ""):
        self.model = model
        self.entries: list[Usage] = []

    def add(self, usages) -> "Ledger":
        self.entries += list(usages or [])
        return self

    def unreported(self, calls: int = 1, model: str | None = None, *, sessions: int = 0) -> "Ledger":
        """`calls` requests given up in flight, or `sessions` sessions cut off before their usage: maybe billed, their
        usage never reported (the cost is then unknown)."""
        if calls or sessions:
            self.entries.append(Usage(model=str(model or self.model or ""), calls=calls,
                                      unreported_calls=calls + sessions, cost_usd=None))
        return self

    @property
    def empty(self) -> bool:
        return not self.entries

    def cost(self, empty: float | None = None) -> float | None:
        if not self.entries:
            return empty
        return None if any(u.cost_usd is None for u in self.entries) else sum(u.cost_usd for u in self.entries)

    def tokens(self, empty: int | None = None) -> int | None:
        """The tokens reported; unknown (None) when no entry reported any: only sessions or calls cut off before
        their usage (`unreported`), never a measured 0."""
        if not self.entries:
            return empty
        if all(u.unreported_calls and not (u.input_tokens or u.output_tokens) for u in self.entries):
            return None
        return sum(u.input_tokens + u.output_tokens for u in self.entries)

    def calls(self) -> int:
        return sum(u.calls for u in self.entries)

    def total(self) -> Usage:
        """The sum as one Usage (its cost None when the ledger is empty or any entry's is unknown)."""
        return Usage(model=self.model, input_tokens=sum(u.input_tokens for u in self.entries),
                     cached_input_tokens=sum(u.cached_input_tokens for u in self.entries),
                     output_tokens=sum(u.output_tokens for u in self.entries), calls=self.calls(),
                     cost_usd=self.cost(None), unreported_calls=sum(u.unreported_calls for u in self.entries))


class Attempts:
    """A rollout's runs, each one's own record and their sum (Hermit's Harbor runner's `_attempts`): the tokens the
    runs reported (unknown only when none did), their model calls (a run that reported none adds 0) and their cost
    (unknown when any run's is); each run's own in `extra.attempts` (its usage alone and its infra cause, `unscored`,
    None for a run that is the agent's result). One run alone is returned as it is."""

    def __init__(self):
        self.runs: list = []

    def add(self, out) -> None:
        self.runs.append(out)

    def output(self):
        import dataclasses
        last = self.runs[-1]
        if len(self.runs) == 1:
            return last
        tokens = [r.tokens for r in self.runs if r.tokens is not None]
        costs = [r.cost_usd for r in self.runs]
        calls = sum(int((r.api_speed or {}).get("model_calls") or 0) for r in self.runs)
        each = [{"tokens": r.tokens, "cost_usd": r.cost_usd, "model_calls": (r.api_speed or {}).get("model_calls"),
                 "unscored": (r.extra or {}).get("unscored")} for r in self.runs]
        return dataclasses.replace(last, tokens=sum(tokens) if tokens else None,
                                   cost_usd=None if None in costs else sum(costs),
                                   api_speed={**(last.api_speed or {}), "model_calls": calls},
                                   extra={**(last.extra or {}), "attempts": each})
