"""A coding-agent CLI's record: its session handle and its usage, read from its JSON event stream, and the cost of
that usage at the backend's price."""
from __future__ import annotations

import json
from pathlib import Path

from hermit.agent.model.usage import Usage


def price_usage(usage: list[Usage], params: dict) -> None:
    """Fill in each usage's cost from `price` (USD per million tokens: {input, cached_input, output}) where the agent
    reported none. Claude Code reports a cost at Anthropic's prices; for a model bound to another backend that is not
    the bill, so the backend's price replaces it (no price: the cost is unknown). On a subscription (`login`) that
    figure is the same estimate at list prices, not what the session costs: none is kept. A usage that holds calls
    whose tokens the CLI did not report (`unreported_calls`) keeps an unknown cost: its reported tokens' price would
    be too little."""
    price = params.get("price")
    binding = params.get("binding") or {}
    foreign = bool(params.get("login")) or \
        (bool(binding) and not str(binding.get("litellm") or "").startswith("anthropic/"))
    for u in usage:
        if foreign:
            u.cost_usd = None
        if price and u.cost_usd is None and not u.unreported_calls:
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
    return session, ([usage] if usage.calls or usage.input_tokens or usage.output_tokens else [])


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
        _add_claude_result(usage, event)
    return session


def _add_claude_result(usage: Usage, event: dict) -> None:
    """One result of a Claude Code stream. A session whose sub-agents run in the background has several results, and
    a resumed one's `modelUsage` and `total_cost_usd` carry its earlier invocations: `model_usage_since_last_result`
    (stream-json only) is what this one spent, sub-agents included. Without it, its `usage` and cost. Input tokens are
    all that was sent: the fresh ones, those read from the prompt cache (`cached_input_tokens`) and those written to it."""
    since = event.get("model_usage_since_last_result")
    parts = [p for p in since.values() if isinstance(p, dict)] if isinstance(since, dict) else []
    if parts:
        read = sum(int(p.get("cacheReadInputTokens") or 0) for p in parts)
        usage.input_tokens += sum(int(p.get("inputTokens") or 0) + int(p.get("cacheCreationInputTokens") or 0)
                                  for p in parts) + read
        usage.cached_input_tokens += read
        usage.output_tokens += sum(int(p.get("outputTokens") or 0) for p in parts)
        cost = sum(float(p.get("costUSD") or 0) for p in parts)
        usage.cost_usd = (usage.cost_usd or 0.0) + cost
    else:
        u = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        read = int(u.get("cache_read_input_tokens") or 0)
        usage.input_tokens += int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + read
        usage.cached_input_tokens += read
        usage.output_tokens += int(u.get("output_tokens") or 0)
        if event.get("total_cost_usd") is not None:
            usage.cost_usd = float(event["total_cost_usd"])
    usage.calls += int(event.get("num_turns", 1) or 0)


def _pi_event(event: dict, usage: Usage, session):
    """Pi: the session header's id, and each assistant message's usage as Pi kept it (`message_end`; the deltas'
    running usage is left out), and a compaction's request (`compaction_end`, whose summary is no message). Pi's
    `input` leaves the cache out, so the input counted is input, cache reads and cache writes, as Codex and Claude
    Code count it; its cost is Pi's own (for a bound model, Pi.adjust_usage leaves it to the price). A request that
    failed counts as a call only when it used tokens (Pi records each failed attempt it retries)."""
    if event.get("type") == "session":
        session = event.get("id") or session
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    result = event.get("result") if isinstance(event.get("result"), dict) else {}
    if event.get("type") == "compaction_end" and isinstance(result.get("usage"), dict):
        message = {"usage": result["usage"]}
    elif not (event.get("type") == "message_end" and message.get("role") == "assistant"):
        return session
    if isinstance(message.get("usage"), dict):
        u = message["usage"]
        cached = int(u.get("cacheRead") or 0)
        tokens = int(u.get("input") or 0) + cached + int(u.get("cacheWrite") or 0)
        if not tokens and not u.get("output") and message.get("stopReason") in ("error", "aborted"):
            return session
        usage.input_tokens += tokens
        usage.cached_input_tokens += cached
        usage.output_tokens += int(u.get("output") or 0)
        usage.calls += 1
        cost = (u.get("cost") or {}).get("total") if isinstance(u.get("cost"), dict) else None
        if cost is not None:
            usage.cost_usd = (usage.cost_usd or 0.0) + float(cost)
    return session


