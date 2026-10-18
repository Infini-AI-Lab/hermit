#!/usr/bin/env python3
"""Wire-level token meter and request relay for the examples' launchers: a local HTTP proxy in front of the
model provider that records the tokens that crossed the wire per bucket (the first URL path segment) and,
for a Codex learner, rewrites what the provider needs rewritten (REWRITE_* below). Standard library only:

    PORT=3451 LEDGER=ledger.json UPSTREAM=https://api.fireworks.ai python -m harnessadapter.runtime.wire_meter

UPSTREAM is the backend's origin (scheme://host[:port]; http for a local vLLM or SGLang server); clients use
http://127.0.0.1:<PORT>/<bucket><path of the backend's base URL> (/inference/v1 for Fireworks, /v1 for a local
server, nothing for a backend served at its origin): `python -m harnessadapter.backends shell <ref>` prints both
as HA_ORIGIN and HA_PATH. The first path segment is the bucket unless it is an API root (v1, inference, ...).

Copied from metaworld's mwm/wire_meter.py (2026-10-14); the notes below are its own.

Wire-level token meter: one instrument for every arm's model bill.

Ported from flywheelbench scripts/wire_meter.py, which established the
principle this file exists for: a self-reported usage ledger is only as
complete as the surface that emits it (there, Prime's RPC ledger said 10.6M
serving input tokens where the wire saw 73.4M), so every arm's bill must be
read off the bytes that actually crossed the wire, by one shared relay.

Adaptations for this repo's sandbox:
  - stdlib only. The relay runs as a bare python:3-alpine sidecar exactly
    like the egress gateway; there is no pip inside the netlock, so httpx is
    out. Consequence: no cross-request connection pool — each forward opens
    its own upstream connection (concurrency here is 4-16 workers, not
    flywheelbench's 64, so the handshake tax is noise).
  - attribution rides the URL, not in-process state. Flywheelbench's driver
    mutates meter.phase between phases of ONE process; our callers are
    separate containers, so the bucket travels with the request: clients set
    api_base = http://<relay>:<PORT>/<bucket>/inference/v1 and the first
    path segment is popped as the ledger bucket.
  - the upstream is reached THROUGH the egress allow-list proxy when PROXY
    is set, so the proxy stays the single choke point out of the sandbox;
    the relay adds a metering hop, not an egress path.
  - the ledger is persisted to LEDGER as JSON every FLUSH_S seconds and on
    SIGTERM; GET /ledger serves it live, GET /health the error counters.

Dialects: OpenAI chat completions (prompt_tokens includes cached) and
Anthropic messages (input_tokens excludes cache components — added back),
same normalisation as the original.

Env: PORT (default 3129), UPSTREAM (default https://api.fireworks.ai),
PROXY (e.g. http://mw-egress-meta:3128), LEDGER (json path), FLUSH_S.

REWRITE_DEVELOPER_ROLE=1 additionally rewrites message role "developer" to
"system" in request bodies (both `input` items of the Responses API and
`messages` of chat completions). codex >= 0.147 speaks ONLY the Responses
API and labels auxiliary context "developer"; Fireworks' kimi-k3 deployment
rejects that role outright (400 Unknown message role, observed 2026-09-01 —
the same request with "system" is accepted, and glm-5p3 accepts both). The
rewrite is a transport-level compatibility shim, off by default; a body that
does not parse as JSON passes through untouched.
"""

from __future__ import annotations

import http.client
import json
import os
import random
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

