"""A task's own Harbor environment, as a Hermit environment: Harbor gives the task's data, its environment and its
verifier; the agent working in it is Hermit's (docs/framework/task.md#harbor-environments).

    environments = HarborEnvironments(settings, base=folder)    # the run's: its settings, disk gate and cleanup
    with environments.session(task_dir, name) as env:           # started (the disk gate first), stopped after
        agent.run(task, adapter, environment=env)               # exec / upload / download, as any environment
        verdict = env.verify(agent_logs=log_dir)                # Harbor's verifier: {rewards, reward, feedback, ...}

Harbor runs in its own venv (Python 3.13, `harbor` 0.25.0), not in Hermit's interpreter, so each environment is a
driver process of its own under Harbor's python (hermit/environment/harbor_driver.py: Harbor's own trial of the task,
its agent Harbor's do-nothing one, driven a piece at a time) and this side speaks JSON lines with it. What Harbor
does to the environment is Harbor's code: the start of a prebuilt image (`docker_image`, pulled) or a Dockerfile's
build, the network policy of the agent phase, the task's tests uploaded only for the verifier, the verifier's
environment (`[verifier] environment_mode`: shared, or a separate one), the reward files, the trial's result.json.
What Hermit adds around it is the warm start Hermit's runner had (hermit/environment/harbor_docker.py, unchanged):
no environment starts while the host's /tmp or Docker's data root has less than its gate free (room kept for the
task's image when it is not here yet, and for the images other environments of the run are still to pull), the
task's prebuilt image removed after it as `docker_cleanup` says, and a driver that had to be killed has its trial's
compose projects taken down by name.

Settings (a run config's `environment.harbor`, merged over the benchmark file it names as `benchmark`):

  tasks_dir         the task directories (relative to the benchmark file's folder; its links followed once)
  environment       Harbor's environment config: type (docker by default; e2b, modal, daytona, ...: whatever Harbor
                    supports, its own extra installed in Harbor's venv), import_path, kwargs, delete, force_build,
                    mounts, extra_docker_compose (relative to the benchmark file's folder), env, override_cpus,
                    override_memory_mb, ...
  verifier          Harbor's verifier config (override_timeout_sec, max_timeout_sec, env, disable, ...)
  verifier_timeout_multiplier, environment_build_timeout_multiplier   Harbor's, at least 1
  agent_timeout_sec the agent's budget (Harbor's override_timeout_sec); null: each task's own
  allowed_hosts     hosts a task whose agent phase has no public network lets the agent reach (Harbor's
                    extra_allowed_hosts); the run adds its agent's model hosts
  docker_cleanup, rmi_threshold_gb, disk_gate_gb, disk_gate_wait_s, tmp_gate_gb, image_sizes
                    the warm start (above): images_keep | rmi_task_image | rmi_over_gb; GB; seconds; the images'
                    compressed sizes (a JSON file), for the room the gate keeps
  jobs_dir          where each environment's trial directory goes (default ~/.cache/hermit/harbor/jobs)
  harbor            Harbor's command (default ~/.cache/hermit/harbor-venv/bin/harbor): its venv's python, beside it or
                    beside the file a link to it names, runs the driver
  start_timeout_s, verify_timeout_s   how long this side waits for the driver's start (pull, build) and verifier
  verifier_timeout  agent (a verifier that ran out of its time scores the agent's solution: Harbor's grading) or
                    infra (not the agent's result)
  pass_reward, reward_key   the reward that is a pass (1.0; null: a scale), the verifier's reward key (reward)
  infra_retries, infra_wait_s   a rollout that is not the agent's result (infra: hermit.environment.harbor_errors) runs
                    again in a fresh environment this many times at most (default 2), each after this long (default
                    300 s), unless its cause runs no better again (NO_RETRY: the disk gate, model_rejected)
  busy_retries, busy_wait_s   a session the provider could not serve runs again in a fresh environment this many times
                    over the task's runs (default 3, not counted in infra_retries), the n-th after n x this (120 s), as
                    the runner's busy_retries

An environment runs the task's commands where Harbor runs them (isolation `remote`): `exec` runs a command in the
task's main container as the task's agent user, from its working directory (the workspace), a variable passed by name
(never on a command line of this machine), a timeout enforced there (`timeout`, its process group killed); `upload` and
`download` move files between this machine and it (a relative path is in the workspace, an absolute one anywhere in
the environment); `install(cli)` has Harbor's own installed agent install a CLI there. `verify` ends the agent's phase:
no command runs in the environment after it.
"""
from __future__ import annotations

