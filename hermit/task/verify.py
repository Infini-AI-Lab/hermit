"""The judge's verdict on one answer: a score in [0, 1], feedback a learning method can read, and anything else the
judge records (JudgeResult). How a task is judged is the task set's own, not the experiment's: a judge function in
this process on the answer it reads from where the agent could not (`judge.mode: host`), or in a sandbox beside the
agent's (`shared` over the agent's work, `separate` over what is collected; hermit.isolate builds those).

    verifier = judge_verifier(score)          # score(task_row, output) -> JudgeResult | dict | float
    verdict = verifier(task, output)          # JudgeResult

A task directory's verifier is its own script (`script` below, `judge: {path: hermit.task.verify, name: script,
mode: shared | separate}`), as Harbor runs it: `tests/test.sh` with the tests at /tests, in the agent's workspace
(its cwd; HERMIT_WORKSPACE) or a fresh copy of what is collected, the agent's output in the file HERMIT_OUTPUT names,
writing /logs/verifier/reward.txt (one number) or reward.json (named numbers; `reward` is the score, else their
mean), and, beyond Harbor, /logs/verifier/feedback.txt: what a learning method reads (else the script's output's
tail). It runs only in the judge's sandbox, which shows the task's tests and a fresh log directory at those names.

A task in its own Harbor environment (`environment.harbor`: hermit.environment.harbor) is verified by Harbor's
verifier in that environment, after the agent, by the run (hermit.trainer.rollout); `environment` below is the judge
that reads that verdict (`judge: {path: hermit.task.verify, name: environment}`): the reward is the score, whatever the
session's end (its timeout, a CLI that exited with an error: Harbor verifies all of them, and its own job mean scores
them), 0 when the verifier left none (`verifier_failed`) or ran out of its time on the agent's solution
(`solution_timed_out`); a rollout that stayed infra after the run's runs again (its environment failed, its verifier
ran no test, its session never started: `unscored`) is not the agent's result: no score (Unscored), so the run records
a failed run (judge_error), as Hermit's Harbor runner's judge refused one. `passed` says whether the reward reached
the task set's pass reward.
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
import subprocess
from typing import Callable, Protocol

from hermit.common import RunOutput
from hermit.task.data import JudgeResult, normalise_judge
from hermit.task.task import Task


class Verifier(Protocol):
    """Called with the whole Task. `takes_task = True` tells hermit.trainer.rollout so (a callable without it is taken
    for a bare judge and wrapped by judge_verifier)."""
    takes_task: bool

    def __call__(self, task: Task, output: RunOutput) -> JudgeResult: ...


TESTS = "/tests"                      # where the judge's sandbox shows a task's tests (Harbor's path)
LOGS = "/logs/verifier"               # ... and the directory the script writes its reward to
SANDBOX_ENV = "HERMIT_VERIFIER_SANDBOX"   # set by the judge's sandbox that shows them (hermit.isolate.SandboxedJudge)
FEEDBACK_CHARS = 4000
TAIL = 1500                           # characters of the verifier's output in the feedback of a task that did not pass


class Unscored(RuntimeError):
    """A run that is not the agent's result (its environment failed): no score is the agent's."""


def judge_verifier(judge: Callable) -> Verifier:
    """A verifier over a judge as a task set writes it: called with the task's row (Task.row: its public half and, under
    `_private`, what only the judge reads) and the agent's output; whatever it returns, normalised."""
    def verify(task: Task, output: RunOutput) -> JudgeResult:
        return normalise_judge(judge(task.row(), output))
    verify.judge = judge                       # the judge it wraps (a sandboxed one keeps its own state)
    verify.takes_task = True                   # called with a Task (hermit.trainer.rollout tells it from a bare judge)
    return verify


def read_reward(logs: Path, output_tail: str = "") -> JudgeResult:
    """The verdict a verifier script left in `logs`: reward.json (named numbers: `reward` is the score, else their
    mean) over reward.txt (one number); feedback.txt, else the script's output's tail. No reward file: an error."""
    logs = Path(logs)
    rewards: dict = {}
    if (logs / "reward.json").is_file():
        value = json.loads((logs / "reward.json").read_text())
        # As Harbor reads it (harbor/verifier/verifier.py): finite numbers, a boolean among them (Python's bool is an
        # int, so Harbor takes `true` for 1)
        if not isinstance(value, dict) or not value or \
                not all(isinstance(v, (int, float)) and math.isfinite(v) for v in value.values()):
            raise ValueError("reward.json must be an object of named finite numbers")
        rewards = {str(k): float(v) for k, v in value.items()}
        score = rewards.get("reward", sum(rewards.values()) / len(rewards))
    elif (logs / "reward.txt").is_file():
        score = float((logs / "reward.txt").read_text().strip())
        if not math.isfinite(score):                      # as Harbor: a NaN or infinite reward is no reward
            raise ValueError(f"reward.txt holds no finite number: {score}")
    else:
        raise RuntimeError(f"the verifier wrote no reward ({LOGS}/reward.txt or reward.json): "
                           f"{output_tail[-FEEDBACK_CHARS:]}")
    feedback = (logs / "feedback.txt").read_text(errors="replace") if (logs / "feedback.txt").is_file() \
        else output_tail
    extra = {"rewards": rewards} if rewards else {}
    return JudgeResult(float(score), feedback[-FEEDBACK_CHARS:], extra)


