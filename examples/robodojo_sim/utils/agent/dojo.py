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
"""
from __future__ import annotations

import ast
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
NAME = POLICY.name
BENCH = "RoboDojo"
# One simulator at a time for one engine: Isaac Sim takes a GPU's memory, and the result path is per run
_ONE = threading.Lock()


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
    except SyntaxError as exc:
        return f"SyntaxError: {exc}"
    acts = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "act"]
    if not acts:
        return "the program defines no top-level `def act(obs)`"
    if len(acts[0].args.args) != 1:
        return "`act` takes one argument, the observation"
    return None


class Engine:
    """RoboDojo at `root` (a checkout with its Assets, the simulator's conda environment `eval_env` and a policy
    environment `policy_env` that has XPolicyLab's server dependencies and numpy)."""

    def __init__(self, root: Path | str, *, policy_env: str = "RoboDojo", eval_env: str = "RoboDojo",
                 env_cfg: str = "arx_x5", action_type: str = "joint", gpu: str | int = 0,
                 timeout_s: float = 6 * 3600, step_timeout_s: float = 60.0):
        self.root = Path(root).expanduser().resolve()
        if not (self.root / "scripts" / "robodojo.sh").is_file():
            raise FileNotFoundError(f"{self.root}/scripts/robodojo.sh: not a RoboDojo checkout")
        self.policy_env, self.eval_env, self.env_cfg = policy_env, eval_env, env_cfg
        self.action_type, self.gpu = action_type, str(gpu)
        self.timeout_s, self.step_timeout_s = float(timeout_s), float(step_timeout_s)

    def validate(self, program: Path) -> str | None:
        return check_program(Path(program).read_text(errors="replace"))

    def _installed(self) -> None:
        target = self.root / "XPolicyLab" / "policy" / NAME
        if not target.is_dir() or policy_digest(target) != policy_digest(POLICY):
            raise RuntimeError(f"{target} is not the harness's policy as it is here ({POLICY}): install it "
                               f"(python utils/policy/install.py --root {self.root}); the episode loop that runs "
                               "in the simulator's process must be the harness's")

    def _worker(self, program: Path, out: Path):
        """The program's worker, confined: (what to close after the run; HERMIT_PROGRAM_WORKER's value). It reads
        the worker and the program's directory; it has no network and sees nothing of RoboDojo's checkout."""
        from hermit.sandbox import Sandbox, framework_env
        if Path(program).resolve().is_relative_to(self.root):
            raise ValueError(f"{program} lies inside RoboDojo's checkout {self.root}: keep programs elsewhere")
        keep = ["PYTHONPATH", "PYTHONDONTWRITEBYTECODE"]
        box = Sandbox(enabled=True, role="robodojo-program", network="isolated", env_allow=keep, framework=False,
                      readable=[WORKER.parent, Path(program).parent], record_dir=out / "sandbox")
        try:
            argv, env = box.wrap([sys.executable, str(WORKER), str(program)], env=framework_env(no_bytecode=True),
                                 keep=keep)
        except BaseException:
            box.close()
            raise
        return box, json.dumps({"argv": argv, "env": env, "stderr": str(out / "program.stderr.log")})

    def run(self, program: Path, *, task: str, layout_seed: int, layouts: int, out: Path) -> list[dict]:
        """Run `program` on the first `layouts` layouts of layout set `layout_seed`; one record per layout
        RoboDojo scored: layout_id, task_success, score. A layout RoboDojo skipped as unstable has no record."""
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        self._installed()
        refused = self.validate(program)
        if refused:
            raise ValueError(refused)
        run_id = f"hermit-{uuid.uuid4().hex[:12]}"
        label = f"hermit-{hashlib.sha256(Path(program).read_bytes()).hexdigest()[:12]}"
        box, worker = self._worker(Path(program), out)
        env = {**os.environ, "ROBODOJO_RUN_ID": run_id, "HERMIT_PROGRAM_WORKER": worker,
               "HERMIT_PROGRAM_LOG": str(out / "program.jsonl"), "HERMIT_PROGRAM_STEP_TIMEOUT": str(self.step_timeout_s)}
        cmd = ["bash", "scripts/robodojo.sh", "eval", "--policy-dir", f"XPolicyLab/policy/{NAME}", "--task", task,
               "--ckpt", label, "--policy-env", self.policy_env, "--eval-env", self.eval_env,
               "--eval-num", str(int(layouts)), "--env-cfg", self.env_cfg, "--action-type", self.action_type,
               "--seed", str(int(layout_seed)), "--policy-gpu", self.gpu, "--env-gpu", self.gpu]
        started = time.time()
        results = self.root / "eval_result" / BENCH / task / NAME
        try:
            with _ONE, open(out / "robodojo.log", "w") as log:
                proc = subprocess.Popen(cmd, cwd=self.root, env=env, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=True)
                try:
                    proc.wait(timeout=self.timeout_s)
                except BaseException:
                    _stop(proc)             # the policy server and the simulator are its children
                    if not isinstance(sys.exc_info()[1], subprocess.TimeoutExpired):
                        raise
                    log.write(f"\nthe evaluation was stopped after {self.timeout_s:.0f} s\n")
            found = sorted(results.glob(f"*/{int(layout_seed)}_ckpt_name={label},*/{run_id}/_result.json"))
            records, summary = [], None
            if found:
                summary = json.loads(found[-1].read_text())
                for detail in (summary.get("details") or {}).values():
                    records.append({"layout_id": int(detail["layout_id"]), "task_success": bool(detail["success"]),
                                    "score": float(detail.get("score") or 0.0)})
                shutil.copytree(found[-1].parent, out / "robodojo", dirs_exist_ok=True)   # the result and videos
                shutil.rmtree(found[-1].parent, ignore_errors=True)       # nothing of a run stays in the checkout
            records.sort(key=lambda r: r["layout_id"])
            (out / "records.json").write_text(json.dumps(
                {"task": task, "layout_seed": int(layout_seed), "layouts": int(layouts), "wall_s": time.time() - started,
                 "returncode": proc.returncode, "found_result": bool(found), "records": records}, indent=1))
            return records
        finally:
            box.close()


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