import atexit
import contextlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import uuid

import yaml

from hermit.environment import harbor_docker as docker
from hermit.environment import harbor_errors as errors
from hermit.environment.base import Environment, ExecResult, Limits, Unsupported

HARBOR = "~/.cache/hermit/harbor-venv/bin/harbor"
JOBS_DIR = "~/.cache/hermit/harbor/jobs"
DRIVER = Path(__file__).resolve().with_name("harbor_driver.py")
DISK_GATE_GB = 20.0
DISK_GATE_WAIT_S = 7200.0
TMP_GATE_GB = 1.0
HOST_TMP = Path("/tmp")
START_TIMEOUT_S = 3600.0             # the driver's start: the image's pull or build, the healthcheck
VERIFY_TIMEOUT_S = 7200.0            # ... and its verifier when Harbor names no timeout (its own ends it first)
VERIFY_ALLOWANCE_S = 1800.0          # beside Harbor's verifier timeout: the tests' upload, a separate environment's
                                     # start, the logs
STOP_TIMEOUT_S = 600.0
SETUP_ALLOWANCE_S = 360.0            # beside a task's agent budget: the CLI's install, the home's upload (the runner's
                                     # SETUP_BUDGET_S)
PASS_REWARD = 1.0
MULTIPLIERS = ("verifier_timeout_multiplier", "environment_build_timeout_multiplier")
SETTINGS = (*MULTIPLIERS, "benchmark", "tasks_dir", "environment", "verifier", "agent_timeout_sec",
            "setup_timeout_sec", "allowed_hosts", "docker_cleanup", "rmi_threshold_gb", "disk_gate_gb",
            "disk_gate_wait_s", "tmp_gate_gb", "image_sizes", "jobs_dir", "harbor", "start_timeout_s",
            "verify_timeout_s", "verifier_timeout", "pass_reward", "reward_key", "infra_retries", "infra_wait_s",
            "busy_retries", "busy_wait_s", "verifier_redact",
            "dataset")
VERIFIER_TIMEOUTS = ("agent", "infra")
FEEDBACK_CHARS = 4000


class EnvironmentUnavailable(RuntimeError):
    """The task's environment could not be given (the disk gate waited its time out, the image did not pull or build,
    the driver died): not the agent's result. `cause` is its infra cause (hermit.environment.harbor_errors: disk,
    sandbox_failed, harbor_failed, harbor_timeout)."""

    def __init__(self, message: str, cause: str = "harbor_failed"):
        super().__init__(message)
        self.cause = cause


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        out[key] = {**out[key], **value} if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def settings_of(section, base: Path) -> tuple[dict, Path]:
    """(the settings, the folder relative paths resolve against) of a run config's `environment.harbor`: a mapping,
    merged over the YAML file its `benchmark` names (whose `dataset:` block is `dataset prepare`'s, not a setting),
    or the path of such a file alone."""
    if isinstance(section, (str, os.PathLike)):
        section = {"benchmark": str(section)}
    if not isinstance(section, dict):
        raise ValueError("environment.harbor is a mapping of Harbor's settings, or the path of a benchmark file")
    given = dict(section)
    folder = Path(base)
    if given.get("benchmark"):
        path = Path(os.path.expanduser(str(given["benchmark"])))
        path = path if path.is_absolute() else (Path(base) / path)
        shared = yaml.safe_load(path.read_text()) or {}
        if not isinstance(shared, dict):
            raise ValueError(f"environment.harbor.benchmark {path} is not a mapping")
        shared.pop("dataset", None)
        given = _merge(shared, {k: v for k, v in given.items() if k != "benchmark"})
        folder = path.resolve().parent
    given.pop("dataset", None)
    unknown = sorted(set(given) - set(SETTINGS))
    if unknown:
        raise ValueError(f"environment.harbor: unknown keys {unknown}; known: {sorted(SETTINGS)}")
    return given, folder


