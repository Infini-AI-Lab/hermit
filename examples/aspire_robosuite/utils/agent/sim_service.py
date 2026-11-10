"""The robot execution engine as a host service: ASPIRE's `cap-harness` for the actors in the task sandbox.

The task sandbox has no GPU, so the simulator runs here, on the host, beside ASPIRE's perception and planning
services; a session reaches it through `bin/sim` (sim_cli.py) on a forwarded 127.0.0.1 port (ASPIRE_SIM_URL in
environment.env). The service is the trusted side of the session's rules, and it trusts nothing the session sends
but the workspace and its task's id: the seeds, the role and the budget are the task row's (--tasks, read here on
the host), and a rollout's directory (<workspaces>/<role>/<task id>-<hex>, made by the framework outside the
sandbox, which can write only inside it) registers one session, of the task its name carries:

    POST /register  {workspace, task, max_steps}
                    once per rollout, by the task's `prepare`, before the agent starts (a retry's `prepare`, in the
                    same workspace, finds its registration and what it spent as they were)
    POST /run       {workspace, cmd: run | run-batch | validate, program, seeds, output_root, no_videos}
                    runs cap-harness for the registered task: only its development and unseen-check seeds, the
                    unseen check once, a verifier's two programs (by content; a single run on the first seed is
                    free), the program and the output inside the workspace, the registration's max_steps
    GET  /health

The session's files are never opened by path here, since the session may have put a link in it: the program is read
without following links (every directory below the rollout's, which the sandbox cannot replace, opened with
O_NOFOLLOW) and run as this service's own copy, the one its digest is taken of; cap-harness writes into this service's
own directory, and the run is moved into the workspace the same way, each file replacing whatever entry has its name.
A run is stopped after --run-timeout seconds, and the runs still going are stopped when the service is.

Every refusal is a 200 with {ok: false, error}. The ledger (registrations, unseen checks, a verifier's programs) is
kept in --state, outside every sandbox. Started by launch.sh:

    python sim_service.py --port 8120 --aspire-root $ASPIRE_ROOT --tasks utils/tasks.jsonl --workspaces <logs>
        --state <dir> [--sim-env '{"MUJOCO_GL": "egl", ...}'] [--workers 4]
"""
from __future__ import annotations

import argparse
import errno
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sim  # noqa: E402
from sim import seeds_of  # noqa: E402


class Ledger:
    """The sessions' registrations and what they have spent, on disk (a restart keeps them)."""

    def __init__(self, path: Path):
        self.path, self.lock = path, threading.Lock()
        self.data = json.loads(path.read_text()) if path.exists() else {}

    def save(self) -> None:                                 # whole or not at all: a full disk leaves the last one
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)