PORT = int(os.environ.get("PORT", "3129"))
UPSTREAM = os.environ.get("UPSTREAM", "https://api.fireworks.ai")
PROXY = os.environ.get("PROXY", "")
LEDGER_PATH = os.environ.get("LEDGER", "")
FLUSH_S = float(os.environ.get("FLUSH_S", "15"))
REWRITE_DEV = os.environ.get("REWRITE_DEVELOPER_ROLE", "") not in ("", "0")
# PRIME_EFFORT (e.g. "high"): on the `prime` bucket, replace Prime's own
# thinking-budget field with reasoning_effort=<value> on every forwarded
# request, so the Prime arm reasons at the same effort as the ICL /
# stateless / mwm hosts (their litellm path sends reasoning_effort). Same
# relay-side alignment as flywheelbench prime_serve._align_effort (their
# user decision 2026-09-01; ours 2026-09-05). Declared deviation from stock
# Prime, whose native dial is the thinkingLevelMap budget (16,384 at max);
# unset = stock behaviour.
PRIME_EFFORT = os.environ.get("PRIME_EFFORT", "")
STRIP_SERVER_TOOLS = os.environ.get("REWRITE_STRIP_SERVER_TOOLS", "") not in ("", "0")
# REWRITE_REPAIR_TOOL_ARGS=1 (2026-09-21): a tool call whose `arguments` is
# not valid JSON gets "{}" and its tool result a note saying so. Why: the
# model occasionally emits a malformed argument string (long shell/heredoc
# payloads); codex 0.155 stores it verbatim and re-sends the history, and
# the upstream rejects every such request with 400 ("arguments must be a
# JSON object string"), reconnects 5 times, and the session dies (2 of 242
# framework sessions on 2026-09-21; the native arm's codex 0.147 chat path
# never showed it). Only the broken field changes; the model sees the
# error in the tool result and retries the call itself.
REPAIR_TOOL_ARGS = os.environ.get("REWRITE_REPAIR_TOOL_ARGS", "") not in ("", "0")
# DUMP_REJECTED=<dir>: write each request the upstream answered with 400 to
# <dir>/<utc>-<bucket>.json (request body + response) for diagnosis.
DUMP_REJECTED = os.environ.get("DUMP_REJECTED", "")
_REPAIR_NOTE = ("[relay] the arguments of this tool call were not valid JSON and "
                "were replaced by {} so the conversation could continue; the call "
                "did not run — repeat it with well-formed JSON arguments.")

_U = urlsplit(UPSTREAM)
ORIGIN_HOST = _U.hostname
ORIGIN_PORT = _U.port or (443 if _U.scheme == "https" else 80)

# Hop-by-hop and hop-describing headers, straight from the original: a
# whitelist silently drops what it has not heard of (retry-after on a 429,
# the provider request id), so drop-lists it is. Accept-Encoding is dropped
# because THIS relay does not decode: the upstream is asked for identity so
# the buffered bytes are parseable JSON/SSE.
_DROP_REQ = {"host", "content-length", "connection", "keep-alive",
             "transfer-encoding", "te", "trailer", "upgrade",
             "proxy-authorization", "accept-encoding"}
_DROP_RESP = {"content-length", "connection", "keep-alive",
              "transfer-encoding", "te", "trailer", "upgrade",
              "content-encoding", "date", "server"}

# Enough for one full SSE response; a longer one still forwards, only its
# usage tail would be missed.
_MAX_BUFFER = 32_000_000

_lock = threading.Lock()
_ledger: dict = {}
_health = {"errors": 0, "no_usage": 0, "non_2xx": 0, "client_aborts": 0,
           "last_status": 0, "last_error": "",
           # provider errors absorbed by the retry loop in do_POST;
           # "gaveup" = retries exhausted and the error relayed as-is
           "retried": {"429": 0, "5xx": 0, "nan400": 0, "gaveup": 0}}

# Provider-error retry (2026-09-05, parity with flywheelbench's Prime proxy
# and with run_task._create_with_retry): a 429, a 5xx, or Fireworks'
# transient "Floating point NaN" 400 is retried HERE, before the client sees
# it, with exponential backoff (Retry-After honoured, capped). Prime's own
# client gives up on a 429 within seconds and the runner then grades the
# ticket as an infra failure; every other client behind this relay already
# backs off on exactly these statuses, so this removes an asymmetry rather
# than granting one. A retry re-sends the SAME request body once the failed
# response has been fully read and its connection closed; a response that
# is finally relayed is always the stream that was opened last. Nothing is
# billed for the absorbed attempts (an error response carries no usage).
PROXY_RETRIES = int(os.environ.get("PROXY_RETRIES", "8"))
PROXY_RETRY_CAP_S = float(os.environ.get("PROXY_RETRY_CAP_S", "60"))
_NAN_400 = b"Floating point NaN"


