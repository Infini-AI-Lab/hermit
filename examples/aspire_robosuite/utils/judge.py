"""The success rate of the selected program on the seeds it is scored on: for an actor, the held-out seeds (the
coordinator's Stage 2); for a verifier, its own five seeds. A seed counts when ASPIRE's harness reports task_success
(Robosuite's own success check, run by the host after the program finished).

Both actors report their facts in extra["task"]: the Codex actor through utils/task.py:collect, the direct actor
(utils/agent/agent.py) in the same schema."""


def score(task: dict, output) -> dict:
    facts = (output.extra or {}).get("task") or {}
    seeds = facts.get("scored_seeds") or []
    passed = int(facts.get("scored_passed") or 0)
    what = "held-out" if facts.get("role") == "actor" else "verifier"
    unseen = facts.get("unseen")
    note = f"; unseen-check {unseen['passed']}/{unseen['total']}" if unseen else ""
    refused = f"; not scored: {facts['scored_refused']}" if facts.get("scored_refused") else ""
    return {"score": passed / len(seeds) if seeds else 0.0,
            "feedback": f"{passed}/{len(seeds)} {what} seeds{note}{refused}"}
