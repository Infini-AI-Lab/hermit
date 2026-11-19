"""AgentDojo native verdicts, using the shared defense metric contract."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from examples.agentdyn.utils.bridge import score

__all__ = ["score"]
