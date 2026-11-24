"""The judge, on the host: the session's scored program (the facts its task functions' `collect` returned: the
engine's utils/agent/task.py, or a method's built on it) run once on the task row's held-out seeds (`eval_seeds`),
which no session can run (the simulator service refuses them), and its success rate. A row with an attempt budget
(`max_programs`) counts only a program its budget ran (the simulator service's ledger). A seed counts when ASPIRE's
harness reports task_success (the benchmark's own success check, run after the program finished), within the
session's max_steps. A row may name its seeds for the feedback (`score_label`, default held-out); facts that
withhold their program (`not_scored`: a method's rule) score 0 with that reason.

The engine's settings come from the launcher's environment: ASPIRE_ROOT (the checkout), ASPIRE_SIM_ENV (the
simulator's environment, a JSON object), ASPIRE_OUT (where the runs go; the service's ledger is its sim-service/)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import sim  # noqa: E402


def score(task: dict, output) -> dict:
    facts = (output.extra or {}).get("task") or {}
    seeds = sim.seeds_of(task.get("eval_seeds"))
    what = task.get("score_label") or "held-out"       # what the seeds are, in the feedback (a row's own name)
    unseen = facts.get("unseen")
    note = f"; unseen-check {unseen['passed']}/{unseen['total']}" if unseen else ""
    if facts.get("not_scored"):
        return {"score": 0.0, "feedback": str(facts["not_scored"]), "seeds": len(seeds)}
    program = facts.get("fix_code")
    if not program or not seeds:
        return {"score": 0.0, "feedback": f"0/{len(seeds)} {what} seeds; not scored: "
                                          f"{'no program' if not program else 'no seeds'}{note}"}
    # A session with an attempt budget (its facts name its workspace): the program must be one its budget ran. A
    # session that runs its programs itself (no workspace: a direct actor) keeps its own count.
    if task.get("max_programs") and facts.get("workspace") and \
            hashlib.sha256(program.encode()).hexdigest() not in _budget(facts["workspace"]):
        return {"score": 0.0, "feedback": "the program is not one of the programs its attempt budget ran",
                "seeds": len(seeds)}
    out = Path(os.environ.get("ASPIRE_OUT", ".")).resolve() / "stage2" / \
        f"{task['id']}-{hashlib.sha256(program.encode()).hexdigest()[:12]}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "program.py").write_text(program)
    engine = sim.Engine(os.environ["ASPIRE_ROOT"], env=json.loads(os.environ.get("ASPIRE_SIM_ENV") or "{}"),
                        workers=int(os.environ.get("ASPIRE_WORKERS", 4)), max_steps=facts.get("max_steps"),
                        timeout_s=7200, benchmark=task.get("benchmark") or "robosuite")
    refused = engine.validate(out / "program.py")
    records = [] if refused else engine.run(out / "program.py", suite=task["suite"], seeds=seeds, out=out / "runs",
                                            task_id=int(task.get("task_id", 0)))
    passed = sum(r["task_success"] for r in records)
    (out / "stage2.json").write_text(json.dumps({"refused": refused, "records": records}, indent=1))
    return {"score": passed / len(seeds),
            "feedback": f"{passed}/{len(seeds)} {what} seeds{note}" + (f"; refused: {refused}" if refused else ""),
            "passed": passed, "seeds": len(seeds)}


def _budget(workspace) -> list[str]:
    """The digests of the programs a session ran within its budget (the simulator service's ledger)."""
    ledger = Path(os.environ.get("ASPIRE_OUT", ".")).resolve() / "sim-service" / "ledger.json"
    try:
        return list((json.loads(ledger.read_text()).get(str(workspace)) or {}).get("programs") or [])
    except (OSError, ValueError):
        return []
