"""The robot execution engine as a host service: ASPIRE's `cap-harness` for the actors in the task sandbox.

The task sandbox has no GPU, so the simulator runs here, on the host, beside ASPIRE's perception and planning
services; a session reaches it through `bin/sim` (sim_cli.py) on a forwarded 127.0.0.1 port (ASPIRE_SIM_URL in
environment.env). The service is the trusted side of the session's rules:

    POST /register  {workspace, role, suite, task_id, dev_seeds, unseen_seeds, max_programs, max_steps}
                    once per workspace, by the task's `prepare`, before the agent starts
    POST /run       {workspace, cmd: run | run-batch | validate, program, seeds, output_root, no_videos, max_steps}
                    runs cap-harness for the registered task: only its development and unseen-check seeds, the
                    unseen check once, a verifier's two programs (by content; a single run on the first seed is
                    free), the program and the output inside the workspace
    GET  /health

Every refusal is a 200 with {ok: false, error}. The ledger (registrations, unseen checks, a verifier's programs) is
kept in --state, outside every sandbox. Started by launch.sh:

    python sim_service.py --port 8120 --aspire-root $ASPIRE_ROOT --workspaces <logs>/rollout --state <dir>
        [--sim-env '{"MUJOCO_GL": "egl", ...}'] [--workers 4]
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading


class Ledger:
    """The sessions' registrations and what they have spent, on disk (a restart keeps them)."""

    def __init__(self, path: Path):
        self.path, self.lock = path, threading.Lock()
        self.data = json.loads(path.read_text()) if path.exists() else {}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=1))


class Service:
    def __init__(self, root: Path, workspaces: list[Path], ledger: Ledger, env: dict, workers: int):
        self.root, self.workspaces, self.ledger = root, [w.resolve() for w in workspaces], ledger
        self.env, self.workers = env, workers
        self.cli = root / ".venv-robosuite" / "bin" / "cap-harness"
        self.python = root / ".venv-robosuite" / "bin" / "python"

    def _within(self, path: Path) -> bool:
        return any(path.is_relative_to(w) for w in self.workspaces)

    def register(self, body: dict) -> dict:
        workspace = Path(str(body.get("workspace", ""))).resolve()
        if not self._within(workspace) or not workspace.is_dir():
            return {"ok": False, "error": f"{workspace} is not a session workspace this service serves"}
        with self.ledger.lock:
            if str(workspace) in self.ledger.data:
                return {"ok": False, "error": "this workspace is registered already"}
            self.ledger.data[str(workspace)] = {
                "role": body.get("role"), "suite": str(body["suite"]), "task_id": int(body.get("task_id", 0)),
                "dev": [int(s) for s in body.get("dev_seeds") or []],
                "unseen": [int(s) for s in body.get("unseen_seeds") or []],
                "max_programs": body.get("max_programs"), "max_steps": body.get("max_steps"),
                "unseen_used": False, "programs": []}
            self.ledger.save()
        return {"ok": True}

    def run(self, body: dict) -> dict:
        workspace = Path(str(body.get("workspace", ""))).resolve()
        session = self.ledger.data.get(str(workspace))
        if session is None:
            return {"ok": False, "error": "unknown workspace: the session is not registered"}
        program = (workspace / str(body.get("program", ""))).resolve()
        if not program.is_relative_to(workspace) or not program.is_file():
            return {"ok": False, "error": f"no program at {body.get('program')} in the workspace"}
        env = {**os.environ, **{k: os.path.expandvars(os.path.expanduser(str(v))) for k, v in self.env.items()}}
        if body.get("cmd") == "validate":
            code = ("import ast,sys\nfrom cap_harness.runtime import _ProgramValidator\n"
                    "_ProgramValidator().visit(ast.parse(open(sys.argv[1]).read()))\nprint('program accepted')")
            done = subprocess.run([str(self.python), "-c", code, str(program)], env=env, cwd=self.root,
                                  capture_output=True, text=True, timeout=300)
            return {"ok": True, "code": done.returncode, "stdout": done.stdout, "stderr": done.stderr[-4000:]}
        out = (workspace / str(body.get("output_root", ""))).resolve()
        if not out.is_relative_to(workspace):
            return {"ok": False, "error": "--output-root must be inside the workspace"}
        seeds = [int(s) for s in (body.get("seeds") or [])]
        allowed = set(session["dev"]) | set(session["unseen"])
        if not seeds or set(seeds) - allowed:
            return {"ok": False, "error": f"seeds {sorted(set(seeds) - allowed)} are not assigned to you "
                                          f"(development {session['dev']}, unseen-check {session['unseen'] or 'none'})"}
        with self.ledger.lock:
            if set(seeds) & set(session["unseen"]):
                if session["unseen_used"]:
                    return {"ok": False, "error": "the unseen-check seeds are run once, and they have been run"}
                session["unseen_used"] = True
            free = body.get("cmd") == "run" and seeds == [min(session["dev"] or seeds)]
            if session.get("max_programs") and not free:
                digest = hashlib.sha256(program.read_bytes()).hexdigest()
                if digest not in session["programs"]:
                    if len(session["programs"]) >= session["max_programs"]:
                        return {"ok": False, "error": f"the attempt budget is {session['max_programs']} programs, "
                                                      f"and it is spent"}
                    session["programs"].append(digest)
            self.ledger.save()
        common = ["--benchmark", "robosuite", "--suite", session["suite"], "--task-id", str(session["task_id"]),
                  "--program", str(program), "--output-root", str(out), "--flat-run-dir"]
        if body.get("no_videos"):
            common.append("--no-videos")
        steps = body.get("max_steps") or session.get("max_steps")
        if steps:
            common += ["--max-steps", str(int(steps))]
        if body.get("cmd") == "run":
            cmd = [str(self.cli), "run", *common, "--seed", str(seeds[0])]
        else:
            out.mkdir(parents=True, exist_ok=True)
            cmd = [str(self.cli), "run-batch", *common, "--seeds", *map(str, seeds),
                   "--workers", str(min(self.workers, len(seeds))), "--results-jsonl", str(out / "results.jsonl"),
                   "--log-dir", str(out / "logs")]
        done = subprocess.run(cmd, env=env, cwd=self.root, capture_output=True, text=True)
        return {"ok": True, "code": done.returncode, "stdout": done.stdout[-20000:], "stderr": done.stderr[-8000:]}


def handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._reply({"ok": self.path == "/health"})

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                route = {"/register": service.register, "/run": service.run}.get(self.path)
                self._reply(route(body) if route else {"ok": False, "error": f"no route {self.path}"})
            except Exception as exc:  # a bad request is the session's error, not the service's end
                self._reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        def log_message(self, *args):
            pass
    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8120)
    ap.add_argument("--aspire-root", type=Path, required=True)
    ap.add_argument("--workspaces", type=Path, action="append", required=True,
                    help="a directory whose sessions this service serves (repeatable)")
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--sim-env", default="{}", help="the simulator's environment, a JSON object")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    a.state.mkdir(parents=True, exist_ok=True)
    service = Service(a.aspire_root.resolve(), a.workspaces, Ledger(a.state / "ledger.json"),
                      json.loads(a.sim_env), a.workers)
    ThreadingHTTPServer((a.host, a.port), handler(service)).serve_forever()


if __name__ == "__main__":
    main()
