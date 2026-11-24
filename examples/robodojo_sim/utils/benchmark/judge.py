"""The score of a session, on the host: its program (`policy.py`, as utils/agent/task.py:collect read it) evaluated
once in RoboDojo-Sim on the task row's held-out layout sets, which no session can run (the simulator service runs
only the development set). The score is the fraction of the held-out layouts (the first `eval_layouts` of each
set, a fixed number) that succeeded by RoboDojo's own success check. A layout with no result counts as failed,
whatever the reason: RoboDojo drops a layout whose episode raised or whose scene went unstable and scores a later
one in its place, and a program that could make that happen must not gain by it. So a later layout's result is
not counted either.

The engine's settings come from the launcher's environment: ROBODOJO_ROOT (the checkout), ROBODOJO_OUT (where the
runs go), ROBODOJO_GPU (or ROBODOJO_JUDGE_GPU, a GPU of the judge's own, so that it does not wait for the sessions'
simulator), ROBODOJO_POLICY_ENV and ROBODOJO_EVAL_ENV (the environments), ROBODOJO_ENV_CFG, ROBODOJO_ACTION_TYPE,
and ROBODOJO_IMAGE and ROBODOJO_CACHE (the simulator in RoboDojo's container: dojo.Engine).

RoboDojo puts no time limit on a set; the judge stops one after ROBODOJO_JUDGE_TIMEOUT seconds (JUDGE_TIMEOUT_S, two
days: a set of the longest task's 50 layouts takes about six hours), and the layouts it did not reach count as
failed. An evaluation that leaves no result at all, or does not start, is the host's failure: the judge raises it,
without the host's paths (the trainer records a judge error, and the Evolver reads its message)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import dojo  # noqa: E402

JUDGE_TIMEOUT_S = 48 * 3600
WHAT = {"NotStarted": "the host's simulator did not start", "NoResult": "the host's simulator left no result"}


def _host(layout_set, call, task):
    """call(), whose failure is the host's (a program's own are scored as failed layouts): raised without the host's
    paths, which the trainer would pass to the Evolver; the whole message goes to the judge's stderr, on the host."""
    try:
        return call()
    except Exception as exc:                                    # noqa: BLE001 - every failure here is the host's
        where = "the engine" if layout_set is None else f"held-out set {layout_set}"
        print(f"judge: {task['id']}, {where}: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise RuntimeError(f"{where}: {type(exc).__name__}: "
                           f"{WHAT.get(type(exc).__name__, 'a failure of the host')}; not the program's doing") from None


def score(task: dict, output) -> dict:
    facts = (output.extra or {}).get("task") or {}
    program = facts.get("program")
    sets = [int(s) for s in task.get("eval_layout_sets") or []]
    count = int(task.get("eval_layouts") or 0)
    total = len(sets) * count
    if not program or not total:
        return {"score": 0.0, "feedback": f"not scored: {'no policy.py' if not program else 'no held-out layouts'}"}
    refused = dojo.check_program(program)
    if refused:
        return {"score": 0.0, "feedback": f"0/{total} held-out layouts; refused: {refused}", "layouts": total}
    out = Path(os.environ.get("ROBODOJO_OUT", ".")).resolve() / "held-out" / \
        f"{task['id']}-{hashlib.sha256(program.encode()).hexdigest()[:12]}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "policy.py").write_text(program)
    env = os.environ
    engine = _host(None, lambda: dojo.Engine(env["ROBODOJO_ROOT"], policy_env=env.get("ROBODOJO_POLICY_ENV", "RoboDojo"),
                         eval_env=env.get("ROBODOJO_EVAL_ENV", "RoboDojo"), env_cfg=env.get("ROBODOJO_ENV_CFG", "arx_x5"),
                         action_type=env.get("ROBODOJO_ACTION_TYPE", "joint"), gpu=env.get("ROBODOJO_JUDGE_GPU") or env.get("ROBODOJO_GPU", "0"),
                         timeout_s=float(env.get("ROBODOJO_JUDGE_TIMEOUT") or JUDGE_TIMEOUT_S)), task)
    passed = missing = 0
    per_set = {}
    for layout_set in sets:
        records = _host(layout_set, lambda: engine.run(out / "policy.py", task=task["suite"], layout_seed=layout_set,
                                                     layouts=count, out=out / f"set-{layout_set}"), task)
        mine = {r["layout_id"]: r for r in records if 0 <= r["layout_id"] < count}    # the set's first `count`
        good = sum(bool(r["task_success"]) for r in mine.values())
        per_set[layout_set] = {"passed": good, "no_result": count - len(mine)}
        passed += good
        missing += count - len(mine)
    (out / "held-out.json").write_text(json.dumps({"sets": per_set, "passed": passed, "layouts": total}, indent=1))
    return {"score": passed / total,
            "feedback": f"{passed}/{total} held-out layouts (sets {', '.join(map(str, sets))})"
                        + (f"; {missing} with no result, counted as failed" if missing else ""),
            "passed": passed, "layouts": total}