def parse_pi(log: Path, model: str) -> tuple[str | None, list[Usage]]:
    """Pi's `--mode json` events: the session id, and every assistant message's usage and cost."""
    return _parse(log, model, _pi_event)


def _dsh_reader():
    """DeepSeek Harness: the session id (its opening `session` event), and each model step's usage (`status` events
    of phase `step_end`): the sum of its attempts' (a request dsh retried is an attempt). dsh's `inputTokens` leaves
    the cache out, so the input counted is input, cache reads and cache writes, as Codex and Claude Code count it. dsh
    reports no cost: it is priced from the backend's price (price_usage), which prices a cache write as input.

    A step that produced nothing (its requests all failed) is no call; one that produced a reply, a thought or a tool
    call is, even when its tokens are not reported: the provider reported no usage (dsh then reports zeros), or an
    attempt reported none at all (a stream that ended before its last event, a failed request of dsh's own provider:
    dsh then leaves the step's usage out). Such a step is an unreported call: it adds no tokens, and the session's
    cost is unknown (price_usage), not the price of what was reported.

    A step one of whose attempts reported no cache count (a request the provider refused, as for a rate limit dsh
    retried, reports zeros, and dsh keeps a cache count only when it is not 0) has its cache counts left out by dsh,
    which its `totalTokens` still holds. What the total holds beyond the counts reported is counted as input, priced
    as a cache write is: the step's price is what it would be with the counts reported, or more if those tokens were
    read from the cache (a price whose cached input is no dearer than its input), never less. The total is the sum
    of the counts on every route but the Responses API's, where it is the server's own `total_tokens`, trusted: what
    it holds beyond the counts is priced as input, and a server that reports none leaves those tokens out.

    What dsh's stream leaves out is not counted: a compaction's summary request, web searches (each a request of
    DeepSeek's own API), the title request (off in a session, hermit.agent.base.cli.DSH_OFF), sub-agents, what a
    provider billed for an attempt it reported as zeros (one cut off mid-stream, before its usage), and the last
    step of a session a stop (SIGTERM: a timeout, a cancelled call) cut short before that step's end: dsh writes a
    step's usage at its end, after its tool calls ran, and a stopped session writes nothing more."""
    produced = False

    def read(event: dict, usage: Usage, session):
        nonlocal produced
        kind = event.get("type")
        if kind == "session":
            session = event.get("sessionId") or session
        if kind in ("text", "thinking", "tool_call"):
            produced = True
        if kind != "status" or event.get("phase") != "step_end":
            return session
        made, produced = produced, False
        u = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        cached, output = int(u.get("cacheReadTokens") or 0), int(u.get("outputTokens") or 0)
        tokens = int(u.get("inputTokens") or 0) + cached + int(u.get("cacheWriteTokens") or 0)
        dropped = max(int(u.get("totalTokens") or 0) - tokens - output, 0)     # cache counts dsh left out
        if not tokens and not output and not dropped:
            usage.calls += 1 if made else 0
            usage.unreported_calls += 1 if made else 0
            return session
        usage.input_tokens += tokens + dropped
        usage.cached_input_tokens += cached
        usage.output_tokens += output
        usage.calls += 1
        return session
    return read


def parse_dsh(log: Path, model: str) -> tuple[str | None, list[Usage]]:
    """DeepSeek Harness's `--json` events: the session id, and every model step's usage (_dsh_reader)."""
    return _parse(log, model, _dsh_reader())


def parse_codex(log: Path, model: str) -> tuple[str | None, list[Usage]]:
    """Codex's `exec --json` events: the thread id, and each completed turn's usage."""
    return _parse(log, model, _codex_event)


def parse_claude(log: Path, model: str) -> tuple[str | None, list[Usage]]:
    """Claude Code's stream-json: the session id, and the result's usage and cost."""
    return _parse(log, model, _claude_event)


def parse_log(log: Path, backend: str, model: str) -> tuple[str | None, list[Usage]]:
    """Session handle and usage from the agent's JSON event stream, read as its CLI writes it (Claude Code's for a
    backend it does not know)."""
    return {"codex": parse_codex, "pi": parse_pi, "dsh": parse_dsh}.get(backend, parse_claude)(log, model)
