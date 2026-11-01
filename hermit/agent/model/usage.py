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
