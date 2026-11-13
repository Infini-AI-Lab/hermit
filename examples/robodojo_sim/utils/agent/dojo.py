"""RoboDojo-Sim (github.com/RoboDojo-Benchmark/RoboDojo) as the engine that scores a program: one evaluation is
RoboDojo's own `scripts/robodojo.sh eval` of the harness's XPolicyLab policy (utils/policy/hermit_program), whose
server passes each observation to the program and its action chunk back.

Who runs where. RoboDojo's simulator process holds the task's privileged state (the success check over object
poses, `task/RoboDojo/tasks/*.py`), and its checkout holds the saved layouts of every evaluation seed
(`Assets/Eval_Layout`), the success conditions and the results. A policy on the same file system could read all of
it, so the program is never imported by the policy server: it runs as a worker (utils/policy/worker.py) in a
bubblewrap sandbox of its own with no network, which reads the program's directory and the worker, and nothing of
RoboDojo's checkout. What reaches it is the observation RoboDojo sends a policy. The episode loop inside the
simulator's process (hermit_program/deploy.py) and the policy's server (model.py) are the harness's: the installed
copies must be byte for byte the ones here, or the evaluation is refused.

An evaluation is a layout set (RoboDojo's `--seed`: a directory of saved layouts) and the first N of its layouts
(`--eval-num`); development and held-out evaluations use different layout sets (utils/prepare.py).

Where Isaac Sim does not install on the host, the simulator runs in RoboDojo's container (`image`, ROBODOJO_IMAGE)
and the policy server, with the program's worker, on the host (split_eval.sh): the same policy, the same result.
"""
from __future__ import annotations

import ast
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
POLICY = HERE.parent / "policy" / "hermit_program"       # the harness's XPolicyLab policy
WORKER = HERE.parent / "policy" / "worker.py"
SPLIT = HERE / "split_eval.sh"                           # the simulator in RoboDojo's container
NOT_STARTED = (70, 125, 126, 127)                        # split_eval.sh: no policy server; docker run's own failures
NAME = POLICY.name
BENCH = "RoboDojo"
# One simulator at a time for a GPU, across the service's sessions and the judge (two processes): Isaac Sim takes
# the GPU's memory, and a second one would fail the first one's layouts
_ONE = threading.Lock()


class Stopped(RuntimeError):
    """The engine was stopped (its service is stopping): no evaluation is started."""


class NotStarted(RuntimeError):
    """The container or the policy server did not start: the host's failure, which no program causes (its worker
    starts with the first episode), so it is reported rather than scored as failed layouts."""


