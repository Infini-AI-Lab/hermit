"""AgentDyn's judge for EvoSafe: native verdicts as per-episode metrics in the judge's `extra` (bridge.score).
Task-level defense metrics are computed from the run records by the method's contract (`contract.summary`), in the
Evolver and in `report.py`, not by the framework."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # AgentDyn's runtime (bridge.py)
from bridge import score  # noqa: E402

__all__ = ["score"]
