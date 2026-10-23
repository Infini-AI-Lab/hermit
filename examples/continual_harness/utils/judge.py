"""Score a Continual Harness window by the benchmark's milestone progress.

The agent's RunOutput carries the game's milestones; the score is the share of the ordered Red milestones reached,
and the feedback names the latest milestone so the Evolver can see what the policy achieved.
"""

def score(task, output):
    extra = getattr(output, "extra", None) or {}
    completed = extra.get("completed") or []
    milestones = extra.get("milestones") or {}
    total = max(len(milestones), 1)
    value = len(completed) / total
    latest = completed[-1] if completed else "none"
    return {"score": value, "feedback": f"reached {len(completed)}/{total} milestones (latest: {latest})"}
