"""Boundary tests for the evolution broker's candidate-code paths.

1. The definition's protected paths are enforced on the REAL write path
   (the file operation helper running under the OS sandbox), not only on an
   in-process Workspace.
2. try_tool runs the candidate under the sandbox with no network, and the
   candidate's binding calls still reach the bound service — through the
   broker's pipe, not through a socket of the child's own.
"""
import http.server
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from harnessadapter.policy.execution import ExecutionPolicy, PolicySandbox

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = shutil.which('codex') and sys.platform in ('darwin', 'linux')

ADAPTER_JSON = {
    "contract": "harnessadapter.python.v1", "id": "probe", "name": "probe", "version": "1",
    "context": "CONTEXT.md",
    "resources": {"search": {"required": True, "access": "read"}},
    "tools": [{"name": "probe", "description": "calls the search binding", "entrypoint": "tools.py:probe",
               "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
               "output_schema": {"type": "object"}}],
}
TOOLS_PY = '''
import socket
def probe(q, _resources=None):
    hits = _resources["search"].search(q, k=2)
    try:
        socket.create_connection(("127.0.0.1", 9), timeout=1); own_socket = "allowed"
    except OSError as exc:
        own_socket = type(exc).__name__
    return {"hits": hits, "own_socket": own_socket, "describe": _resources["search"].describe()}
'''


class _SearchHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        body = json.dumps({"results": [{"docid": "d1", "score": 1.0, "q": q.get("q", [""])[0]}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

    def log_message(self, *args):
        pass


def _session(tmp: Path) -> Path:
    session = tmp / "session"
    for name in ("adapter", "evidence", "scratch", "control", "method", "environment", "history", "host"):
        (session / name).mkdir(parents=True)
    (session / "adapter/adapter.json").write_text(json.dumps(ADAPTER_JSON))
    (session / "adapter/CONTEXT.md").write_text("probe\n")
    (session / "adapter/tools.py").write_text(TOOLS_PY)
    (session / "adapter/core.py").write_text("PROTECTED = True\n")
    (session / "evidence/README.md").write_text("# e\n")
    return session


@unittest.skipUnless(SANDBOX, "requires Codex sandbox executor")
class ProtectedPathsOnTheRealWritePath(unittest.TestCase):
    def test_sandboxed_write_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _session(Path(tmp))
            (session / "control/protected.json").write_text(json.dumps(["core.py", "adapter.json"]))
            policy = ExecutionPolicy.parse({"backend": "codex", "read_access": {"paths": []}, "network": False})
            (session / "control/policy.json").write_text(json.dumps({"requested": policy.serialize()}))
            sandbox = PolicySandbox(session, policy)
            for path in ("core.py", "adapter.json", "CONTRACT.md"):
                with self.assertRaises(ValueError, msg=path):
                    sandbox.operation("write_file", {"area": "adapter", "path": path, "content": "x\n"})
            self.assertEqual((session / "adapter/core.py").read_text(), "PROTECTED = True\n")
            sandbox.operation("write_file", {"area": "adapter", "path": "notes.md", "content": "ok\n"})
            self.assertEqual((session / "adapter/notes.md").read_text(), "ok\n")


@unittest.skipUnless(SANDBOX, "requires Codex sandbox executor")
class TryToolThroughTheBrokerPipe(unittest.TestCase):
    def test_bindings_reach_service_without_child_network(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), _SearchHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                session = _session(Path(tmp))
                policy = ExecutionPolicy.parse({"backend": "codex", "read_access": {"paths": []}, "network": False})
                (session / "control/policy.json").write_text(json.dumps({"requested": policy.serialize()}))
                sandbox = PolicySandbox(session, policy)
                from harnessadapter.adapter.bindings import parse_bindings
                real = parse_bindings([f"search=http://127.0.0.1:{server.server_port}"])
                argv = [sys.executable, "-m", "harnessadapter.adapter.try_tool", "--adapter", str(session / "adapter"),
                        "--tool", "probe", "--arguments", json.dumps({"q": "hello"}),
                        "--binding", "search:" + real["search"].describe()]
                served = []

                def serve(request):
                    served.append(request["method"])
                    return getattr(real[request["binding"]], request["method"])(*request["args"], **request["kwargs"])

                child = sandbox.run_rpc(argv, serve, cwd=session / "adapter", timeout=120)
        finally:
            server.shutdown()
        self.assertFalse(child["timed_out"], child["stderr"])
        self.assertIn("result", child["done"] or {}, child)
        result = child["done"]["result"]
        self.assertEqual(served, ["search"])
        self.assertEqual(result["hits"], [{"docid": "d1", "score": 1.0, "q": "hello"}])
        self.assertNotEqual(result["own_socket"], "allowed")      # the child itself has no network
        self.assertTrue(result["describe"].startswith("ranked keyword search"))


if __name__ == "__main__":
    unittest.main()
