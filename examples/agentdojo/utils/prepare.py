"""Import frozen AgentDojo cases; keep every variant of a user task in one split."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

REPLAY = Path(__file__).resolve().parents[2] / "agentdyn/utils"
sys.path[:0] = [str(REPLAY), str(REPLAY.parents[2])]     # AgentDyn's runtime; hermit from this checkout
from bridge import BENCHMARKS

# AgentDyn's prepare.py, by its path: this file has the same name.
_spec = importlib.util.spec_from_file_location("agentdyn_prepare", REPLAY / "prepare.py")
_replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_replay)
prepare_replay, read_logs = _replay.prepare, _replay.read_logs


def prepare(cases, output, seed=0):
    return prepare_replay(cases, output, seed, benchmark="agentdojo")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path, help="frozen case JSONL or native log directory with --from-logs")
    parser.add_argument("output", type=Path, help="new output directory")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--from-logs", action="store_true")
    parser.add_argument("--suite", choices=sorted(BENCHMARKS["agentdojo"][0]), default="workspace")
    args = parser.parse_args()
    cases = read_logs(args.cases, args.suite) if args.from_logs else [
        json.loads(line) for line in args.cases.read_text().splitlines() if line.strip()]
    print(json.dumps(prepare(cases, args.output, args.seed), indent=2))


if __name__ == "__main__":
    main()
