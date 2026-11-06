"""A coding-agent CLI's record: its session handle and its usage, read from its JSON event stream, and the cost of
that usage at the backend's price."""
from __future__ import annotations

import json
from pathlib import Path

from hermit.agent.model.usage import Usage


def price_usage(usage: list[Usage], params: dict) -> None:
    """Fill in each usage's cost from `price` (USD per million tokens: {input, cached_input, output}) where the agent
    reported none. Claude Code reports a cost at Anthropic's prices; for a model bound to another backend that is not
    the bill, so the backend's price replaces it (no price: the cost is unknown)."""
    price = params.get("price")
    binding = params.get("binding") or {}
    foreign = bool(binding) and not str(binding.get("litellm") or "").startswith("anthropic/")
    for u in usage:
        if foreign:
            u.cost_usd = None
        if price and u.cost_usd is None:
            u.cost_usd = ((u.input_tokens - u.cached_input_tokens) * price.get("input", 0)
                          + u.cached_input_tokens * price.get("cached_input", price.get("input", 0))
                          + u.output_tokens * price.get("output", 0)) / 1e6


def _parse(log: Path, model: str, read) -> tuple[str | None, list[Usage]]:
    """Session handle and usage from a JSON event stream, each event given to `read(event, usage, session)`, which
    adds the event's usage and returns the session handle. The stream also carries the CLI's own messages; a line
    that is not a JSON object (one that does not parse, or a quoted word or a number that does) is no event."""
    session, usage = None, Usage(model=model)
    if not log.exists():
        return None, []
    for line in log.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        session = read(event, usage, session)
    return session, ([usage] if usage.calls else [])


def _codex_event(event: dict, usage: Usage, session):
    if event.get("type") == "thread.started":
        session = event.get("thread_id") or session
    if event.get("type") == "turn.completed" and event.get("usage"):
        u = event["usage"]
        usage.input_tokens += int(u.get("input_tokens", 0))
        usage.cached_input_tokens += int(u.get("cached_input_tokens", 0))
        usage.output_tokens += int(u.get("output_tokens", 0))
        usage.calls += 1
    return session


def _claude_event(event: dict, usage: Usage, session):
    session = event.get("session_id") or session
    if event.get("type") == "result":
        u = event.get("usage") or {}
        usage.input_tokens += int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0))
        usage.cached_input_tokens += int(u.get("cache_read_input_tokens", 0))
        usage.output_tokens += int(u.get("output_tokens", 0))
        usage.calls += int(event.get("num_turns", 1))
        if event.get("total_cost_usd") is not None:
            usage.cost_usd = float(event["total_cost_usd"])
    return session


def parse_codex(log: Path, model: str) -> tuple[str | None, list[Usage]]:
    """Codex's `exec --json` events: the thread id, and each completed turn's usage."""
    return _parse(log, model, _codex_event)


def parse_claude(log: Path, model: str) -> tuple[str | None, list[Usage]]:
    """Claude Code's stream-json: the session id, and the result's usage and cost."""
    return _parse(log, model, _claude_event)


def parse_log(log: Path, backend: str, model: str) -> tuple[str | None, list[Usage]]:
    """Session handle and usage from the agent's JSON event stream: Codex's, else Claude Code's."""
    return (parse_codex if backend == "codex" else parse_claude)(log, model)
