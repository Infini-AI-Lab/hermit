"""A model-only Responses relay; acting-agent source and request semantics stay fixed.

The private relay provides the existing OfficeQA actor's normal model backend.
It exposes only model operations and native token refresh, never a web proxy. Backend
credentials stay on the host in API-key mode. Native ChatGPT sessions use main's
login handling and a fixed model/auth relay. Logs omit credentials, prompts and answers.
"""
from __future__ import annotations

import hmac
import json
from pathlib import Path

from hermit.agent.model.backends import Binding
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer



def local_tools_only(tools):
    for tool in tools:
        if not isinstance(tool, dict):
            return False
        kind = tool.get('type')
        if kind == 'namespace':
            if not isinstance(tool.get('tools'), list) or not local_tools_only(tool['tools']):
                return False
        elif kind not in ('function', 'custom'):
            return False
    return True


class ModelGateway:
    def __init__(self, *, binding: Binding | None = None, client_key: str = "", log: Path,
                 login: str | None = None, model: str | None = None, host="127.0.0.1", port=0):
        if (binding is None) != (login == 'chatgpt'):
            raise ValueError('choose a model binding or the native ChatGPT login')
        self.binding, self.client_key, self.log = binding, client_key, log
        self.native = login == 'chatgpt'
        self.model = binding.model if binding else model
        if not self.model:
            raise ValueError('the model must be fixed')
        self.lock = threading.Lock()
        self.log.parent.mkdir(parents=True, exist_ok=True)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def handle(self):
                try:
                    super().handle()
                except OSError:
                    owner.audit('disconnect', getattr(self, 'command', 'HTTP'),
                                getattr(self, 'path', ''), 0)

            def response(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def authorized(self):
                value = self.headers.get('Authorization', '')
                if owner.native:
                    return value.startswith('Bearer ') and bool(value[7:].strip())
                return hmac.compare_digest(value, 'Bearer ' + owner.client_key)

            def do_CONNECT(self):
                owner.audit("deny", self.command, self.path, 403)
                self.response(403, {"error": "This is a model endpoint, not a web proxy"})

            def do_GET(self):
                if owner.native:
                    from urllib.parse import urlsplit
                    if self.authorized() and urlsplit(self.path).path == '/v1/models':
                        self.forward('https://chatgpt.com/backend-api/codex' + self.path[3:])
                    else:
                        self.deny()
                    return
                if not self.authorized() or self.path.rstrip("/") != "/v1/models":
                    owner.audit("deny", self.command, self.path, 403)
                    self.response(403, {"error": "Only authenticated model operations are allowed"})
                    return
                self.response(200, {"object": "list", "data": [{"id": owner.model, "object": "model"}]})

            def do_POST(self):
                import requests
                from urllib.parse import urlsplit
                route = urlsplit(self.path).path
                refreshing = owner.native and self.path == '/oauth/token'
                allowed = ({'/v1/responses', '/v1/responses/compact'}
                           if owner.native else {'/v1/responses'})
                if not refreshing and (not self.authorized() or route not in allowed):
                    owner.audit("deny", self.command, self.path, 403)
                    self.response(403, {"error": "Only allowlisted model operations are allowed"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.response(400, {"error": "invalid request size"})
                    return
                if not 0 < size <= 32 * 1024 * 1024:
                    self.response(400, {"error": "invalid request size"})
                    return
                raw = self.rfile.read(size)
                try:
                    body = json.loads(raw)
                except ValueError:
                    self.response(400, {"error": "invalid JSON"})
                    return
                if refreshing:
                    if not isinstance(body, dict) or body.get('grant_type') != 'refresh_token' or not body.get('refresh_token'):
                        self.response(400, {'error': 'only native token refresh is supported'})
                        return
                    self.forward('https://auth.openai.com/oauth/token', raw)
                    return
                if not isinstance(body, dict) or not isinstance(body.get("tools", []), list):
                    self.response(400, {"error": "invalid model request"})
                    return
                if body.get("model") != owner.model:
                    self.response(403, {"error": "model is not allowlisted"})
                    return
                if not local_tools_only(body.get('tools', [])):
                    self.response(403, {"error": "only client-executed tools are allowed"})
                    return
                if owner.native:
                    self.forward('https://chatgpt.com' + (route.replace('/v1/', '/backend-api/codex/', 1)), raw)
                    return
                key = owner.binding.key()
                if owner.binding.backend.key_required and not key:
                    self.response(503, {"error": "Configured model backend key is unavailable"})
                    return
                headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
                if key:
                    headers['Authorization'] = 'Bearer ' + key
                for name in ("User-Agent", "session_id", "version", "client-request-id"):
                    if self.headers.get(name):
                        headers[name] = self.headers[name]
                started = time.monotonic()
                try:
                    endpoint = owner.binding.base_url.rstrip('/') + '/responses'
                    with requests.post(endpoint, data=json.dumps(body).encode(), headers=headers,
                                       stream=True, allow_redirects=False, timeout=(30, 900)) as upstream:
                        self.send_response(upstream.status_code)
                        self.send_header("Content-Type", upstream.headers.get("Content-Type", "text/event-stream"))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        for chunk in upstream.iter_content(chunk_size=None):
                            if chunk:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                        owner.audit("allow", self.command, self.path, upstream.status_code,
                                    seconds=round(time.monotonic() - started, 3))
                except (requests.RequestException, BrokenPipeError, ConnectionResetError):
                    owner.audit("error", self.command, self.path, 502)

            def deny(self):
                owner.audit('deny', self.command, self.path, 403)
                self.response(403, {'error': 'only model requests and native token refresh are allowed'})

            def forward(self, endpoint, raw=None):
                import requests
                headers = {name: value for name, value in self.headers.items()
                           if name.lower() not in ('host', 'connection', 'content-length', 'accept-encoding')}
                started = time.monotonic()
                try:
                    request = requests.get if raw is None else requests.post
                    with request(endpoint, **({'data': raw} if raw is not None else {}), headers=headers,
                                 stream=True, allow_redirects=False, timeout=(30, 900)) as upstream:
                        self.send_response(upstream.status_code)
                        for name, value in upstream.headers.items():
                            if name.lower() == 'content-type' or name.lower().startswith(('x-codex-', 'x-request-')):
                                self.send_header(name, value)
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        for chunk in upstream.iter_content(chunk_size=None):
                            if chunk:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                        owner.audit('allow', self.command, self.path, upstream.status_code,
                                    seconds=round(time.monotonic() - started, 3))
                except (requests.RequestException, BrokenPipeError, ConnectionResetError):
                    owner.audit('error', self.command, self.path, 502)

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self):
        return self.server.server_address[1]

    def audit(self, action, method, path, code, **extra):
        with self.lock, self.log.open("a") as stream:
            stream.write(json.dumps({"time": time.time(), "action": action, "method": method,
                                     "path": path, "status": code, **extra}) + "\n")

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
