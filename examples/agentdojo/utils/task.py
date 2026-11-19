"""Official AgentDojo entry point, reusing AgentDyn's supervised replay runtime."""
from pathlib import Path
import sys

# Function loaders and standalone preparation need not start at the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from examples.agentdyn.utils import bridge as replay


def run(task, adapter, *, params, base):
    replay.validate_case(task, benchmark="agentdojo")
    return replay.run(task, adapter, params={**params, "benchmark": "agentdojo"}, base=base)


def score(task, output):
    return replay.score(task, output)
