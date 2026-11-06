"""The host's side of a stream: one driver process per multi-step task, asked for one step at a time.

The driver (`harborside/driver.py`, under Harbor's interpreter) holds the task's environment open between steps
and answers on a Unix socket in the stream's directory; this client starts it when there is none (under a file
lock, so concurrent rollouts of one stream start one driver), waits for its turn (the driver runs steps in order),
asks for its step and reads the step's result back. The stream's directory:

    <jobs_dir>/streams/<stream>/
      driver.json          {pid, socket, started, trial_dir}: the live driver
      driver.log           its log (Harbor's trial log is in trial_dir)
      .lock                the start lock
      steps/<index>-<name>/   this step's staging (deploy/) and curated log
      trials/<stream>/     Harbor's trial directory: steps/<name>/{agent,verifier,result.json}, result.json at the end

Protocol: one JSON object per line, one request per connection.
    {"op": "status"}                      -> {"ok": true, "ready": bool, "next_index": i, "running": name|null,
                                              "finished": bool, "aborted": reason|null, "error": msg|null}
    {"op": "step", "name", "index", "deploy_dir", "agent_kwargs"}
                                          -> {"ok": true, "step": <StepResult>, "paths": {...}, "aborted": ...}
                                           | {"ok": false, "out_of_order": true, "next_index": i} | {"ok": false, "error"}
    {"op": "result", "index": i}          -> the stored reply of a finished step, or {"ok": false, "error"}
    {"op": "stop"}                        -> {"ok": true}
"""
from __future__ import annotations

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

DRIVER_INFO = "driver.json"
DRIVER_LOG = "driver.log"
POLL_S = 5.0
START_TIMEOUT_S = 3600.0                                 # the environment's build and the agent's install
HARBORSIDE = Path(__file__).resolve().parent / "harborside"


class StreamError(RuntimeError):
    pass


class StreamOrderError(StreamError):
    """The step's turn did not come: an earlier step is missing, or this one was already run."""