class HarborEnvironments:
    """The run's Harbor environments: one for each task, sharing the settings, the disk gate's room for pulls and the
    image cleanup's lock."""

    def __init__(self, settings: dict, *, base: Path, allowed_hosts=(), log=None):
        self.settings, self.base = settings_of(settings, base)
        s = self.settings
        if not s.get("tasks_dir"):
            raise ValueError("environment.harbor.tasks_dir is required (the task directories)")
        self.tasks_dir = Path(os.path.realpath(self.path(s["tasks_dir"])))
        self.jobs_dir = Path(os.path.expanduser(str(s.get("jobs_dir") or JOBS_DIR))).resolve()
        self.harbor = self._harbor(s.get("harbor"))
        self.python = str(Path(os.path.realpath(self.harbor)).parent / "python")
        if not (os.path.isfile(self.python) and os.access(self.python, os.X_OK)):
            raise ValueError(f"{self.python} is not an executable file: a Harbor environment's driver runs under the "
                             f"python of Harbor's venv, beside environment.harbor.harbor ({self.harbor}) or beside "
                             f"the file a link of that name points to (default {HARBOR})")
        self.cleanup = str(s.get("docker_cleanup") or "images_keep")
        if self.cleanup not in docker.CLEANUP_MODES:
            raise ValueError(f"environment.harbor.docker_cleanup must be one of {', '.join(docker.CLEANUP_MODES)}, "
                             f"not {self.cleanup!r}")
        self.verifier_timeout = str(s.get("verifier_timeout") or "agent")
        if self.verifier_timeout not in VERIFIER_TIMEOUTS:
            raise ValueError(f"environment.harbor.verifier_timeout is agent or infra, not {self.verifier_timeout!r}")
        for key in MULTIPLIERS:
            if s.get(key) is not None and float(s[key]) < 1.0:
                raise ValueError(f"environment.harbor.{key} must be at least 1, not {s[key]}")
        self.pass_reward = PASS_REWARD if "pass_reward" not in s else \
            (None if s["pass_reward"] is None else float(s["pass_reward"]))
        self.reward_key = str(s.get("reward_key") or "reward")
        self.redactions = self._redactions(s.get("verifier_redact"))
        self.infra_retries = self._count("infra_retries", errors.INFRA_RETRIES, int)
        self.infra_wait_s = self._count("infra_wait_s", errors.INFRA_WAIT_S, float)
        self.busy_retries = self._count("busy_retries", errors.BUSY_RETRIES, int)
        self.busy_wait_s = self._count("busy_wait_s", errors.BUSY_WAIT_S, float)
        sizes = json.loads(self.path(s["image_sizes"]).read_text()) if s.get("image_sizes") else {}
        self.image_sizes = {k: v for k, v in sizes.items() if not str(k).startswith("_")}
        self.pulls = docker.Pulls()
        self.allowed_hosts = [*(s.get("allowed_hosts") or []), *(h for h in allowed_hosts
                                                                  if h not in (s.get("allowed_hosts") or []))]
        self.log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        self._root: Path | None = None
        self.live: set = set()                  # the environments started and not stopped: stopped at exit
        self.stopping = threading.Event()       # set once the run ends (a lost sign-in): no gate or pause waits on
        self.admission = None                   # the agent's login's plan admission around each environment (a
                                                # context manager factory: hermit.isolate._plan_admission)
        self._lock = threading.Lock()
        atexit.register(self.stop_all)

    def stop_all(self) -> None:
        """Every environment still up (this process is ending): stopped, its compose projects taken down."""
        with self._lock:
            left = list(self.live)
        for env in left:
            with contextlib.suppress(Exception):
                env.stop()

    def _count(self, key: str, default, kind):
        value = self.settings.get(key)
        try:
            value = default if value is None else kind(value)
        except (TypeError, ValueError):
            value = -1
        if value < 0:
            raise ValueError(f"environment.harbor.{key} is a number, at least 0, not {self.settings.get(key)!r}")
        return value

    def path(self, value) -> Path:
        p = Path(os.path.expandvars(str(value))).expanduser()
        return p if p.is_absolute() else (self.base / p).resolve()

    @staticmethod
    def _harbor(value) -> str:
        value = os.path.expanduser(str(value or HARBOR))
        if "/" not in value:
            value = shutil.which(value) or value
        return os.path.abspath(value) if "/" in value else value

    def task_dir(self, task) -> Path:
        """A task's directory: its private half's (a directory task), else its row's `harbor_task` under
        tasks_dir."""
        private = getattr(task, "private", None) or {}
        public = getattr(task, "public", task)
        if private.get("task_dir"):
            return Path(private["task_dir"])
        name = public.get("harbor_task")
        if not name:
            raise ValueError(f"task {public.get('id')!r} names no Harbor task directory (harbor_task)")
        return self.tasks_dir / str(name)

    # --- the warm start: the disk gate and the image cleanup (hermit.environment.harbor_docker, unchanged) -------
    def _gate(self, key: str, default: float) -> float:
        value = self.settings.get(key)
        return float(default if value is None else value)

    def docker_root(self) -> Path:
        if self._root is None:
            self._root = docker.data_root()
        return self._root

    def image_gb(self, image: str) -> float:
        compressed = self.image_sizes.get(image)
        return float(compressed) * docker.ON_DISK / docker.GB if compressed else 0.0

    def wait_for_disk(self, task_dir: Path, admitted: list, cancel=None) -> bool:
        """The disk gate: the host's /tmp first, then Docker's data root with room for the task's prebuilt image when
        it is not here yet (`image_sizes`), beside the room other environments still to pull hold (`admitted` gets
        the key of the room this one holds, given back by `done`)."""
        wait_s = float(self.settings.get("disk_gate_wait_s") or DISK_GATE_WAIT_S)
        cancel = cancel or self.stopping
        if not docker.wait_for_disk(HOST_TMP, self._gate("tmp_gate_gb", TMP_GATE_GB), wait_s, cancel=cancel,
                                    log=self.log):
            return False
        if cancel.is_set():                              # the run ended between the gates
            return False
        image = docker.task_image(task_dir)
        return docker.wait_for_disk(self.docker_root(), self._gate("disk_gate_gb", DISK_GATE_GB), wait_s,
                                    image=image, image_gb=self.image_gb(image) if image else 0.0, pulls=self.pulls,
                                    admitted=admitted, cancel=cancel, log=self.log)

    def disk_held(self, task_dir: Path) -> str:
        """Which gate held an environment back once wait_for_disk gave up."""
        tmp_gate = self._gate("tmp_gate_gb", TMP_GATE_GB)
        if tmp_gate > 0 and docker.free_gb(HOST_TMP) < tmp_gate:
            return f"the host's {HOST_TMP} stayed under {tmp_gate:g} GB free"
        gate = self._gate("disk_gate_gb", DISK_GATE_GB)
        held = f"the Docker data root {self.docker_root()} stayed under {gate:g} GB free"
        image = docker.task_image(task_dir)
        image_gb = self.image_gb(image) if image else 0.0
        if image_gb and docker.image_size(image) is None:
            held += f" with {image_gb:.1f} GB more for the task's image {image}, not here yet"
        with self.pulls.lock:
            taken = self.pulls.taken_gb()
        if taken:
            held += f", beside {taken:.1f} GB held for the images other environments are still to pull"
        return held

    def cleanup_images(self, task_dir: Path) -> None:
        docker.cleanup_images(self.cleanup, task_dir, self.jobs_dir / ".locks",
                              threshold_gb=float(self.settings.get("rmi_threshold_gb") or 1.0))

    # --- what each environment is given --------------------------------------------------------------------------
    def environment_config(self) -> dict:
        given = self.settings.get("environment") or {}
        if not isinstance(given, dict):
            raise ValueError("environment.harbor.environment is a mapping (Harbor's environment config)")
        env = {"delete": True, **{k: v for k, v in given.items() if v is not None}}
        if env.get("extra_docker_compose"):
            env["extra_docker_compose"] = [str(self.path(p)) for p in env["extra_docker_compose"]]
        if env.get("mounts"):
            env["mounts"] = [{**m, "source": str(self.path(m["source"]))} if m.get("type", "bind") == "bind" and
                             m.get("source") else m for m in env["mounts"]]
        return env

    def start_request(self, task_dir: Path, trials_dir: Path, name: str) -> dict:
        s = self.settings
        agent = {"override_timeout_sec": s.get("agent_timeout_sec"),
                 "override_setup_timeout_sec": s.get("setup_timeout_sec"),
                 "extra_allowed_hosts": list(self.allowed_hosts) or None}
        return {"op": "start", "task_dir": str(task_dir), "trials_dir": str(trials_dir), "trial_name": name,
                "agent": {k: v for k, v in agent.items() if v is not None}, "environment": self.environment_config(),
                "verifier": {k: v for k, v in (s.get("verifier") or {}).items() if v is not None},
                "trial": {k: float(s[k]) for k in MULTIPLIERS if s.get(k) is not None}}

    def open(self, task_dir: Path, name: str | None = None) -> "HarborEnvironment":
        """The environment of one task, not started yet."""
        return HarborEnvironment(task_dir, owner=self, name=name)

    @contextlib.contextmanager
    def session(self, task_dir: Path, name: str | None = None):
        """One task's environment, started (the disk gate first) and stopped after, however the block ends."""
        env = self.open(task_dir, name)
        try:
            env.start()
            yield env
        finally:
            env.stop()

    @staticmethod
    def _redactions(patterns) -> list[str]:
        """verifier_redact: regexes removed from the verifier's output the feedback and an infra verdict quote (what
        the learner must not read, a test's expected answer), the Harbor runner's setting."""
        patterns = [patterns] if isinstance(patterns, str) else list(patterns or [])
        for pattern in patterns:
            try:
                re.compile(str(pattern))
            except re.error as exc:
                raise ValueError(f"environment.harbor.verifier_redact: {pattern!r} is no regular expression "
                                 f"({exc})") from None
        return [str(p) for p in patterns]

    def redact(self, text: str) -> str:
        for pattern in self.redactions:
            text = re.sub(pattern, "[redacted]", text)
        return text

    def verdict(self, verified: dict) -> dict:
        """What a verifier's answer is for a judge, as Hermit's Harbor runner read a trial (hermit.environment.
        harbor_errors): the reward (`reward_key`, else the one reward, else their mean), the rewards, the feedback
        (feedback.txt, else the verifier's output's tail), a status and its cause: ok; `timeout` for a verifier that
        ran out of its time on the agent's solution (solution_timed_out, scored 0; infra with verifier_timeout: infra);
        `agent_error` for one that left no reward Harbor could read (verifier_failed, scored 0, as Harbor grades it);
        `infra` for an environment that failed under the verifier, a verifier that ran no test (its tools never arrived,
        the host's disk full) or no exception and no reward: not the agent's result."""
        rewards = verified.get("rewards") or None
        exception = verified.get("exception") or None
        reward = (rewards or {}).get(self.reward_key)   # the named reward only: none there is the runner's infra
        reward = float(reward) if reward is not None else None
        # Harbor's trial as the runner read its result.json: the reward under its key, the exception Harbor recorded
        result = {"verifier_result": {"rewards": dict(rewards or {})},
                  "exception_info": {"exception_type": exception.get("type"), "exception_message":
                                     exception.get("message") or ""} if exception else None}
        read = errors.classify(result, reward_key=self.reward_key)
        read = errors.verifier_timeout_verdict(read, self.verifier_timeout)
        logs = Path(verified.get("verifier_dir") or "") if verified.get("verifier_dir") else None
        read = errors.verifier_verdict(read, logs, self.redactions)
        feedback = ""
        for name in ("feedback.txt", "test-stdout.txt"):
            if logs is not None and (logs / name).is_file() and not (logs / name).is_symlink():
                feedback = self.redact((logs / name).read_text(encoding="utf-8", errors="replace"))[-FEEDBACK_CHARS:]
                break
        passed = None if self.pass_reward is None else (reward is not None and reward >= self.pass_reward)
        return {"reward": reward, "rewards": rewards, "status": read["status"], "failure_cause": read["failure_cause"],
                "passed": passed, "exception": exception,
                "exception_message": self.redact(str(read["exception_message"] or "")),
                "feedback": feedback, "verifier_dir": verified.get("verifier_dir"),
                "trial_dir": verified.get("trial_dir")}