def norm_usage(u: dict) -> dict:
    """Any wire dialect -> {input, cacheRead, output}; input is TOTAL
    prompt tokens including cached, so the dialects mean the same thing."""
    if "input_tokens_details" in u:                          # responses API
        # input_tokens already INCLUDES the cached portion (unlike
        # Anthropic's, which excludes it) — report it as-is.
        det = u.get("input_tokens_details") or {}
        return {"input": u.get("input_tokens", 0) or 0,
                "cacheRead": det.get("cached_tokens", 0) or 0,
                "output": u.get("output_tokens", 0) or 0}
    if "input_tokens" in u or "output_tokens" in u:          # anthropic
        cr = u.get("cache_read_input_tokens", 0) or 0
        cw = u.get("cache_creation_input_tokens", 0) or 0
        return {"input": (u.get("input_tokens", 0) or 0) + cr + cw,
                "cacheRead": cr,
                "output": u.get("output_tokens", 0) or 0}
    det = u.get("prompt_tokens_details") or {}               # openai
    return {"input": u.get("prompt_tokens", 0) or 0,
            "cacheRead": det.get("cached_tokens", 0) or 0,
            "output": u.get("completion_tokens", 0) or 0}


def collect_usage(buf: bytes, sse: bool):
    """Non-stream: the body's usage. SSE: merge fragments last-value-wins
    (Anthropic splits usage across message_start/message_delta, OpenAI puts
    a complete object in the final chunk)."""
    if not sse:
        try:
            o = json.loads(buf or b"{}")
        except json.JSONDecodeError:
            return None
        u = o.get("usage") if isinstance(o, dict) else None
        return norm_usage(u) if u else None
    merged: dict = {}
    for line in buf.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        try:
            o = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(o, dict):
            continue
        for u in (o.get("usage"),
                  (o.get("message") or {}).get("usage")
                  if isinstance(o.get("message"), dict) else None,
                  # Responses API: the terminal response.completed event
                  # nests usage under "response".
                  (o.get("response") or {}).get("usage")
                  if isinstance(o.get("response"), dict) else None):
            if isinstance(u, dict):
                for k, v in u.items():
                    if v is not None:
                        merged[k] = v
    return norm_usage(merged) if merged else None


# Tool types the model provider executes ITSELF (server-side). Fireworks'
# kimi-k3 responses endpoint rejects a request that mixes these with the
# client-executed function tools codex always sends (400 "Mixed
# client-executed tools ... with server-executed MCP/SSE/Python/WebSearch
# tools ... is not supported", 2026-09-02). The meta learner is netlocked
# and never needs a server-side web search, so dropping them is safe and
# leaves codex's own shell/apply_patch function tools intact.
_SERVER_TOOL_TYPES = {
    "web_search", "web_search_preview", "web_search_2025_08_26",
    "code_interpreter", "computer", "computer_use_preview",
    "file_search", "image_generation", "mcp",
}


def align_effort(body: bytes) -> bytes:
    """Swap `thinking: {...}` for `reasoning_effort: PRIME_EFFORT`; also
    covers calls where the glm shim omitted the thinking field (those ran
    the provider default). Unparseable bodies pass through unchanged."""
    try:
        d = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(d, dict) or "model" not in d:
        return body
    d.pop("thinking", None)
    d["reasoning_effort"] = PRIME_EFFORT
    _health["effort_aligned"] = _health.get("effort_aligned", 0) + 1
    return json.dumps(d).encode()


def _bad_args(value) -> bool:
    """True when a tool call's `arguments` string does not parse as a JSON object."""
    if value is None:
        return False
    if isinstance(value, dict):
        return False
    if not isinstance(value, str):
        return True
    try:
        return not isinstance(json.loads(value), dict)
    except (json.JSONDecodeError, TypeError, ValueError):
        return True