def resolve_env(env: dict) -> dict[str, str]:
    """task.toml's [verifier.env] as Harbor resolves it (harbor/utils/env.py): `${NAME}` and `${NAME:-default}` from
    this process's environment (the judge sandbox's: what environment.env lets in), anything else as written."""
    out = {}
    for key, value in env.items():
        match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*))?\}", str(value))
        if not match:
            out[str(key)] = str(value)
        elif match.group(1) in os.environ:
            out[str(key)] = os.environ[match.group(1)]
        elif match.group(2) is not None:
            out[str(key)] = match.group(2)
        else:
            raise RuntimeError(f"[verifier.env] {key} needs {match.group(1)}, which the judge's sandbox does not have "
                               "(environment.env)")
    return out


def script(task: dict, output: RunOutput) -> JudgeResult:
    """A task directory's own verifier, `tests/test.sh`, run in the judge's sandbox (module docstring)."""
    if os.environ.get(SANDBOX_ENV) != "1" or not Path(TESTS, "test.sh").is_file():
        raise RuntimeError("the script verifier runs in the judge's sandbox, which shows the task's tests at /tests "
                           "(judge.mode: shared or separate, with the run's sandbox on)")
    private = task.get("_private") or {}
    timeout = (private.get("verifier") or {}).get("timeout_sec")
    workspace = Path(str((output.extra or {}).get("workspace") or os.getcwd()))
    answer = Path(LOGS) / "agent_output.json"
    answer.write_text(json.dumps(output.output, ensure_ascii=False, default=str))
    env = {**os.environ, **resolve_env((private.get("verifier") or {}).get("env") or {}),
           "HERMIT_OUTPUT": str(answer), "HERMIT_WORKSPACE": str(workspace)}
    try:
        done = subprocess.run(["bash", f"{TESTS}/test.sh"], cwd=str(workspace if workspace.is_dir() else Path.cwd()),
                              env=env, capture_output=True, text=True, timeout=float(timeout) if timeout else None)
        tail = (done.stdout + done.stderr)[-FEEDBACK_CHARS:]
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"") + (exc.stderr or b"")
        tail = out.decode(errors="replace") if isinstance(out, bytes) else str(out)
        raise RuntimeError(f"the verifier ran longer than its timeout_sec={timeout}: "
                           f"{tail[-FEEDBACK_CHARS:]}") from None
    verdict = read_reward(Path(LOGS), tail)
    verdict.extra["exit_code"] = done.returncode
    return verdict


def environment(task: dict, output: RunOutput) -> JudgeResult:
    """The verdict of the task's own environment's verifier (module docstring), which the run put in
    `output.extra["verified"]` (hermit.environment.harbor.HarborEnvironments.verdict)."""
    verdict = (output.extra or {}).get("verified")
    if not isinstance(verdict, dict):
        raise RuntimeError("the output holds no verdict of the task's environment: the environment judge is for a task "
                           "set in its own environments (environment.harbor)")
    status, cause = verdict.get("status") or "infra", verdict.get("failure_cause")
    exception = verdict.get("exception") or {}
    why = (output.extra or {}).get("unscored")
    if status == "infra" or why:
        said = str(verdict.get("exception_message") or exception.get("message") or "")[:600]
        raise Unscored(f"the rollout is not the agent's result ({why or cause or 'infra'}"
                       f"{': ' + said if said else ''}): it has no score")
    reward = verdict.get("reward")
    passed = verdict.get("passed")
    extra = output.extra or {}
    if status == "ok":
        feedback = "passed" if passed else f"reward {reward:g}" if reward is not None else "no reward"
    else:
        feedback = f"{status} ({cause}{': ' + exception['type'] if exception.get('type') else ''})"
        if cause == "solution_timed_out":
            feedback += ("; the verifier ran out of its time on the agent's solution (too slow to finish its tests, "
                         "or a process it left that hung them)")
    if extra.get("timed_out"):
        feedback += "; the agent ran out of its time budget"
    if not passed and verdict.get("feedback"):
        text = str(verdict["feedback"]).strip()
        feedback += "\nverifier output (tail):\n" + (text[-TAIL:] if len(text) > TAIL else text)
    return JudgeResult(float(reward) if reward is not None else 0.0, feedback,
                       {"status": status, "passed": passed, "rewards": verdict.get("rewards"),
                        **({"failure_cause": cause} if cause else {}),
                        **({"trial_dir": verdict["trial_dir"]} if verdict.get("trial_dir") else {})})
