"""Official AgentDojo entry point, reusing AgentDyn's supervised replay runtime."""
from pathlib import Path
import sys

# AgentDyn's runtime, imported by file name as AgentDyn's own modules import each other.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agentdyn/utils"))
import bridge as replay


def run(task, adapter, *, params, base):
    replay.validate_case(task, benchmark="agentdojo")
    return replay.run(task, adapter, params={**params, "benchmark": "agentdojo"}, base=base)


def score(task, output):
    return replay.score(task, output)
