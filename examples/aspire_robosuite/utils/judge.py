"""The success rate of the selected program on the seeds it is scored on: for an actor, the held-out seeds (the
coordinator's Stage 2); for a verifier, its own five seeds. A seed counts when ASPIRE's harness reports task_success
(Robosuite's own success check, run by the host after the program finished). A verifier counts only with its own
verdict `verified` (ASPIRE: `refuted` and `void` are never promoted); a session that broke its rules scores 0.

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
    if facts.get("role") == "verifier":
        # ASPIRE's verdict grades the attempt: only `verified` counts (`refuted`, `void`, or no report score 0)
        verdict = (facts.get("verification") or {}).get("verdict")
        if verdict != "verified":
            return {"score": 0.0, "feedback": f"verifier verdict {verdict or 'missing'}; {passed}/{len(seeds)} seeds"
                                              f"{refused}"}
    return {"score": passed / len(seeds) if seeds else 0.0,
            "feedback": f"{passed}/{len(seeds)} {what} seeds{note}{refused}"}
