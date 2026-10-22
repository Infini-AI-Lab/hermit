"""Start one host runner (database_runner.py or poker_runner.py) in clbench's environment and read its result.

Settings come from the environment the launcher prepares, or from agent.params:
  MWM_TASK_WORKDIR   the run's task directory: evidence/ (the native store), host/, current_db, outcomes.jsonl
                     (agent.params.workdir)
  MWM_CLBENCH        the clbench checkout (default ~/code/clbench; agent.params.clbench)
  MWM_HOST_MODEL     the host model as clbench's litellm names it (fireworks_ai/..., hosted_vllm/...); the launcher
                     derives it from a backend reference. agent.params.model names it instead: a `<backend>/<model>`
                     reference (resolved by the framework into a binding: the litellm id, the endpoint and the key
                     variable are taken from it). Default: GLM 5.3 Flash on Fireworks.
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
DEFAULT_MODEL = "fireworks/accounts/fireworks/models/glm-5p3-flash"


def host_model(params: dict) -> tuple[str, dict]:
    """The litellm id the runner is given and the environment clbench's litellm needs for it (its key variable; the
    endpoint when CLB_API_BASE is not set by the launcher), from agent.params.model or MWM_HOST_MODEL."""
    model = params.get("model")
    if not model:
        if os.environ.get("MWM_HOST_MODEL"):
            return os.environ["MWM_HOST_MODEL"], {}      # a litellm id, as the launcher sets it; never a reference
        from hermit.backends import registry, resolve
        model = resolve(DEFAULT_MODEL, registry()).spec()
    if isinstance(model, str):
        from hermit.backends import registry, resolve
        model = resolve(model, registry()).spec()
    from hermit.backends import Binding
    binding = Binding.from_spec(model)
    if binding.backend.key_required and binding.api_key_env and not binding.key():
        raise RuntimeError(f"{binding.api_key_env} is not set (the key of backend {binding.backend.name!r})")
    env = {binding.litellm_key_env: binding.key() or "local"}      # a keyless server: litellm wants some string
    if not os.environ.get("CLB_API_BASE"):
        env["CLB_API_BASE"] = binding.base_url
    return binding.litellm_model, env


def run_host(runner: str, task: dict, adapter: Path, params: dict | None = None) -> tuple[dict, Path, float]:
    """The runner's JSON result, the task's log directory (the raw record), and the wall time. Settings: agent.params
    (`workdir`, `clbench`, `model`), else the launcher's environment."""
    params = params or {}
    workdir = Path(params.get("workdir") or os.environ["MWM_TASK_WORKDIR"])
    clbench = Path(params.get("clbench") or os.environ.get("MWM_CLBENCH", Path.home() / "code" / "clbench"))
    model, model_env = host_model(params)
    # `log` becomes the task's trajectory in the learning material: it gets the raw record only (the native
    # learner's view). The runner's own stdout/stderr may name host-side paths and stay in runner_logs/.
    log = workdir / "episodes" / task["id"]
    log.mkdir(parents=True, exist_ok=True)
    runner_logs = workdir / "runner_logs"
    runner_logs.mkdir(parents=True, exist_ok=True)
    command = [str(clbench / ".venv" / "bin" / "python"), str(HERE / runner),
               "--task", json.dumps(task), "--adapter", str(Path(adapter).resolve()), "--workdir", str(workdir),
               "--model", model, "--clbench", str(clbench), "--framework", str(FRAMEWORK)]
    env = {**os.environ, **model_env}
    t0 = time.time()
    for attempt in (1, 2):
        # One retry after a crash (the seat already retries model calls and cools down 120 s once). A stream
        # mismatch is never retried: the task row and the stream disagree, and playing on would mislabel records.
        suffix = "" if attempt == 1 else ".retry"
        with open(runner_logs / f"{task['id']}{suffix}.stderr.log", "w") as err:
            proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=err, text=True, cwd=str(clbench), env=env)
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
