"""AgentDojo native verdicts, using the shared defense metric contract."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agentdyn/utils"))
from bridge import score

__all__ = ["score"]