def socket_path(stream_dir: Path) -> Path:
    """Where the driver listens: a short path (AF_UNIX allows 107 bytes) under the user's runtime directory, named
    by a digest of the stream directory. Never /tmp: a sandboxed caller has a private one."""
    import hashlib
    name = hashlib.sha1(str(Path(stream_dir).resolve()).encode()).hexdigest()[:16] + ".sock"
    bases = []
    if os.environ.get("XDG_RUNTIME_DIR") and Path(os.environ["XDG_RUNTIME_DIR"]).is_dir():
        bases.append(Path(os.environ["XDG_RUNTIME_DIR"]))
    bases += [Path.home() / ".cache" / "hermit" / "harbor", Path("/tmp") / f"hermit-harbor-{os.getuid()}"]
    for base in bases:
        path = base / "hermit-harbor-sockets" / name
        if len(str(path).encode()) <= 100:
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
    raise StreamError(f"no short enough path for the stream's socket (tried {[str(b) for b in bases]})")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class StreamClient:
    def __init__(self, stream_dir: Path, *, python: str, log=None):
        self.dir = Path(stream_dir)
        self.python = python                              # Harbor's venv interpreter
        self.log = log or (lambda m: print(m, file=sys.stderr, flush=True))

    # --- the driver process -----------------------------------------------------------------------------------
    @property
    def info_path(self) -> Path:
        return self.dir / DRIVER_INFO

    def info(self) -> dict | None:
        try:
            return json.loads(self.info_path.read_text())
        except (OSError, ValueError):
            return None

    def running(self) -> dict | None:
        """The live driver's record, or None when there is none (or it died)."""
        info = self.info()
        if info and alive(int(info.get("pid", 0))) and Path(info.get("socket", "")).exists():
            return info
        return None

    def ensure(self, *, task_dir: Path, name: str, agent: dict, environment: dict, verifier: dict | None = None,
               idle_s: float = 4 * 3600, env: dict | None = None, start_timeout_s: float = START_TIMEOUT_S) -> dict:
        """The live driver, started if there is none, ready (environment up, agent installed) or raising with the
        error it reported."""
        self.dir.mkdir(parents=True, exist_ok=True)
        if os.environ.get("HA_ADAPTER_SANDBOX") == "1" and self.running() is None:
            # The rollout runs in a sandboxed worker: a driver it started would die with it (bubblewrap's
            # --die-with-parent), and its socket would sit in a private /tmp. The stream's environment has to
            # outlive the call, so streams need the rollout role unconfined for now.
            raise StreamError("a stream's driver cannot be started from a sandboxed rollout: set "
                              "sandbox.profiles.rollout.enabled: false for a multi-step (stream) task set")
        with open(self.dir / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                info = self.running()
                if info is None:
                    info = self._start(task_dir=task_dir, name=name, agent=agent, environment=environment,
                                       verifier=verifier or {}, idle_s=idle_s, env=env)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        self._wait_ready(info, start_timeout_s)
        return info

    def _start(self, *, task_dir: Path, name: str, agent: dict, environment: dict, verifier: dict, idle_s: float,
               env: dict | None) -> dict:
        sock = socket_path(self.dir)
        sock.unlink(missing_ok=True)
        self.info_path.unlink(missing_ok=True)
        cmd = [self.python, "-m", "harborside.driver", "--task-dir", str(task_dir), "--stream-dir", str(self.dir),
               "--name", name, "--socket", str(sock), "--agent-json", json.dumps(agent),
               "--environment-json", json.dumps(environment), "--verifier-json", json.dumps(verifier),
               "--idle-s", str(idle_s)]
        run_env = {**os.environ, **(env or {})}
        run_env["PYTHONPATH"] = str(HARBORSIDE.parent) + (os.pathsep + run_env["PYTHONPATH"] if run_env.get("PYTHONPATH") else "")
        with open(self.dir / DRIVER_LOG, "a") as log:
            log.write(f"=== {datetime.now().isoformat(timespec='seconds')} start: {' '.join(cmd)}\n")
            log.flush()
            proc = subprocess.Popen(cmd, env=run_env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True, cwd=str(self.dir))
        deadline = time.time() + 120
        while time.time() < deadline:
            info = self.info()
            if info and Path(info.get("socket", "")).exists():
                return info
            if proc.poll() is not None:
                raise StreamError(f"the stream driver exited with {proc.returncode} before it listened; see "
                                  f"{self.dir / DRIVER_LOG}")
            time.sleep(0.5)
        raise StreamError(f"the stream driver did not listen within 120 s; see {self.dir / DRIVER_LOG}")

    def _wait_ready(self, info: dict, timeout_s: float) -> None:
        deadline = time.time() + timeout_s
        while True:
            status = self.request({"op": "status"}, timeout=60)
            if status.get("error"):
                raise StreamError(f"the stream's environment failed: {status['error']}")
            if status.get("ready"):
                return
            if status.get("finished"):
                raise StreamError("the stream driver finished before this step")
            if time.time() >= deadline or not alive(int(info["pid"])):
                raise StreamError(f"the stream's environment was not ready within {timeout_s:.0f} s; see "
                                  f"{self.dir / DRIVER_LOG}")
            time.sleep(POLL_S)

    # --- requests ---------------------------------------------------------------------------------------------
    def request(self, obj: dict, *, timeout: float | None) -> dict:
        info = self.info()
        if not info:
            raise StreamError("no stream driver record")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(30)
            s.connect(info["socket"])
            s.sendall((json.dumps(obj) + "\n").encode())
            s.settimeout(timeout)
            chunks = []
            while True:
                chunk = s.recv(1 << 16)
                if not chunk:
                    break
                chunks.append(chunk)
                if chunk.endswith(b"\n"):
                    break
        data = b"".join(chunks).decode("utf-8", errors="replace").strip()
        if not data:
            raise StreamError("the stream driver closed the connection without an answer")
        return json.loads(data)

    def status(self) -> dict:
        return self.request({"op": "status"}, timeout=60)

    def wait_turn(self, index: int, *, timeout_s: float) -> dict:
        """Block until the driver's next step is `index` (earlier steps of the stream, running in other rollouts,
        come first); the status then. Raises when the stream finished, aborted or died first."""
        deadline = time.time() + timeout_s
        while True:
            status = self.status()
            if status.get("error"):
                raise StreamError(f"the stream's environment failed: {status['error']}")
            nxt = int(status.get("next_index") or 0)
            if nxt == index and not status.get("running"):
                return status
            if nxt > index:
                raise StreamOrderError(f"step {index} was already run (the driver is at step {nxt})")
            if status.get("finished") or status.get("aborted"):
                raise StreamError(f"the stream {'aborted: ' + str(status.get('aborted')) if status.get('aborted') else 'finished'} "
                                  f"before step {index}")
            if time.time() >= deadline:
                raise StreamOrderError(f"waited {timeout_s:.0f} s for step {index}'s turn (the driver is at step {nxt}): "
                                       "a stream's steps run in order, each after the one before it")
            time.sleep(POLL_S)

    def step(self, *, name: str, index: int, deploy_dir: str | None, agent_kwargs: dict | None,
             timeout_s: float) -> dict:
        try:
            return self.request({"op": "step", "name": name, "index": index, "deploy_dir": deploy_dir,
                                 "agent_kwargs": agent_kwargs or {}}, timeout=timeout_s)
        except (socket.timeout, TimeoutError):
            return {"ok": False, "expired": True, "error": f"no answer from the stream driver within {timeout_s:.0f} s"}

    def result(self, index: int) -> dict:
        return self.request({"op": "result", "index": index}, timeout=60)

    def stop(self, *, timeout_s: float = 900) -> None:
        try:
            self.request({"op": "stop"}, timeout=timeout_s)
        except (OSError, StreamError, ValueError) as exc:
            self.log(f"stopping the stream driver: {exc}")