class _Driver:
    """The driver process and its JSON lines: requests by id, answered in any order by a reader thread."""

    def __init__(self, python: str, log_path: Path, env: dict):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(log_path, "ab")
        self.proc = subprocess.Popen([python, "-I", str(DRIVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.log, env=env, start_new_session=True, cwd=str(log_path.parent))
        self.lock = threading.Lock()
        self.waiting: dict[int, queue.Queue] = {}
        self.next_id = 0
        self.dead: str | None = None
        self.outbox: queue.Queue = queue.Queue()          # the requests to write: a writer thread of its own, so that
        self.reader = threading.Thread(target=self._read, daemon=True)   # a hung driver's full pipe holds no caller
        self.reader.start()                                              # past its request's timeout
        self.writer = threading.Thread(target=self._write, daemon=True)
        self.writer.start()

    def _write(self) -> None:
        while True:
            item = self.outbox.get()
            if item is None:
                return
            rid, data = item
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                box = self.waiting.pop(rid, None)
                if box is not None:
                    box.put({"ok": False, "error": f"the Harbor driver is gone: {exc}", "type": "DriverGone"})

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            box = self.waiting.pop(reply.get("id"), None)
            if box is not None:
                box.put(reply)
        self.dead = f"the Harbor driver exited ({self.proc.wait()})"
        for box in list(self.waiting.values()):
            box.put({"ok": False, "error": self.dead, "type": "DriverGone"})
        self.waiting.clear()

    def request(self, op: str, timeout: float | None, **fields) -> dict:
        box: queue.Queue = queue.Queue()
        with self.lock:
            if self.dead:
                raise EnvironmentUnavailable(self.dead, "harbor_failed")
            self.next_id += 1
            rid = self.next_id
            self.waiting[rid] = box
        self.outbox.put((rid, (json.dumps({"id": rid, "op": op, **fields}, default=str) + "\n").encode()))
        try:
            reply = box.get(timeout=timeout)
        except queue.Empty:
            self.waiting.pop(rid, None)
            raise TimeoutError(f"the Harbor driver did not answer {op} in {timeout:.0f} s") from None
        if not reply.get("ok"):
            if reply.get("type") == "DriverGone":
                raise EnvironmentUnavailable(str(reply.get("error")), "harbor_failed")
            error = RuntimeError(str(reply.get("error") or f"{op} failed"))
            error.kind = reply.get("type")
            raise error
        return reply

    def close(self, timeout: float) -> bool:
        """Close its stdin (it stops its trial and exits); True when it exited by itself, else it is killed. A writer
        still blocked on a driver that stopped reading holds the stream: the driver is killed first, never waited on
        through a close that would block on the writer's lock."""
        self.outbox.put(None)
        self.writer.join(timeout=5)
        if self.writer.is_alive():
            self._kill()
            self.writer.join(timeout=10)
            self.reader.join(timeout=5)
            self.log.close()
            return False
        with contextlib.suppress(OSError):
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=timeout)
            done = True
        except subprocess.TimeoutExpired:
            done = False
            self._kill()
        self.reader.join(timeout=5)
        self.log.close()
        return done

    def _kill(self) -> None:
        for sig in (15, 9):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, sig)
            try:
                self.proc.wait(timeout=20)
                return
            except subprocess.TimeoutExpired:
                continue


