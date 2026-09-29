"""One process-wide inference scheduler and narrow pre-generation retries.

Never retries a timeout, unknown failure, or partly generated response. Callers
classify explicit refusal evidence (kind 'rate', shared cooldown) or a dropped
connection before generation (kind 'transport', local backoff only) and keep
the request bytes unchanged. This module has no network, credential,
model-selection or agent-loop functionality.
"""
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import socket
import threading
import time
import itertools

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (15, 30, 60, 120)
# A plain client signature: the provider's edge (Cloudflare) has blocked the bare urllib one (error 1010).
HEADERS = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "officeqa-harness/1.0"}


def edge_block(code, body):
    """An edge/WAF refusal (non-JSON 403 such as Cloudflare 'error code: 1010'): transient, re-sendable."""
    return code == 403 and not isinstance(body, dict)


def utc():
    return datetime.now(timezone.utc).isoformat()


def retry_after_seconds(value, *, now=None):
    if value is None:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            seconds = parsed.timestamp() - (time.time() if now is None else now)
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def nonzero_usage(value):
    if isinstance(value, dict):
        return any(nonzero_usage(v) for v in value.values())
    if isinstance(value, list):
        return any(nonzero_usage(v) for v in value)
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value != 0


def usage_is_reported(usage):
    if not isinstance(usage, dict):
        return False
    pairs = (("input_tokens", "output_tokens"), ("prompt_tokens", "completion_tokens"))
    numeric = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    return (numeric(usage.get("total_tokens")) or
            any(all(numeric(usage.get(k)) for k in pair) for pair in pairs))


def explicit_rate_error(error):
    """Inspect provider error fields, never ordinary generated model text."""
    if isinstance(error, dict):
        values = [error.get(k) for k in ("code", "type", "message", "error")]
        return any(explicit_rate_error(v) for v in values)
    if isinstance(error, list):
        return any(explicit_rate_error(v) for v in error)
    if not isinstance(error, str):
        return False
    text = error.lower().replace("_", " ").replace("-", " ")
    return "rate limit" in text or "too many requests" in text


def no_generation(body, *, observed_generation=False, responses_envelope=False):
    if observed_generation or not isinstance(body, dict):
        return not observed_generation and body is None
    if body.get("output") or body.get("choices"):
        return False
    text = body.get("text")
    # In a Responses envelope this exact field is output-format configuration,
    # not generated text. Output/events/usage remain independently checked.
    if responses_envelope and text == {"format": {"type": "text"}}:
        text = None
    if text or any(body.get(k) for k in ("content", "reasoning_content", "tool_calls")):
        return False
    return not nonzero_usage(body.get("usage"))


def retryable_refusal(http_status, body, *, sse=False, observed_generation=False, stream_errors=()):
    if not no_generation(body, observed_generation=observed_generation, responses_envelope=sse):
        return False
    if http_status == 429:
        return True
    return (http_status == 200 and sse and isinstance(body, dict) and body.get("status") == "failed"
            and (explicit_rate_error(body.get("error")) or explicit_rate_error(list(stream_errors))))


class RateScheduler:
    def __init__(self, max_in_flight=2, min_interval=1.0, *, backoffs=BACKOFF_SECONDS):
        self.max_in_flight = max_in_flight
        self.min_interval = min_interval
        self.backoffs = tuple(backoffs)
        self.condition = threading.Condition()
        self.in_flight = 0
        self.last_dispatch = float("-inf")
        self.cool_until = 0.0
        self.rate_streak = 0

    def acquire(self, deadline, stop=None):
        started = time.monotonic()
        with self.condition:
            while True:
                now = time.monotonic()
                if stop is not None and stop.is_set():
                    raise TimeoutError("Transport stopped before dispatch")
                if deadline is not None and now >= deadline:
                    raise TimeoutError("Transport deadline exhausted while waiting for inference admission")
                not_before = max(self.cool_until, self.last_dispatch + self.min_interval)
                if self.in_flight < self.max_in_flight and now >= not_before:
                    self.in_flight += 1
                    self.last_dispatch = now
                    return {"wait_seconds": round(now - started, 6), "dispatched_utc": utc(),
                            "in_flight_at_dispatch": self.in_flight,
                            "global_min_dispatch_interval_seconds": self.min_interval,
                            "global_max_in_flight": self.max_in_flight}
                delay = .1 if deadline is None else min(.1, deadline - now)
                if not_before > now:
                    delay = min(delay, not_before - now)
                self.condition.wait(max(.001, delay))

    def release(self):
        with self.condition:
            self.in_flight -= 1
            self.condition.notify_all()

    def refused(self, attempt_number, retry_after=None):
        with self.condition:
            self.rate_streak += 1
            step = max(attempt_number, self.rate_streak) - 1
            base = self.backoffs[min(step, len(self.backoffs) - 1)]
            seconds = max(base, retry_after or 0)
            self.cool_until = max(self.cool_until, time.monotonic() + seconds)
            self.condition.notify_all()
            return {"backoff_seconds": base, "retry_after_seconds": retry_after,
                    "shared_cooldown_seconds": seconds, "shared_rate_streak": self.rate_streak}

    def succeeded(self):
        with self.condition:
            self.rate_streak = 0
            # Never shorten a cooldown already promised to another request.
            self.condition.notify_all()


