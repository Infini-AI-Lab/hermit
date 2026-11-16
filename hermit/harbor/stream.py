"""The host's side of a stream: one driver process per multi-step task, asked for one step at a time.

The driver (`harborside/driver.py`, under Harbor's interpreter) holds the task's environment open between steps
and answers on a Unix socket in the stream's directory; this client starts it when there is none (under a file
lock, so concurrent rollouts of one stream start one driver), waits for its turn (the driver runs steps in order),
asks for its step and reads the step's result back. A driver that answers no stop (hung in a step) is ended with its
process group, and its trial's compose projects are taken down by name, as an expired trial's are. The stream's
directory:

    <jobs_dir>/streams/<stream>/
      driver.json          {pid, socket, name, started, trial_dir}: the live driver (name: Harbor's trial)
      driver.log           its log (Harbor's trial log is in trial_dir)
      .lock                the start lock
      steps/<index>-<name>/   this step's staging (deploy/), launch plan (launch/) and curated log
      trials/<stream>/     Harbor's trial directory: steps/<name>/{agent,verifier,result.json}, result.json at the end
      replies/<index>.json the driver's answer for each step it ran
      earlier/<n>/         an earlier driver's trials/ and replies/, set aside when a new driver starts here (Harbor's
                           trial of the same name would reuse its step directories: a step that leaves no output of
                           its own would be read with the earlier run's); driver.log goes on, each start headed

Protocol: one JSON object per line, one request per connection.
    {"op": "status"}                      -> {"ok": true, "ready": bool, "next_index": i, "running": name|null,
                                              "finished": bool, "aborted": reason|null, "error": msg|null}
    {"op": "step", "name", "index", "plan", "agent_kwargs"}   (plan: the step's launch plan, hermit.harbor.launch)
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
import signal
import socket
import subprocess
import sys
import time

DRIVER_INFO = "driver.json"
DRIVER_LOG = "driver.log"
EARLIER = "earlier"                                      # earlier drivers' trials and replies, set aside
POLL_S = 5.0
TURN_GRACE_POLLS = 2                                     # a missing earlier step is waited for this many polls more
START_TIMEOUT_S = 3600.0                                 # the environment's build and the agent's install
STOP_TIMEOUT_S = 900.0                                   # a stop's answer: the environment down, the result written
HARBORSIDE = Path(__file__).resolve().parent / "harborside"


class StreamError(RuntimeError):
    pass


class StreamOrderError(StreamError):
    """The step's turn did not come: an earlier step is missing, or this one was already run."""