class HarborEnvironment(Environment):
    """One task's Harbor environment (module docstring). Its workspace is the task's working directory in the
    environment, a path there, not on this machine."""
    isolation = "remote"

    def __init__(self, task_dir: Path, *, owner: HarborEnvironments, name: str | None = None,
                 limits: Limits | None = None):
        super().__init__(PurePosixPath("/"), limits)
        self.task_dir = Path(task_dir)
        self.owner = owner
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in (name or self.task_dir.name))
        self.name = f"{safe[:48]}__{uuid.uuid4().hex[:8]}"
        self.trials_dir = owner.jobs_dir / self.name
        self.driver: _Driver | None = None
        self.started: dict = {}
        self.instruction: str | None = None
        self.network_public = True
        self.plan_admitted = False      # the run holds the agent's login's plan admission around this environment
        self.setup_budget_s = SETUP_ALLOWANCE_S
        self.admitted: list = []
        self.verified: dict | None = None

    def __repr__(self) -> str:
        return f"HarborEnvironment({self.task_dir.name!r}, workspace={str(self.workspace)!r})"

    # --- the lifecycle -------------------------------------------------------------------------------------------
    def start(self) -> "HarborEnvironment":
        owner = self.owner
        if not self.task_dir.is_dir():
            raise ValueError(f"no Harbor task directory {self.task_dir}")
        if owner.stopping.is_set():
            raise EnvironmentUnavailable("the run ended: no environment started", "stopped")
        held = not owner.wait_for_disk(self.task_dir, self.admitted)
        if owner.stopping.is_set():                      # the run ended while it waited: nothing started, room back
            for key in self.admitted:
                owner.pulls.done(key)
            self.admitted = []
            raise EnvironmentUnavailable("the run ended while the disk gate held the task: no environment started",
                                         "stopped")
        if held:
            raise EnvironmentUnavailable(f"{owner.disk_held(self.task_dir)}: no environment started", "disk")
        with owner._lock:
            owner.live.add(self)
        try:                                             # this machine's failing: infra, never run again (host_failed)
            self.trials_dir.mkdir(parents=True, exist_ok=True)
            tmp = owner.jobs_dir / "tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            env = {**os.environ, "TMPDIR": str(tmp)}
            self.driver = _Driver(owner.python, self.trials_dir / "driver.log", env)
        except OSError as exc:
            raise EnvironmentUnavailable(f"this machine could not start the environment's driver: "
                                         f"{type(exc).__name__}: {exc}", "host_failed") from None
        try:
            started = self.driver.request(**owner.start_request(self.task_dir, self.trials_dir, self.name),
                                          timeout=float(owner.settings.get("start_timeout_s") or START_TIMEOUT_S))
        except (RuntimeError, TimeoutError) as exc:
            kind = "TimeoutError" if isinstance(exc, TimeoutError) else getattr(exc, "kind", None)
            raise EnvironmentUnavailable(f"the task's environment did not start: {exc}"[-3000:],
                                         errors.start_cause(kind, str(exc))) from None
        self.started = started
        self.workspace = PurePosixPath(started.get("workdir") or "/")
        self.instruction = started.get("instruction")           # Harbor's own, as its agents are given it
        self.network_public = (started.get("agent_network") or "public") == "public"
        kind = (owner.settings.get("environment") or {}).get("type") if isinstance(
            owner.settings.get("environment"), dict) else None
        self.same_clock = kind in (None, "docker")       # a local Docker container shares this machine's clock
        self.setup_budget_s = float(started.get("setup_timeout_sec") or 0) + SETUP_ALLOWANCE_S
        budget = started.get("agent_timeout_sec")
        self.limits = Limits(readable=self.limits.readable, hidden=self.limits.hidden, network=self.limits.network,
                             timeout_s=float(budget) if budget else self.limits.timeout_s)
        return self

    def stop(self) -> None:
        """The trial's result written and the environment down; the room its pull held given back, its image
        removed as docker_cleanup says. A driver that does not stop within STOP_TIMEOUT_S is killed and its compose
        projects taken down by name."""
        driver, self.driver = self.driver, None
        try:
            if driver is not None:
                stopped = False
                with contextlib.suppress(RuntimeError, TimeoutError, EnvironmentUnavailable):
                    driver.request("stop", timeout=STOP_TIMEOUT_S)
                    stopped = True
                if not driver.close(30 if stopped else 60) or not stopped:
                    docker.compose_down(self.name)
                self.owner.cleanup_images(self.task_dir)
        finally:
            for key in self.admitted:
                self.owner.pulls.done(key)
            self.admitted = []
            with self.owner._lock:
                self.owner.live.discard(self)

    def _live(self) -> _Driver:
        if self.driver is None:
            raise EnvironmentUnavailable("the environment is not started (or was stopped)")
        if self.verified is not None:
            raise Unsupported("the task was verified: its environment takes no more commands")
        return self.driver

    # --- the agent's operations ----------------------------------------------------------------------------------
    def path(self, relative: str | Path = "", *, follow: bool = True) -> PurePosixPath:
        """A path of the environment: a relative one in the workspace (one that leaves it is refused), an absolute
        one as it is."""
        rel = PurePosixPath(str(relative))
        if rel.is_absolute():
            return rel
        parts: list[str] = []
        for part in rel.parts:
            if part == "..":
                if not parts:
                    raise ValueError(f"{relative} lies outside the workspace")
                parts.pop()
            elif part not in (".", ""):
                parts.append(part)
        return PurePosixPath(self.workspace, *parts)

    def exec(self, command, *, cwd=None, env=None, timeout=None, stdin=None, user=None, by_fd=None) -> ExecResult:
        """A command in the task's main container (Environment.exec). `by_fd` ({a variable of `env`: the variable that
        names its descriptor}): each such secret reaches the command on a descriptor instead, out of every process's
        environment there (the driver's container_prologue)."""
        if self.limits.readable or self.limits.hidden or self.limits.network is not None:
            raise Unsupported("a Harbor environment is the task's own: it takes no readable mounts, hidden paths or "
                              "network limits of Hermit's (the task's task.toml says what it reaches)")
        line = command if isinstance(command, str) else shlex.join([str(c) for c in command])
        timeout = timeout if timeout is not None else self.limits.timeout_s
        reply = self._operate("exec", None if timeout is None else float(timeout) + 300, command=line,
                              cwd=str(self.path(cwd or "")), env={str(k): str(v) for k, v in (env or {}).items()},
                              timeout_sec=timeout, stdin=stdin, user=user, by_fd=dict(by_fd or {}))
        return ExecResult(code=int(reply.get("code", -1)), output=str(reply.get("output") or ""),
                          timed_out=bool(reply.get("timed_out")))

    def _operate(self, op: str, timeout, **fields) -> dict:
        """One of the agent's operations: a failure the environment's (its driver's, Docker's) is EnvironmentUnavailable
        (infra), as the Harbor runner took a trial's environment failing under its agent."""
        try:
            return self._live().request(op, timeout, **fields)
        except EnvironmentUnavailable:
            raise
        except (RuntimeError, TimeoutError) as exc:
            if getattr(exc, "kind", None) == "AgentPhaseOver":
                raise
            raise EnvironmentUnavailable(f"{op} failed in the task's environment: {exc}"[-3000:],
                                         errors.start_cause(getattr(exc, "kind", None), str(exc))) from None

    def upload(self, source, target) -> PurePosixPath:
        dest = self.path(target)
        self._operate("upload", 1800, source=str(Path(source).absolute()), target=str(dest))
        return dest

    def download(self, source, target) -> Path:
        target = Path(target)
        self._operate("download", 1800, source=str(self.path(source)), target=str(target.absolute()))
        return target

    def install(self, cli: str, version: str | None = None) -> str | None:
        """The CLI installed by Harbor's own installed agent of that name (codex, claude-code, ...), as Harbor's trials
        install it; the version it reports."""
        try:
            reply = self._live().request("install", float(self.started.get("setup_timeout_sec") or 900) + 60,
                                         agent=cli, version=version)
        except (RuntimeError, TimeoutError) as exc:      # the agent's setup failed: infra, as Harbor's trial grades it
            if isinstance(exc, EnvironmentUnavailable):
                raise
            raise EnvironmentUnavailable(f"{errors.INSTALL_FAILED}: {exc}"[-3000:], "sandbox_failed") from None
        return reply.get("version")

    def ensure(self, packages) -> None:
        """System packages (python3 for an adapter's hooks) installed with Harbor's own installer, under the
        environment's baseline network policy."""
        try:
            self._live().request("ensure", float(self.started.get("setup_timeout_sec") or 900) + 60,
                                 packages=list(packages))
        except (RuntimeError, TimeoutError) as exc:
            if isinstance(exc, EnvironmentUnavailable):
                raise
            raise EnvironmentUnavailable(f"the system packages' install failed: {exc}"[-3000:],
                                         "sandbox_failed") from None

    def verify(self, agent_logs: Path | str | None = None) -> dict:
        """Harbor's verifier on the agent's work (the agent's phase ends first: no command runs after it); the
        verdict (HarborEnvironments.verdict). `agent_logs`, a folder of this machine, is the trial's agent logs
        (/logs/agent), which a verifier may read."""
        if self.verified is not None:
            return self.verified
        driver = self._live()
        # Harbor's own verifier timeout (its overrides and multiplier) ends a verifier; this side waits for that and the
        # verifier's preparation and the logs beside it (verify_timeout_s overrides)
        own = self.started.get("verifier_timeout_sec")
        watchdog = float(self.owner.settings.get("verify_timeout_s") or
                         (float(own) + VERIFY_ALLOWANCE_S if own else VERIFY_TIMEOUT_S))
        try:
            verified = driver.request("verify", watchdog,
                                      agent_logs=str(Path(agent_logs).absolute()) if agent_logs else None)
        except (RuntimeError, TimeoutError) as exc:      # the driver's or this side's, not Harbor's verifier's
            said = f"{getattr(exc, 'kind', None) or type(exc).__name__}: {exc}"[-3000:]
            verified = {"rewards": None, "exception": {"type": errors.DRIVER_ERROR, "message": said}}
        verified["trial_dir"] = self.started.get("trial_dir")
        self.verified = self.owner.verdict(verified)
        return self.verified


__all__ = ["EnvironmentUnavailable", "HarborEnvironment", "HarborEnvironments", "settings_of"]
