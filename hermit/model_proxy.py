"""The run's model proxy: no sandbox holds a model key (docs/developer/sandbox-design.md, "Model proxy").

A sandboxed session is given a loopback base URL and a secret of its own in place of the provider's URL and key.
The proxy checks the secret, puts the real key (read from the host's environment when the request arrives) in its
place under the same header, and forwards the request to the provider, streaming the answer back. A request that
already carries the real key (the host's own calls through a routed binding) passes too.

    proxy = ModelProxy(log=run_dir / "sandbox" / "models.jsonl")
    binding, env = proxy.route(binding)      # the binding as the sandbox sees it, and {key variable: secret}

One line per request goes to the log: the route, the method, the path, the status, the bytes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import threading
from urllib.parse import urlsplit

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length"}


@dataclass
class Route:
    origin: str                  # scheme://host[:port] of the provider
    key_env: str | None          # the host variable holding the real key
    secret: str                  # what the sandbox sends instead


def rewrite(text: str, mapping: dict) -> str:
    """`text` with every provider origin in `mapping` replaced by its proxy root, where the origin ends (not
    http://127.0.0.1:801 inside http://127.0.0.1:8011)."""
    for origin, root in mapping.items():
        text = re.sub(re.escape(origin) + r"(?![\w.:-])", lambda _m, r=root: r, text)
    return text


def route_session(sandbox, bindings, command: list[str], env: dict) -> tuple[list[str], dict, list[str]]:
    """A session's command and environment with every provider of `bindings` on the sandbox's model proxy and a
    secret in place of each key (a variable that holds the real key gets the secret): (command, env, the key
    variables to pass whatever the sandbox's allowlist says). Unchanged without a proxy."""
    proxy, keep = getattr(sandbox, "model_proxy", None), []
    for binding in (bindings if proxy is not None else []):
        mapping, key_env, secret = proxy.origins(binding)
        if not mapping:
            continue
        command = [rewrite(str(a), mapping) for a in command]
        real = os.environ.get(key_env) if key_env else None
        env = {k: (secret if real and v == real else rewrite(str(v), mapping)) for k, v in env.items()}
        if key_env:
            env[key_env] = secret
            keep.append(key_env)
    return command, env, keep


class ModelProxy:
    def __init__(self, log: Path | None = None):
        self.routes: dict[str, Route] = {}
        self.log = Path(log) if log else None
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"            # the answer ends when the connection closes: streams pass through

            def log_message(self, *args):            # the proxy's own log, not stderr
                pass

            def _forward(self):
                rid, _, rest = self.path.lstrip("/").partition("/")
                route = outer.routes.get(rid)
                if route is None:
                    return self._refuse(404, "unknown route")
                headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
                auth = {k.lower(): k for k in headers if k.lower() in ("authorization", "x-api-key")}
                real = os.environ.get(route.key_env, "") if route.key_env else ""
                given = [headers[k] for k in auth.values()]
                tokens = [g.split(" ", 1)[1] if g.lower().startswith("bearer ") else g for g in given]
                if route.key_env and not any(t in (route.secret, real) and t for t in tokens):
                    return self._refuse(401, "the request does not carry this sandbox's secret")
                for lower, name in auth.items():
                    headers.pop(name)
                    if real:
                        headers[name] = f"Bearer {real}" if lower == "authorization" else real
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                target = urlsplit(route.origin)
                conn_cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
                kwargs = {"context": ssl.create_default_context()} if target.scheme == "https" else {}
                conn = conn_cls(target.hostname, target.port, timeout=600, **kwargs)
                sent = 0
                try:
                    conn.request(self.command, "/" + rest, body=body, headers=headers)
                    resp = conn.getresponse()
                    self.send_response(resp.status, resp.reason)
                    for k, v in resp.getheaders():
                        if k.lower() not in HOP:
                            self.send_header(k, v)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while True:
                        chunk = resp.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        sent += len(chunk)
                    status = resp.status
                except OSError as exc:
                    status = 502
                    try:
                        self._refuse(502, f"the provider could not be reached: {exc}")
                    except OSError:
                        pass
                finally:
                    conn.close()
                outer._write(rid, self.command, "/" + rest, status, sent)

            def _refuse(self, code: int, why: str):
                data = json.dumps({"error": {"message": f"hermit model proxy: {why}"}}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                outer._write(self.path.lstrip("/").partition("/")[0], self.command, self.path, code, 0)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _forward

        class Server(ThreadingHTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def origins(self, binding: dict) -> tuple[dict, str | None, str | None]:
        """For a binding (its plain form: base_url, messages_url, api_key_env): {provider origin: proxy root} for every
        provider URL, the key variable, and the secret a sandbox sends in its place. A server on the loopback that
        takes no key (a local vLLM) is reached directly; one whose key is set (a meter in front of a provider, which
        passes the caller's key on) is routed like any provider. The proxy root keeps the provider's path after it,
        so any URL of the provider becomes a URL of the proxy by its prefix."""
        key_env = binding.get("api_key_env")
        keyed = bool(key_env and os.environ.get(key_env))
        secret = "hermit-" + secrets.token_urlsafe(24)
        mapping = {}
        for field in ("base_url", "messages_url"):
            url = binding.get(field)
            if not url or (urlsplit(url).hostname in ("127.0.0.1", "localhost") and not keyed):
                continue
            parts = urlsplit(url)
            origin = f"{parts.scheme}://{parts.netloc}"
            if origin in mapping:
                continue
            with self._lock:
                rid = secrets.token_hex(6)
                self.routes[rid] = Route(origin=origin, key_env=key_env, secret=secret)
            mapping[origin] = f"http://127.0.0.1:{self.port}/{rid}"
        return mapping, key_env, (secret if mapping else None)

    def route(self, binding: dict) -> tuple[dict, dict]:
        """`binding` as a sandbox sees it (its provider URLs on the proxy), and the environment it needs:
        {api_key_env: secret}. A binding on the loopback without a key is left as it is."""
        mapping, key_env, secret = self.origins(binding)
        out = dict(binding)
        for field in ("base_url", "messages_url"):
            if out.get(field):
                out[field] = rewrite(out[field], mapping)
        return out, ({key_env: secret} if key_env and secret else {})

    def _write(self, rid: str, method: str, path: str, status: int, sent: int) -> None:
        if self.log is None:
            return
        line = json.dumps({"time": datetime.now(timezone.utc).isoformat(), "route": rid, "method": method,
                           "path": path, "status": status, "bytes": sent})
        with self._lock:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log, "a") as f:
                f.write(line + "\n")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
