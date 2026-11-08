"""Generic Linux/bubblewrap Python execution. Never falls back to host execution.

MWM owns discovery and admission policy; this module supplies the execution primitive: the framework's sandbox
(hermit.sandbox) with no network, no environment variables but the interpreter's, the adapter read-only at /w and the
tool's directory at /work, on the framework's interpreter (its environment's packages, as every sandbox shows them).
macOS sandbox-exec is intentionally not treated as equivalent.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys


class PythonSandbox:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def status(self):
        available = sys.platform.startswith("linux") and shutil.which("bwrap") is not None
        return {"available": available, "backend": "linux-bubblewrap" if available else None,
                "reason": "" if available else "Linux bubblewrap is unavailable; executable admission and execution are disabled."}

    def _run(self, directory: str | None, argv: list[str], timeout=20):
        from hermit.sandbox import Bind, Sandbox
        state = self.status()
        if not state["available"]:
            return {**state, "passed": False, "output": state["reason"]}
        binds, chdir = [], Path("/tmp")
        if directory is not None:
            work = (self.root / directory).resolve()
            if not work.is_relative_to(self.root) or not work.is_dir():
                raise ValueError("Sandbox work directory must be inside the bound adapter")
            binds, chdir = [Bind(self.root, "/w"), Bind(work, "/work")], work
        box = Sandbox(network="no-network", binds=binds, env_allow=[], framework=False, enforce="required",
                      role="python")
        try:
            env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
            command, env = box.wrap([sys.executable, "-B", *argv], env=env, keep=list(env), chdir=chdir)
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout)
            return {**state, "passed": result.returncode == 0, "exit_code": result.returncode,
                    "output": (result.stdout + result.stderr)[-4000:], "stdout": result.stdout[-4000:]}
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return {**state, "passed": False, "output": str(exc)}
        finally:
            box.close()

    def test(self, directory: str):
        return self._run(directory, ["test.py"])

    def invoke(self, directory: str, arguments: dict):
        script = "import json,sys; import run; print(json.dumps(run.run(**json.loads(sys.argv[1]))))"
        return self._run(directory, ["-c", script, json.dumps(arguments)])

    def run_code(self, code: str, arguments: dict):
        script = "import json,sys; ns={'args':json.loads(sys.argv[2])}; exec(sys.argv[1],ns); print(json.dumps(ns.get('result')))"
        return self._run(None, ["-c", script, code, json.dumps(arguments)], timeout=600)