def repair_tool_args(d: dict) -> int:
    """Replace unparseable tool-call arguments in the history with "{}" and
    make the matching tool result say so (inserting one when absent).
    Responses API: `input` items function_call / function_call_output by
    call_id. Chat API: assistant `tool_calls[]` and role "tool" messages by
    tool_call_id. Returns the number of calls repaired."""
    fixed = 0
    items = d.get("input")
    if isinstance(items, list):
        bad_ids = []
        for it in items:
            if isinstance(it, dict) and it.get("type") == "function_call" and _bad_args(it.get("arguments")):
                it["arguments"] = "{}"
                bad_ids.append(it.get("call_id"))
                fixed += 1
        for cid in bad_ids:
            out = next((it for it in items if isinstance(it, dict) and it.get("type") == "function_call_output"
                        and it.get("call_id") == cid), None)
            if out is None:
                idx = next(i for i, it in enumerate(items) if isinstance(it, dict) and it.get("type") == "function_call" and it.get("call_id") == cid)
                items.insert(idx + 1, {"type": "function_call_output", "call_id": cid, "output": _REPAIR_NOTE})
            elif isinstance(out.get("output"), str):
                out["output"] = _REPAIR_NOTE + "\n" + out["output"]
            else:
                out["output"] = _REPAIR_NOTE
    msgs = d.get("messages")
    if isinstance(msgs, list):
        bad_ids = []
        for m in msgs:
            if isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list):
                for tc in m["tool_calls"]:
                    fn = tc.get("function") if isinstance(tc, dict) else None
                    if isinstance(fn, dict) and _bad_args(fn.get("arguments")):
                        fn["arguments"] = "{}"
                        bad_ids.append(tc.get("id"))
                        fixed += 1
        for cid in bad_ids:
            out = next((m for m in msgs if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id") == cid), None)
            if out is None:
                idx = next(i for i, m in enumerate(msgs) if isinstance(m, dict) and m.get("role") == "assistant"
                           and any(isinstance(tc, dict) and tc.get("id") == cid for tc in m.get("tool_calls") or []))
                msgs.insert(idx + 1, {"role": "tool", "tool_call_id": cid, "content": _REPAIR_NOTE})
            elif isinstance(out.get("content"), str):
                out["content"] = _REPAIR_NOTE + "\n" + out["content"]
            else:
                out["content"] = _REPAIR_NOTE
    return fixed


def rewrite_request(body: bytes) -> bytes:
    """Two transport shims, both env-gated by the caller, applied together
    so the body is parsed once: role "developer" -> "system", and (when
    REWRITE_STRIP_SERVER_TOOLS) drop server-executed tools from `tools`.
    Anything unparseable passes through unchanged — a shim must never turn a
    working request into a broken one."""
    try:
        d = json.loads(body)
        if not isinstance(d, dict):
            return body
        changed = False
        if REWRITE_DEV:
            for key in ("input", "messages"):
                items = d.get(key)
                if not isinstance(items, list):
                    continue
                for m in items:
                    if isinstance(m, dict) and m.get("role") == "developer":
                        m["role"] = "system"
                        changed = True
        if REPAIR_TOOL_ARGS:
            n = repair_tool_args(d)
            if n:
                changed = True
                with _lock:
                    _health["tool_args_repaired"] = _health.get("tool_args_repaired", 0) + n
                print(f"wire meter: repaired {n} malformed tool-call argument(s)", flush=True)
        if STRIP_SERVER_TOOLS and isinstance(d.get("tools"), list):
            kept, dropped = [], []
            for t in d["tools"]:
                typ = t.get("type") if isinstance(t, dict) else None
                if typ in _SERVER_TOOL_TYPES:
                    dropped.append(typ)
                else:
                    kept.append(t)
            if dropped:
                d["tools"] = kept
                changed = True
                print(f"wire meter: stripped server tools {dropped}",
                      flush=True)
        return json.dumps(d).encode() if changed else body
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body


# The first segments an API path may start with (OpenAI-style roots, Fireworks' /inference, DeepSeek's
# /anthropic, and the endpoints themselves at a base URL with no path at all): a request whose path starts
# with one of these carries no bucket.
API_ROOTS = {"v1", "inference", "anthropic", "openai", "api", "chat", "completions", "responses", "messages",
             "models", "embeddings"}


def split_bucket(request_path: str) -> tuple[str, str]:
    """(bucket, upstream path): the first segment is the bucket unless the path is an API path itself."""
    parts = request_path.lstrip("/").split("/", 1)
    if len(parts) == 2 and parts[0] not in API_ROOTS:
        return parts[0], "/" + parts[1]
    return "unattributed", request_path


def _upstream_conn():
    if PROXY:
        p = urlsplit(PROXY)
        c = http.client.HTTPSConnection(p.hostname, p.port or 3128,
                                        timeout=650)
        c.set_tunnel(ORIGIN_HOST, ORIGIN_PORT)
        return c
    cls = (http.client.HTTPSConnection if _U.scheme == "https"
           else http.client.HTTPConnection)
    return cls(ORIGIN_HOST, ORIGIN_PORT, timeout=650)


def _flush(path: str = LEDGER_PATH):
    if not path:
        return
    with _lock:
        doc = {"ledger": {k: dict(v) for k, v in _ledger.items()},
               "health": dict(_health), "ts": time.time()}
    tmp = f"{path}.tmp.{threading.get_ident()}"
    try:
        with open(tmp, "w") as f:
            json.dump(doc, f, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle_one_request(self):
        # Quiet the normal end of a keep-alive connection (see original:
        # the SDK closes its socket as soon as it has its bytes).
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def do_GET(self):
        if self.path == "/health":          # the upstream too: a launcher refuses a port whose meter faces elsewhere
            with _lock:
                body = json.dumps({**_health, "upstream": UPSTREAM}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/ledger":
            with _lock:
                body = json.dumps({"ledger": _ledger,
                                   "health": _health}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # Any other GET (/<bucket>/v1/models, the preflight of `harnessadapter check`) is relayed as is, unmetered.
        _, path = split_bucket(self.path)
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in _DROP_REQ}
        hdrs["Accept-Encoding"] = "identity"
        try:
            conn = _upstream_conn()
            conn.request("GET", path, headers=hdrs)
            r = conn.getresponse()
            data = r.read()
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in _DROP_RESP:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            conn.close()
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _health["errors"] += 1
                _health["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
            self.send_error(502)

    def do_POST(self):
        # /<bucket>/<upstream path>. No bucket segment -> "unattributed",
        # counted rather than refused: a mis-configured client's calls must
        # still reach the model AND still land on the bill.
        bucket, path = split_bucket(self.path)

        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        if (REWRITE_DEV or STRIP_SERVER_TOOLS or REPAIR_TOOL_ARGS) and body:
            body = rewrite_request(body)
        # "prime" and the per-run buckets prime_<hash> (2026-09-08; hashed 2026-09-13)
        if PRIME_EFFORT and bucket.startswith("prime") and body:
            body = align_effort(body)
        hdrs = {k: v for k, v in self.headers.items()
                if k.lower() not in _DROP_REQ}
        hdrs["Content-Length"] = str(len(body))
        hdrs["Accept-Encoding"] = "identity"

        buf = bytearray()
        status = 0
        ct = "application/json"
        conn = None
        headers_sent = False
        relayed = 0
        expected = None
        prefetched = None      # a 400 body already read to inspect it
        try:
            attempt = 0
            while True:
                conn = _upstream_conn()
                conn.request("POST", path, body=body, headers=hdrs)
                r = conn.getresponse()
                status = r.status
                kind = None
                prefetched = None
                if status == 429:
                    kind = "429"
                elif status >= 500:
                    kind = "5xx"
                elif status == 400:
                    prefetched = r.read()
                    if _NAN_400 in prefetched:
                        kind = "nan400"
                if kind is None or attempt >= PROXY_RETRIES:
                    if kind is not None:
                        with _lock:
                            _health["retried"]["gaveup"] += 1
                    break                       # relay this response
                ra = r.getheader("Retry-After")
                try:
                    r.read()                    # drain so the socket closes clean
                except Exception:
                    pass
                conn.close()
                conn = None
                with _lock:
                    _health["retried"][kind] += 1
                    _health["last_status"] = status
                try:
                    wait = float(ra) if ra else 0.0
                except ValueError:
                    wait = 0.0
                wait = min(PROXY_RETRY_CAP_S,
                           max(wait, (2 ** attempt) + random.random()))
                time.sleep(wait)
                attempt += 1
            ct = r.getheader("Content-Type", "application/json")
            cl = r.getheader("Content-Length")
            expected = int(cl) if cl and cl.isdigit() else None
            self.send_response(status)
            for k, v in r.getheaders():
                if k.lower() not in _DROP_RESP:
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            headers_sent = True
            while True:
                if prefetched is not None:
                    chunk, prefetched = prefetched, b""
                    if not chunk:
                        break
                else:
                    chunk = r.read1(65536)
                if not chunk:
                    break
                try:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()     # SSE must not sit in a buffer: the
                    # caller's stall watchdog measures chunk arrival
                except (BrokenPipeError, ConnectionResetError) as exc:
                    # THE CLIENT hung up mid-response (its own watchdog, so it
                    # can retry on a fresh connection). Not a relay fault.
                    with _lock:
                        _health["client_aborts"] += 1
                        _health["last_error"] = (
                            f"{type(exc).__name__}: {exc}"[:200])
                    return
                relayed += len(chunk)
                if len(buf) < _MAX_BUFFER:
                    buf += chunk
            if expected is not None and relayed < expected:
                # Upstream closed early. read1() returns b"" at EOF without
                # raising, so without this check a truncated body would be
                # sealed with a terminal chunk and the client would take a
                # cut-off answer as a complete 200 (audit 2026-08-31, F2).
                raise ConnectionError(
                    f"upstream truncated: {relayed}/{expected} bytes")
            self.wfile.write(b"0\r\n\r\n")
        except Exception as exc:
            with _lock:
                _health["errors"] += 1
                _health["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
            if not headers_sent:
                try:
                    self.send_error(502)   # 5xx so the caller's retry treats
                except Exception:          # it as the transient failure it is
                    pass
            # Headers already sent: abort WITHOUT the terminal chunk — the
            # client sees an incomplete chunked stream and errors, instead of
            # receiving 502 bytes injected mid-body.
            self.close_connection = True
            return
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        if not 200 <= status < 300:
            # An error response carries no usage BECAUSE NOTHING WAS BILLED.
            with _lock:
                _health["non_2xx"] += 1
                _health["last_status"] = status
            if DUMP_REJECTED and status == 400:
                try:
                    os.makedirs(DUMP_REJECTED, exist_ok=True)
                    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
                    fn = os.path.join(DUMP_REJECTED, f"{stamp}-{bucket}-{threading.get_ident()}.json")
                    with open(fn, "w") as f:
                        json.dump({"path": path, "status": status,
                                   "request": body.decode("utf-8", errors="replace"),
                                   "response": bytes(buf).decode("utf-8", errors="replace")[:4000]}, f)
                except Exception as exc:  # noqa: BLE001 — diagnostics never break the relay
                    print(f"wire meter: dump failed: {exc}", flush=True)
            return
        try:
            u = collect_usage(bytes(buf), "text/event-stream" in ct)
        except Exception as exc:
            # gzip-despite-identity, non-UTF-8, usage of a wrong type — the
            # response was already delivered; the bill must degrade to a
            # counted no_usage, never to a dead handler thread.
            with _lock:
                _health["no_usage"] += 1
                _health["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
            u = None
        if u:
            with _lock:
                b = _ledger.setdefault(
                    bucket, {"input": 0, "cacheRead": 0, "output": 0,
                             "calls": 0})
                for k in ("input", "cacheRead", "output"):
                    b[k] += u[k]
                b["calls"] += 1
        else:
            with _lock:
                _health["no_usage"] += 1


class Server(ThreadingHTTPServer):
    # request_queue_size is read off the SERVER, not the handler (original's
    # 64-worker load test); 512 is far above any concurrency here.
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 512


def main():
    srv = Server(("0.0.0.0", PORT), Handler)

    def _bye(sig, frame):
        _flush()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    def _flusher():
        while True:
            time.sleep(FLUSH_S)
            _flush()

    threading.Thread(target=_flusher, daemon=True).start()
    print(f"wire meter on :{PORT} upstream={UPSTREAM} "
          f"proxy={PROXY or 'direct'} ledger={LEDGER_PATH or 'off'}",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
