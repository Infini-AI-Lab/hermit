"""`bin/sim`: the robot execution engine as the Codex actor reaches it from its workspace.

    bin/sim run-batch --seeds 101-125 --program code/v00_initial.py --output-root debug/initial [--no-videos]
    bin/sim run --seed 101 --program code/probe.py --output-root debug/explore [--no-videos]
    bin/sim validate --program code/v01.py         # the sandbox's static check only

It asks the host's simulator service (utils/agent/sim_service.py) to run ASPIRE's `cap-harness` for this
workspace's task; the service holds the rules (the assigned seeds, the unseen check once, a verifier's two
programs) and writes the runs into the workspace. Paths are relative to this workspace.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import urllib.request

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "sim.json").read_text())


def seeds_of(values) -> list[int]:
    out = []
    for value in values:
        for part in str(value).replace(",", " ").split():
            lo, _, hi = part.partition("-")
            out += list(range(int(lo), int(hi) + 1)) if hi else [int(lo)]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(prog="bin/sim")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "run-batch", "validate"):
        p = sub.add_parser(name)
        p.add_argument("--program", type=Path, required=True)
        if name != "validate":
            p.add_argument("--output-root", type=Path, required=True)
            p.add_argument("--no-videos", action="store_true")
            p.add_argument("--max-steps", type=int)
        if name == "run":
            p.add_argument("--seed", type=int, required=True)
        if name == "run-batch":
            p.add_argument("--seeds", nargs="+", required=True)
            p.add_argument("--workers", type=int, help=argparse.SUPPRESS)
        # flags ASPIRE's templates pass to cap-harness that the service sets itself: accepted, ignored
        for flag in ("--init-mode", "--log-dir", "--results-jsonl", "--benchmark", "--suite", "--task-id"):
            p.add_argument(flag, help=argparse.SUPPRESS)
        p.add_argument("--flat-run-dir", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    workspace = Path(CONFIG["workspace"])
    rel = lambda p: str(p.resolve().relative_to(workspace)) if p.resolve().is_relative_to(workspace) else str(p)
    body = {"workspace": str(workspace), "cmd": a.cmd, "program": rel(a.program)}
    if a.cmd != "validate":
        body.update(output_root=rel(a.output_root), no_videos=a.no_videos, max_steps=a.max_steps,
                    seeds=[a.seed] if a.cmd == "run" else seeds_of(a.seeds))
    request = urllib.request.Request(CONFIG["url"].rstrip("/") + "/run", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=24 * 3600) as reply:
        answer = json.loads(reply.read())
    if not answer.get("ok"):
        print(f"bin/sim: refused: {answer.get('error')}", file=sys.stderr)
        sys.exit(2)
    sys.stdout.write(answer.get("stdout", ""))
    sys.stderr.write(answer.get("stderr", ""))
    sys.exit(answer.get("code", 1))


if __name__ == "__main__":
    main()
