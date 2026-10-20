"""The wire meter's UPSTREAM_KEY_ENV: the relay sends its own key upstream and the client's never passes."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest


class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[dict] = []

    def log_message(self, *a):
        pass

    def _answer(self):
        self.seen.append({k.lower(): v for k, v in self.headers.items()})
        body = json.dumps({"usage": {"prompt_tokens": 3, "completion_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._answer()

    def do_GET(self):
        self._answer()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def upstream():
    _Upstream.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", _Upstream.seen
    server.shutdown()


def _meter(upstream_url: str, extra: dict) -> tuple[subprocess.Popen, int]:
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("PROXY", "UPSTREAM_KEY_ENV")}
    env.update(PORT=str(port), UPSTREAM=upstream_url, LEDGER="", **extra)
    proc = subprocess.Popen([sys.executable, "-m", "hermit.runtime.wire_meter"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc, port


def _health(port: int) -> dict:
    deadline = time.time() + 15
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                return json.loads(r.read())
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(0.1)


def _call(port: int, method: str, path: str) -> int:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     data=b"{}" if method == "POST" else None,
                                     headers={"Authorization": "Bearer client-placeholder", "x-api-key": "client-x",
                                              "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as r:
        r.read()
        return r.status


def test_the_relay_key_replaces_the_clients(upstream):
    url, seen = upstream
    proc, port = _meter(url, {"UPSTREAM_KEY_ENV": "TEST_RELAY_KEY", "TEST_RELAY_KEY": "relay-only-value"})
    try:
        assert _health(port)["key_from"] == "TEST_RELAY_KEY"
        assert _call(port, "POST", "/lane_a/v1/chat/completions") == 200
        assert _call(port, "GET", "/lane_a/v1/models") == 200
    finally:
        proc.terminate()
        out = proc.communicate(timeout=10)[0].decode()
    assert len(seen) == 2
    for headers in seen:
        assert headers["authorization"] == "Bearer relay-only-value"
        assert "x-api-key" not in headers
    assert "relay-only-value" not in out and "key=$TEST_RELAY_KEY" in out      # the log names the variable only


def test_without_it_the_clients_key_passes(upstream):
    url, seen = upstream
    proc, port = _meter(url, {})
    try:
        assert _health(port)["key_from"] == "client"
        assert _call(port, "POST", "/lane_a/v1/chat/completions") == 200
    finally:
        proc.terminate()
        proc.communicate(timeout=10)
    assert seen[0]["authorization"] == "Bearer client-placeholder" and seen[0]["x-api-key"] == "client-x"


def test_an_unset_key_variable_refuses_to_start(upstream):
    url, _ = upstream
    proc, _ = _meter(url, {"UPSTREAM_KEY_ENV": "TEST_RELAY_KEY_NOT_SET"})
    out = proc.communicate(timeout=15)[0].decode()
    assert proc.returncode != 0 and "TEST_RELAY_KEY_NOT_SET" in out