class TurnLostError(StreamOrderError):
    """The step's turn cannot come: no rollout runs the step its driver waits for, or the stream has no driver and no
    rollout of its first step is running to start one (`why`)."""

    def __init__(self, index: int, why: str):
        self.why = why
        super().__init__(f"step {index}'s turn cannot come: {why}; a stream's steps run in order, each after the one "
                         "before it")


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

    def gone(self) -> bool:
        """Whether the driver's process has ended: no record, no such process, or a zombie (a driver this process
        started and that died stays one until it is reaped, and `alive` still finds it): no request reaches it again."""
        info = self.info()
        if not info:
            return True
        pid = int(info.get("pid") or 0)
        if pid <= 0:
            return False
        if not alive(pid):
            return True
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
        except (OSError, IndexError):
            return False                                  # no /proc here: alive's answer

    def ensure(self, *, task_dir: Path, name: str, agent: dict, environment: dict, verifier: dict | None = None,
               idle_s: float = 4 * 3600, env: dict | None = None, start_timeout_s: float = START_TIMEOUT_S,
               trial: dict | None = None) -> dict:
        """The live driver, started if there is none, ready (environment up, agent installed) or raising with the
        error it reported. A driver it starts runs with `env` as it is (this process's environment when none is
        given), and `trial` holds the settings of Harbor's trial config (its timeout multipliers)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        if os.environ.get("HA_ADAPTER_SANDBOX") == "1" and self.running() is None:
            # A driver started from a sandboxed worker would die with it (bubblewrap's --die-with-parent), and its
            # socket would sit in a private /tmp. A Harbor task set's pipeline runs on the host (its environment is
            # Harbor's), so this is a task's own agent driving a stream from inside the task sandbox.
            raise StreamError("a stream's driver cannot be started from inside the task sandbox: run the task set "
                              "with agent.name: harbor, whose pipeline runs on the host")
        with open(self.dir / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                info = self.running()
                if info is None:
                    info = self._start(task_dir=task_dir, name=name, agent=agent, environment=environment,
                                       verifier=verifier or {}, idle_s=idle_s, env=env, trial=trial or {})
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        self._wait_ready(info, start_timeout_s)
        return info

    def _start(self, *, task_dir: Path, name: str, agent: dict, environment: dict, verifier: dict, idle_s: float,
               env: dict | None, trial: dict) -> dict:
        sock = socket_path(self.dir)
        sock.unlink(missing_ok=True)
        self.info_path.unlink(missing_ok=True)
        self._set_aside()
        cmd = [self.python, "-m", "harborside.driver", "--task-dir", str(task_dir), "--stream-dir", str(self.dir),
               "--name", name, "--socket", str(sock), "--agent-json", json.dumps(agent),
               "--environment-json", json.dumps(environment), "--verifier-json", json.dumps(verifier),
               "--trial-json", json.dumps(trial), "--idle-s", str(idle_s)]
        # The environment as given (harbor_env: what Harbor's processes may have), never this process's back under it
        run_env = dict(os.environ if env is None else env)
        run_env["PYTHONPATH"] = str(HARBORSIDE.parent) + (os.pathsep + run_env["PYTHONPATH"]
                                                          if run_env.get("PYTHONPATH") else "")
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

    def _set_aside(self) -> None:
        """What an earlier driver of this directory left, moved to earlier/<n>/ before a new one starts (under the start
        lock): its trial directory, whose step directories Harbor's next trial of the same name would reuse (a step
        that leaves no output of its own would be read with the earlier run's: its usage billed again, its reply and
        outputs taken for the step's), and its replies. driver.log, which every step's curated log copies, stays: each
        driver's part of it begins with the line its start writes."""
        left = [self.dir / name for name in ("trials", "replies") if os.path.lexists(self.dir / name)]
        if not left:
            return
        taken = [int(p.name) for p in (self.dir / EARLIER).glob("*") if p.name.isdigit()]
        place = self.dir / EARLIER / str(max(taken, default=0) + 1)
        place.mkdir(parents=True)
        for path in left:
            path.rename(place / path.name)

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
        try:
            return json.loads(data)
        except ValueError:                                # cut off: a driver killed while it wrote its answer
            raise StreamError("the stream driver closed the connection before its answer was whole") from None

    def status(self) -> dict:
        return self.request({"op": "status"}, timeout=60)

    def wait_turn(self, index: int, *, timeout_s: float, missing=None) -> dict:
        """Block until the driver's next step is `index` (earlier steps of the stream, running in other rollouts,
        come first); the status then. Raises when the stream finished, aborted or died first (an aborted stream's
        driver refuses every step, the step at its turn too, so that step is never asked for), and when `missing`
        (called with the step the driver waits for, while it is idle at an earlier one) says why no rollout will ask
        for that step, on TURN_GRACE_POLLS polls more in a row (by then a rollout submitted just before this one has
        come): a turn that cannot come ends at once (TurnLostError), not at the deadline."""
        deadline = time.time() + timeout_s
        missed = 0
        while True:
            status = self.status()
            if status.get("error"):
                raise StreamError(f"the stream's environment failed: {status['error']}")
            nxt = int(status.get("next_index") or 0)
            if nxt > index:
                raise StreamOrderError(f"step {index} was already run (the driver is at step {nxt})")
            if status.get("finished") or status.get("aborted"):    # before its turn: the driver would refuse the step
                raise StreamError(f"the stream {'aborted: ' + str(status.get('aborted')) if status.get('aborted') else 'finished'} "
                                  f"before step {index}")
            if nxt == index and not status.get("running"):
                return status
            why = missing(nxt) if missing is not None and not status.get("running") else None
            missed = missed + 1 if why else 0
            if missed > TURN_GRACE_POLLS:
                raise TurnLostError(index, why)
            if time.time() >= deadline:
                raise StreamOrderError(f"waited {timeout_s:.0f} s for step {index}'s turn (the driver is at step {nxt}): "
                                       "a stream's steps run in order, each after the one before it")
            time.sleep(POLL_S)

    def step(self, *, name: str, index: int, plan: str | None, agent_kwargs: dict | None,
             timeout_s: float) -> dict:
        try:
            return self.request({"op": "step", "name": name, "index": index, "plan": plan,
                                 "agent_kwargs": agent_kwargs or {}}, timeout=timeout_s)
        except (socket.timeout, TimeoutError):
            return {"ok": False, "expired": True, "error": f"no answer from the stream driver within {timeout_s:.0f} s"}

    def result(self, index: int) -> dict:
        return self.request({"op": "result", "index": index}, timeout=60)

    def stop(self, *, timeout_s: float | None = None) -> None:
        """Ask the driver to stop (its step loop ends, the environment goes down, the trial's result is written) and
        wait for its answer. A driver that gives none within `timeout_s` (STOP_TIMEOUT_S) is hung in a step, which the
        stop would wait behind (one whose request expired), and neither its idle stop nor a later stop would end it:
        it is killed (kill)."""
        try:
            self.request({"op": "stop"}, timeout=STOP_TIMEOUT_S if timeout_s is None else timeout_s)
        except TimeoutError as exc:
            killed = self.kill()
            self.log(f"stopping the stream driver: no answer ({exc}); "
                     + ("killed it, its compose projects taken down" if killed else "no process of it to kill"))
        except (OSError, StreamError, ValueError) as exc:
            self.log(f"stopping the stream driver: {exc}")

    def kill(self) -> bool:
        """End the driver's process group (it leads one: started in a session of its own), SIGTERM then SIGKILL, as
        an expired trial's `harbor run` is ended, and take its trial's compose projects down by name (its environment,
        which its own stop would have taken down: docker.compose_down); whether it did. Only a live process whose
        command line names this stream's directory is signalled: the pid its record gives may since be another
        program's."""
        pid = self._driver_pid()
        if pid is None:
            return False
        for sig, grace in ((signal.SIGTERM, 30.0), (signal.SIGKILL, 10.0)):
            try:
                os.killpg(pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            deadline = time.time() + grace
            while time.time() < deadline and not self.gone():
                time.sleep(0.2)
        if self.gone():                     # its socket, which a killed driver leaves (its own exit removes it)
            socket_path(self.dir).unlink(missing_ok=True)
        name = (self.info() or {}).get("name")
        if name:
            from hermit.harbor import docker
            docker.compose_down(str(name))
        return True

    def _driver_pid(self) -> int | None:
        """The pid of this directory's live driver, when its command line (/proc) says it is one and it leads its own
        process group; None otherwise (no record, a process gone, one of another program, no /proc to tell)."""
        pid = int((self.info() or {}).get("pid") or 0)
        if pid <= 0 or self.gone():
            return None
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            leads = os.getpgid(pid) == pid
        except OSError:
            return None
        where = argv.index(b"--stream-dir") + 1 if b"--stream-dir" in argv else len(argv)
        named = where < len(argv) and Path(os.fsdecode(argv[where])).resolve() == self.dir.resolve()
        return pid if leads and named and b"harborside.driver" in argv else None
