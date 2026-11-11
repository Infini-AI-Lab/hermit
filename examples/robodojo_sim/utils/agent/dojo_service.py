"""RoboDojo's simulator as a host service for the actors in the task sandbox: `bin/sim` (sim_cli.py) asks it to
evaluate a program on the session's development layouts. It is the ASPIRE example's simulator service
(examples/aspire_robosuite/utils/agent/sim_service.py: the registration of a session by its rollout directory and
task id, the program read and the output written without following a link the session left, the ledger) with
RoboDojo's engine (dojo.Engine) in place of cap-harness:

    POST /register  {workspace, task}        once per rollout, by the task's `prepare`
    POST /run       {workspace, cmd: run-batch | validate, program, seeds, output_root}
                    `seeds` are layout ids of the task row's development layout set, from the first (RoboDojo
                    evaluates a set's first N); a held-out set is never run here
    GET  /health

    python dojo_service.py --port 8130 --robodojo-root $ROBODOJO_ROOT --tasks utils/tasks.jsonl --workspaces <logs>
        --state <dir> [--gpu 0] [--policy-env RoboDojo] [--eval-env RoboDojo]
"""
from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import signal
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dojo  # noqa: E402

sys.path.append(str(HERE.parents[2] / "aspire_robosuite" / "utils" / "agent"))
import sim_service as base  # noqa: E402  (the ASPIRE example's)


class Service(base.Service):
    def __init__(self, engine: dojo.Engine, tasks: dict, workspaces: list[Path], ledger: base.Ledger, **options):
        super().__init__(engine.root, tasks, workspaces, ledger, {}, 1, **options)
        self.engine = engine

    def _check_row(self, row: dict) -> None:
        if row.get("benchmark") != "robodojo":
            raise ValueError(f"task {row.get('id')} is not a RoboDojo task")

    def _validate(self, session, program: Path) -> tuple[int, str, str]:
        refused = self.engine.validate(program)
        return (1, "", refused + "\n") if refused else (0, "program accepted\n", "")

    def _launch(self, body, session, program: Path, own_out: Path, seeds: list[int]) -> tuple[int, str, str]:
        row = self.tasks[session["task"]]
        if sorted(seeds) != list(range(len(seeds))):
            return 2, "", "bin/sim: layouts are run from the first: --layouts N runs layouts 0..N-1\n"
        with self.lock:
            if self.stopping:
                return 1, "", "bin/sim: the simulator service is stopping\n"
        try:
            records = self.engine.run(program, task=row["suite"], layout_seed=int(row["layout_set"]),
                                      layouts=len(seeds), out=own_out)
        except ValueError as exc:                          # the program was refused before a simulator started
            return 1, "", f"bin/sim: {exc}\n"
        passed = sum(r["task_success"] for r in records)
        lines = [f"layout {r['layout_id']}: {'success' if r['task_success'] else 'fail'} (score {r['score']:.2f})"
                 for r in records]
        failed = dojo.failures(own_out)
        if failed:
            lines.append(f"the program failed in {len(failed)} episode(s); the first: {failed[0].get('error')}")
        if len(records) < len(seeds):
            lines.append(f"{len(seeds) - len(records)} layout(s) have no result (skipped by RoboDojo as unstable, or "
                         "the evaluation ended early: robodojo.log)")
        lines.append(f"{passed}/{len(records)} layouts succeeded")
        return 0, "\n".join(lines) + "\n", ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8130)
    ap.add_argument("--robodojo-root", type=Path, required=True)
    ap.add_argument("--tasks", type=Path, required=True, help="the task rows (utils/tasks.jsonl): their layouts")
    ap.add_argument("--workspaces", type=Path, action="append", required=True)
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--gpu", default="0")
    ap.add_argument("--policy-env", default="RoboDojo")
    ap.add_argument("--eval-env", default="RoboDojo")
    ap.add_argument("--env-cfg", default="arx_x5")
    ap.add_argument("--action-type", default="joint")
    ap.add_argument("--run-timeout", type=float, default=6 * 3600)
    ap.add_argument("--nonce", default="")
    a = ap.parse_args()
    if not shutil.which("bwrap"):
        sys.exit("dojo_service: bubblewrap (bwrap) is not on PATH: every program runs confined (dojo.Engine)")
    engine = dojo.Engine(a.robodojo_root, policy_env=a.policy_env, eval_env=a.eval_env, env_cfg=a.env_cfg,
                         action_type=a.action_type, gpu=a.gpu, timeout_s=a.run_timeout)
    engine._installed()                                     # refused now rather than on a session's first run
    a.state.mkdir(parents=True, exist_ok=True)
    tasks = {r["id"]: r for r in (json.loads(line) for line in a.tasks.read_text().splitlines() if line.strip())}
    service = Service(engine, tasks, a.workspaces, base.Ledger(a.state / "ledger.json"), run_timeout=a.run_timeout)
    service.nonce = a.nonce

    def _bye(sig, frame):
        service.stop()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _bye)
    ThreadingHTTPServer((a.host, a.port), base.handler(service)).serve_forever()


if __name__ == "__main__":
    main()