GLOBAL = RateScheduler()


@contextmanager
def wire_deadline(response, deadline):
    """Stop indefinite HTTP/SSE drips; socket timeout alone is only an idle bound."""
    if deadline is None:
        yield
        return
    expired = threading.Event()
    def expire():
        expired.set()
        try:
            # urllib HTTPResponse exposes its connected socket through this path.
            response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass
        try: response.close()
        except Exception: pass
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        expire()
        raise TimeoutError("Wire wall-clock deadline exhausted")
    timer = threading.Timer(remaining, expire)
    timer.daemon = True
    timer.start()
    try:
        yield
        if expired.is_set() or time.monotonic() > deadline:
            raise TimeoutError("Wire wall-clock deadline exhausted")
    finally:
        timer.cancel()


@dataclass
class Outcome:
    value: object
    details: dict


class AttemptFailure(Exception):
    def __init__(self, message, *, details=None, retryable=False, retry_after=None, kind='rate'):
        super().__init__(message)
        self.details = details or {}
        self.retryable = bool(retryable)
        self.retry_after = retry_after
        self.kind = kind


class TransportFailure(Exception):
    def __init__(self, message, attempts):
        super().__init__(message)
        self.attempts = attempts


def run(operation, *, timeout, request_sha256, scheduler=None, stop=None, on_attempt=None,
        max_attempts=MAX_ATTEMPTS):
    """Return (operation value, records); identical-body attempts up to max_attempts.

    operation(wire_timeout, attempt_number) must return Outcome or raise
    AttemptFailure. Only explicitly classified AttemptFailure can be retried.
    A rate refusal enters the shared scheduler cooldown; a transport drop only
    sleeps a local backoff (it must not throttle other callers).
    A logical call's timeout includes global queue/cooldown waits and every wire.
    """
    scheduler = GLOBAL if scheduler is None else scheduler
    deadline = None if timeout is None else time.monotonic() + timeout
    records = []
    for number in itertools.count(1):
        queued_at = time.monotonic()
        record = {"attempt": number, "request_sha256": request_sha256,
                  "queued_utc": utc(), "dispatched": False, "phase": "waiting"}
        if on_attempt:
            on_attempt(dict(record))
        try:
            receipt = scheduler.acquire(deadline, stop)
        except Exception as exc:
            record.update(phase="finished", status="not_dispatched", error=str(exc), usage_known=True,
                          usage_unknown=False, finished_utc=utc(), wait_seconds=round(time.monotonic()-queued_at,6))
            records.append(record)
            if on_attempt:
                on_attempt(dict(record))
            raise TransportFailure(str(exc), records) from exc
        record.update(receipt, dispatched=True, phase="in_flight")
        if on_attempt:
            on_attempt(dict(record))
        begin = time.monotonic()
        value, failure = None, None
        try:
            if stop is not None and stop.is_set():
                raise AttemptFailure("Transport stopped before wire dispatch",
                                     details={"wire_dispatched":False,"usage_known":True})
            # Per-wire socket bound within the logical deadline: a dead connection is
            # detected within the hour; an unbounded (None) deadline keeps no bound.
            response = operation(None if deadline is None else min(3600, max(.01, deadline - begin)), number)
            value = response.value
            if deadline is not None and time.monotonic() > deadline:
                raise AttemptFailure("Transport completed after its total deadline; not forwarded",
                                     details={**response.details, "completed_after_deadline":True})
            record.update(response.details, status="succeeded", retryable=False)
            scheduler.succeeded()
        except AttemptFailure as exc:
            failure = exc
            transport = exc.retryable and exc.kind == "transport"
            record.update(exc.details, error=str(exc), retryable=exc.retryable,
                          status="transport_retry" if transport else "rate_refused" if exc.retryable else "failed")
            if transport:
                record["backoff_seconds"] = scheduler.backoffs[min(number - 1, len(scheduler.backoffs) - 1)]
            elif exc.retryable:
                record.update(scheduler.refused(number, exc.retry_after))
        except Exception as exc:
            failure = AttemptFailure(type(exc).__name__ + ": " + str(exc))
            record.update(status="failed", error=str(failure), retryable=False,
                          usage_known=False, usage_unknown=True)
        finally:
            scheduler.release()
        record.update(phase="finished", finished_utc=utc(), wire_seconds=round(time.monotonic() - begin, 6))
        record.setdefault("usage_known", False)
        record["usage_unknown"] = not record["usage_known"]
        records.append(record)
        if on_attempt:
            on_attempt(dict(record))
        if failure is None:
            return value, records
        if not failure.retryable or (max_attempts is not None and number >= max_attempts):
            raise TransportFailure(str(failure), records) from failure
        if record["status"] == "transport_retry":
            wait = record["backoff_seconds"]
            if deadline is not None:
                wait = min(wait, max(0, deadline - time.monotonic()))
            # Local wait only; a set stop surfaces as not_dispatched on the next acquire.
            if stop is not None: stop.wait(wait)
            else: time.sleep(wait)
    raise AssertionError("Unreachable attempt loop")
