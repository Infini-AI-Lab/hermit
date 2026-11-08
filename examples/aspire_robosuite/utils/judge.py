"""The coordinator's Stage 2, on the host: the session's selected program (utils/agent/task.py:collect) run once on
the seeds it is scored on, and its success rate. For an actor these are the task row's held-out seeds, which no
session can run (the simulator service refuses them); for a verifier, its own development seeds, and it counts only
with its own verdict `verified` (ASPIRE: `refuted` and `void` are never promoted). A seed counts when ASPIRE's
harness reports task_success (Robosuite's own success check, run after the program finished).

The engine's settings come from the launcher's environment: ASPIRE_ROOT (the checkout), ASPIRE_SIM_ENV (the
simulator's environment, a JSON object), ASPIRE_OUT (where the runs go)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent"))
import sim  # noqa: E402


def score(task: dict, output) -> dict:
    facts = (output.extra or {}).get("task") or {}
    verifier = task.get("role") == "verifier"
    seeds = sim.seeds_of(task.get("debug_seeds") if verifier else task.get("eval_seeds"))
    unseen = facts.get("unseen")
    note = f"; unseen-check {unseen['passed']}/{unseen['total']}" if unseen else ""
    what = "verifier" if verifier else "held-out"
    if verifier and (facts.get("verification") or {}).get("verdict") != "verified":
        verdict = (facts.get("verification") or {}).get("verdict") or "missing"
        return {"score": 0.0, "feedback": f"verifier verdict {verdict}", "seeds": len(seeds)}
    program = facts.get("fix_code")
    if not program or not seeds:
        return {"score": 0.0, "feedback": f"0/{len(seeds)} {what} seeds; not scored: "
                                          f"{'no program' if not program else 'no seeds'}{note}"}
    out = Path(os.environ.get("ASPIRE_OUT", ".")) / "stage2" / \
        f"{task['id']}-{hashlib.sha256(program.encode()).hexdigest()[:12]}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "program.py").write_text(program)
    engine = sim.Engine(os.environ["ASPIRE_ROOT"], env=json.loads(os.environ.get("ASPIRE_SIM_ENV") or "{}"),
                        workers=int(os.environ.get("ASPIRE_WORKERS", 4)), timeout_s=7200)
    refused = engine.validate(out / "program.py")
    records = [] if refused else engine.run(out / "program.py", suite=task["suite"], seeds=seeds, out=out / "runs",
                                            task_id=int(task.get("task_id", 0)))
    passed = sum(r["task_success"] for r in records)
    (out / "stage2.json").write_text(json.dumps({"refused": refused, "records": records}, indent=1))
    return {"score": passed / len(seeds),
            "feedback": f"{passed}/{len(seeds)} {what} seeds{note}" + (f"; refused: {refused}" if refused else ""),
            "passed": passed, "seeds": len(seeds)}