def policy_digest(folder: Path) -> str:
    """The digest of a policy directory's files (names and contents; bytecode aside)."""
    digest = hashlib.sha256()
    for path in sorted(p for p in Path(folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(str(path.relative_to(folder)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def install(root: Path) -> Path:
    """The harness's policy into RoboDojo's checkout (XPolicyLab imports `XPolicyLab.policy.<name>` from there)."""
    target = Path(root) / "XPolicyLab" / "policy" / NAME
    if not target.parent.is_dir():
        raise FileNotFoundError(f"{target.parent}: not a RoboDojo checkout with its XPolicyLab submodule")
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(POLICY, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def check_program(source: str) -> str | None:
    """None, or why the program is refused before a simulator is started for it: it must parse and define
    `act(obs)`. This is a convenience, not a boundary: the sandbox is."""
    try:
        tree = ast.parse(source)
    except Exception as exc:               # noqa: BLE001 - SyntaxError, a NUL byte's ValueError, RecursionError
        return f"{type(exc).__name__}: {exc}"
    acts = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "act"]
    if not acts:
        return "the program defines no top-level `def act(obs)`"
    if len(acts[0].args.args) != 1:
        return "`act` takes one argument, the observation"
    return None


class Engine:
    """RoboDojo at `root` (a checkout with its Assets, the simulator's conda environment `eval_env` and a policy
    environment `policy_env`, a conda environment's name or an environment's path, that has XPolicyLab's server
    dependencies and numpy). With `image` (default: ROBODOJO_IMAGE) the simulator runs in that container image
    instead of `eval_env`, its caches under `cache` (default: ROBODOJO_CACHE, or .cache/hermit-container in the
    checkout)."""

    def __init__(self, root: Path | str, *, policy_env: str = "RoboDojo", eval_env: str = "RoboDojo",
                 env_cfg: str = "arx_x5", action_type: str = "joint", gpu: str | int = 0,
                 timeout_s: float = 6 * 3600, step_timeout_s: float = 60.0, image: str | None = None,
                 cache: Path | str | None = None):
        self.root = Path(root).expanduser().resolve()
        if not (self.root / "scripts" / "robodojo.sh").is_file():
            raise FileNotFoundError(f"{self.root}/scripts/robodojo.sh: not a RoboDojo checkout")
        self.policy_env, self.eval_env, self.env_cfg = policy_env, eval_env, env_cfg
        self.action_type, self.gpu = action_type, str(gpu)
        self.timeout_s, self.step_timeout_s = float(timeout_s), float(step_timeout_s)
        self.image = image if image is not None else os.environ.get("ROBODOJO_IMAGE") or None
        self.cache = Path(cache or os.environ.get("ROBODOJO_CACHE")
                          or self.root / ".cache" / "hermit-container").expanduser().resolve()
        if self.image:                     # a missing image would otherwise show as layouts with no result
            try:
                found = subprocess.run(["docker", "image", "inspect", self.image], capture_output=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"ROBODOJO_IMAGE={self.image}: docker cannot be run ({exc})") from exc
            if found.returncode != 0:
                raise RuntimeError(f"ROBODOJO_IMAGE={self.image}: no such image (docker build -t {self.image} . "
                                   "in the RoboDojo checkout)")
        self.running: set[subprocess.Popen] = set()
        self._guard, self.stopping = threading.Lock(), False
        self._idle, self._active = threading.Condition(), 0      # the started runs not yet cleaned up

    def stop(self) -> None:
        """Stop the evaluations this engine is running and start no other (the service's, when it is stopped): a
        run waiting for the GPU is refused once it has it (`run`: Stopped)."""
        with self._guard:
            self.stopping = True
            for proc in list(self.running):
                _stop(proc)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until no started run is left, its cleanup included (the container's removal, nothing of it in the
        checkout): a service that exits right after stop() would otherwise leave that to chance. A run still waiting
        for its GPU has started nothing and is not waited for: it finds the engine stopping when it gets the GPU."""
        with self._idle:
            return self._idle.wait_for(lambda: not self._active, timeout)

    def validate(self, program: Path) -> str | None:
        return check_program(Path(program).read_text(errors="replace"))

    def _installed(self) -> None:
        target = self.root / "XPolicyLab" / "policy" / NAME
        if not target.is_dir() or policy_digest(target) != policy_digest(POLICY):
            raise RuntimeError(f"{target} is not the harness's policy as it is here ({POLICY}): install it "
                               f"(python utils/policy/install.py --root {self.root}); the episode loop that runs "
                               "in the simulator's process must be the harness's")

    def _worker(self, staged: Path, out: Path, private: Path):
        """The program's worker, confined: (what to close after the run; HERMIT_PROGRAM_WORKER's value). It reads
        the worker and `staged`, a directory that holds the program and nothing else (not the run's output, where
        the held-out results are written while it runs); it has no network, and the sandbox's own check finds
        RoboDojo's checkout and the run's output missing inside (a checkout under a system directory the sandbox
        shows whole would be refused there)."""
        from hermit.sandbox import Sandbox, framework_env
        keep = ["PYTHONPATH", "PYTHONDONTWRITEBYTECODE"]
        box = Sandbox(enabled=True, role="robodojo-program", network="isolated", env_allow=keep, framework=False,
                      readable=[WORKER.parent, staged], probe_absent=[self.root, out, private],
                      record_dir=private / "sandbox")
        try:
            argv, env = box.wrap([sys.executable, str(WORKER), str(staged / "policy.py")],
                                 env=framework_env(no_bytecode=True), keep=keep)
        except BaseException:
            box.close()
            raise
        return box, json.dumps({"argv": argv, "env": env, "stderr": str(out / "program.stderr.log")})

    def run(self, program: Path, *, task: str, layout_seed: int, layouts: int, out: Path,
            private: Path | None = None) -> list[dict]:
        """Run `program` on layout set `layout_seed` until RoboDojo has scored `layouts` layouts; one record per
        scored layout: layout_id, task_success, score. RoboDojo takes the set's layouts from the first and replaces
        one it abandons (an unstable scene) by the next, so a record's layout_id can be `layouts` or more: a caller
        that scores counts the ids below `layouts` and treats a missing one as failed (utils/judge.py).

        `out` receives what the program's author may see (the result, the videos, the program's own log and
        stderr); `private` (default: a directory beside `out`) the rest: RoboDojo's log and the sandbox's record,
        which name host paths."""
        out = Path(out)
        private = Path(private) if private else out.parent / f"{out.name}.private"
        for path in (out, private):                 # refused before anything is made there
            if path.resolve().is_relative_to(self.root):
                raise ValueError(f"{path} lies inside RoboDojo's checkout {self.root}: keep the runs elsewhere")
        self._installed()
        refused = self.validate(program)
        if refused:
            raise ValueError(refused)
        out.mkdir(parents=True, exist_ok=True)
        private.mkdir(parents=True, exist_ok=True)
        return self._evaluate(program, task, layout_seed, layouts, out, private)

    def _evaluate(self, program: Path, task: str, layout_seed: int, layouts: int, out: Path,
                  private: Path) -> list[dict]:
        run_id = f"hermit-{uuid.uuid4().hex[:12]}"
        code = Path(program).read_bytes()
        label = f"hermit-{hashlib.sha256(code).hexdigest()[:12]}"
        # The program alone, beside the run's directories and in neither: the sandbox shows a readable path's
        # ancestors as empty directories, and the run's output and private files are to be missing inside
        staged = Path(tempfile.mkdtemp(prefix=f".{out.name}.program-", dir=out.parent))
        (staged / "policy.py").write_bytes(code)
        box, worker = self._worker(staged, out, private)
        env = {**os.environ, "ROBODOJO_RUN_ID": run_id, "HERMIT_PROGRAM_WORKER": worker,
               "HERMIT_PROGRAM_LOG": str(out / "program.jsonl"), "HERMIT_PROGRAM_STEP_TIMEOUT": str(self.step_timeout_s)}
        cmd = ["bash", "scripts/robodojo.sh", "eval", "--policy-dir", f"XPolicyLab/policy/{NAME}", "--task", task,
               "--ckpt", label, "--policy-env", self.policy_env, "--eval-env", self.eval_env,
               "--eval-num", str(int(layouts)), "--env-cfg", self.env_cfg, "--action-type", self.action_type,
               "--seed", str(int(layout_seed)), "--policy-gpu", self.gpu, "--env-gpu", self.gpu]
        if self.image:
            cmd = ["bash", str(SPLIT), self.image, NAME, task, label, self.policy_env, str(int(layouts)),
                   self.env_cfg, self.action_type, str(int(layout_seed)), self.gpu, run_id,
                   str(self.cache / f"gpu{self.gpu}")]      # a GPU's own: two simulators never share a cache
        started = time.time()
        results = self.root / "eval_result" / BENCH / task / NAME
        # The GPU's lock, shared by the service and the judge whatever their TMPDIR: in the checkout (the user's own,
        # where RoboDojo writes its results), opened without truncating it
        locks = self.root / ".cache" / "hermit-locks"
        lock, timed_out, started_run = None, False, False
        try:
            locks.mkdir(parents=True, exist_ok=True)
            lock = os.open(locks / f"gpu{self.gpu}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            with _ONE, open(private / "robodojo.log", "w") as log:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with self._guard:                   # started and listed at once: stop() sees every run
                    if self.stopping:
                        raise Stopped("the engine is stopping: no evaluation is started")
                    proc = subprocess.Popen(cmd, cwd=self.root, env=env, stdout=log, stderr=subprocess.STDOUT,
                                            start_new_session=True)
                    self.running.add(proc)
                    with self._idle:
                        self._active += 1
                    started_run = True
                try:
                    proc.wait(timeout=self.timeout_s)
                except BaseException:
                    _stop(proc)             # the policy server and the simulator are its children
                    if not isinstance(sys.exc_info()[1], subprocess.TimeoutExpired):
                        raise
                    timed_out = True
                    log.write(f"\nthe evaluation was stopped after {self.timeout_s:.0f} s\n")
                finally:
                    with self._guard:
                        self.running.discard(proc)
                    if self.image:          # a container the script could not stop itself (it was killed)
                        container = f"hermit-robodojo-{run_id}"
                        try:
                            rm = subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=120,
                                                check=False)
                            if rm.returncode and b"no such container" not in (rm.stderr or b"").lower():
                                log.write(f"\ncontainer {container} was not removed: "
                                          f"{(rm.stderr or b'').decode(errors='replace').strip()}\n")
                        except (OSError, subprocess.TimeoutExpired) as exc:
                            # docker did not answer: the evaluation's own outcome stands, and the log says so
                            log.write(f"\ncontainer {container} was not removed: {exc}\n")
            found = sorted(results.glob(f"*/{int(layout_seed)}_ckpt_name={label},*/{run_id}/_result.json"))
            records, summary = [], None
            if found:
                summary = json.loads(found[-1].read_text())
                for detail in (summary.get("details") or {}).values():
                    records.append({"layout_id": int(detail["layout_id"]), "task_success": bool(detail["success"]),
                                    "score": float(detail.get("score") or 0.0)})
                shutil.copytree(found[-1].parent, out / "robodojo", dirs_exist_ok=True)   # the result and videos
            for left in results.glob(f"*/*/*{run_id}*"):                  # nothing of a run stays in the checkout:
                shutil.rmtree(left, ignore_errors=True) if left.is_dir() else left.unlink(missing_ok=True)   # its
                # result directory, and the resume manifest a stopped run leaves
            records.sort(key=lambda r: r["layout_id"])
            (out / "records.json").write_text(json.dumps(
                {"task": task, "layout_seed": int(layout_seed), "layouts": int(layouts), "wall_s": time.time() - started,
                 "returncode": proc.returncode, "timed_out": timed_out, "timeout_s": self.timeout_s,
                 "found_result": bool(found), "records": records}, indent=1))
            if self.image and proc.returncode in NOT_STARTED:
                raise NotStarted(f"the evaluation could not start (split_eval.sh exit {proc.returncode}): "
                                 f"see {private / 'robodojo.log'}")
            return records
        finally:
            if lock is not None:
                os.close(lock)
            box.close()
            shutil.rmtree(staged, ignore_errors=True)
            if started_run:
                with self._idle:
                    self._active -= 1
                    self._idle.notify_all()


def failures(out: Path) -> list[dict]:
    """The program's failures the policy server recorded for a run (hermit_program/model.py)."""
    path = Path(out) / "program.jsonl"
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == "program_failed"]


def _stop(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    except ProcessLookupError:
        pass
