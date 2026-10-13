"""Start one host runner (database_runner.py or poker_runner.py) in clbench's environment and read its result.

Settings come from the environment the launcher prepares (the Trainer does not forward pipeline.params):
  MWM_TASK_WORKDIR   the run's task directory: evidence/ (the native store), host/, current_db, outcomes.jsonl
  MWM_CLBENCH        the clbench checkout (default ~/code/clbench)
  MWM_METAWORLD      the metaworld checkout, for its host seat (default ~/code/metaworld)
  MWM_HOST_MODEL     the host model (default the Fireworks GLM 5.3 flash id)
  plus clbench's own: CLB_SCORING, CLB_QUERY_BUDGET, CLB_API_BASE, CLB_REASONING_EFFORT, the API key, MW_META_SQL
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

HERE = Path(__file__).resolve().parent
FRAMEWORK = HERE.parents[3]
DEFAULT_MODEL = "fireworks_ai/accounts/fireworks/models/glm-5p3-flash"


def run_host(runner: str, task: dict, adapter: Path) -> tuple[dict, Path, float]:
    """The runner's JSON result, the task's log directory (the raw record), and the wall time."""
    workdir = Path(os.environ["MWM_TASK_WORKDIR"])
    clbench = Path(os.environ.get("MWM_CLBENCH", Path.home() / "code" / "clbench"))
    metaworld = Path(os.environ.get("MWM_METAWORLD", Path.home() / "code" / "metaworld"))
    # `log` becomes the task's trajectory in the learning material: it gets the raw record only (the native
    # learner's view). The runner's own stdout/stderr may name host-side paths and stay in runner_logs/.
    log = workdir / "episodes" / task["id"]
    log.mkdir(parents=True, exist_ok=True)
    runner_logs = workdir / "runner_logs"
    runner_logs.mkdir(parents=True, exist_ok=True)
    command = [str(clbench / ".venv" / "bin" / "python"), str(HERE / runner),
               "--task", json.dumps(task), "--adapter", str(Path(adapter).resolve()), "--workdir", str(workdir),
               "--model", os.environ.get("MWM_HOST_MODEL", DEFAULT_MODEL), "--clbench", str(clbench),
               "--metaworld", str(metaworld), "--framework", str(FRAMEWORK)]
    t0 = time.time()
    for attempt in (1, 2):
        # One retry after a crash (the seat already retries model calls and cools down 120 s once). A stream
        # mismatch is never retried: the task row and the stream disagree, and playing on would mislabel records.
        suffix = "" if attempt == 1 else ".retry"
        with open(runner_logs / f"{task['id']}{suffix}.stderr.log", "w") as err:
            proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=err, text=True, cwd=str(clbench))
        (runner_logs / f"{task['id']}{suffix}.stdout.log").write_text(proc.stdout)
        lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
        if proc.returncode == 0 and lines:
            break
        tail = (runner_logs / f"{task['id']}{suffix}.stderr.log").read_text()[-1500:]
        if attempt == 2 or "stream mismatch" in tail:
            raise RuntimeError(f"{runner} exited with {proc.returncode}: {tail}")
        time.sleep(60)
    result = json.loads(lines[-1])
    raw = workdir / "evidence" / result["raw_path"].removeprefix("/evidence/")
    if raw.is_file():
        (log / raw.name).write_text(raw.read_text())
    return result, log, round(time.time() - t0, 1)
