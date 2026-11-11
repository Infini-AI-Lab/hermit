"""XPolicyLab's `Model` for a program the acting agent wrote: the policy server (this process, started by RoboDojo's
launcher in the policy environment) never imports the program. It runs it as a worker process the harness confined
(HERMIT_PROGRAM_WORKER: the command and environment `dojo.Engine` prepared: a sandbox with no network that reads the
program and nothing of RoboDojo's checkout), and passes it each observation and takes back its action chunk over
the worker's pipes:

    to the worker    a pickled {"op": "reset"} or {"op": "act", "obs": <RoboDojo's observation>}; arrays go as
                     their bytes, dtype and shape (`packed`), so the two sides need not share a numpy version
    from the worker  JSON: {"ok": true, "actions": [...]} or {"ok": false, "error": "..."}; never unpickled

A chunk is checked here before it reaches the simulator (the keys RoboDojo executes, their sizes, finite numbers).
A program that fails, answers late or answers with something else is not an error of the evaluation: the robot
holds its joints for that chunk, the failure is written to HERMIT_PROGRAM_LOG, and the episode goes on to its
horizon, where it fails on its own.
"""
import json
import math
import os
import pickle
import select
import struct
import subprocess
import time

import numpy as np

try:                                      # XPolicyLab's base class, where the policy environment has it
    from XPolicyLab.model_template import ModelTemplate
except Exception:                         # noqa: BLE001 - the interface is the three methods below
    ModelTemplate = object

# The action keys RoboDojo executes for the bimanual ARX X5 (eval_env.py) and their sizes: joint targets or
# end-effector poses for the arms (not both in one action), and the grippers
ARM = {"joint": {"left_arm_joint_state": 6, "right_arm_joint_state": 6},
       "ee": {"left_ee_pose": 7, "right_ee_pose": 7}}
GRIPPER = {"left_ee_joint_state": 1, "right_ee_joint_state": 1}
MAX_CHUNK = 100                           # four seconds at RoboDojo's 25 Hz
STEP_TIMEOUT_S = float(os.environ.get("HERMIT_PROGRAM_STEP_TIMEOUT", 60))   # RoboDojo's client waits 120 s


class ProgramError(Exception):
    pass


def checked(actions) -> list[dict]:
    """The program's chunk as RoboDojo takes it: a non-empty list of at most MAX_CHUNK actions, each with the arm
    keys of one mode (any of them) and the grippers, finite numbers of the right count."""
    if not isinstance(actions, list) or not 0 < len(actions) <= MAX_CHUNK:
        raise ProgramError(f"act() must return a list of 1 to {MAX_CHUNK} actions")
    out = []
    for action in actions:
        if not isinstance(action, dict) or not action:
            raise ProgramError("an action is a non-empty dict")
        modes = {mode for mode, keys in ARM.items() if any(k in action for k in keys)}
        if len(modes) > 1:
            raise ProgramError("an action mixes joint and end-effector arm keys")
        sizes = {**(ARM[modes.pop()] if modes else {}), **GRIPPER}
        unknown = sorted(set(action) - set(sizes))
        if unknown:
            raise ProgramError(f"unknown action keys {unknown}")
        one = {}
        for key, value in action.items():
            values = [float(v) for v in (value if isinstance(value, (list, tuple)) else [value])]
            if len(values) != sizes[key] or not all(math.isfinite(v) for v in values):
                raise ProgramError(f"{key} must be {sizes[key]} finite numbers")
            one[key] = np.asarray(values, dtype=np.float32)
        out.append(one)
    return out


def packed(value):
    """An observation without numpy objects: an array as {"__nd__": (dtype, shape, bytes)}."""
    if isinstance(value, np.ndarray):
        array = np.ascontiguousarray(value)
        return {"__nd__": (array.dtype.str, list(array.shape), array.tobytes())}
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(k): packed(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [packed(v) for v in value]
    return value


class Worker:
    """The confined process that runs the program."""

    def __init__(self, spec: dict):
        self.proc = subprocess.Popen(spec["argv"], env=spec.get("env"), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=open(spec.get("stderr") or os.devnull, "ab"))

    def call(self, message: dict, timeout: float) -> dict:
        data = pickle.dumps(message, protocol=4)
        self.proc.stdin.write(struct.pack("<Q", len(data)) + data)
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        size = struct.unpack("<Q", self._read(8, deadline))[0]
        if size > 16 << 20:
            raise ProgramError("the program's answer is larger than 16 MiB")
        answer = json.loads(self._read(size, deadline))
        if not isinstance(answer, dict):
            raise ProgramError("the worker's answer is not an object")
        return answer

    def _read(self, count: int, deadline: float) -> bytes:
        fd, chunks = self.proc.stdout.fileno(), []
        while count:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                raise ProgramError(f"no answer within {STEP_TIMEOUT_S:.0f} s")
            chunk = os.read(fd, count)
            if not chunk:
                raise ProgramError("the program's process ended")
            chunks.append(chunk)
            count -= len(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        try:
            self.proc.kill()
            self.proc.wait(timeout=10)
        except Exception:                 # noqa: BLE001 - already gone
            pass


class Model(ModelTemplate):
    def __init__(self, model_cfg):
        self.model_cfg = model_cfg
        spec = os.environ.get("HERMIT_PROGRAM_WORKER")
        if not spec:
            raise RuntimeError("HERMIT_PROGRAM_WORKER is not set: this policy is started by the harness's engine "
                               "(examples/robodojo_sim/utils/agent/dojo.py), which confines the program")
        self.spec = json.loads(spec)
        self.log = os.environ.get("HERMIT_PROGRAM_LOG")
        self.worker, self.obs, self.episode, self.failed = None, None, -1, None

    def _note(self, **fields) -> None:
        if self.log:
            with open(self.log, "a") as handle:
                handle.write(json.dumps({"episode": self.episode, **fields}) + "\n")

    def _fail(self, error) -> None:
        """The program is out for the rest of the episode: its process may be mid-answer, so it is replaced."""
        self.failed = str(error)[:2000]
        self._note(event="program_failed", error=self.failed)
        if self.worker is not None:
            self.worker.close()
            self.worker = None

    def reset(self):
        self.episode += 1
        self.obs, self.failed = None, None
        try:
            if self.worker is None:
                self.worker = Worker(self.spec)
            answer = self.worker.call({"op": "reset"}, STEP_TIMEOUT_S)
            if not answer.get("ok"):
                raise ProgramError(answer.get("error") or "reset failed")
            self._note(event="reset")
        except Exception as exc:          # noqa: BLE001 - the program's failure, not the evaluation's
            self._fail(exc)

    def update_obs(self, obs):
        self.obs = obs

    def get_action(self):
        if self.failed is None:
            try:
                answer = self.worker.call({"op": "act", "obs": packed(self.obs)}, STEP_TIMEOUT_S)
                if not answer.get("ok"):
                    raise ProgramError(answer.get("error") or "act failed")
                return checked(answer.get("actions"))
            except Exception as exc:      # noqa: BLE001
                self._fail(exc)
        return [self._hold()]

    def _hold(self) -> dict:
        """The joints where they are: what the robot does for a chunk its program did not give."""
        state = (self.obs or {}).get("state") or {}
        hold = {}
        for key in (*ARM["joint"], *GRIPPER):
            if state.get(key) is not None:
                hold[key] = np.asarray(state[key], dtype=np.float32).reshape(-1)
        if not hold:
            hold = {key: np.zeros(1, dtype=np.float32) for key in GRIPPER}
        return hold