# The run's files whose paths are rewritten from this service's directory to the workspace's (results.jsonl, run.json)
TEXT = {".json", ".jsonl", ".txt", ".log", ".md", ".yaml", ".yml", ".csv"}
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _descend(rollout: Path, parts, create: bool = False) -> int:
    """A descriptor of rollout/<parts...>, no link followed below the rollout; the caller closes it."""
    fd = os.open(rollout, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
            inner = os.open(part, DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = inner
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read(rollout: Path, path: Path) -> bytes:
    """A regular file of the session's, read without following a link."""
    parts = path.relative_to(rollout).parts
    fd = _descend(rollout, parts[:-1])
    try:
        f = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)
    with os.fdopen(f, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"{path.name} is not a regular file")
        return handle.read()


def _move(src: Path, fd: int, swaps: dict) -> None:
    """src's files into the directory fd names: each replaces the entry of its name (a link is replaced, not
    followed), its directories opened without following links; the service's paths in its text become the session's."""
    for entry in os.scandir(src):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            try:
                os.mkdir(entry.name, dir_fd=fd)
            except FileExistsError:
                pass
            inner = os.open(entry.name, DIRECTORY, dir_fd=fd)
            try:
                _move(Path(entry.path), inner, swaps)
            finally:
                os.close(inner)
        elif entry.is_file():
            if Path(entry.name).suffix in TEXT:
                text = Path(entry.path).read_text(errors="surrogateescape")
                for mine, theirs in swaps.items():
                    text = text.replace(mine, theirs)
                Path(entry.path).write_text(text, errors="surrogateescape")
            try:
                os.replace(entry.path, entry.name, dst_dir_fd=fd)
            except OSError as exc:
                if exc.errno != errno.EXDEV:                     # another filesystem: copied instead
                    raise
                try:
                    os.unlink(entry.name, dir_fd=fd)
                except FileNotFoundError:
                    pass
                out = os.open(entry.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
                with os.fdopen(out, "wb") as handle, open(entry.path, "rb") as source:
                    shutil.copyfileobj(source, handle)


class Service:
    def __init__(self, root: Path, tasks: dict, workspaces: list[Path], ledger: Ledger, env: dict, workers: int,
                 *, scratch: Path | None = None, run_timeout: float = 4 * 3600, gpu=()):
        self.root, self.tasks, self.workspaces, self.ledger = root, tasks, [w.resolve() for w in workspaces], ledger
        self.env, self.workers, self.run_timeout, self.gpu = env, workers, float(run_timeout), list(gpu)
        self.scratch = Path(scratch or ledger.path.parent / "runs")      # this service's own: outside every sandbox
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.nonce = ""
        self.cli = root / ".venv-robosuite" / "bin" / "cap-harness"
        self.python = root / ".venv-robosuite" / "bin" / "python"
        self.running: set[subprocess.Popen] = set()
        self.lock, self.stopping = threading.Lock(), False

    def _rollout(self, workspace: Path) -> Path | None:
        """The rollout directory a workspace is in: <workspaces>/<role>/<name>, the sandbox's writable root."""
        for served in self.workspaces:
            if workspace.is_relative_to(served) and len(workspace.relative_to(served).parts) > 2:
                return served.joinpath(*workspace.relative_to(served).parts[:2])
        return None

    def register(self, body: dict) -> dict:
        workspace = Path(str(body.get("workspace", ""))).resolve()
        rollout = self._rollout(workspace)
        if rollout is None or not workspace.is_dir():
            return {"ok": False, "error": f"{workspace} is not a session workspace this service serves"}
        row = self.tasks.get(str(body.get("task")))
        if row is None or not re.fullmatch(re.escape(row["id"]) + r"-[0-9a-f]+", rollout.name):
            return {"ok": False, "error": f"the rollout {rollout.name} is not one of task {body.get('task')}"}
        with self.ledger.lock:
            if str(workspace) in self.ledger.data:          # a retry's prepare (the same workspace, made afresh)
                return {"ok": True}
            if any(Path(w).is_relative_to(rollout) for w in self.ledger.data):
                return {"ok": False, "error": "this rollout has registered its session already"}
            verifier = row.get("role") == "verifier"
            self.ledger.data[str(workspace)] = {
                "task": row["id"], "role": "verifier" if verifier else "actor", "suite": str(row["suite"]),
                "task_id": int(row.get("task_id", 0)), "dev": seeds_of(row.get("debug_seeds")),
                "unseen": seeds_of(row.get("unseen_seeds")), "max_programs": 2 if verifier else None,
                "max_steps": body.get("max_steps"), "unseen_used": False, "programs": []}
            self.ledger.save()
        return {"ok": True}

    def run(self, body: dict) -> dict:
        workspace = Path(str(body.get("workspace", ""))).resolve()
        session = self.ledger.data.get(str(workspace))
        if session is None:
            return {"ok": False, "error": "unknown workspace: the session is not registered"}
        rollout = self._rollout(workspace)
        program = (workspace / str(body.get("program", ""))).resolve()
        try:
            if rollout is None or not program.is_relative_to(workspace):
                raise OSError("outside the workspace")
            code = _read(rollout, program)
        except OSError:
            return {"ok": False, "error": f"no program at {body.get('program')} in the workspace"}
        out = (workspace / str(body.get("output_root", ""))).resolve()
        if body.get("cmd") != "validate" and not out.is_relative_to(workspace):
            return {"ok": False, "error": "--output-root must be inside the workspace"}
        own = Path(tempfile.mkdtemp(prefix="run-", dir=self.scratch))
        try:
            (own / program.name).write_bytes(code)
            return self._run(body, session, own / program.name, code, own / "out", rollout, out,
                             {str(own / "out"): str(out), str(own / program.name): str(program)})
        finally:
            shutil.rmtree(own, ignore_errors=True)

    def _run(self, body, session, program, code, own_out, rollout, out, swaps) -> dict:
        env = {**sim.scrubbed(os.environ),
               **{k: os.path.expandvars(os.path.expanduser(str(v))) for k, v in self.env.items()}}
        if body.get("cmd") == "validate":
            check = ("import ast,sys\nfrom cap_harness.runtime import _ProgramValidator\n"
                     "_ProgramValidator().visit(ast.parse(open(sys.argv[1]).read()))\nprint('program accepted')")
            returncode, stdout, stderr = self._execute([str(self.python), "-c", check, str(program)], env, 300,
                                                       program.parent, gpu=[])
            return {"ok": True, "code": returncode, "stdout": self._swap(stdout, swaps),
                    "stderr": self._swap(stderr, swaps)[-4000:]}
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
                digest = hashlib.sha256(code).hexdigest()
                if digest not in session["programs"]:
                    if len(session["programs"]) >= session["max_programs"]:
                        return {"ok": False, "error": f"the attempt budget is {session['max_programs']} programs, "
                                                      f"and it is spent"}
                    session["programs"].append(digest)
            self.ledger.save()
        own_out.mkdir()
        common = ["--benchmark", "robosuite", "--suite", session["suite"], "--task-id", str(session["task_id"]),
                  "--program", str(program), "--output-root", str(own_out), "--flat-run-dir"]
        if body.get("no_videos"):
            common.append("--no-videos")
        if session.get("max_steps"):                        # the registration's, as the judge's (not the request's)
            common += ["--max-steps", str(int(session["max_steps"]))]
        if body.get("cmd") == "run":
            cmd = [str(self.cli), "run", *common, "--seed", str(seeds[0])]
        else:
            cmd = [str(self.cli), "run-batch", *common, "--seeds", *map(str, seeds),
                   "--workers", str(min(self.workers, len(seeds))), "--results-jsonl", str(own_out / "results.jsonl"),
                   "--log-dir", str(own_out / "logs")]
        returncode, stdout, stderr = self._execute(cmd, env, self.run_timeout, program.parent, own_out, gpu=self.gpu)
        if own_out.is_symlink() or not own_out.is_dir():    # replaced by the program: nothing of it is delivered
            return {"ok": False, "error": "the run's output directory was replaced while it ran"}
        try:
            fd = _descend(rollout, out.relative_to(rollout).parts, create=True)
            try:
                _move(own_out, fd, swaps)
            finally:
                os.close(fd)
        except OSError as exc:
            return {"ok": False, "error": f"the run could not be written into {body.get('output_root')} "
                                          f"(a link in its path?): {exc}"}
        return {"ok": True, "code": returncode, "stdout": self._swap(stdout, swaps)[-20000:],
                "stderr": self._swap(stderr, swaps)[-8000:]}

    @staticmethod
    def _swap(text: str, swaps: dict) -> str:
        for mine, theirs in swaps.items():
            text = text.replace(mine, theirs)
        return text

    def _execute(self, cmd: list[str], env: dict, timeout: float, own: Path, out: Path | None = None,
                 gpu=()) -> tuple[int, str, str]:
        """cmd confined (sim.confine: it reads this run's own directory, the program, and writes only its output),
        in a process group of its own, stopped with its workers at the timeout or when the service stops."""
        box, args, env = sim.confine(cmd, root=self.root, env=env, readable=[own], writable=[out] if out else [],
                                     gpu=gpu,
                                     hidden=[self.ledger.path.parent, self.scratch, *self.workspaces])
        try:
            with self.lock:                                 # started and listed at once: stop() sees every run
                if self.stopping:
                    box.close()
                    return 1, "", "bin/sim: the simulator service is stopping"
                proc = subprocess.Popen(args, env=env, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, start_new_session=True)
                self.running.add(proc)
        except BaseException:
            box.close()
            raise
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return proc.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            self._kill(proc, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            return 124, stdout, stderr + f"\nbin/sim: the run was stopped after {timeout:.0f} s"
        finally:
            with self.lock:
                self.running.discard(proc)
            box.close()

    @staticmethod
    def _kill(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self) -> None:
        with self.lock:
            self.stopping = True
            for proc in self.running:
                self._kill(proc, signal.SIGTERM)


def handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):                                   # the nonce: launch.sh knows its own service by it
            self._reply({"ok": self.path == "/health", "nonce": service.nonce})

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
    ap.add_argument("--tasks", type=Path, required=True, help="the task rows (utils/tasks.jsonl): their seeds")
    ap.add_argument("--workspaces", type=Path, action="append", required=True,
                    help="a directory whose sessions this service serves (repeatable)")
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--sim-env", default="{}", help="the simulator's environment, a JSON object")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--run-timeout", type=float, default=4 * 3600, help="seconds a run may take")
    ap.add_argument("--gpu", default="", help="the GPUs the simulator renders on, the N of /dev/nvidia<N> (8,9); "
                                              "none: software rendering")
    ap.add_argument("--nonce", default="", help="returned by /health: the launcher's check that it is this service")
    a = ap.parse_args()
    a.state.mkdir(parents=True, exist_ok=True)
    tasks = {r["id"]: r for r in (json.loads(line) for line in a.tasks.read_text().splitlines() if line.strip())}
    service = Service(a.aspire_root.resolve(), tasks, a.workspaces, Ledger(a.state / "ledger.json"),
                      json.loads(a.sim_env), a.workers, run_timeout=a.run_timeout, gpu=sim.gpus_of(a.gpu))
    service.nonce = a.nonce

    def _bye(sig, frame):                                   # launch.sh's trap: the runs go with the service
        service.stop()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _bye)
    ThreadingHTTPServer((a.host, a.port), handler(service)).serve_forever()


if __name__ == "__main__":
    main()
