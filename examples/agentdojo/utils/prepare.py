"""Import frozen AgentDojo cases; keep every variant of a user task in one split."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from examples.agentdyn.utils.bridge import BENCHMARKS
from examples.agentdyn.utils.prepare import prepare as prepare_replay, read_logs


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
