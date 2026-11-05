"""`bin/sim`: the robot execution engine as the Codex actor reaches it from its workspace.

    bin/sim run-batch --seeds 101-125 --program code/v00_initial.py --output-root debug/initial [--no-videos]
    bin/sim run --seed 101 --program code/probe.py --output-root debug/explore [--no-videos]
    bin/sim validate --program code/v01.py         # the sandbox's static check only

It runs ASPIRE's `cap-harness` for this workspace's task (benchmark robosuite, the suite and task id, the flat run
directory, the engine's environment) and refuses what the assignment does not allow: a seed outside the development
and unseen-check seeds, the unseen-check seeds a second time, and an output root outside the workspace. Settings come
from `sim.json` beside this file, written by the task's `prepare`. A verifier may run at most two programs on its
seeds. These are rules, not a sandbox: the session can reach past them, and the task's `collect` audits it after the
fact (utils/task.py:audit).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "sim.json").read_text())


def seeds_of(values) -> list[int]:
    out = []
    for value in values:
        for part in str(value).replace(",", " ").split():
            lo, _, hi = part.partition("-")
            out += list(range(int(lo), int(hi) + 1)) if hi else [int(lo)]
    return out


def refuse(message: str) -> None:
    print(f"bin/sim: refused: {message}", file=sys.stderr)
    sys.exit(2)


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
            p.add_argument("--workers", type=int, default=CONFIG["workers"])
        # flags ASPIRE's templates pass to cap-harness that bin/sim sets itself: accepted, ignored
        for flag in ("--init-mode", "--log-dir", "--results-jsonl", "--benchmark", "--suite", "--task-id"):
            p.add_argument(flag, help=argparse.SUPPRESS)
        p.add_argument("--flat-run-dir", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()

    workspace = Path(CONFIG["workspace"]).resolve()
    root = Path(CONFIG["aspire_root"])
    env = {**os.environ, **{k: os.path.expandvars(os.path.expanduser(str(v))) for k, v in CONFIG["env"].items()}}
    program = a.program.resolve()
    if not program.is_file():
        refuse(f"no program at {a.program}")
    if a.cmd == "validate":
        code = ("import ast,sys\nfrom cap_harness.runtime import _ProgramValidator\n"
                "_ProgramValidator().visit(ast.parse(open(sys.argv[1]).read()))\nprint('program accepted')")
        sys.exit(subprocess.run([str(root / ".venv-robosuite" / "bin" / "python"), "-c", code, str(program)],
                                env=env, cwd=root).returncode)

    out = a.output_root.resolve()
    if not out.is_relative_to(workspace):
        refuse(f"--output-root must be inside the workspace {workspace}")
    seeds = [a.seed] if a.cmd == "run" else seeds_of(a.seeds)
    allowed = set(CONFIG["dev_seeds"]) | set(CONFIG["unseen_seeds"])
    outside = sorted(set(seeds) - allowed)
    if outside:
        refuse(f"seeds {outside} are not assigned to you (development {CONFIG['dev_seeds_text']}, "
               f"unseen-check {CONFIG['unseen_seeds_text'] or 'none'})")
    unseen = sorted(set(seeds) & set(CONFIG["unseen_seeds"]))
    ledger = HERE / "unseen_used.json"
    if unseen:
        if ledger.exists():
            refuse("the unseen-check seeds are run once, and they have been run")
        ledger.write_text(json.dumps({"seeds": unseen, "program": str(program)}))

    if a.cmd == "run-batch" and CONFIG.get("max_programs"):
        # the verifier's attempt budget (ASPIRE: at most two programs on its seeds)
        attempts = HERE / "attempts.json"
        used = json.loads(attempts.read_text()) if attempts.exists() else []
        if str(program) not in used:
            if len(used) >= CONFIG["max_programs"]:
                refuse(f"the attempt budget is {CONFIG['max_programs']} programs, and it is spent: {used}")
            attempts.write_text(json.dumps(used + [str(program)]))

    cli = str(root / ".venv-robosuite" / "bin" / "cap-harness")
    common = ["--benchmark", "robosuite", "--suite", CONFIG["suite"], "--task-id", str(CONFIG["task_id"]),
              "--program", str(program), "--output-root", str(out), "--flat-run-dir"]
    if a.no_videos:
        common.append("--no-videos")
    if a.max_steps or CONFIG.get("max_steps"):
        common += ["--max-steps", str(a.max_steps or CONFIG["max_steps"])]
    if a.cmd == "run":
        cmd = [cli, "run", *common, "--seed", str(a.seed)]
    else:
        out.mkdir(parents=True, exist_ok=True)
        cmd = [cli, "run-batch", *common, "--seeds", *map(str, seeds), "--workers", str(min(a.workers, len(seeds))),
               "--results-jsonl", str(out / "results.jsonl"), "--log-dir", str(out / "logs")]
    sys.exit(subprocess.run(cmd, env=env, cwd=root).returncode)


if __name__ == "__main__":
    main()
