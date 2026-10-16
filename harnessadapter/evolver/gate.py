"""ToolGate: the learner's view of the Trainer's tool endpoint, with only an allowed list of tools passing.

The Trainer's endpoint serves every tool of the run (check_spec, run_agent, llm, budget, the task's tools). A learner
that may use only some of them gets this gate's URL instead of the endpoint's: GET /tools lists the allowed tools,
POST /call/<name> forwards an allowed call and refuses any other. The AgentEvolver opens one per step when
`params.tools` is set; the mwm Evolver opens one for the venue's channels.
"""
from __future__ import annotations

import http.server
import json
import threading
import urllib.request


class ToolGate:
    def __init__(self, upstream: str, allowed: list[str]):
        allowed = set(allowed)

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body: bytes):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.rstrip("/") != "/tools":
                    return self._send(404, b'{"error": "not found"}')
                try:
                    with urllib.request.urlopen(upstream + "/tools", timeout=30) as r:
                        tools = [t for t in json.loads(r.read()) if t["name"] in allowed]
                except Exception as exc:                        # noqa: BLE001
                    return self._send(502, json.dumps({"error": f"tool endpoint: {type(exc).__name__}: {exc}"}).encode())
                self._send(200, json.dumps(tools).encode())

            def do_POST(self):
                name = self.path[len("/call/"):] if self.path.startswith("/call/") else None
                if name not in allowed:
                    return self._send(404, json.dumps({"error": f"unknown tool {name}"}).encode())
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                request = urllib.request.Request(f"{upstream}/call/{name}", data=data,
                                                 headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(request, timeout=1800) as r:
                        self._send(200, r.read())
                except Exception as exc:                        # noqa: BLE001
                    self._send(502, json.dumps({"error": f"tool endpoint: {type(exc).__name__}: {exc}"}).encode())

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
