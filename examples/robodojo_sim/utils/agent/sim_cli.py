"""`bin/sim`: RoboDojo's simulator as the acting agent reaches it from its workspace.

    bin/sim run --layouts 3 --program policy.py --output-root runs/v1      # the first 3 development layouts
    bin/sim validate --program policy.py                                   # parses, defines act(obs)

It asks the host's simulator service (dojo_service.py), which evaluates the program in RoboDojo on this task's
development layouts only and writes the run (the per-layout result, the videos, the program's log) into the
workspace. Paths are relative to this workspace. `run` returns when the evaluation has ended, which takes minutes:
it says so at once and once a minute after, on stderr, so that a caller who watches it sees that it is running.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
import time
import urllib.request

CONFIG = json.loads((Path(__file__).resolve().parent / "sim.json").read_text())


def main() -> None:
    ap = argparse.ArgumentParser(prog="bin/sim")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "validate"):
        p = sub.add_parser(name)
        p.add_argument("--program", type=Path, required=True)
        if name == "run":
            p.add_argument("--layouts", type=int, required=True, help="how many development layouts, from the first")
            p.add_argument("--output-root", type=Path, required=True)
    a = ap.parse_args()
    workspace = Path(CONFIG["workspace"])
    rel = lambda p: str(p.resolve().relative_to(workspace)) if p.resolve().is_relative_to(workspace) else str(p)
    body = {"workspace": str(workspace), "cmd": "validate" if a.cmd == "validate" else "run-batch",
            "program": rel(a.program)}
    if a.cmd == "run":
        body.update(output_root=rel(a.output_root), seeds=list(range(max(a.layouts, 0))))
    request = urllib.request.Request(CONFIG["url"].rstrip("/") + "/run", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    ended = threading.Event()
    if a.cmd == "run":
        print(f"bin/sim: evaluating {max(a.layouts, 0)} layout(s): several minutes to start the simulator, then "
              "several minutes a layout, longer if another evaluation is ahead of it. This command returns when it "
              f"has ended; {rel(a.output_root)}/records.json is written then. Do not stop it.", file=sys.stderr,
              flush=True)

        def still_running(started=time.time()):
            while not ended.wait(60):
                print(f"bin/sim: still running ({(time.time() - started) / 60:.0f} min)", file=sys.stderr, flush=True)

        threading.Thread(target=still_running, daemon=True).start()
    try:
        with urllib.request.urlopen(request, timeout=24 * 3600) as reply:
            answer = json.loads(reply.read())
    finally:
        ended.set()
    if not answer.get("ok"):
        print(f"bin/sim: refused: {answer.get('error')}", file=sys.stderr)
        sys.exit(2)
    sys.stdout.write(answer.get("stdout", ""))
    sys.stderr.write(answer.get("stderr", ""))
    sys.exit(answer.get("code", 1))


if __name__ == "__main__":
    main()
