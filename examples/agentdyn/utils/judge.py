"""AgentDyn's judge for EvoSafe: native verdicts as per-episode metrics in the judge's `extra` (bridge.score).
Task-level defense metrics are computed from the run records by the method's contract (`contract.summary`), in the
Evolver and in `report.py`, not by the framework."""
from bridge import score

__all__ = ["score"]
