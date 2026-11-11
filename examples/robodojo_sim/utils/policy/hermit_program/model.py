"""XPolicyLab's `Model` for a program the acting agent wrote: the policy server (this process, started by RoboDojo's
launcher in the policy environment) never imports the program. It runs it as a worker process the harness confined
(HERMIT_PROGRAM_WORKER: the command and environment `dojo.Engine` prepared: a sandbox with no network that reads the
program and nothing of RoboDojo's checkout), and passes it each observation and takes back its action chunk over
the worker's pipes:

    to the worker    a pickled {"op": "reset"} or {"op": "act", "obs": <RoboDojo's observation>}; arrays go as
                     their bytes, dtype and shape (`packed`), so the two sides need not share a numpy version
    from the worker  JSON: {"ok": true, "actions": [...]} or {"ok": false, "error": "..."}; never unpickled

A chunk is checked and completed here before it reaches the simulator (`checked`: every action whole, the keys
RoboDojo executes, their sizes, bounded numbers).
A program that fails, answers late or answers with something else is not an error of the evaluation: the robot
holds its joints for that chunk, the failure is written to HERMIT_PROGRAM_LOG, and the episode goes on to its
horizon, where it fails on its own.
"""
import json
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


LIMIT = 1.0e3                             # no joint angle, position or gripper value is near it


def checked(actions, state: dict | None = None) -> list[dict]:
    """The program's chunk as RoboDojo takes it: a non-empty list of at most MAX_CHUNK actions. Every action that
    reaches the simulator is whole: both arms in one mode and both grippers, each the right count of float32
    numbers within LIMIT. What the program left out is filled from `state`, the observation it answered (the
    joints, poses and grippers as they are), so an omitted key holds its value. RoboDojo raises on an action
    with a key missing and then drops the layout unscored, and a program must not be able to drop a layout."""
    if not isinstance(actions, list) or not 0 < len(actions) <= MAX_CHUNK:
        raise ProgramError(f"act() must return a list of 1 to {MAX_CHUNK} actions")
    state = state or {}
    out = []
    for action in actions:
        if not isinstance(action, dict):
            raise ProgramError("an action is a dict")
        modes = {mode for mode, keys in ARM.items() if any(k in action for k in keys)}
        if len(modes) > 1:
            raise ProgramError("an action mixes joint and end-effector arm keys")
        sizes = {**ARM[modes.pop() if modes else "joint"], **GRIPPER}
        unknown = sorted(set(action) - set(sizes))
        if unknown:
            raise ProgramError(f"unknown action keys {unknown}")
        one = {}
        for key, size in sizes.items():
            value = action[key] if key in action else state.get(key)
            if value is None:
                raise ProgramError(f"{key} is neither in the action nor in the observation's state")
            array = np.asarray(value, dtype=np.float64).reshape(-1)
            if array.shape != (size,) or not np.all(np.isfinite(array)) or np.any(np.abs(array) > LIMIT):
                raise ProgramError(f"{key} must be {size} finite numbers within {LIMIT:g}")
            one[key] = array.astype(np.float32)
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
                return checked(answer.get("actions"), (self.obs or {}).get("state"))
            except Exception as exc:      # noqa: BLE001
                self._fail(exc)
        return [self._hold()]

    def _hold(self) -> dict:
        """The joints where they are: what the robot does for a chunk its program did not give."""
        state = (self.obs or {}).get("state") or {}
        try:
            return checked([{}], state)[0]
        except Exception:                 # noqa: BLE001 - an observation without the state: a whole action still
            return {key: np.zeros(size, dtype=np.float32) for key, size in {**ARM["joint"], **GRIPPER}.items()}
